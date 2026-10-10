# Development

[Architecture](#architecture) · [Tests](#tests)

← [Back to the README](../README.md)

## Architecture

A FastAPI backend with a vanilla-JS frontend, in one container:

```
backend/
  app/
    main.py                 API endpoints, background loops; serves the frontend
    config.py               Settings from .env, language mapping
    auth.py                 Optional password (APP_PASSWORD)
    llm_provider.py         Anthropic / OpenAI / Gemini behind one interface, token usage
    target.py               Tandoor or Mealie (RECIPE_MANAGER) - hands out the right client
    tandoor_client.py       Tandoor API client (import, get-or-create, records writes for undo)
    tandoor_helpers.py      Shared Tandoor helpers (paging, merges, name collisions)
    mealie_client.py        Mealie API client (import, lookups)

    # Import
    pdf_processor.py, epub_processor.py, image_processor.py, url_processor.py (web pages, link lists)
    video_processor.py      YouTube / Instagram / TikTok: description, caption, subtitles
    migration.py            Moving recipes from the other recipe manager (no AI)
    docx_processor.py, text_processor.py   Word documents; pasted text, Markdown, .txt (links or text)
    site_scan.py            Website scan: sitemap / link crawl, schema.org recipe detection (no AI)
    ocr.py, image_preprocessing.py   Tesseract OCR, deskew/crop, two-page spreads
    ai_extractor.py         Recipe extraction: prompt, chunking, dedup, language detection
    import_matching.py      Match ingredients, units and tags before the review
    image_gen.py            Optional AI images for recipes without a photo
    jobs.py, watcher.py     Import jobs; the watched folder

    # Review & maintenance
    tool_jobs.py            Tool runs and their suggestions (persisted)
    apply_queue.py          Background queue that applies/skips/undoes suggestions
    undo.py                 Undo journals: replays recorded writes backwards (Tandoor)
    tools_*.py, recipe_restructure.py, nutrition_properties.py   The individual tools
    recipe_amounts.py       Amounts into the steps (Tandoor templates / written out), proofreading
    recipe_doctor.py        Contradictions inside a recipe (found without AI, fixed with it)
    photo_quality.py        Weak photos, measured from the pixels
    prep_notes.py           "gemahlene Mandeln" -> "Mandeln" + note "gemahlen"
    db_chat.py              Changes by chat: fixed action catalog, recipes found by the app
    tools_new_recipes.py    "Process new recipes" workflow and its automatic runs
    mealie_maintenance.py, mealie_tools.py   The tiles and tools with Mealie
    health.py, duplicates.py, ignored.py   Collection overview (no AI)
    maintenance.py, app_settings.py, usage_log.py, notify.py   Automation, budget, usage, notifications

    # Plan
    tools_meal_plan.py      Weekly plan and its chat, rests across the week
    household.py            Household profile (persons, never / rather not, fixed days, nutrition goals)
    nutrition.py            Energy and protein per serving from Tandoor / Mealie
    shopping_text.py        Shopping list to share
    guest_menu.py           Guest menu: courses from your recipes, swap, meal plan, shopping list
    meal_prep.py            One work plan for several recipes (meal prep, guest menu)
    perishability.py        Which ingredients spoil quickly (order of the week)
    cook_today.py           "What can I cook today?" ingredient index, matching, fridge photo
    cook_feedback.py        "How was it?"
    mealie_plan.py          Meal plan, shopping list and ratings with Mealie
    seasonal.py             What's in season

    static/                 Frontend (index.html, app.js, i18n.js, style.css)
  scripts/                  Command-line scripts (see setup.md)
  tests/                    pytest suite with in-memory fakes of Tandoor and Mealie
```

## Tests

The tricky parts – merging and undo against an in-memory fake Tandoor, the
Mealie import, tools and planning against a fake Mealie, the background
queue, duplicate detection, the weekly plan and its chat, the household
profile and nutrition goals, the guest menu, the meal-prep plan, *what can
I cook today?*, video, voice and migration imports, the
amounts in the steps, the recipe doctor, the photo check, the maintenance
chat, budget and schedule, the review inbox – have automated tests. They
need neither a recipe manager nor an AI key and run in a few seconds:

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

GitHub runs them on every pull request (`.github/workflows/tests.yml`).
`.github/workflows/docker.yml` builds the image for amd64 and arm64 and
publishes it to the GitHub Container Registry – `:latest` on every push to
`main`, `:1.2.0` and `:1.2` for a tag `v1.2.0` – after the tests passed;
for a pull request that touches the Dockerfile it only builds it.
