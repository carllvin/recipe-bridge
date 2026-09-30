"""The weekly plan is reviewed on the Plan page - its days don't show under Review."""
from fastapi.testclient import TestClient

from app import main, tool_jobs
from app.schemas import ToolSuggestion


def suggestion(kind, status="pending"):
    return ToolSuggestion(id=kind + status, kind=kind, summary=kind, status=status)


def test_plan_days_stay_out_of_the_review(isolated):
    plan = tool_jobs.create_tool_job("meal_plan")
    plan.status = "ready"
    plan.suggestions = [suggestion("meal_plan"), suggestion("meal_plan", "error")]
    tool_jobs.save_tool_job(plan)
    tags = tool_jobs.create_tool_job("tags_season")
    tags.status = "ready"
    tags.suggestions = [suggestion("season")]
    tool_jobs.save_tool_job(tags)
    scanning_plan = tool_jobs.create_tool_job("meal_plan")  # still planning - not a running review job either
    tool_jobs.save_tool_job(scanning_plan)

    api = TestClient(main.app)
    inbox = api.get("/api/inbox").json()
    assert [i["kind"] for i in inbox["items"]] == ["season"] and inbox["running"] == []
    assert api.get("/api/inbox/count").json() == {"count": 1, "running": 0}
