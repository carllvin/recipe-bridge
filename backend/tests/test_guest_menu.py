"""Guest menu: a menu from your own recipes, guests' allergies left out,
one course changed at a time, into the meal plan for the guests, with its
own shopping list."""
import json

from fastapi.testclient import TestClient

from app import app_settings, cook_today, llm_provider, main
from test_plan_chat import ids, mealie  # noqa: F401 - fixture


class AI:
    def __init__(self, monkeypatch, pick):
        self.seen, self.pick = [], pick
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.ask)

    def ask(self, prompt, content, max_tokens=0, **kw):
        assert "menu for guests" in prompt
        payload = json.loads(content)
        self.seen.append(payload)
        return json.dumps({"courses": self.pick(payload), "note": "Ein leichtes Herbstmenü."}), None


def test_menu_reroll_plan_and_shopping(mealie, monkeypatch):  # noqa: F811
    feta = next(f for f in mealie.db["food"].values() if f["name"] == "Feta")
    mealie.add_recipe("Feta-Creme", [(feta, None)], totalTime="10 min")
    mealie.add_recipe("Apfelkuchen", [], totalTime="60 min")
    cook_today._rebuild()
    app_settings.update({"household": {"avoid": "Rindfleisch"}})
    names = ids(mealie)
    choice = {"starter": ["Feta-Creme", "Linsensuppe"], "main": ["Lachs mit Spinat"], "dessert": ["Apfelkuchen"]}

    def pick(p):
        keep = {k["course"] for k in p["keep"]}
        return [{"course": c, "recipe_id": names[choice[c][len(AI_.seen) - 1 if c == "starter" else 0]], "reason": "passt"}
                for c in p["courses"] if c not in keep]
    AI_ = AI(monkeypatch, pick)
    api = TestClient(main.app)
    job = api.post("/api/guest-menu", json={"occasion": "Geburtstag", "date": "2026-10-10", "persons": 8,
                                            "courses": ["starter", "main", "dessert"], "guests_never": "Nudeln"}).json()
    seen = AI_.seen[0]
    lines = [line.split("|")[1] for line in seen["recipes"]]
    assert "Rindergulasch" not in lines and "Nudeln mit Feta" not in lines   # household + guests
    assert seen["persons"] == 8 and seen["courses"] == ["starter", "main", "dessert"]
    assert [(m["course"], m["recipe"]["name"]) for m in job["meta"]["menu"]] == [
        ("starter", "Feta-Creme"), ("main", "Lachs mit Spinat"), ("dessert", "Apfelkuchen")]

    job = api.post(f"/api/guest-menu/{job['id']}/reroll", json={"course": "starter"}).json()
    assert AI_.seen[1]["keep"] == [{"course": "main", "recipe_id": names["Lachs mit Spinat"]},
                                   {"course": "dessert", "recipe_id": names["Apfelkuchen"]}]
    assert [m["recipe"]["name"] for m in job["meta"]["menu"]] == ["Linsensuppe", "Lachs mit Spinat", "Apfelkuchen"]

    assert api.post(f"/api/guest-menu/{job['id']}/plan",
                    json={"meal_type": {"id": "dinner", "name": "Abendessen"}, "add_to_shopping": False}).json() == {"planned": 3}
    assert [e["date"] for e in mealie.mealplans] == ["2026-10-10"] * 3

    shopping = api.get(f"/api/guest-menu/{job['id']}/shopping-list").json()
    assert shopping["persons"] == 8 and shopping["days"] == ["2026-10-10"]
    assert all(j.tool != "guest_menu" for j in main._review_jobs())  # not a Review matter
