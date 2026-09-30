"""Fix on the "without nutrition" / "without category" tiles only sends the
ingredients those tiles list to the AI - not every ingredient."""
import json

from fastapi.testclient import TestClient

from app import health, ignored, llm_provider, main, tool_jobs, tools_ingredients


def overview(monkeypatch, nutrition, category):
    monkeypatch.setattr(health, "_read", lambda: {"computed_at": 1, "items": {
        "foods_without_nutrition": [{"key": str(i), "name": f"f{i}"} for i in nutrition],
        "foods_without_category": [{"key": str(i), "name": f"f{i}"} for i in category]}})


def run(tandoor, monkeypatch, meta):
    for i in range(1, 7):  # six ingredients, none with plural, nutrition or category
        tandoor.add("food", {"id": i, "name": f"Zutat {i}", "plural_name": "", "properties": [], "supermarket_category": None})
    seen = []

    def ai(prompt, content, max_tokens=0):
        seen.extend(i["id"] for i in json.loads(content)["ingredients"])
        return "[]", None
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "complete_tool_text", ai)
    job = tool_jobs.create_tool_job("ingredients_enrich")
    job.meta.update(meta)
    tool_jobs.save_tool_job(job)
    tools_ingredients.run_enrich_scan(job.id)
    return sorted(seen), tool_jobs.get_tool_job(job.id)


def test_from_a_tile_only_the_listed_ingredients(tandoor, monkeypatch):
    overview(monkeypatch, nutrition=[2], category=[4])
    ignored.add("foods_without_category", [{"key": "4", "name": "f4"}])
    seen, job = run(tandoor, monkeypatch, {"focus": "tiles"})
    assert seen == [2] and job.progress_total == 1


def test_without_focus_everything_as_before(tandoor, monkeypatch):
    overview(monkeypatch, nutrition=[2], category=[4])
    seen, _job = run(tandoor, monkeypatch, {})
    assert seen == [1, 2, 3, 4, 5, 6]


def test_without_an_overview_everything(tandoor, monkeypatch):
    monkeypatch.setattr(health, "_read", lambda: {})
    seen, _job = run(tandoor, monkeypatch, {"focus": "tiles"})
    assert seen == [1, 2, 3, 4, 5, 6]


def test_the_tile_button_sends_the_focus(tandoor, monkeypatch):
    started = []
    monkeypatch.setattr(main, "_TOOL_SCANS", {**main._TOOL_SCANS, "ingredients_enrich": lambda job_id: started.append(job_id)})
    job_id = TestClient(main.app).post("/api/tools/ingredients/enrich", json={"focus": "tiles"}).json()["job_id"]
    assert tool_jobs.get_tool_job(job_id).meta["focus"] == "tiles"
