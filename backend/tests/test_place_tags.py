"""After an import, new tags without a group go into a fitting existing group."""
from app import llm_provider, tools_tag_groups, tools_tags


def test_imported_tags_go_into_their_group(tandoor, monkeypatch):
    tandoor.add("keyword", {"id": 1, "name": "Quelle", "numchild": 1})
    tandoor.add("keyword", {"id": 2, "name": "nytcooking", "parent": 1})
    tandoor.add("keyword", {"id": 3, "name": "Diät", "numchild": 1})
    tandoor.add("keyword", {"id": 4, "name": "vegan", "parent": 3})
    tandoor.add("keyword", {"id": 5, "name": "einfachkochen"})
    tandoor.add("keyword", {"id": 6, "name": "glutenfrei"})
    tandoor.add("keyword", {"id": 7, "name": "schnell"})       # fits nothing - stays
    tandoor.add("keyword", {"id": 8, "name": "alt, ungrouped"})  # not from this import - not sent
    seen = {}

    def ai(job, prompt, payload, max_tokens, cache=False):
        seen.update(payload)
        job.token_usage.input_tokens += 100
        return {"einfachkochen": "Quelle", "glutenfrei": "Diät", "schnell": None}
    monkeypatch.setattr(tools_tags, "_complete_json", ai)
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    moved = tools_tag_groups.place_imported_tags(["einfachkochen", "glutenfrei", "schnell", "Herbst"])
    assert moved == 2
    assert sorted(seen["tags"]) == ["einfachkochen", "glutenfrei", "schnell"]
    assert {"name": "Quelle", "examples": ["nytcooking"]} in seen["groups"]
    assert tandoor.db["keyword"][5]["parent"] == 1 and tandoor.db["keyword"][6]["parent"] == 3
    assert not tandoor.db["keyword"][7].get("parent")


def test_nothing_without_groups(tandoor, monkeypatch):
    tandoor.add("keyword", {"id": 5, "name": "einfachkochen"})
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_tags, "_complete_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI")))
    assert tools_tag_groups.place_imported_tags(["einfachkochen"]) == 0


def test_source_tags_go_together_without_ai(tandoor, monkeypatch):
    from app import source_tag, tool_jobs
    tandoor.add("keyword", {"id": 1, "name": "Quelle", "numchild": 1})
    tandoor.add("keyword", {"id": 2, "name": "nytcooking", "parent": 1})
    tandoor.add("keyword", {"id": 5, "name": "einfachkochen"})
    tandoor.add("keyword", {"id": 6, "name": "emmikochteinfach"})
    tandoor.add("keyword", {"id": 7, "name": "vegan"})
    source_tag.remember("hosts", "cooking.nytimes.com", "nytcooking")
    # given before source tags were remembered: learnt from the recipes' addresses
    source_tag.learn_from([{"source_url": "https://www.einfachkochen.de/x", "keywords": [{"name": "einfachkochen"}]},
                           {"source_url": "https://emmikochteinfach.de/y", "keywords": [{"name": "emmikochteinfach"},
                                                                                         {"name": "vegan"}]}])
    sent = {}
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_tags, "_complete_json", lambda job, prompt, payload, max_tokens, cache=False:
                        sent.update(payload) or [])
    job = tool_jobs.create_tool_job("tags_groups")
    tools_tag_groups.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    (s,) = job.suggestions
    assert s.detail["group"] == "Quelle" and s.detail["group_id"] == 1
    assert {t["name"] for t in s.detail["tags"]} == {"einfachkochen", "emmikochteinfach"}
    assert [t["name"] for t in sent["tags"]] == ["vegan"]  # only the rest goes to the AI


def test_an_imports_source_tag_needs_no_groups_yet(tandoor, monkeypatch):
    from app import source_tag
    tandoor.add("keyword", {"id": 5, "name": "ottolenghi"})
    source_tag.remember_tag("ottolenghi")
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_tags, "_complete_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI")))
    assert tools_tag_groups.place_imported_tags(["ottolenghi"]) == 1
    group = next(k for k in tandoor.db["keyword"].values() if k["name"] == "Quelle")
    assert tandoor.db["keyword"][5]["parent"] == group["id"]
