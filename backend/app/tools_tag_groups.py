"""Tag groups: Tandoor can nest tags ("Diet" > "vegan", "vegetarian"), which
makes a long tag list much easier to use. The AI (tools model) sorts the
tags into a few groups; each suggestion is one group - creating the group
tag if needed and moving its tags under it. Undoable (see undo.py: moves are
reverted, a created group tag is deleted again).

Health tile "keywords_ungrouped": tags on the top level that aren't a group
themselves. Tags you want to keep ungrouped can be ignored there."""
from __future__ import annotations

import logging
import uuid

from . import ignored, llm_provider, tandoor_client, tool_jobs, tools_tags
from .config import settings
from .schemas import ToolSuggestion
from .tools_conversions import _fetch_all

log = logging.getLogger("recipe-bridge")

METRIC = "keywords_ungrouped"
MAX_TAGS = 400  # most used first

SYSTEM_PROMPT = """You organize the tags (keywords) of a home cook's recipe
collection into a few groups so the tag list gets clearer. The recipe app
shows a group as a parent tag with its tags below it.

You receive JSON: {"language": string, "groups": [existing group names],
"tags": [{"name": string, "recipes": number of recipes using it,
"group": null}]} - the tags that aren't in any group yet

Form 4 to 10 groups that people typically filter recipes by - for example
diet (vegetarian, vegan, gluten-free ...), cuisine or country, course / type
of dish (breakfast, soup, dessert ...), season, occasion, cooking method,
effort. Name new groups in the given language; reuse an existing group
wherever it fits.

Put a tag into a group only when it clearly belongs there - leave tags that
fit no group out. Each tag goes into at most one group. Never put a group
into another group.

Respond with ONLY a JSON array (no explanation, no markdown fence):
[{"group": "<group name>", "tags": ["<exact tag name>", ...]}]
"""


def _parent_id(keyword):
    parent = keyword.get("parent")
    return parent.get("id") if isinstance(parent, dict) else parent


def ungrouped(keywords) -> list[dict]:
    return sorted((k for k in keywords if not _parent_id(k) and not k.get("numchild")),
                  key=lambda k: (k.get("name") or "").casefold())


# ---------- source tags go together, without AI ----------
# The app knows which tags it gave as a recipe's source (source_tag.py) -
# "einfachkochen" can't be told from its name. They go into the group
# that already holds source tags, else one named like it, else a new one.

SOURCE_GROUP_NAMES = {"quelle", "quellen", "herkunft", "source", "sources", "fonte", "fonti", "fuente", "fuentes",
                      "origine", "origen"}
SOURCE_GROUP = {"de": "Quelle", "en": "Source", "fr": "Source", "it": "Fonte", "es": "Fuente"}


def source_group(keywords) -> tuple[dict | None, str]:
    """(the existing group for source tags or None, its name)."""
    from . import source_tag
    from .config import get_language_code
    sources = source_tag.all_tags()
    groups = {k["id"]: k for k in keywords if k.get("numchild")}
    counts = {}
    for k in keywords:
        parent = _parent_id(k)
        if parent in groups and k["name"].strip().casefold() in sources:
            counts[parent] = counts.get(parent, 0) + 1
    if counts:
        best = groups[max(counts, key=counts.get)]
        return best, best["name"]
    named = next((g for g in groups.values() if g["name"].strip().casefold() in SOURCE_GROUP_NAMES), None)
    if named:
        return named, named["name"]
    return None, SOURCE_GROUP.get(get_language_code(settings.output_language), "Source")


def split_sources(tags) -> tuple[list, list]:
    """(source tags, the others) of a list of keyword dicts."""
    from . import source_tag
    sources = source_tag.all_tags()
    return ([k for k in tags if k["name"].strip().casefold() in sources],
            [k for k in tags if k["name"].strip().casefold() not in sources])


def run_scan(job_id: str) -> None:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        if not llm_provider.is_configured():
            job.status, job.error = "error", llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return
        with tandoor_client.get_client() as client:
            job.progress_label = "Loading the tags..."
            tool_jobs.save_tool_job(job)
            keywords = _fetch_all(client, "keyword")
        by_id = {k["id"]: k for k in keywords}
        groups = {k["name"].casefold(): k for k in keywords if k.get("numchild")}
        skip = ignored.keys(METRIC)
        # Only the tags the tile lists (top level, not a group, not ignored) -
        # tags already in a group stay where they are; the existing groups
        # are sent by name.
        tags = sorted((k for k in ungrouped(keywords) if str(k["id"]) not in skip),
                      key=lambda k: -(k.get("numrecipe") or 0))[:MAX_TAGS]
        sources, tags = split_sources(tags)  # the app's own source tags: not for the AI
        by_name = {k["name"].casefold(): k for k in tags}
        job.progress_total = 1
        job.cost_estimate = f"{len(tags)} tag(s) -> 1 AI call with the tools model."
        job.progress_label = "Sorting the tags into groups..."
        tool_jobs.save_tool_job(job)

        payload = {
            "language": settings.output_language,
            "groups": sorted(k["name"] for k in groups.values()),
            "tags": [{"name": k["name"], "recipes": k.get("numrecipe") or 0,
                      "group": (by_id.get(_parent_id(k)) or {}).get("name")} for k in tags],
        }
        suggestions, placed = [], set()
        if sources:
            group, name = source_group(keywords)
            members = [{"id": k["id"], "name": k["name"]} for k in sources]
            suggestions.append(ToolSuggestion(
                id=uuid.uuid4().hex[:10], kind="group_tags",
                summary=f"{name}: {', '.join(m['name'] for m in members)}"[:200],
                detail={"group": name, "group_id": group["id"] if group else None, "tags": members},
                preview=f"{'existing' if group else 'new'} group {name!r} (sources)\n"
                        + "\n".join(f"  - {m['name']}" for m in members),
            ))
        answer = tools_tags._complete_json(job, SYSTEM_PROMPT, payload, max_tokens=4000) if tags else []
        for entry in answer if isinstance(answer, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("group") or "").strip()[:128]
            if not name or name.casefold() in by_name:
                continue  # empty, or a tag itself (would turn a used tag into a group)
            group = groups.get(name.casefold())
            members = []
            for tag_name in entry.get("tags") or []:
                tag = by_name.get(str(tag_name).strip().casefold())
                if tag is None or tag["id"] in placed or (group and _parent_id(tag) == group["id"]):
                    continue
                placed.add(tag["id"])
                members.append({"id": tag["id"], "name": tag["name"]})
            if not members:
                continue
            names = ", ".join(m["name"] for m in members)
            suggestions.append(ToolSuggestion(
                id=uuid.uuid4().hex[:10], kind="group_tags",
                summary=f"{name}: {names}" if len(names) < 160 else f"{name}: {len(members)} tags",
                detail={"group": name, "group_id": group["id"] if group else None, "tags": members},
                preview=f"{'existing' if group else 'new'} group {name!r}\n" + "\n".join(f"  - {m['name']}" for m in members),
            ))
        job.progress_current = 1
        job.suggestions = suggestions
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Tag group scan failed for job %s", job_id)
        job.status, job.error = "error", str(exc)
        tool_jobs.save_tool_job(job)


def _find_or_create_group(client, name, group_id):
    if group_id:
        resp = client.get(f"/keyword/{group_id}/")
        if resp.status_code == 200:
            return group_id
    resp = client.get("/keyword/", params={"query": name, "page_size": 50})
    if resp.status_code == 200:
        data = resp.json()
        for k in data.get("results", data) if isinstance(data, dict) else data:
            if (k.get("name") or "").casefold() == name.casefold():
                return k["id"]
    resp = client.post("/keyword/", json={"name": name})
    if resp.status_code not in (200, 201):
        raise tandoor_client.TandoorError(f"Could not create the group {name!r}: {resp.status_code} {resp.text[:200]}")
    return resp.json()["id"]


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion
    d = suggestion.detail
    try:
        with tandoor_client.get_client() as client:
            group_id = _find_or_create_group(client, d["group"], d.get("group_id"))
            failed = []
            for tag in d["tags"]:
                resp = client.put(f"/keyword/{tag['id']}/move/{group_id}/")
                if resp.status_code >= 400:
                    failed.append(f"{tag['name']} ({resp.status_code})")
            if failed and len(failed) == len(d["tags"]):
                raise tandoor_client.TandoorError("Could not move: " + ", ".join(failed))
            if failed:
                suggestion.error = "Not moved: " + ", ".join(failed)
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion


# ---------- after an import: new tags go straight into a fitting group ----------

PLACE_PROMPT = """You sort a few new tags (keywords) of a home cook's recipe
collection into the tag groups that already exist. You receive JSON:
{"groups": [{"name": string, "examples": [tags already in it]}], "tags": [string]}

For each tag pick the ONE existing group it clearly belongs to, judging by the
group's name and examples (e.g. a website, video channel or cookbook author
goes where other sources are; "vegan" where other diets are). When no group
clearly fits, give null - never invent a group.

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"<tag>": "<group name>" | null, ...}
"""
EXAMPLES_PER_GROUP = 8


def place_imported_tags(tag_names) -> int:
    """The given tags (an import's) that are in no group yet go into the
    existing group they fit - one small AI call with the tools model, applied
    right away; nothing when there are no groups. Returns how many moved."""
    from . import health, usage_log
    from .schemas import ToolJob
    wanted = {str(n).strip().casefold() for n in tag_names if str(n).strip()}
    if not wanted or not llm_provider.is_configured() or usage_log.budget_status().get("exceeded"):
        return 0
    with tandoor_client.get_client() as client:
        keywords = tandoor_client.fetch_pages(client, "/keyword/")
        groups = [k for k in keywords if k.get("numchild")]
        new = [k for k in ungrouped(keywords) if k["name"].strip().casefold() in wanted]
        sources, new = split_sources(new)
        moved = 0
        if sources:  # the import's source tag: with the other sources, no AI needed
            group, name = source_group(keywords)
            group_id = group["id"] if group else _find_or_create_group(client, name, None)
            for tag in sources:
                if client.put(f"/keyword/{tag['id']}/move/{group_id}/").status_code < 400:
                    moved += 1
        if not groups or not new:
            if moved:
                health.mark_changed()
            return moved
        members = {}
        for k in keywords:
            if _parent_id(k):
                members.setdefault(_parent_id(k), []).append(k["name"])
        payload = {"groups": [{"name": g["name"], "examples": members.get(g["id"], [])[:EXAMPLES_PER_GROUP]}
                              for g in groups],
                   "tags": [k["name"] for k in new]}
        job = ToolJob(id="after-import", tool="tags_groups")
        try:
            answer = tools_tags._complete_json(job, PLACE_PROMPT, payload, max_tokens=40 * len(new) + 100)
        except Exception as exc:  # noqa: BLE001 - the tags stay on the tile then
            log.info("Placing imported tags failed: %s", exc)
            return 0
        finally:
            usage_log.record("tags_groups", job.token_usage.input_tokens, job.token_usage.output_tokens)
        by_group = {g["name"].strip().casefold(): g for g in groups}
        by_tag = {k["name"].strip().casefold(): k for k in new}
        for tag_name, group_name in (answer.items() if isinstance(answer, dict) else []):
            tag, group = by_tag.get(str(tag_name).strip().casefold()), by_group.get(str(group_name or "").strip().casefold())
            if tag and group and group["id"] != tag["id"]:
                resp = client.put(f"/keyword/{tag['id']}/move/{group['id']}/")
                if resp.status_code < 400:
                    moved += 1
                    log.info("Tag %r put into the group %r after the import", tag["name"], group["name"])
    if moved:
        health.mark_changed()
    return moved
