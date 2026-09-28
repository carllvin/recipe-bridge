"""Sorting tags into groups (Tandoor's tag tree), undoable."""
from app import main, tool_jobs, tools_tag_groups, tools_tags


def tags(tandoor):
    for kid, name, n in [(1, "vegan", 12), (2, "vegetarisch", 30), (3, "italienisch", 8), (4, "Suppe", 5),
                         (5, "Omas Beste", 2)]:
        tandoor.add("keyword", {"id": kid, "name": name, "numrecipe": n, "numchild": 0, "parent": None})
    tandoor.add("keyword", {"id": 9, "name": "Küche", "numrecipe": 0, "numchild": 0, "parent": None})
    tandoor.add("keyword", {"id": 10, "name": "Anlass", "numchild": 1, "parent": None})
    tandoor.db["keyword"][5]["parent"] = 10


def scan(monkeypatch, answer):
    sent = {}

    def fake(job, prompt, payload, max_tokens):
        sent.update(payload)
        return answer
    monkeypatch.setattr(tools_tags, "_complete_json", fake)
    monkeypatch.setattr(tools_tag_groups.llm_provider, "is_configured", lambda: True)
    job = tool_jobs.create_tool_job("tags_groups")
    tools_tag_groups.run_scan(job.id)
    return tool_jobs.get_tool_job(job.id), sent


def test_suggestions_are_checked(tandoor, monkeypatch):
    tags(tandoor)
    job, sent = scan(monkeypatch, [
        {"group": "Ernährung", "tags": ["vegan", "Vegetarisch", "gibt es nicht"]},
        {"group": "Suppe", "tags": ["italienisch"]},            # a tag can't become a group
        {"group": "Küche", "tags": ["italienisch", "vegan"]},  # vegan already placed; Küche is a used tag name
        {"group": "Anlass", "tags": ["Omas Beste"]},           # already there
    ])
    assert sent["groups"] == ["Anlass"] and {t["name"] for t in sent["tags"]} >= {"vegan", "Omas Beste"}
    assert [(s.detail["group"], [t["name"] for t in s.detail["tags"]]) for s in job.suggestions] == [
        ("Ernährung", ["vegan", "vegetarisch"])]


def test_apply_creates_group_moves_tags_and_undo(tandoor, monkeypatch):
    tags(tandoor)
    job, _ = scan(monkeypatch, [{"group": "Ernährung", "tags": ["vegan", "vegetarisch"]},
                                {"group": "Anlass", "tags": ["Suppe"]}])
    new, existing = job.suggestions
    s = main._perform_suggestion_action(job.id, new.id, "apply")
    assert s.status == "applied" and s.undoable
    group = next(k for k in tandoor.db["keyword"].values() if k["name"] == "Ernährung")
    assert tandoor.db["keyword"][1]["parent"] == group["id"] == tandoor.db["keyword"][2]["parent"]
    assert main._perform_suggestion_action(job.id, existing.id, "apply").status == "applied"
    assert tandoor.db["keyword"][4]["parent"] == 10
    main._perform_suggestion_action(job.id, new.id, "undo")
    assert tandoor.db["keyword"][1]["parent"] is None and "Ernährung" not in tandoor.names("keyword")
    main._perform_suggestion_action(job.id, existing.id, "undo")
    assert tandoor.db["keyword"][4]["parent"] is None


def test_groups_are_not_offered_as_recipe_tags():
    all_tags = [{"id": 1, "name": "vegan"}, {"id": 2, "name": "Ernährung", "numchild": 3}]
    assert tools_tags.tag_vocabulary(all_tags, []) == ["vegan"]


def test_ungrouped_counts_top_level_tags_only(tandoor):
    tags(tandoor)
    names = [k["name"] for k in tools_tag_groups.ungrouped(tandoor.db["keyword"].values())]
    assert names == ["italienisch", "Küche", "Suppe", "vegan", "vegetarisch"]
