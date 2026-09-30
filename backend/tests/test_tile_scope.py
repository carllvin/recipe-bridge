"""Tools started from a tile read and send only what that tile is about."""
import json
import os

from app import health, llm_provider, mealie_client, mealie_maintenance, mealie_tools, tool_jobs, tools_conversions, tools_tags
from app.config import settings
from fake_mealie import FakeMealie


def write_overview(data):
    os.makedirs(settings.data_dir, exist_ok=True)
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": 1, "metrics": {}, **data}, f)


def recipe_reads(requests, prefix):
    return sorted(p for m, p in requests if m == "GET" and p.startswith(prefix) and p.rstrip("/") != prefix.rstrip("/"))


# ---------- 1. few tags (Tandoor) ----------

def test_few_tags_only_the_tile_recipes(tandoor, monkeypatch):
    for kid, name in enumerate(["a", "b", "c", "d", "e", "vegetarisch"], 1):
        tandoor.add("keyword", {"id": kid, "name": name, "numrecipe": 1})
    tandoor.add("recipe", {"id": 1, "name": "Wenig Tags", "steps": [], "keywords": [{"id": 1, "name": "a"}]})
    tandoor.add("recipe", {"id": 2, "name": "Viele Tags, Diät nie geprüft", "steps": [],
                           "keywords": [{"id": k, "name": n} for k, n in enumerate("abcde", 1)]})
    write_overview({"items": {"recipes_few_tags": [{"key": "1", "name": "Wenig Tags"}]},
                    "recipe_versions": {"1": None, "2": None}})
    sent = []

    def ai(job, prompt, payload, max_tokens):
        sent.extend(r["id"] for r in payload["recipes"])
        return []
    monkeypatch.setattr(tools_tags, "_complete_json", ai)
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    job = tool_jobs.create_tool_job("tags_suggest_more")
    tools_tags.run_suggest_more_scan(job.id)
    assert tool_jobs.get_tool_job(job.id).status == "ready"
    assert sent == [1]  # recipe 2 isn't in the tile - not read, not sent
    assert recipe_reads(tandoor.requests, "/recipe/") == ["/recipe/1/"]


# ---------- 3. conversions (Tandoor) ----------

def test_conversions_start_from_the_overview(tandoor):
    for fid in (5, 6):
        tandoor.add("food", {"id": fid, "name": f"Zutat {fid}"})
    tandoor.add("unit", {"id": 7, "name": "EL"})
    tandoor.add("recipe", {"id": 1, "name": "Alt", "updated_at": "v1",
                           "steps": [{"ingredients": [{"food": {"id": 5}, "unit": {"id": 7}}]}]})
    tandoor.add("recipe", {"id": 2, "name": "Geändert", "updated_at": "v2-neu",
                           "steps": [{"ingredients": [{"food": {"id": 6}, "unit": {"id": 7}}]}]})
    write_overview({"items": {"missing_conversions": []}, "recipe_versions": {"1": "v1", "2": "v2"},
                    "recipe_pairs": [[5, 7, 3]]})
    job = tool_jobs.create_tool_job("conversions")
    with tandoor.client() as client:
        pairs = tools_conversions._pairs_since_overview(client, job)
    assert pairs == {(5, 7): 3, (6, 7): 1}
    assert recipe_reads(tandoor.requests, "/recipe/") == ["/recipe/2/"]  # only the changed one


def test_conversions_without_overview_read_everything(tandoor):
    job = tool_jobs.create_tool_job("conversions")
    with tandoor.client() as client:
        assert tools_conversions._pairs_since_overview(client, job) is None


# ---------- 4. Mealie ----------

def mealie_setup(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    food = fake.add("food", "Kürbis")
    for n in range(5):
        fake.add_recipe(f"Rezept {n}", [(food, None)])
    return fake, food


def test_mealie_tools_read_only_listed_and_changed_recipes(monkeypatch, isolated):
    fake, food = mealie_setup(monkeypatch)
    health._compute()
    data = json.load(open(os.path.join(settings.data_dir, "health.json")))
    assert set(data["recipe_versions"]) == set(fake.recipes) and data["usage"]["food"] == {food["id"]: 5}
    # all five lack a season tag; one of them gets edited after the overview
    fake.recipes["rezept-3"]["description"] = "neu"
    fake._touch("rezept-3")
    data["items"]["recipes_without_season"] = data["items"]["recipes_without_season"][:2]
    write_overview(data)
    fake.requests.clear()
    with mealie_client.get_client() as client:
        got = mealie_maintenance.recipes_for(client, "recipes_without_season", lambda r: True)
    assert sorted(r["slug"] for r in got) == ["rezept-0", "rezept-1", "rezept-3"]
    assert recipe_reads(fake.requests, "/recipes/") == ["/recipes/rezept-0", "/recipes/rezept-1", "/recipes/rezept-3"]


def test_mealie_unused_and_duplicates_use_the_overview_counts(monkeypatch, isolated):
    fake, food = mealie_setup(monkeypatch)
    fake.add("food", "Unbenutzt")
    health._compute()
    fake.requests.clear()
    job = tool_jobs.create_tool_job("unused_foods")
    job.meta["target"] = "mealie"
    tool_jobs.save_tool_job(job)
    mealie_maintenance.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert [s.detail["name"] for s in job.suggestions] == ["Unbenutzt"]
    assert recipe_reads(fake.requests, "/recipes/") == []  # nothing changed since the overview


def test_mealie_without_overview_reads_all(monkeypatch, isolated):
    fake, _food = mealie_setup(monkeypatch)
    with mealie_client.get_client() as client:
        assert len(mealie_maintenance.recipes_for(client, "recipes_without_season", lambda r: True)) == 5
