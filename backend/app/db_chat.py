"""Maintain -> chat: changes to the collection asked for in plain words
("rename 'Paprika rot' to 'rote Paprika'", "every recipe with salmon gets
the tag 'Fisch'", "which recipes have no photo?").

Safe by design:
- the AI only picks actions from a fixed catalog (CATALOG in the prompt) -
  no free API access;
- it only DESCRIBES which recipes are meant (a filter); the app finds them
  itself in the live data, so nothing is guessed;
- every action becomes a suggestion with the number of recipes it touches
  and their names; nothing changes before "apply" (with Tandoor: undoable
  like every other suggestion). Large ones (> CONFIRM_ABOVE recipes) and
  deleting a tag need an extra confirmation in the page.

Works with Tandoor and Mealie (supermarket categories only exist in
Tandoor)."""
from __future__ import annotations

import json
import logging
import re
import uuid

from . import (cook_today, json_answer, llm_provider, mealie_client, mealie_plan, prep_notes, tandoor_client, target,
               tool_jobs, usage_log)
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_helpers import delete_entity, fetch_recipe_overview, find_recipes_by_filter, minimal_ref

log = logging.getLogger("recipe-bridge")

TOOL = "db_chat"
CONFIRM_ABOVE = 50
MAX_TAGS_SHOWN = 400
MAX_FOUND_LISTED = 40
HISTORY = 6
MAX_ANSWER_RECIPES = 200
ENTITIES = ("food", "unit", "keyword")

PROMPT = """You turn a home cook's request about their recipe collection (kept in
{manager}) into actions. Language for "reply": {language}. You will receive a
JSON object: {"message": string, "history": [{"role", "text"}], "recipe_count": int,
"tags": [string], "cookbooks": [string], "categories": [string]}
("categories" = supermarket categories; empty when there are none).

FILTER - which recipes are meant; every field optional, all combined with AND:
{"name": string (part of the title), "ingredient": [string] (uses any of these),
 "tag": [string] (has any of these), "without_tag": [string] (has none of these),
 "cookbook": string, "missing": ["image" | "servings" | "time" | "tags"]}
An empty FILTER {} means every recipe.

Actions ("entity" is "food" (ingredient), "unit" or "tag"):
{"type": "rename", "entity": ..., "name": <current name>, "new_name": <new name>}
{"type": "merge", "entity": ..., "names": [<names that mean the same>], "into": <name to keep>}
{"type": "add_tag", "recipes": FILTER, "tag": <tag>}
{"type": "remove_tag", "recipes": FILTER, "tag": <tag>}
{"type": "delete_tag", "tag": <tag>} (removes it from every recipe and deletes it)
{"type": "set_servings", "recipes": FILTER, "servings": <number>}
{"type": "add_to_cookbook", "recipes": FILTER, "cookbook": <name, new or existing>}
{"type": "set_category", "foods": [<ingredient names>], "category": <name>} (only if "categories" is given)
{"type": "find", "recipes": FILTER} (a question - the app lists the recipes)

Rules:
- use only these actions; if the request needs something else, give no
  actions and say in "reply" what is possible instead
- for tags and cookbooks use the exact spelling from the lists when one is
  meant; ingredients and units: the cook's words (the app finds them)
- "history" is the conversation so far ("it", "those" may refer to it)
- "reply": one or two short sentences, what you are proposing (or the
  answer), no lists - the app shows the details

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"reply": string, "actions": [<action>, ...]}
"""


class Problem(Exception):
    """An action that can't be carried out as asked - said in the chat."""


# ---------- reading the collection ----------

def _norm(text) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


class Collection:
    """What the actions need, read live from the recipe manager."""

    def __init__(self, client):
        self.client = client
        self.mealie = target.is_mealie()
        self._items = {}
        self._recipes = None
        self._books = None

    def items(self, entity) -> list[dict]:
        if entity not in self._items:
            api = mealie_client if self.mealie else tandoor_client
            self._items[entity] = api.fetch_all_items(self.client, entity)
        return self._items[entity]

    def categories(self) -> list[str]:
        if self.mealie:
            return []
        try:
            return [c["name"] for c in tandoor_client.fetch_all_items(self.client, "supermarket-category")]
        except tandoor_client.TandoorError:
            return []

    def cookbooks(self) -> list[str]:
        api = mealie_client if self.mealie else tandoor_client
        try:
            return api.list_cookbooks(self.client)
        except tandoor_client.TandoorError:
            return []

    def recipes(self) -> list[dict]:
        """[{"id" (Mealie: slug), "name", "tags", "servings", "image", "minutes", "cookbooks"}]"""
        if self._recipes is None:
            if self.mealie:
                self._recipes = [{
                    "id": r.get("slug"), "uid": r.get("id"), "name": r.get("name", ""),
                    "tags": [t.get("name", "") for t in r.get("tags") or []],
                    "servings": r.get("recipeServings") or r.get("recipeYieldQuantity") or 0,
                    "image": bool(r.get("image")), "minutes": mealie_plan.minutes(r),
                    "cookbooks": [c.get("name", "") for c in r.get("recipeCategory") or []],
                } for r in mealie_client._paged(self.client, "/recipes") if r.get("slug")]
            else:
                from .migration import _tandoor_books
                books = _tandoor_books(self.client)
                self._recipes = [{
                    "id": r["id"], "name": r.get("name", ""),
                    "tags": [k.get("name") or k.get("label") or "" for k in r.get("keywords") or []],
                    "servings": r.get("servings") or 0, "image": bool(r.get("image")),
                    "minutes": (r.get("working_time") or 0) + (r.get("waiting_time") or 0),
                    "cookbooks": books.get(r["id"], []),
                } for r in fetch_recipe_overview(self.client)]
        return self._recipes

    def using_food(self, food_id) -> set:
        if self.mealie:
            resp = mealie_client._check(self.client.get("/recipes", params={"foods": food_id, "perPage": 1000}),
                                        "finding recipes")
            return {r.get("slug") for r in resp.json().get("items", [])}
        return {r["id"] for r in find_recipes_by_filter(self.client, "foods", food_id)}

    # -- names --
    def find(self, entity, name) -> dict | None:
        """The entry called `name` - exactly, else the only one that is the
        same word in singular/plural."""
        wanted = _norm(name)
        items = self.items(entity)
        exact = [i for i in items if _norm(i["name"]) == wanted]
        if exact:
            return exact[0]
        close = [i for i in items if prep_notes._same(i["name"], name)]
        return close[0] if len(close) == 1 else None


def select(col: Collection, flt: dict | None) -> list[dict]:
    """The recipes a FILTER means, found in the live data."""
    flt = flt if isinstance(flt, dict) else {}
    recipes = col.recipes()
    if flt.get("name"):
        part = _norm(flt["name"])
        recipes = [r for r in recipes if part in _norm(r["name"])]
    if flt.get("tag"):
        wanted = {_norm(t) for t in _list(flt["tag"])}
        recipes = [r for r in recipes if wanted & {_norm(t) for t in r["tags"]}]
    if flt.get("without_tag"):
        unwanted = {_norm(t) for t in _list(flt["without_tag"])}
        recipes = [r for r in recipes if not unwanted & {_norm(t) for t in r["tags"]}]
    if flt.get("cookbook"):
        book = _norm(flt["cookbook"])
        recipes = [r for r in recipes if book in {_norm(b) for b in r["cookbooks"]}]
    missing = set(_list(flt.get("missing")))
    for field, lacks in (("image", lambda r: not r["image"]), ("servings", lambda r: not r["servings"]),
                         ("time", lambda r: not r["minutes"]), ("tags", lambda r: not r["tags"])):
        if field in missing:
            recipes = [r for r in recipes if lacks(r)]
    if flt.get("ingredient"):
        ids = set()
        for word in _list(flt["ingredient"]):
            have = cook_today._norm(word)
            foods = [f for f in col.items("food") if cook_today._matches(have, [f["name"], f.get("plural_name") or ""])]
            if not foods:
                raise Problem(f"no ingredient called {word!r}")
            for food in foods[:30]:
                ids |= col.using_food(food["id"])
        recipes = [r for r in recipes if r["id"] in ids]
    return recipes


def _list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value or [] if str(v).strip()]


# ---------- the AI's plan -> suggestions ----------

ENTITY_OF = {"food": "food", "ingredient": "food", "unit": "unit", "tag": "keyword", "keyword": "keyword"}
LABEL = {"food": "ingredient", "unit": "unit", "keyword": "tag"}


def _names(recipes) -> str:
    shown = [r["name"] for r in recipes[:MAX_FOUND_LISTED]]
    more = len(recipes) - len(shown)
    return "\n".join(f"• {n}" for n in shown) + (f"\n… and {more} more" if more > 0 else "")


def _suggestion(kind, summary, detail, recipes=None) -> ToolSuggestion:
    if recipes is not None:
        detail["recipe_ids"] = [r["id"] for r in recipes]
        if len(recipes) > CONFIRM_ABOVE:
            detail["confirm"] = True
    return ToolSuggestion(id=uuid.uuid4().hex[:10], kind=kind, summary=summary, detail=detail,
                          preview=_names(recipes) if recipes else None)


def _entity_action(col, action) -> ToolSuggestion:
    entity = ENTITY_OF.get(str(action.get("entity", "")).casefold())
    if not entity:
        raise Problem("unknown kind of entry")
    label = LABEL[entity]
    if action["type"] == "rename":
        item = col.find(entity, action.get("name", ""))
        new_name = str(action.get("new_name") or "").strip()
        if item is None:
            raise Problem(f"no {label} called {action.get('name')!r}")
        if not new_name or new_name == item["name"]:
            raise Problem(f"no new name for {item['name']!r}")
        existing = col.find(entity, new_name)
        if existing and existing["id"] != item["id"] and _norm(existing["name"]) == _norm(new_name):
            # the new name exists already: merging is what renaming would mean
            return _merge(entity, existing, [item], existing["name"])
        detail = {"type": "rename", "entity": entity, "id": item["id"], "new_name": new_name}
        return _suggestion("rename", f"{label}: rename {item['name']!r} -> {new_name!r}", detail)
    # merge
    names = _list(action.get("names"))
    into = str(action.get("into") or "").strip()
    found = [col.find(entity, n) for n in names]
    unknown = [n for n, f in zip(names, found) if f is None]
    if unknown:
        raise Problem(f"no {label} called " + ", ".join(repr(n) for n in unknown))
    keep = col.find(entity, into) if into else None
    keep = keep or found[0]
    others = [f for f in {f["id"]: f for f in found}.values() if f["id"] != keep["id"]]
    if not others:
        raise Problem(f"nothing to merge into {keep['name']!r}")
    return _merge(entity, keep, others, into or keep["name"])


def _merge(entity, keep, others, keep_name) -> ToolSuggestion:
    detail = {"type": "merge", "entity": entity, "keep_id": keep["id"], "keep_name": keep_name,
              "remove_ids": [o["id"] for o in others]}
    text = f"{LABEL[entity]}: merge {', '.join(repr(o['name']) for o in others)} into {keep['name']!r}"
    if keep_name != keep["name"]:
        text += f" -> {keep_name!r}"
    return _suggestion("merge", text, detail)


def to_suggestions(col, actions) -> tuple[list[ToolSuggestion], list[str], list[dict]]:
    """(suggestions, problems, answers) - answers are the "find" results."""
    suggestions, problems, answers = [], [], []
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        try:
            if kind in ("rename", "merge"):
                suggestions.append(_entity_action(col, action))
            elif kind == "find":
                found = select(col, action.get("recipes"))
                answers.append({"count": len(found),
                                "recipes": [{"id": r["id"], "name": r["name"]} for r in found[:MAX_ANSWER_RECIPES]]})
            elif kind in ("add_tag", "remove_tag"):
                tag = str(action.get("tag") or "").strip()
                if not tag:
                    raise Problem("no tag given")
                found = select(col, action.get("recipes"))
                has = lambda r: _norm(tag) in {_norm(t) for t in r["tags"]}  # noqa: E731
                found = [r for r in found if not has(r)] if kind == "add_tag" else [r for r in found if has(r)]
                if not found:
                    raise Problem(f"no recipe to {'tag with' if kind == 'add_tag' else 'untag'} {tag!r}")
                known = col.find("keyword", tag)
                tag = known["name"] if known and _norm(known["name"]) == _norm(tag) else tag
                verb = "add tag" if kind == "add_tag" else "remove tag"
                suggestions.append(_suggestion("bulk_tag", f"recipes: {verb} {tag!r} ({len(found)})",
                                               {"op": kind, "tag": tag}, found))
            elif kind == "delete_tag":
                item = col.find("keyword", str(action.get("tag") or ""))
                if item is None or _norm(item["name"]) != _norm(action.get("tag")):
                    raise Problem(f"no tag called {action.get('tag')!r}")
                found = [r for r in col.recipes() if _norm(item["name"]) in {_norm(t) for t in r["tags"]}]
                s = _suggestion("delete_tag", f"tag: delete {item['name']!r} (used by {len(found)} recipe(s))",
                                {"id": item["id"], "name": item["name"]}, found)
                s.detail["confirm"] = True
                suggestions.append(s)
            elif kind == "set_servings":
                servings = action.get("servings")
                if not isinstance(servings, (int, float)) or not 0 < servings <= 100:
                    raise Problem("no sensible number of servings")
                found = [r for r in select(col, action.get("recipes")) if r["servings"] != servings]
                if not found:
                    raise Problem("no recipe to change")
                suggestions.append(_suggestion("bulk_servings", f"recipes: servings -> {servings:g} ({len(found)})",
                                               {"servings": servings}, found))
            elif kind == "add_to_cookbook":
                book = str(action.get("cookbook") or "").strip()
                if not book:
                    raise Problem("no cookbook given")
                found = [r for r in select(col, action.get("recipes")) if _norm(book) not in {_norm(b) for b in r["cookbooks"]}]
                if not found:
                    raise Problem(f"no recipe to add to {book!r}")
                book = next((b for b in col.cookbooks() if _norm(b) == _norm(book)), book)
                suggestions.append(_suggestion("bulk_cookbook", f"recipes: into cookbook {book!r} ({len(found)})",
                                               {"cookbook": book}, found))
            elif kind == "set_category":
                if col.mealie:
                    raise Problem("supermarket categories exist only in Tandoor")
                category = str(action.get("category") or "").strip()
                foods = [(n, col.find("food", n)) for n in _list(action.get("foods"))]
                unknown = [n for n, f in foods if f is None]
                if unknown:
                    raise Problem("no ingredient called " + ", ".join(repr(n) for n in unknown))
                if not category or not foods:
                    raise Problem("no category or ingredients given")
                known = [f for _n, f in foods]
                suggestions.append(ToolSuggestion(
                    id=uuid.uuid4().hex[:10], kind="food_category",
                    summary=f"ingredients: category {category!r} for {', '.join(repr(f['name']) for f in known)}",
                    detail={"category": category, "food_ids": [f["id"] for f in known]}))
            else:
                problems.append(f"unknown action {kind!r}")
        except Problem as exc:
            problems.append(str(exc))
    return suggestions, problems, answers


# ---------- one chat message ----------

def chat(message: str, job_id: str | None = None) -> dict:
    """Asks the AI for a plan and turns it into suggestions of a db_chat run
    (one run per conversation). Returns the run's id and this turn."""
    if not llm_provider.is_configured():
        raise tandoor_client.TandoorError(llm_provider.missing_key_hint())
    job = tool_jobs.get_tool_job(job_id) if job_id else None
    if job is None or job.tool != TOOL:
        job = tool_jobs.create_tool_job(TOOL)
        job.status = "ready"
        job.meta["chat"] = []
    history = job.meta.get("chat", [])
    with target.client().get_client() as client:
        col = Collection(client)
        tags = sorted((k["name"] for k in col.items("keyword")), key=str.casefold)[:MAX_TAGS_SHOWN]
        payload = {"message": message, "history": [{"role": h["role"], "text": h["text"]} for h in history[-HISTORY:]],
                   "recipe_count": len(col.recipes()), "tags": tags, "cookbooks": col.cookbooks(),
                   "categories": col.categories()}

        def ask(system_prompt):
            out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                         max_tokens=2000)
            tokens_in, tokens_out = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
            job.token_usage.input_tokens += tokens_in
            job.token_usage.output_tokens += tokens_out
            usage_log.record(TOOL, tokens_in, tokens_out)
            return out, usage
        prompt = PROMPT.replace("{language}", settings.output_language).replace("{manager}", target.name())
        answer = json_answer.complete(ask, prompt)[0]
        answer = answer if isinstance(answer, dict) else {}
        suggestions, problems, answers = to_suggestions(col, answer.get("actions"))
    for s in suggestions:
        s.detail["turn"] = len(history) // 2
        if s.detail.get("confirm"):
            s.detail["flagged"] = True  # not part of "select all" in Review either
    job.suggestions += suggestions
    turn = {"reply": str(answer.get("reply") or "").strip(), "suggestion_ids": [s.id for s in suggestions],
            "problems": problems, "answers": answers}
    job.meta["chat"] = history + [{"role": "user", "text": message},
                                  {"role": "assistant", "text": turn["reply"] or "-", **turn}]
    tool_jobs.save_tool_job(job)
    return {"job_id": job.id, **turn}


# ---------- applying ----------

def _apply(client, suggestion) -> None:
    d = suggestion.detail
    mealie = target.is_mealie()
    if suggestion.kind in ("rename", "merge"):
        if mealie:
            from . import mealie_tools
            mealie_tools.apply(client, suggestion)
        return  # Tandoor: the tool's own apply (apply_suggestion below)
    if suggestion.kind == "bulk_tag":
        for rid in d["recipe_ids"]:
            _set_tag(client, rid, d["tag"], d["op"] == "add_tag", mealie)
    elif suggestion.kind == "delete_tag":
        if mealie:
            resp = client.delete(f"/organizers/tags/{d['id']}")
            if resp.status_code != 404:
                mealie_client._check(resp, "deleting the tag")
        else:
            for rid in d.get("recipe_ids", []):  # off the recipes first, so undo can put it back
                _set_tag(client, rid, d["name"], False, mealie)
            delete_entity(client, "keyword", d["id"])
    elif suggestion.kind == "bulk_servings":
        for rid in d["recipe_ids"]:
            if mealie:
                mealie_client._check(client.patch(f"/recipes/{rid}", json={"recipeServings": d["servings"]}), "servings")
            else:
                _ok(client.patch(f"/recipe/{rid}/", json={"servings": d["servings"]}))
    elif suggestion.kind == "bulk_cookbook":
        api = mealie_client if mealie else tandoor_client
        book_id, endpoint = api.get_or_create_cookbook(client, d["cookbook"])
        for rid in d["recipe_ids"]:
            api.add_recipe_to_cookbook(client, book_id, endpoint, rid)
    elif suggestion.kind == "food_category":
        cat_id, cat_name = tandoor_client._get_or_create(client, "supermarket-category", d["category"])
        for fid in d["food_ids"]:
            _ok(client.patch(f"/food/{fid}/", json={"supermarket_category": {"id": cat_id, "name": cat_name}}))
    else:
        raise tandoor_client.TandoorError(f"Unknown suggestion kind {suggestion.kind!r}")


def _ok(resp):
    if resp.status_code not in (200, 201, 204):
        raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
    return resp


def _set_tag(client, rid, tag, add: bool, mealie: bool) -> None:
    if mealie:
        from . import mealie_tools
        if add:
            mealie_tools.add_tags(client, rid, [tag])
            return
        recipe = mealie_tools._fetch(client, rid)
        tags = [t for t in recipe.get("tags") or [] if _norm(t.get("name")) != _norm(tag)]
        mealie_tools._patch(client, rid, {"tags": tags})
        return
    recipe = _ok(client.get(f"/recipe/{rid}/")).json()
    keywords = [minimal_ref(k) for k in recipe.get("keywords") or []]
    if add:
        if _norm(tag) in {_norm(k["name"]) for k in keywords}:
            return
        kid, name = tandoor_client._get_or_create(client, "keyword", tag)
        keywords.append({"id": kid, "name": name})
    else:
        keywords = [k for k in keywords if _norm(k["name"]) != _norm(tag)]
    _ok(client.patch(f"/recipe/{rid}/", json={"keywords": keywords}))


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion
    if suggestion.kind in ("rename", "merge") and not target.is_mealie():
        from . import tools_ingredients, tools_tags, tools_units
        apply_fn = {"food": tools_ingredients.apply_suggestion, "unit": tools_units.apply_suggestion,
                    "keyword": tools_tags.apply_suggestion}[suggestion.detail["entity"]]
        return apply_fn(job_id, suggestion_id)
    try:
        with target.client().get_client() as client:
            _apply(client, suggestion)
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
