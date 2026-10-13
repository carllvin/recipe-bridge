"""The collection is read once in full; then only recipes Tandoor reports changed."""
import httpx

from app import tandoor_helpers


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


def test_ingredient_changes_drop_the_copy():
    from app import health
    from app.schemas import ToolSuggestion
    recipes = [{"id": i, "name": f"R{i}", "updated_at": "x"} for i in range(1, 4)]
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, []))
    health.settle("ingredients_review", ToolSuggestion(id="s", kind="merge", summary="",
                                                       detail={"keep_id": 1, "remove_ids": [2]}))
    calls = []
    tandoor_helpers.fetch_all_recipes_full(tandoor_with(recipes, calls))
    assert len(calls) == 4  # everything again
