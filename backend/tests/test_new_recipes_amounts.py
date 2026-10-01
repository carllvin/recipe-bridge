"""Amounts into the steps as part of the new-recipes run: suggested for the
new recipes (also after an import, but never applied without review), and
for a recipe that gets revised once the revision is applied."""
import pytest

from app import llm_provider, recipe_amounts, recipe_restructure, tool_jobs, tools_new_recipes, tools_recipes
from app.schemas import ToolSuggestion


@pytest.fixture
def run(tandoor, monkeypatch):
    tandoor.add("food", {"id": 1, "name": "Mehl"})
    tandoor.add("recipe", {"id": 7, "name": "Teig", "description": "", "keywords": [], "steps": [
        {"name": "", "instruction": "Das Mehl verrühren.",
         "ingredients": [{"id": 70, "food": {"id": 1}, "unit": None, "amount": 250, "note": ""}]}]})
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_new_recipes, "_new_recipe_ids", lambda client, job_id=None: [7])
    monkeypatch.setattr(tools_recipes, "already_in_target_language", lambda r, code: True)
    monkeypatch.setattr(llm_provider, "complete_tool_text", lambda *a, **kw: ("[]", None))
    planned = []

    def plan(job, recipe):
        planned.append(recipe["id"])
        return ToolSuggestion(id=f"a{len(planned)}", kind="amounts_in_steps", summary="amounts",
                              detail={"recipe_id": recipe["id"], "plan": {"steps": []}})
    monkeypatch.setattr(recipe_amounts, "plan_suggestion", plan)

    def start(trigger=None):
        job = tool_jobs.create_tool_job("new_recipes")
        if trigger:
            job.meta["trigger"] = trigger
        tool_jobs.save_tool_job(job)
        tools_new_recipes.run_scan(job.id)
        return tool_jobs.get_tool_job(job.id), planned
    return start


@pytest.mark.parametrize("trigger", [None, "import"])
def test_new_recipes_get_the_amounts_suggestion(run, trigger):
    job, planned = run(trigger)
    assert planned == [7]
    amounts = [s for s in job.suggestions if s.kind == "amounts_in_steps"]
    assert len(amounts) == 1 and amounts[0].status == "pending"  # after an import too: reviewed, not auto-applied


def test_a_revised_recipe_follows_once_the_revision_is_applied(run, monkeypatch, tandoor):
    monkeypatch.setattr(recipe_restructure, "needs_restructure", lambda r: r["id"] == 7 and not r.get("revised"))
    monkeypatch.setattr(recipe_restructure, "plan_suggestion", lambda job, r: ToolSuggestion(
        id="r1", kind="restructure_recipe", summary="revise", detail={"recipe_id": 7, "plan": {}}))

    def apply_plan(job, suggestion):
        tandoor.db["recipe"][7]["revised"] = True
        suggestion.status = "applied"
        return suggestion
    monkeypatch.setattr(recipe_restructure, "apply_plan", apply_plan)
    job, planned = run()
    assert planned == [] and [s.kind for s in job.suggestions] == ["restructure_recipe"]
    tools_new_recipes.apply_suggestion(job.id, "r1")
    job = tool_jobs.get_tool_job(job.id)
    assert planned == [7] and [s.kind for s in job.suggestions] == ["restructure_recipe", "amounts_in_steps"]
    assert not job.meta.get("marked")  # still something to review


def test_after_an_import_only_the_data_is_applied_right_away(monkeypatch):
    applied = []
    monkeypatch.setattr(tools_new_recipes, "_apply_fn", lambda job_id, sid, action: applied.append(sid))
    job = tool_jobs.create_tool_job("new_recipes")
    job.suggestions = [ToolSuggestion(id="e1", kind="enrich", summary="x"),
                       ToolSuggestion(id="a1", kind="amounts_in_steps", summary="y")]
    tools_new_recipes._apply_all(job)
    assert applied == ["e1"]
