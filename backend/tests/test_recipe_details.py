"""Tiles 'recipes without servings' / 'without a photo' and their tools."""
import httpx

from app import ignored, image_gen, main, tool_jobs, tools_recipe_details, tools_tags


def kitchen(tandoor):
    tandoor.add("food", {"id": 1, "name": "Kürbis"})
    tandoor.add("unit", {"id": 2, "name": "g"})
    for rid, name, servings, image in [(10, "Kürbissuppe", 1, None), (11, "Brot", 0, "http://img/brot.jpg"),
                                       (12, "Eintopf", 4, None), (13, "Espresso", None, None)]:
        tandoor.add("recipe", {"id": rid, "name": name, "servings": servings, "image": image, "keywords": [],
                               "steps": [{"instruction": "", "ingredients": [{"food": {"id": 1}, "unit": {"id": 2}, "amount": 800}]}]})


def test_what_counts():
    assert tools_recipe_details.lacks_servings({"servings": 1}) and tools_recipe_details.lacks_servings({})
    assert not tools_recipe_details.lacks_servings({"servings": 4})
    assert tools_recipe_details.lacks_image({"image": None}) and not tools_recipe_details.lacks_image({"image": "x"})


def test_servings_are_estimated_applied_and_undoable(tandoor, monkeypatch):
    kitchen(tandoor)
    ignored.add("recipes_without_servings", [{"key": "13", "name": "Espresso"}])
    sent = []

    def fake(job, prompt, payload, max_tokens):
        sent.extend(payload)
        return [{"id": 10, "servings": 4}, {"id": 11, "servings": 1}]
    monkeypatch.setattr(tools_tags, "_complete_json", fake)
    monkeypatch.setattr(tools_recipe_details.llm_provider, "is_configured", lambda: True)

    job = tool_jobs.create_tool_job("recipes_servings")
    tools_recipe_details.run_servings_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert [r["id"] for r in sent] == [10, 11]  # 12 has servings, 13 is ignored
    assert sent[0]["ingredients"] == ["800 g Kürbis"]
    assert [s.detail["servings"] for s in job.suggestions] == [4]  # "1" is not a suggestion

    s = main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply")
    assert s.status == "applied" and s.undoable and tandoor.db["recipe"][10]["servings"] == 4
    main._perform_suggestion_action(job.id, job.suggestions[0].id, "undo")
    assert tandoor.db["recipe"][10]["servings"] == 1


def test_photos_are_generated_only_when_applied(tandoor, monkeypatch):
    kitchen(tandoor)
    generated, uploaded = [], []
    monkeypatch.setattr(image_gen, "is_configured", lambda: True)
    monkeypatch.setattr(image_gen, "generate_image", lambda prompt: generated.append(prompt) or b"\x89PNG fake")
    monkeypatch.setattr(tools_recipe_details.tandoor_client, "upload_image",
                        lambda client, rid, path: uploaded.append((rid, open(path, "rb").read())))

    job = tool_jobs.create_tool_job("recipes_images")
    tools_recipe_details.run_images_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert sorted(s.detail["recipe_id"] for s in job.suggestions) == [10, 12, 13] and generated == []

    first = next(s for s in job.suggestions if s.detail["recipe_id"] == 10)
    assert main._perform_suggestion_action(job.id, first.id, "apply").status == "applied"
    assert len(generated) == 1 and "Kürbissuppe" in generated[0] and uploaded == [(10, b"\x89PNG fake")]


def test_photo_tool_needs_image_generation(monkeypatch):
    monkeypatch.setattr(image_gen, "is_configured", lambda: False)
    job = tool_jobs.create_tool_job("recipes_images")
    tools_recipe_details.run_images_scan(job.id)
    assert tool_jobs.get_tool_job(job.id).status == "error"
