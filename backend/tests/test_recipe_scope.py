"""Recipe tools read only what the health tile listed plus what changed."""
import json
import os

from app import recipe_scope
from app.config import settings
from app.tools_recipe_details import lacks_servings


def setup(tandoor):
    for rid, servings in [(10, 1), (11, 4), (12, 4), (13, 1)]:
        tandoor.add("recipe", {"id": rid, "name": f"R{rid}", "servings": servings, "updated_at": "t1",
                               "keywords": [], "steps": []})


def detail_reads(tandoor):
    return sorted(int(p.split("/")[2]) for m, p in tandoor.requests if m == "GET" and p.count("/") == 3)


def test_without_overview_everything_is_read(tandoor):
    setup(tandoor)
    with tandoor.client() as client:
        found = recipe_scope.recipes_for(client, "recipes_without_servings", lacks_servings)
    assert [r["id"] for r in found] == [10, 13]
    assert detail_reads(tandoor) == [10, 11, 12, 13]


def test_tile_list_plus_new_and_changed(tandoor):
    setup(tandoor)
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": 1, "items": {"recipes_without_servings": [{"key": "10"}, {"key": "13"}]},
                   "recipe_versions": {"10": "t1", "11": "t1", "12": "t1", "13": "t1"}}, f)
    tandoor.db["recipe"][12]["updated_at"] = "t2"
    tandoor.db["recipe"][12]["servings"] = 0  # edited since: now lacks servings
    tandoor.add("recipe", {"id": 14, "name": "Neu", "servings": 1, "updated_at": "t2", "keywords": [], "steps": []})
    with tandoor.client() as client:
        found = recipe_scope.recipes_for(client, "recipes_without_servings", lacks_servings, skip={"13"})
    assert [r["id"] for r in found] == [10, 12, 14]
    assert detail_reads(tandoor) == [10, 12, 14]  # 11 unchanged and fine, 13 ignored
