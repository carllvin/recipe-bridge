"""The collection is read once in full; then only recipes Tandoor reports changed."""
import httpx

from app import tandoor_helpers
from test_plan_chat import mealie  # noqa: F401 - fixture


def tandoor_with(recipes, calls):
    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/recipe/"):
            return httpx.Response(200, json={"results": [{"id": r["id"], "updated_at": r["updated_at"]} for r in recipes],
                                             "next": None})
        rid = int(path.rstrip("/").split("/")[-1])
        found = next((r for r in recipes if r["id"] == rid), None)
        return httpx.Response(200, json=found) if found else httpx.Response(404)
    return httpx.Client(base_url="http://tandoor/api", transport=httpx.MockTransport(handler))


def test_second_read_fetches_only_changed():
    recipes = [{"id": i, "name": f"R{i}", "updated_at": "2026-10-01"} for i in range(1, 9)]
    calls = []
    first = tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert [r["id"] for r in first] == list(range(1, 9)) and len(calls) == 9
    recipes[2] = {**recipes[2], "name": "R3 neu", "updated_at": "2026-10-13"}
    del recipes[5]  # deleted in Tandoor
    recipes.append({"id": 20, "name": "Neu", "updated_at": "2026-10-13"})
    calls.clear()
    second = tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert sorted(calls[1:]) == ["/api/recipe/20/", "/api/recipe/3/"]
    assert [r["name"] for r in second] == ["R1", "R2", "R3 neu", "R4", "R5", "R7", "R8", "Neu"]


def test_ingredient_change_rereads_only_the_recipes_using_it():
    from app import health
    from app.schemas import ToolSuggestion

    def uses(food_id):
        return [{"ingredients": [{"food": {"id": food_id, "name": f"F{food_id}"}}]}]
    recipes = [{"id": i, "name": f"R{i}", "updated_at": "x", "steps": uses(i)} for i in range(1, 6)]
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, []))
    health.settle("ingredients_review", ToolSuggestion(id="s", kind="merge", summary="",
                                                       detail={"keep_id": 1, "remove_ids": [2]}))
    calls = []
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert sorted(calls[1:]) == ["/api/recipe/1/", "/api/recipe/2/"]  # not R3-R5
    health.settle("tags_groups", ToolSuggestion(id="t", kind="group_tags", summary="",
                                                detail={"tags": [{"id": 9}]}))  # no recipe has the tag
    calls.clear()
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert calls == ["/api/recipe/"]


def test_unknown_change_reads_everything_again():
    from app import health
    from app.schemas import ToolSuggestion
    recipes = [{"id": i, "name": f"R{i}", "updated_at": "x"} for i in range(1, 4)]
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, []))
    health.settle("some_tool", ToolSuggestion(id="s", kind="rename", summary="", detail={"id": 1}))
    calls = []
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert len(calls) == 4


def test_mealie_reads_only_changed(mealie):  # noqa: F811
    from app import mealie_client, mealie_maintenance
    with mealie_client.get_client() as client:
        first = mealie_maintenance.fetch_recipes_full(client)
    mealie.requests.clear()
    slug = next(iter(mealie.recipes))
    mealie.recipes[slug]["updatedAt"] = "2026-10-14T12:00:00"
    with mealie_client.get_client() as client:
        second = mealie_maintenance.fetch_recipes_full(client)
    single = [path for method, path in mealie.requests if path.startswith("/recipes/")]
    assert len(second) == len(first) and single == [f"/recipes/{slug}"]
