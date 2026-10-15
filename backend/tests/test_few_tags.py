"""'Few tags': filled up to five, and a declined suggestion isn't asked for again."""
from app import main, tool_jobs, tools_tags
from app.schemas import ToolJob, ToolSuggestion
from test_diet_tags import recipe


def test_filled_up_to_five(monkeypatch):
    many = ["Hauptgericht", "italienisch", "herzhaft", "schnell", "Ofen", "einfach"]
    monkeypatch.setattr(tools_tags, "_complete_json", lambda job, prompt, payload, max_tokens, cache=False: [
        {"id": 1, "tags": many, "diet": []}, {"id": 2, "tags": many, "diet": []}])
    recipes = [recipe(1, "Lasagne", ["Nudeln"]),                       # no tag: 5 missing
               recipe(2, "Pizza", ["Mehl"], ["Abendessen", "Party", "Teig"])]  # 3 tags: 2 missing
    out = {s.detail["recipe_id"]: s.detail["tags"] for s in
           tools_tags.suggest_tags_suggestions(ToolJob(id="j", tool="tags_suggest_more"), recipes, [])}
    assert out[1] == many[:5] and out[2] == many[:2]


def test_a_declined_suggestion_is_not_sent_again():
    job = tool_jobs.create_tool_job("tags_suggest_more")
    job.suggestions = [ToolSuggestion(id="s1", kind="suggest_tags", summary="",
                                      detail={"recipe_id": 7, "tags": ["x"], "version": "2026-10-01"})]
    tool_jobs.save_tool_job(job)
    main._perform_suggestion_action(job.id, "s1", "skip")
    unchanged = {"id": 7, "updated_at": "2026-10-01"}
    changed = {"id": 7, "updated_at": "2026-10-15"}
    assert tools_tags.not_declined([unchanged, {"id": 8, "updated_at": "x"}]) == [{"id": 8, "updated_at": "x"}]
    assert tools_tags.not_declined([changed]) == [changed]  # asked again once the recipe changed


def test_year_round_recipes_leave_the_season_tile(monkeypatch):
    from app import ignored
    monkeypatch.setattr(tools_tags, "SEASON_BATCH_SIZE", 2)
    calls = []

    def ai(job, prompt, payload, max_tokens, cache=False):
        calls.append([r["id"] for r in payload])
        if 5 in calls[-1]:
            raise RuntimeError("AI down")  # this batch is asked again next time
        return [{"id": 1, "season": "Winter"}, {"id": 2, "season": None}]
    monkeypatch.setattr(tools_tags, "_complete_json", ai)
    recipes = [recipe(1, "Grünkohl", ["Grünkohl"]), recipe(2, "Spaghetti", ["Nudeln"]),
               recipe(5, "Spargel", ["Spargel"])]
    out = tools_tags.season_suggestions(ToolJob(id="j", tool="tags_season"), recipes)
    assert [s.detail["recipe_id"] for s in out] == [1]
    assert ignored.keys("recipes_without_season") == {"2"}
