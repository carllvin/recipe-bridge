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
"tags": [string], "tag_groups": [string], "cookbooks": [string], "categories": [string]}
("categories" = supermarket categories; empty when there are none. "tag_groups"
= the tags that have other tags sorted below them - Tandoor's tag groups,
e.g. "Quelle" or "Mahlzeit"; empty when the recipe manager has none).

FILTER - which recipes are meant; every field optional, all combined with AND:
{"name": string (part of the title), "ingredient": [string] (uses any of these),
 "tag": [string] (has any of these), "without_tag": [string] (has none of these),
 "cookbook": string, "missing": ["image" | "servings" | "time" | "tags"]}
An empty FILTER {} means every recipe.

Actions ("entity" is "food" (ingredient), "unit" or "tag"):
{"type": "rename", "entity": ..., "name": <current name>, "new_name": <new name>}
{"type": "merge", "entity": ..., "names": [<names that mean the same>], "into": <name to keep>}
{"type": "change_case", "entity": ..., "case": "lower" | "upper" | "capitalize", "names": [<names>]}
  (writes names in small letters, capitals, or with a capital first letter;
  leave out "names" for all of them - the app lists every change itself)
{"type": "add_tag", "recipes": FILTER, "tag": <tag>}
{"type": "remove_tag", "recipes": FILTER, "tag": <tag>}
{"type": "delete_tag", "tag": <tag>} (removes it from every recipe and deletes it)
{"type": "set_servings", "recipes": FILTER, "servings": <number>}
{"type": "add_to_cookbook", "recipes": FILTER, "cookbook": <name, new or existing>}
{"type": "set_category", "foods": [<ingredient names>], "category": <name>} (only if "categories" is given)
{"type": "group_tags", "tags": [<tag names>] | "ungrouped", "group": <group name, new or existing>}
  (sorts tags into a tag group; "ungrouped" = every tag that is in no group yet -
  the "tags without a group" tile; only with Tandoor)
{"type": "replace_tag", "recipes": FILTER, "tag": <tag>, "new_tag": <tag>} (on those recipes)
{"type": "replace_ingredient", "recipes": FILTER, "ingredient": <name>, "new_ingredient": <name>}
  (only in those recipes - the ingredient itself stays for the others)
{"type": "remove_from_cookbook", "recipes": FILTER, "cookbook": <name>}
{"type": "rename_cookbook", "cookbook": <name>, "new_name": <name>}
{"type": "delete_cookbook", "cookbook": <name>} (the recipes stay)
{"type": "delete_recipes", "recipes": FILTER} (never with an empty FILTER)
{"type": "rename_recipe", "recipes": FILTER, "new_name": <title>} (FILTER meaning one recipe)
{"type": "edit_titles", "recipes": FILTER, "find": <text>, "replace": <text; "" removes it>}
{"type": "edit_titles", "recipes": FILTER, "case": "lower" | "upper" | "capitalize"}
{"type": "set_time", "recipes": FILTER, "prep_minutes": <number>, "cook_minutes": <number>}
  (either or both)
{"type": "set_plural", "entity": "food" | "unit", "name": <name>, "plural": <plural>}
{"type": "run_tool", "tool": TOOL, "recipes": FILTER} (starts a maintenance tool with
  AI for just those recipes; its suggestions come into the review inbox). TOOL is
  "season" (season tag), "more_tags" (fill up to five tags), "source" (source tag),
  "translate" (into {language}), "restructure" (steps, ingredient groups),
  "amounts" (amounts into the steps), "servings", "images" (find a photo),
  "photos" (improve weak photos)
{"type": "undo"} (takes back the changes applied in the chat's last step)
{"type": "find", "recipes": FILTER} (a question - the app lists the recipes)

Rules:
- use only these actions; if the request needs something else, give no
  actions and say in "reply" what is possible instead
- for tags and cookbooks use the exact spelling from the lists when one is
  meant; ingredients and units: the cook's words (the app finds them)
- a change to many names at once that one action covers (e.g. all tags in
  small letters -> change_case) is that one action, never a long list of renames
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


CASES = {"lower": str.lower, "upper": str.upper, "capitalize": lambda name: name[:1].upper() + name[1:]}


def _change_case(col, action) -> list[ToolSuggestion]:
    """Every name (or the given ones) in other letters - one rename each;
    names that end up the same ("Vegan", "VEGAN") are merged."""
    entity = ENTITY_OF.get(str(action.get("entity", "")).casefold())
    write = CASES.get(str(action.get("case", "")).casefold())
    if not entity or not write:
        raise Problem("unknown kind of entry or spelling")
    names = _list(action.get("names"))
    items = [col.find(entity, n) for n in names] if names else col.items(entity)
    unknown = [n for n, item in zip(names, items) if item is None]
    if unknown:
        raise Problem(f"no {LABEL[entity]} called " + ", ".join(repr(n) for n in unknown))
    by_new: dict[str, list[dict]] = {}
    for item in {i["id"]: i for i in items if i and i.get("name")}.values():
        by_new.setdefault(write(item["name"].strip()), []).append(item)
    out = []
    for new_name, same in sorted(by_new.items(), key=lambda pair: pair[0].casefold()):
        keep = next((i for i in same if i["name"] == new_name), same[0])
        others = [i for i in same if i is not keep]
        if others:
            out.append(_merge(entity, keep, others, new_name))
        elif keep["name"] != new_name:
            out.append(_suggestion("rename", f"{LABEL[entity]}: rename {keep['name']!r} -> {new_name!r}",
                                   {"type": "rename", "entity": entity, "id": keep["id"], "new_name": new_name}))
    if not out:
        raise Problem(f"every {LABEL[entity]} is written that way already")
    return out


def _merge(entity, keep, others, keep_name) -> ToolSuggestion:
    detail = {"type": "merge", "entity": entity, "keep_id": keep["id"], "keep_name": keep_name,
              "remove_ids": [o["id"] for o in others]}
    text = f"{LABEL[entity]}: merge {', '.join(repr(o['name']) for o in others)} into {keep['name']!r}"
    if keep_name != keep["name"]:
        text += f" -> {keep_name!r}"
    return _suggestion("merge", text, detail)


def to_suggestions(col, actions, job=None) -> tuple[list[ToolSuggestion], list[str], list[dict]]:
    """(suggestions, problems, answers) - answers are the "find" results."""
    suggestions, problems, answers = [], [], []
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        try:
            if kind in ("rename", "merge"):
                suggestions.append(_entity_action(col, action))
            elif kind == "change_case":
                suggestions += _change_case(col, action)
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
            elif kind == "group_tags":
                suggestions.append(_group_tags(col, action))
            elif kind == "undo":
                suggestions.append(_undo(col, action, job))
            elif kind in MORE:
                result = MORE[kind](col, action)
                suggestions += result if isinstance(result, list) else [result]
            else:
                problems.append(f"unknown action {kind!r}")
        except Problem as exc:
            problems.append(str(exc))
    return suggestions, problems, answers


def _group_tags(col, action) -> ToolSuggestion:
    """Tags into a tag group (Tandoor) - applied like the tag-groups tool's
    suggestions (tools_tag_groups.apply_suggestion)."""
    from . import tools_tag_groups
    if col.mealie:
        raise Problem("tag groups exist only in Tandoor")
    group = str(action.get("group") or "").strip()
    if not group:
        raise Problem("no group given")
    keywords = tandoor_client.fetch_pages(col.client, "/keyword/")
    existing = next((k for k in keywords if _norm(k.get("name")) == _norm(group)), None)
    wanted = action.get("tags")
    if wanted == "ungrouped" or _list(wanted) == ["ungrouped"]:
        tags = tools_tag_groups.ungrouped(keywords)
    else:
        by_name = {_norm(k.get("name")): k for k in keywords}
        unknown = [t for t in _list(wanted) if _norm(t) not in by_name]
        if unknown:
            raise Problem("no tag called " + ", ".join(repr(t) for t in unknown))
        tags = [by_name[_norm(t)] for t in _list(wanted)]
    if existing:  # not into itself, and not what is in that group already
        tags = [k for k in tags if k["id"] != existing["id"] and tools_tag_groups._parent_id(k) != existing["id"]]
    if not tags:
        raise Problem(f"no tag to sort into {group!r}")
    name = existing["name"] if existing else group
    s = ToolSuggestion(
        id=uuid.uuid4().hex[:10], kind="group_tags",
        summary=f"tags: {len(tags)} into the group {name!r}" + ("" if existing else " (new)"),
        detail={"group": name, "group_id": existing["id"] if existing else None,
                "tags": [{"id": k["id"], "name": k.get("name", "")} for k in tags]},
        preview="\n".join(f"• {k.get('name', '')}" for k in tags[:MAX_FOUND_LISTED])
        + (f"\n… and {len(tags) - MAX_FOUND_LISTED} more" if len(tags) > MAX_FOUND_LISTED else ""))
    if len(tags) > CONFIRM_ABOVE:
        s.detail["confirm"] = True
    return s


# ---------- more actions ----------

def _found(col, action, what) -> list[dict]:
    found = select(col, action.get("recipes"))
    if not found:
        raise Problem(f"no recipe to {what}")
    return found


def _replace_tag(col, action) -> ToolSuggestion:
    old, new = str(action.get("tag") or "").strip(), str(action.get("new_tag") or "").strip()
    if not old or not new or _norm(old) == _norm(new):
        raise Problem("no tag and new tag given")
    found = [r for r in select(col, action.get("recipes")) if _norm(old) in {_norm(t) for t in r["tags"]}]
    if not found:
        raise Problem(f"no recipe with the tag {old!r}")
    known = col.find("keyword", new)
    new = known["name"] if known and _norm(known["name"]) == _norm(new) else new
    old = next((t for r in found for t in r["tags"] if _norm(t) == _norm(old)), old)
    return _suggestion("bulk_tag", f"recipes: tag {old!r} -> {new!r} ({len(found)})",
                       {"op": "replace_tag", "tag": old, "new_tag": new}, found)


def _replace_ingredient(col, action) -> ToolSuggestion:
    old = col.find("food", str(action.get("ingredient") or ""))
    new = str(action.get("new_ingredient") or "").strip()
    if old is None:
        raise Problem(f"no ingredient called {action.get('ingredient')!r}")
    if not new or _norm(new) == _norm(old["name"]):
        raise Problem("no new ingredient given")
    using = col.using_food(old["id"])
    found = [r for r in select(col, action.get("recipes")) if r["id"] in using]
    if not found:
        raise Problem(f"no recipe with {old['name']!r} to change")
    known = col.find("food", new)
    new = known["name"] if known else new
    return _suggestion("replace_food", f"recipes: ingredient {old['name']!r} -> {new!r} ({len(found)})",
                       {"food_id": old["id"], "food": old["name"], "new_food": new}, found)


def _book(col, name) -> dict:
    """{"id", "name", "endpoint"} of the cookbook (Mealie: category)."""
    for endpoint in (["category"] if col.mealie else tandoor_client._COOKBOOK_ENDPOINT_CANDIDATES):
        try:
            api = mealie_client if col.mealie else tandoor_client
            books = api.fetch_all_items(col.client, endpoint)
        except tandoor_client.TandoorError:
            continue
        book = next((b for b in books if _norm(b["name"]) == _norm(name)), None)
        if book:
            return {"id": book["id"], "name": book["name"], "endpoint": endpoint}
        break
    raise Problem(f"no cookbook called {name!r}")


def _remove_from_cookbook(col, action) -> ToolSuggestion:
    book = _book(col, str(action.get("cookbook") or ""))
    found = [r for r in select(col, action.get("recipes")) if _norm(book["name"]) in {_norm(b) for b in r["cookbooks"]}]
    if not found:
        raise Problem(f"no recipe to take out of {book['name']!r}")
    return _suggestion("cookbook_remove", f"recipes: out of the cookbook {book['name']!r} ({len(found)})",
                       {"book": book}, found)


def _rename_cookbook(col, action) -> ToolSuggestion:
    book = _book(col, str(action.get("cookbook") or ""))
    new = str(action.get("new_name") or "").strip()
    if not new or new == book["name"]:
        raise Problem(f"no new name for {book['name']!r}")
    return _suggestion("cookbook_rename", f"cookbook: rename {book['name']!r} -> {new!r}", {"book": book, "new_name": new})


def _delete_cookbook(col, action) -> ToolSuggestion:
    book = _book(col, str(action.get("cookbook") or ""))
    found = [r for r in col.recipes() if _norm(book["name"]) in {_norm(b) for b in r["cookbooks"]}]
    s = _suggestion("cookbook_delete", f"cookbook: delete {book['name']!r} ({len(found)} recipe(s) stay)",
                    {"book": book}, found)
    s.detail["confirm"] = True
    return s


def _delete_recipes(col, action) -> ToolSuggestion:
    flt = action.get("recipes")
    if not isinstance(flt, dict) or not any(flt.values()):
        raise Problem("deleting every recipe isn't offered - say which ones")
    found = _found(col, action, "delete")
    s = _suggestion("delete_recipes", f"recipes: delete {len(found)}", {}, found)
    s.detail["confirm"] = True
    return s


def _rename_recipe(col, action) -> ToolSuggestion:
    found = _found(col, action, "rename")
    new = str(action.get("new_name") or "").strip()[:128]
    if len(found) != 1:
        raise Problem(f"{len(found)} recipes match - say which one")
    if not new or new == found[0]["name"]:
        raise Problem("no new title given")
    return _titles({found[0]["id"]: (found[0]["name"], new)})


def _edit_titles(col, action) -> ToolSuggestion:
    found = _found(col, action, "rename")
    write = CASES.get(str(action.get("case") or "").casefold())
    find = str(action.get("find") or "")
    if not write and not find.strip():
        raise Problem("no text to change in the titles")
    changes = {}
    for r in found:
        if write:
            new = write(r["name"])
        else:
            new = re.sub(re.escape(find.strip()), str(action.get("replace") or ""), r["name"], flags=re.IGNORECASE)
            new = re.sub(r"\s{2,}", " ", new).strip(" -–,:")
        if new and new != r["name"]:
            changes[r["id"]] = (r["name"], new[:128])
    if not changes:
        raise Problem("no title would change")
    return _titles(changes)


def _titles(changes: dict) -> ToolSuggestion:
    rows = [f"• {old} → {new}" for old, new in changes.values()]
    more = len(rows) - MAX_FOUND_LISTED
    s = ToolSuggestion(id=uuid.uuid4().hex[:10], kind="bulk_titles", summary=f"recipes: new title ({len(changes)})",
                       detail={"titles": {str(k): new for k, (_old, new) in changes.items()},
                               "recipe_ids": list(changes)},
                       preview="\n".join(rows[:MAX_FOUND_LISTED]) + (f"\n… and {more} more" if more > 0 else ""))
    if len(changes) > CONFIRM_ABOVE:
        s.detail["confirm"] = True
    return s


def _minutes(value):
    if value is None or value == "":
        return None
    if not isinstance(value, (int, float)) or not 0 <= value <= 24 * 60:
        raise Problem("no sensible time given")
    return int(round(value))


def _set_time(col, action) -> ToolSuggestion:
    prep, cook = _minutes(action.get("prep_minutes")), _minutes(action.get("cook_minutes"))
    if prep is None and cook is None:
        raise Problem("no time given")
    found = _found(col, action, "change")
    parts = [f"prep {prep} min" if prep is not None else "", f"cooking {cook} min" if cook is not None else ""]
    return _suggestion("bulk_time", f"recipes: {', '.join(p for p in parts if p)} ({len(found)})",
                       {"prep": prep, "cook": cook}, found)


def _set_plural(col, action) -> ToolSuggestion:
    entity = ENTITY_OF.get(str(action.get("entity", "")).casefold())
    if entity not in ("food", "unit"):
        raise Problem("plurals exist for ingredients and units")
    item = col.find(entity, str(action.get("name") or ""))
    plural = str(action.get("plural") or "").strip()
    if item is None:
        raise Problem(f"no {LABEL[entity]} called {action.get('name')!r}")
    if not plural or plural == (item.get("plural_name") or ""):
        raise Problem(f"no new plural for {item['name']!r}")
    return _suggestion("set_plural", f"{LABEL[entity]}: plural of {item['name']!r} -> {plural!r}",
                       {"entity": entity, "id": item["id"], "plural": plural})


# chat name -> (maintenance tool, what it does)
TOOLS = {"season": ("tags_season", "season tag"), "more_tags": ("tags_suggest_more", "more tags"),
         "source": ("recipes_source", "source tag"), "translate": ("recipes_translate", "translate"),
         "restructure": ("recipes_restructure", "restructure"), "amounts": ("recipes_amounts", "amounts into the steps"),
         "servings": ("recipes_servings", "servings"), "images": ("recipes_images", "find a photo"),
         "photos": ("recipes_photos", "improve the photo")}


def _run_tool(col, action) -> ToolSuggestion:
    name = str(action.get("tool") or "").casefold()
    if name not in TOOLS:
        raise Problem(f"no tool {name!r}")
    tool, what = TOOLS[name]
    if col.mealie:
        from . import mealie_maintenance
        if tool not in mealie_maintenance.TOOLS:
            raise Problem(f"{what!r} needs Tandoor")
    found = _found(col, action, "check")
    s = _suggestion("run_tool", f"AI: {what} for {len(found)} recipe(s)", {"tool": tool}, found)
    s.detail.pop("confirm", None)  # nothing changes yet - its suggestions are reviewed
    return s


def _undo(col, action, job=None) -> ToolSuggestion:
    if col.mealie:
        raise Problem("with Mealie, changes can't be taken back")
    done = [s for s in (job.suggestions if job else []) if s.status == "applied" and s.undoable and s.kind != "undo_turn"]
    if not done:
        raise Problem("nothing applied in this chat to take back")
    last = max(s.detail.get("turn", 0) for s in done)
    ids = [s.id for s in done if s.detail.get("turn", 0) == last]
    return ToolSuggestion(id=uuid.uuid4().hex[:10], kind="undo_turn", summary=f"undo: {len(ids)} change(s)",
                          detail={"ids": ids},
                          preview="\n".join(f"• {s.summary}" for s in done if s.id in ids))


MORE = {"replace_tag": _replace_tag, "replace_ingredient": _replace_ingredient,
        "remove_from_cookbook": _remove_from_cookbook, "rename_cookbook": _rename_cookbook,
        "delete_cookbook": _delete_cookbook, "delete_recipes": _delete_recipes, "rename_recipe": _rename_recipe,
        "edit_titles": _edit_titles, "set_time": _set_time, "set_plural": _set_plural, "run_tool": _run_tool}


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
        groups = sorted((k["name"] for k in col.items("keyword") if k.get("numchild")), key=str.casefold)
        payload = {"message": message, "history": [{"role": h["role"], "text": h["text"]} for h in history[-HISTORY:]],
                   "recipe_count": len(col.recipes()), "tags": tags, "tag_groups": groups,
                   "cookbooks": col.cookbooks(), "categories": col.categories()}

        def ask(system_prompt):
            out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                         max_tokens=4000)
            tokens_in, tokens_out = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
            job.token_usage.input_tokens += tokens_in
            job.token_usage.output_tokens += tokens_out
            usage_log.record(TOOL, tokens_in, tokens_out)
            return out, usage
        prompt = PROMPT.replace("{language}", settings.output_language).replace("{manager}", target.name())
        cut_off = []
        try:
            answer = json_answer.complete(ask, prompt)[0]
        except json.JSONDecodeError:  # cut off - far too many actions for one answer
            answer, cut_off = {}, ["the AI's answer was too long - ask for a smaller part at a time"]
        answer = answer if isinstance(answer, dict) else {}
        suggestions, problems, answers = to_suggestions(col, answer.get("actions"), job)
        problems = cut_off + problems
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

def _apply(client, suggestion, renamed=None) -> None:
    """renamed: Mealie recipe slugs changed by a new title in this chat (old -> new)."""
    d = suggestion.detail
    mealie = target.is_mealie()
    if mealie and renamed:
        d = {**d, "recipe_ids": [renamed.get(r, r) for r in d.get("recipe_ids") or []],
             "titles": {renamed.get(k, k): v for k, v in (d.get("titles") or {}).items()}}
    if suggestion.kind in ("rename", "merge"):
        if mealie:
            from . import mealie_tools
            mealie_tools.apply(client, suggestion)
        return  # Tandoor: the tool's own apply (apply_suggestion below)
    if suggestion.kind == "bulk_tag" and d["op"] == "replace_tag":
        for rid in d["recipe_ids"]:
            _edit_tags(client, rid, [d["tag"]], [d["new_tag"]], mealie)
    elif suggestion.kind == "bulk_tag":
        for rid in d["recipe_ids"]:
            _set_tag(client, rid, d["tag"], d["op"] == "add_tag", mealie)
    elif suggestion.kind == "replace_food":
        _replace_food(client, d, mealie)
    elif suggestion.kind in ("cookbook_remove", "cookbook_rename", "cookbook_delete"):
        _cookbook(client, suggestion.kind, d, mealie)
    elif suggestion.kind == "delete_recipes":
        for rid in d["recipe_ids"]:
            if mealie:
                resp = client.delete(f"/recipes/{rid}")
                if resp.status_code != 404:
                    mealie_client._check(resp, "deleting the recipe")
            else:
                resp = client.delete(f"/recipe/{rid}/")
                if resp.status_code != 404:
                    _ok(resp)
    elif suggestion.kind == "bulk_titles":
        for rid, name in d["titles"].items():
            if mealie:
                saved = mealie_client._check(client.patch(f"/recipes/{rid}", json={"name": name}), "the title")
                slug = saved.json().get("slug") if saved.content else None
                if slug and slug != rid and renamed is not None:
                    renamed.update({old: slug for old, now in renamed.items() if now == rid} | {rid: slug})
            else:
                _ok(client.patch(f"/recipe/{rid}/", json={"name": name}))
    elif suggestion.kind == "bulk_time":
        for rid in d["recipe_ids"]:
            if mealie:
                body = {k: f"{v} min" for k, v in (("prepTime", d["prep"]), ("performTime", d["cook"])) if v is not None}
                if d["prep"] is not None and d["cook"] is not None:
                    body["totalTime"] = f"{d['prep'] + d['cook']} min"
                mealie_client._check(client.patch(f"/recipes/{rid}", json=body), "the times")
            else:
                body = {k: v for k, v in (("working_time", d["prep"]), ("waiting_time", d["cook"])) if v is not None}
                _ok(client.patch(f"/recipe/{rid}/", json=body))
    elif suggestion.kind == "set_plural":
        if mealie:
            path = f"{mealie_client.ENDPOINTS[d['entity']]}/{d['id']}"
            item = mealie_client._check(client.get(path), "loading the entry").json()
            mealie_client._check(client.put(path, json={**item, "pluralName": d["plural"]}), "the plural")
        else:
            _ok(client.patch(f"/{d['entity']}/{d['id']}/", json={"plural_name": d["plural"]}))
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
    _edit_tags(client, rid, [] if add else [tag], [tag] if add else [], mealie)


def _edit_tags(client, rid, remove, add, mealie: bool) -> None:
    """Takes tags off a recipe and puts others on - one save."""
    gone = {_norm(t) for t in remove}
    if mealie:
        from . import mealie_tools
        recipe = mealie_tools._fetch(client, rid)
        tags = [{"id": t["id"], "name": t["name"], "slug": t.get("slug")} for t in recipe.get("tags") or []
                if _norm(t.get("name")) not in gone]
        lookup = mealie_client._Lookup(client)
        for name in add:
            if _norm(name) not in {_norm(t["name"]) for t in tags}:
                tag = lookup.get_or_create("keyword", name)
                tags.append({"id": tag["id"], "name": tag["name"], "slug": tag.get("slug")})
        mealie_tools._patch(client, rid, {"tags": tags})
        return
    recipe = _ok(client.get(f"/recipe/{rid}/")).json()
    keywords = [minimal_ref(k) for k in recipe.get("keywords") or [] if _norm(k.get("name")) not in gone]
    before = len(recipe.get("keywords") or [])
    for name in add:
        if _norm(name) not in {_norm(k["name"]) for k in keywords}:
            kid, canonical = tandoor_client._get_or_create(client, "keyword", name)
            keywords.append({"id": kid, "name": canonical})
    if len(keywords) != before or add:
        _ok(client.patch(f"/recipe/{rid}/", json={"keywords": keywords}))


def _replace_food(client, d, mealie: bool) -> None:
    """The ingredient becomes another one - only in the listed recipes."""
    if mealie:
        from . import mealie_tools
        new = mealie_client._Lookup(client).get_or_create("food", d["new_food"])
        for rid in d["recipe_ids"]:
            recipe = mealie_tools._fetch(client, rid)
            rows = recipe.get("recipeIngredient") or []
            for row in rows:
                if (row.get("food") or {}).get("id") == d["food_id"]:
                    row["food"] = {"id": new["id"], "name": new["name"]}
            mealie_tools._patch(client, rid, {"recipeIngredient": rows})
        return
    from .tools_ingredients import _repoint_recipe_food
    new_id, new_name = tandoor_client._get_or_create(client, "food", d["new_food"])
    for rid in d["recipe_ids"]:
        recipe = _ok(client.get(f"/recipe/{rid}/")).json()
        _ok(client.patch(f"/recipe/{rid}/", json=_repoint_recipe_food(recipe, d["food_id"], new_id, new_name)))


def _cookbook(client, kind, d, mealie: bool) -> None:
    book = d["book"]
    if mealie:  # cookbooks are categories
        path = f"/organizers/categories/{book['id']}"
        if kind == "cookbook_rename":
            item = mealie_client._check(client.get(path), "loading the category").json()
            mealie_client._check(client.put(path, json={**item, "name": d["new_name"]}), "renaming")
            return
        if kind == "cookbook_delete":
            mealie_client._check(client.delete(path), "deleting the category")
            return
        from . import mealie_tools
        for rid in d["recipe_ids"]:
            recipe = mealie_tools._fetch(client, rid)
            kept = [c for c in recipe.get("recipeCategory") or [] if c.get("id") != book["id"]]
            mealie_tools._patch(client, rid, {"recipeCategory": kept})
        return
    endpoint = f"/{book['endpoint']}/{book['id']}/"
    if kind == "cookbook_rename":
        _ok(client.patch(endpoint, json={"name": d["new_name"]}))
        return
    entries = "recipe-book-entry" if book["endpoint"] == "recipe-book" else "cookbook-recipe"
    rows = tandoor_client.fetch_pages(client, f"/{entries}/", params={"book": book["id"]})
    wanted = None if kind == "cookbook_delete" else {str(r) for r in d["recipe_ids"]}
    for row in rows:
        in_book = row.get("book")
        in_book = in_book.get("id") if isinstance(in_book, dict) else in_book
        recipe = row.get("recipe")
        recipe = recipe.get("id") if isinstance(recipe, dict) else recipe
        if in_book == book["id"] and (wanted is None or str(recipe) in wanted):
            _ok(client.delete(f"/{entries}/{row['id']}/"))  # one by one, so undo can put them back
    if kind == "cookbook_delete":
        _ok(client.delete(endpoint))


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion
    if suggestion.kind == "group_tags":
        from . import tools_tag_groups
        return tools_tag_groups.apply_suggestion(job_id, suggestion_id)
    if suggestion.kind in ("run_tool", "undo_turn"):
        try:
            if suggestion.kind == "run_tool":
                _start_tool(suggestion)
            else:
                _undo_turn(job, suggestion)
            suggestion.status = "applied"
        except Exception as exc:  # noqa: BLE001
            suggestion.status, suggestion.error = "error", str(getattr(exc, "detail", "") or exc)
        tool_jobs.save_tool_job(job)
        return suggestion
    if suggestion.kind in ("rename", "merge") and not target.is_mealie():
        from . import tools_ingredients, tools_tags, tools_units
        apply_fn = {"food": tools_ingredients.apply_suggestion, "unit": tools_units.apply_suggestion,
                    "keyword": tools_tags.apply_suggestion}[suggestion.detail["entity"]]
        return apply_fn(job_id, suggestion_id)
    try:
        with target.client().get_client() as client:
            _apply(client, suggestion, job.meta.setdefault("renamed", {}) if target.is_mealie() else None)
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion


def _start_tool(suggestion) -> None:
    """The maintenance tool for just these recipes (recipe_scope "only") -
    its suggestions come into the review inbox like a tile's."""
    from . import main  # late: main imports this module
    started = main._start_tool_job(suggestion.detail["tool"], {"only": suggestion.detail["recipe_ids"]})
    suggestion.detail["job_id"] = started["job_id"]


def _undo_turn(job, suggestion) -> None:
    """Takes back the chat's last applied changes, newest first. Not
    recorded itself - it isn't one more change to undo."""
    from . import main, undo
    failed = []
    with undo.paused():
        for sid in reversed(suggestion.detail["ids"]):
            try:
                main._perform_suggestion_action(job.id, sid, "undo")
            except Exception as exc:  # noqa: BLE001
                failed.append(str(exc))
    if failed:
        raise tandoor_client.TandoorError("; ".join(failed))
