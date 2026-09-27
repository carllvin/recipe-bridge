"""Shared helpers reused across tandoor_client-based maintenance tools - both
the CLI scripts (scripts/_shared.py re-exports this module) and the web app's
tools use the exact same, single-source logic - avoiding the class of bug
where two copies of the same merge/verification logic silently drift apart."""

import logging

import httpx

log = logging.getLogger("tandoor-helper")


def fetch_recipe_overview(client: httpx.Client, max_recipes: int | None = None) -> list[dict]:
    """Every recipe's compact list entry (id, name, updated_at ...) - a few
    paged requests instead of one per recipe."""
    items = []
    url = "/recipe/"
    params = {"page_size": 200}
    for _ in range(50):
        resp = client.get(url, params=params if url == "/recipe/" else None)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        items.extend(item for item in results if item.get("id") is not None)
        if max_recipes and len(items) >= max_recipes:
            items = items[:max_recipes]
            break
        next_url = data.get("next") if isinstance(data, dict) else None
        if not next_url:
            break
        url = next_url
        params = None
    return items


def fetch_recipes_full(client: httpx.Client, ids) -> list[dict]:
    full = []
    for rid in ids:
        resp = client.get(f"/recipe/{rid}/")
        if resp.status_code == 200:
            full.append(resp.json())
    return full


def fetch_all_recipes_full(client: httpx.Client, max_recipes: int | None = None) -> list[dict]:
    """Fetches every recipe's full detail (steps + ingredients + keywords).
    Used whenever a script needs to inspect what a recipe actually contains,
    not just its compact list-view fields."""
    return fetch_recipes_full(client, [item["id"] for item in fetch_recipe_overview(client, max_recipes)])


def compute_usage_maps(recipes_full: list[dict]) -> dict[str, dict[int, int]]:
    """Given full recipe details, counts how many recipes reference each
    food/unit/keyword id. Returns {"food": {id: count}, "unit": {...}, "keyword": {...}}."""
    usage: dict[str, dict[int, int]] = {"food": {}, "unit": {}, "keyword": {}}
    for recipe in recipes_full:
        seen_this_recipe: dict[str, set[int]] = {"food": set(), "unit": set(), "keyword": set()}
        for step in recipe.get("steps", []):
            for ing in step.get("ingredients", []):
                food = ing.get("food")
                if food and food.get("id") is not None:
                    seen_this_recipe["food"].add(food["id"])
                unit = ing.get("unit")
                if unit and unit.get("id") is not None:
                    seen_this_recipe["unit"].add(unit["id"])
        for kw in recipe.get("keywords", []):
            if kw.get("id") is not None:
                seen_this_recipe["keyword"].add(kw["id"])

        for entity, ids in seen_this_recipe.items():
            for entity_id in ids:
                usage[entity][entity_id] = usage[entity].get(entity_id, 0) + 1

    return usage


def print_header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


# Rough per-tool token-cost profiles, calibrated against real runs (the
# "chunked_review" numbers come from an actual 1216-ingredient review: 16
# chunks of 80, ~1900 input / ~1400 output tokens per chunk). These are
# ESTIMATES shown BEFORE any AI call is made, so you know roughly what a run
# will cost before committing to it - not just a running total after the
# fact. Actual usage varies with how many items in each chunk need a change
# and how verbose the model's answer is, so treat this as an order of
# magnitude, not an exact quote.
COST_PROFILES = {
    "chunked_review": {"chunk_size": 80, "input_per_chunk": 1900, "output_per_chunk": 1400},
    "batched_season": {"chunk_size": 25, "input_per_chunk": 2500, "output_per_chunk": 450},  # season check, 25 recipes per call
    "batched_suggest_tags": {"chunk_size": 20, "input_per_chunk": 3500, "output_per_chunk": 700},  # tag vocabulary sent once per 20 recipes
    "chunked_enrich": {"chunk_size": 40, "input_per_chunk": 1500, "output_per_chunk": 2600},  # plural + nutrition + category, ~65 output tokens per food
    "per_recipe_tiny": {"input_per_item": 150, "output_per_item": 15},      # season check (CLI)
    "per_recipe_small": {"input_per_item": 300, "output_per_item": 60},     # metadata, nutrition (CLI)
    "per_recipe_translate": {"input_per_item": 500, "output_per_item": 500},  # full recipe re-translation
}


def estimate_cost(item_count: int, profile: str) -> tuple[int, int, int]:
    """Returns (estimated_ai_calls, estimated_input_tokens, estimated_output_tokens)
    for running a tool with the given profile over item_count items."""
    cfg = COST_PROFILES[profile]
    if "chunk_size" in cfg:
        calls = max(1, -(-item_count // cfg["chunk_size"])) if item_count else 0  # ceil division
        return calls, calls * cfg["input_per_chunk"], calls * cfg["output_per_chunk"]
    calls = item_count
    return calls, calls * cfg["input_per_item"], calls * cfg["output_per_item"]


def format_cost_estimate(item_count: int, profile: str) -> str:
    calls, input_tok, output_tok = estimate_cost(item_count, profile)
    return (
        f"Estimated before starting: {item_count} item(s) -> ~{calls} AI call(s), "
        f"~{input_tok:,} input / ~{output_tok:,} output tokens. This is a rough "
        f"order-of-magnitude guess (calibrated against a real run) - actual usage "
        f"depends on how many items need a change."
    )


class TokenTracker:
    """Accumulates AI token usage across a script run so cost is visible
    during the run (via progress_line, printed periodically) and at the end
    (via summary_line) - not just guessed at from the script's docstring."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, usage) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.calls += 1

    def progress_line(self) -> str:
        return f"  [tokens so far: {self.input_tokens} in / {self.output_tokens} out, {self.calls} AI call(s)]"

    def summary_line(self) -> str:
        if self.calls == 0:
            return "No AI calls were made."
        return f"AI usage: {self.input_tokens} input token(s), {self.output_tokens} output token(s) across {self.calls} call(s)."


def chunked(items: list, size: int):
    """Yields successive `size`-sized slices of `items` - used to keep AI review
    prompts (which include a whole list of entities) to a reasonable length."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def minimal_ref(ref: dict | None) -> dict | None:
    """Rebuilds a food/unit reference as a minimal {id, name} dict, dropping
    any other (possibly read-only) fields Tandoor included when it returned
    the recipe - sending those back verbatim can trigger a 400."""
    if ref is None:
        return None
    return {"id": ref["id"], "name": ref.get("name", "")}


def find_recipes_by_filter(client: httpx.Client, filter_param: str, filter_value: int) -> list[dict]:
    """Paginated GET /recipe/?<filter_param>=<filter_value> - the confirmed-
    working way to find recipes using a given food (filter_param='foods') or
    keyword (filter_param='keywords'). There's no equivalent confirmed filter
    for units - see find_recipes_using_unit below for that case instead."""
    recipes = []
    url = "/recipe/"
    params = {filter_param: filter_value, "page_size": 200}
    for _ in range(50):
        resp = client.get(url, params=params if url == "/recipe/" else None)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        recipes.extend(results)
        next_url = data.get("next") if isinstance(data, dict) else None
        if not next_url:
            break
        url = next_url
        params = None
    return recipes


def find_recipes_using_unit(recipes_full: list[dict], unit_id: int) -> list[dict]:
    """Scans an already-fetched full recipe list for ones using a given unit -
    there's no confirmed /recipe/?units=<id> filter (unlike foods/keywords), so
    this is the fallback for that one entity type. Note: recipes_full is a
    snapshot - see entity_still_referenced() for the live re-check to use right
    before deleting something, which must NOT rely on this kind of cache."""
    matches = []
    for recipe in recipes_full:
        for step in recipe.get("steps", []):
            for ing in step.get("ingredients", []):
                unit = ing.get("unit")
                if unit and unit.get("id") == unit_id:
                    matches.append(recipe)
                    break
            else:
                continue
            break
    return matches


def entity_still_referenced(client: httpx.Client, entity: str, entity_id: int, recipe_ids: list[int]) -> list[int]:
    """Re-checks, LIVE (never from a cached/pre-fetched recipe list), whether
    any of the given recipe ids still reference entity_id as a food, unit, or
    keyword. Always use this - not a cached snapshot - as the final check
    right before deleting a merged-away duplicate: checking a stale snapshot
    would report "still used" even immediately after a fully successful
    repoint, since the snapshot was never updated, and would block every
    deletion for no reason. (This is exactly the bug this function was
    introduced to fix - it existed independently in two different scripts
    before being consolidated here.)
    entity is "food", "unit", or "keyword"."""
    still = []
    for rid in recipe_ids:
        resp = client.get(f"/recipe/{rid}/")
        if resp.status_code != 200:
            continue
        fresh = resp.json()
        if entity == "keyword":
            if any(kw.get("id") == entity_id for kw in fresh.get("keywords", [])):
                still.append(rid)
            continue
        for step in fresh.get("steps", []):
            found = False
            for ing in step.get("ingredients", []):
                ref = ing.get(entity)
                if ref and ref.get("id") == entity_id:
                    still.append(rid)
                    found = True
                    break
            if found:
                break
    return still


REQUIRED_ACTION_FIELDS = {
    "rename": {"id", "new_name"},
    "merge": {"keep_id", "keep_name", "remove_ids"},
    "set_plural": {"id", "plural_name"},
}


def validate_actions(actions: list, source: str = "") -> list[dict]:
    """AI-generated JSON occasionally has one malformed entry - e.g. a
    "rename" action missing its "new_name" field. Left in, that crashes deep
    inside resolve_name_collisions or a suggestion-summary function with a
    bare KeyError, taking the whole scan down with it. This drops just the
    bad entry (logging what was dropped) so one malformed suggestion never
    loses every other suggestion the scan already found."""
    valid = []
    for action in actions:
        if not isinstance(action, dict):
            log.warning("Dropping non-dict action from %s: %r", source, action)
            continue
        action_type = action.get("type")
        required = REQUIRED_ACTION_FIELDS.get(action_type)
        if required is None:
            log.warning("Dropping action with unknown type from %s: %r", source, action)
            continue
        if not required.issubset(action.keys()):
            log.warning("Dropping action missing field(s) %s from %s: %r", required - action.keys(), source, action)
            continue
        valid.append(action)
    return valid


def resolve_name_collisions(actions: list[dict], items: list[dict]) -> list[dict]:
    """An AI review call only sees one chunk of items at a time (to keep each
    prompt a reasonable size), so it can suggest renaming/merging into a name
    that already exists as a SEPARATE entry elsewhere in the full list -
    invisible to it in that chunk. Applying such a rename verbatim crashes
    Tandoor with a database uniqueness error - the same failure mode
    tandoor_client.py's _get_or_create was built to avoid for ordinary
    imports; chunked AI review re-introduces the same risk on its own, larger
    scale. This folds any such collision into the action as an extra merge
    target instead of ever sending a rename that would collide.

    The collision index covers both an item's "name" AND its "plural_name"
    (when present, e.g. for units) - Tandoor's uniqueness constraint appears
    to span both, so a proposed plural that matches another unit's regular
    name (not just another unit's plural) can crash the same way.

    `actions` should already be pre-filtered with validate_actions() - this
    function assumes every action has the fields its type requires. `items`
    is the FULL list of existing {"id", "name", ...} entities (not just the
    chunk that produced `actions`). The result is run through
    consolidate_actions(), so no entry appears in more than one action."""
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    for item in items:
        norm = item["name"].strip().lower()
        name_to_id.setdefault(norm, item["id"])
        id_to_name[item["id"]] = item["name"]
        plural = item.get("plural_name")
        if plural:
            name_to_id.setdefault(plural.strip().lower(), item["id"])

    resolved = []
    for action in actions:
        if action["type"] == "rename":
            norm = action["new_name"].strip().lower()
            existing_id = name_to_id.get(norm)
            if existing_id and existing_id != action["id"]:
                resolved.append({
                    "type": "merge", "keep_id": existing_id,
                    "keep_name": action["new_name"], "remove_ids": [action["id"]],
                })
                continue
            resolved.append(action)

        elif action["type"] == "merge":
            norm = action["keep_name"].strip().lower()
            existing_id = name_to_id.get(norm)
            keep_id = action["keep_id"]
            remove_ids = list(action["remove_ids"])
            if existing_id and existing_id != keep_id and existing_id not in remove_ids:
                # A third, untouched entry already legitimately owns this
                # exact name - fold it into the merge group as the one that
                # gets kept, so the rename Tandoor sees is a no-op instead of
                # a collision.
                remove_ids.append(keep_id)
                keep_id = existing_id
            resolved.append({"type": "merge", "keep_id": keep_id, "keep_name": action["keep_name"], "remove_ids": remove_ids})

        elif action["type"] == "set_plural":
            # A plural like "Knolle" -> "Knollen" can collide the exact same
            # way if some OTHER entry is already named "Knollen" outright -
            # that's really the same underlying duplicate the review step
            # should have merged in the first place, just only visible once
            # both chunks are compared. Turn it into that merge instead of a
            # plural-setting PATCH that would crash on the collision.
            norm = action["plural_name"].strip().lower()
            existing_id = name_to_id.get(norm)
            if existing_id and existing_id != action["id"]:
                resolved.append({
                    "type": "merge", "keep_id": existing_id,
                    "keep_name": id_to_name[existing_id], "remove_ids": [action["id"]],
                })
                continue
            resolved.append(action)

        else:
            resolved.append(action)

    return consolidate_actions(resolved)


def _action_ids(action: dict) -> set[int]:
    if action["type"] == "merge":
        return {action["keep_id"], *action["remove_ids"]}
    return {action["id"]}


def _action_target_name(action: dict) -> str | None:
    if action["type"] == "merge":
        return action["keep_name"].strip().lower()
    if action["type"] == "rename":
        return action["new_name"].strip().lower()
    return None


def consolidate_actions(actions: list[dict]) -> list[dict]:
    """Folds rename/merge actions that touch the same entry, or that end up
    with the same target name, into ONE merge. Without this, the same entry
    shows up in two suggestions (e.g. "merge 'cakes' into 'Kuchen'" and
    "merge 'cakes', 'cake' into 'Kuchen'" - one from the AI directly, the
    other created by resolve_name_collisions, or from two different chunks);
    applying the first deletes 'cakes', and the second then fails with a 404
    trying to delete it again. Grouping also catches two chunks that each
    renamed a different entry to the same brand-new name, which would
    otherwise crash on Tandoor's uniqueness constraint.

    set_plural actions are kept as-is unless their entry gets merged away
    (then there's nothing left to set a plural on)."""
    groupable = [a for a in actions if a["type"] in ("rename", "merge")]
    others = [a for a in actions if a["type"] not in ("rename", "merge")]

    parent = list(range(len(groupable)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner_by_key: dict = {}
    for idx, action in enumerate(groupable):
        keys = [("id", i) for i in _action_ids(action)]
        target = _action_target_name(action)
        if target:
            keys.append(("name", target))
        for key in keys:
            if key in owner_by_key:
                parent[find(idx)] = find(owner_by_key[key])
            else:
                owner_by_key[key] = idx

    groups: dict[int, list[dict]] = {}
    for idx, action in enumerate(groupable):
        groups.setdefault(find(idx), []).append(action)

    consolidated = []
    for root in sorted(groups):
        group = groups[root]
        if len(group) == 1:
            consolidated.append(group[0])
            continue
        all_ids: list[int] = []
        for action in group:
            for i in sorted(_action_ids(action)):
                if i not in all_ids:
                    all_ids.append(i)
        merges = [a for a in group if a["type"] == "merge"]
        if not merges and len(all_ids) == 1:
            consolidated.append(group[0])  # the same rename proposed twice
            continue
        if merges:
            # The keep_id most merges agree on wins - resolve_name_collisions
            # sets it to the entry that already owns the target name, so
            # that's the one that must survive.
            keep_counts: dict[int, int] = {}
            for m in merges:
                keep_counts[m["keep_id"]] = keep_counts.get(m["keep_id"], 0) + 1
            keep_id = max(keep_counts, key=lambda k: (keep_counts[k], -all_ids.index(k)))
            keep_name = next(m["keep_name"] for m in merges if m["keep_id"] == keep_id)
        else:
            keep_id = group[0]["id"]
            keep_name = group[0]["new_name"]
        consolidated.append({
            "type": "merge", "keep_id": keep_id, "keep_name": keep_name,
            "remove_ids": [i for i in all_ids if i != keep_id],
        })

    removed = {rid for a in consolidated if a["type"] == "merge" for rid in a["remove_ids"]}
    consolidated.extend(a for a in others if a.get("id") not in removed)
    return consolidated


def entity_exists(client: httpx.Client, entity: str, entity_id: int) -> bool:
    """GET /<entity>/<id>/ - False on 404. Used before merging so an entry
    that an earlier suggestion in the same run already merged away is simply
    skipped instead of failing the whole merge."""
    resp = client.get(f"/{entity}/{entity_id}/")
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def delete_entity(client: httpx.Client, entity: str, entity_id: int) -> None:
    """DELETE /<entity>/<id>/, treating 404 (already gone) as success."""
    resp = client.delete(f"/{entity}/{entity_id}/")
    if resp.status_code not in (200, 202, 204, 404):
        raise RuntimeError(f"Could not delete {entity} #{entity_id}: {resp.status_code} {resp.text[:200]}")
