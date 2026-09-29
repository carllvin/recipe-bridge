# Recipe Bridge

A companion web app for your self-hosted recipe manager –
[Tandoor Recipes](https://tandoor.dev/) or [Mealie](https://mealie.io/).
It **imports recipes from almost anywhere** – cookbooks (PDF, EPUB, Word),
phone photos and handwritten cards, web pages, whole recipe websites,
browser bookmarks, pasted text – with the help of an AI (Claude, ChatGPT or
Gemini), **keeps your collection tidy** (duplicates, translations, tags,
nutrition, unit conversions …) and **helps you plan meals** from your own
recipes. It never changes anything behind your back: every change is shown
as a suggestion first, and with Tandoor applied changes can be undone.

*Formerly "Tandoor Helper" – see [Updating](#updating) if you're coming from
it.*

<p align="center">
  <img src="screenshots/import.png" alt="Import page with the photo collector" width="85%">
</p>

- [What it does](#what-it-does)
  - [📥 Import](#-import) · [✅ Review](#-review) · [🔧 Maintain](#-maintain) · [📅 Plan](#-plan) · [On the phone](#on-the-phone)
- [Tandoor or Mealie](#tandoor-or-mealie)
- [Setup](#setup) · [Updating](#updating)
- [Configuration](#configuration)
- [AI providers and costs](#ai-providers-and-costs)
- [Data and backups](#data-and-backups)
- [Command-line scripts](#command-line-scripts)
- [A note on the recipe managers' APIs](#a-note-on-the-recipe-managers-apis)
- [Architecture](#architecture) · [Tests](#tests) · [Known limitations](#known-limitations)

## What it does

The app has four areas, reachable from the top bar (on the phone: the bottom
bar). Texts in the app name your recipe manager – "Tandoor" or "Mealie".

### 📥 Import

**Files** – drop them on the import area or click to choose:

- **Cookbooks as PDF, EPUB or Word (.docx)** – the AI reads every page (in
  overlapping chunks, so books with hundreds of pages work), finds each
  recipe with its page range, ingredients, steps, times, servings, tags and
  photo. Pictures embedded in a Word document become photo candidates.
- **Photos of cookbook pages** – drop them in or, on the phone, take them one
  by one with the camera. They are collected first, can be reordered and
  removed, and are imported together; tick *"all photos show one single
  recipe"* when ingredients and method are on different pages. Scans and
  photos go through OCR (Tesseract) with deskewing, cropping and two-page
  spread detection.
- **Handwriting** – for recipe cards and notes, tick *handwriting*: the AI
  reads the photos itself instead of the OCR (costs a little more).
- **A `.txt` file** – either a **list of recipe links** (one per line, or any
  text containing links; up to 50) or **recipe text**; the app tells them
  apart by the content. Markdown files (`.md`) are read as text.
- **Browser bookmarks** – the HTML export of Chrome, Firefox, Safari or Edge
  opens a pick list with the bookmark folders as a filter.

**From the web and the clipboard** – three tabs below the import area:

- **🔗 Link** – a single recipe page. Afterwards the app offers to scan the
  same website for more recipes.
- **🌐 Scan a website** – enter a food blog's homepage or a category page.
  The app looks for recipe pages – via the site's sitemap, or by following
  the links of the page you entered up to the chosen **depth** (1–4 levels,
  incl. "page 2, 3 …") – and recognizes them by the recipe data they embed
  (schema.org), **without AI**. Results show up while it runs, with photo,
  time and an *already in your collection* mark; you pick which ones to
  import. It respects the site's `robots.txt`, reads at most one page per
  second and checks up to 300 pages.
- **📝 Paste text** – a recipe from a message, an email or a note.

<p align="center">
  <img src="screenshots/scan.png" alt="Picking recipes found by the website scan" width="70%">
</p>

Links (single, from a list, a scan or bookmarks) are read one page at a
time – the page's schema.org recipe data is used when present – and each
recipe keeps its link as the source. Links that can't be loaded or contain
no recipe are skipped and listed in the review.

**More ways in** (under *More ways* on the import page):

- **📱 Share from the phone** – install the app on your phone (browser menu →
  *Install app* / *Add to home screen*; needs HTTPS). It then shows up in the
  share menu: links, text and photos land right in the import.
- **🔖 Bookmark button** – drag it to your browser's bookmark bar; one click
  on a recipe page sends it to the app.
- **📂 Watched folder** – files put into a folder (`WATCH_DIR`) are imported
  on their own and wait under ✅ Review → *New imports*.

Everything is **translated into your language** (`OUTPUT_LANGUAGE`) and
**converted to metric** on the way.

> **Recipes from other apps** (Paprika, Nextcloud Cookbook, RecipeSage, …):
> Tandoor and Mealie import their export files themselves. Afterwards run
> **Process new recipes** on the 🔧 Maintain page to translate them, match
> their ingredients to yours and add tags.

Before anything goes into your collection you review the result – and the
review covers everything, so nothing has to be checked again afterwards:

<p align="center">
  <img src="screenshots/review_recipes.png" alt="Reviewing the recipes found in a cookbook" width="85%">
</p>

- Edit title, description, servings, times, tags, ingredients and steps;
  pick or deselect the photo.
- Each ingredient is assigned to the step that needs it (dropdown on the
  right), so the ingredients show up at the right step.
- Ingredients, units and tags are matched against your collection:
  ✓ exists · ↺ matched to an existing entry ("onions" → "Onion") · new. A
  matched entry shows what was read below it ("read as: onions").
- Recipes that probably exist already are flagged (exact matches are
  deselected), and missing photos can optionally be generated by an image
  AI.
- **Import** puts the selected recipes into a cookbook (created if needed;
  with Mealie a category); failed ones can be retried on their own and an
  import can be undone. With Tandoor, the new ingredients then get their
  details (plural, nutrition, supermarket category, gram conversions) filled
  in automatically – shown under ✅ Review → *Recently applied*, with undo.

### ✅ Review

One inbox for every suggestion – from automatic runs, from tools you
started and imports that arrived without you (phone, watched folder) –
grouped by kind:

<p align="center">
  <img src="screenshots/review_inbox.png" alt="The review inbox" width="85%">
</p>

- **Merge & rename** duplicates (ingredients, units, tags), **ingredient
  details** (plural, nutrition, supermarket category), **gram conversions**,
  **recipe revisions** (one block of text split into steps, ingredients
  assigned to steps – with a before/after preview), **translations**,
  **tags & season**, **servings**, **photos**, **unused entries** and
  **meal plan** entries.
- Tick what you want and **Apply** or **Skip**. Applying runs **in the
  background on the server**, strictly one after another – you can close the
  page; the queue even survives a restart.
- Suggestions that failed (e.g. the recipe manager was briefly unreachable)
  stay in a **Failed** group with the error: *Retry* or *Dismiss*.
- **Recently applied** lists what was changed in the last 14 days. With
  Tandoor any of it can be **undone**, including merges (the merged-away
  ingredient is recreated and the recipes point to it again).

### 🔧 Maintain

<p align="center">
  <img src="screenshots/maintain.png" alt="Collection health, automation and budget" width="85%">
</p>

- **Process new recipes** handles recipes added since the last run directly
  in your recipe manager (URL import, app, by hand): translates them right
  away, then suggests revising their structure, matching their
  ingredients/units/tags to existing ones, filling in ingredient details and
  conversions (Tandoor) and adding tags. Recipes imported through this app
  were already covered by the review.
- **State of your collection** counts what's left to do – without AI – in
  groups:
  - *Ingredients*: possible duplicates, without nutrition or supermarket
    category, missing conversions, unused;
  - *Units*: possible duplicates, unused;
  - *Recipes*: not in your language, one block of text / all ingredients in
    step 1, without a season, few tags, without servings, without a photo;
  - *Tags*: not in a tag group, unused.

  **Fix** starts the tool for exactly that tile (e.g. only the likely
  duplicates go to the AI, not your whole ingredient list). **Entries** lists
  them; single entries can be **ignored** (e.g. *Water* without nutrition)
  and are never suggested again. Each group also has a tool for the whole
  collection (all ingredients, all units, tag translate & simplify). Tiles
  show when a fix is running or waiting for review, and the overview
  recounts on its own after you applied something.
- **Automation & budget** (stored in the app, no restart needed):
  - *Automatic maintenance* – at a chosen time every N days, prepare
    suggestions for the selected tiles.
  - *Monthly AI budget* – a token limit; once reached, automatic runs pause
    until the next month (optionally manual starts and imports too).
  - *Notifications* – a test message for the configured channels.
  - *AI usage* of the last 30 days per tool.

### 📅 Plan

<p align="center">
  <img src="screenshots/plan.png" alt="How was it, what can I cook today and the weekly plan" width="85%">
</p>

- **How was it?** – rate the meals you planned in the last days. With
  Tandoor the rating goes in as a cook log, with Mealie as your rating plus
  the recipe's "last made" date – either way the weekly plan uses it.
- **What can I cook today?** – type what you have at home, or take a
  **photo of the fridge** and let the AI list what it sees, and get matching
  recipes from your collection (the matching needs no AI; "tomato" also
  finds "cherry tomatoes"). Salt, pepper, oil, sugar, flour, butter,
  vinegar, onions and garlic count as always at home. *Plan for today* puts
  a recipe on the meal plan.
- **Weekly plan** – the AI picks one of your recipes per day:
  - the week starts on your **shopping day** (the coming Saturday by
    default): recipes with quickly perishing ingredients – fresh fish, mince,
    leafy greens, herbs, berries – come first, pantry and frozen dishes
    towards the end;
  - it prefers recipes that use what's **already at home** (typed or from a
    fridge photo);
  - it follows your wishes ("2x vegetarian, quick on weekdays"), the season,
    variety, your ratings and how long ago you cooked something;
  - days that are already planned are left out, any day can be re-rolled,
    and a **chat** below the week changes it on request ("something with
    beef on Thursday", "swap Monday and Wednesday");
  - the selected days go into the meal plan, optionally with the
    ingredients on the shopping list.

### On the phone

The whole app works on the phone – with a bottom navigation, the camera for
cookbook and fridge photos, the share menu for links, and the review inbox
for approving suggestions on the go. Optional push notifications (ntfy or
Telegram) tell you when an import is ready, automatic runs prepared
suggestions or the AI budget runs low.

<p align="center">
  <img src="screenshots/mobile.png" alt="Review inbox and plan on a phone" width="60%">
</p>

## Tandoor or Mealie

`RECIPE_MANAGER` chooses which one the app works with (`tandoor` by
default).

| | Tandoor | Mealie |
|---|---|---|
| Import (all sources), review, matching | ✓ | ✓ |
| Weekly plan, what can I cook today, how was it | ✓ | ✓ (rating + "last made" instead of a cook log) |
| Duplicates, unused entries, servings, photos | ✓ | ✓ |
| Tags (clean up, season, more tags), translate & revise recipes, process new recipes | ✓ | ✓ |
| Automatic maintenance | ✓ | ✓ (for the tiles above) |
| Nutrition, supermarket categories, unit conversions, tag groups | ✓ | – (no matching data model) |
| Undo applied changes | ✓ | – (every change is still reviewed first) |

With Mealie, the cookbook name of an import becomes a category (Mealie's
cookbooks are saved filters, e.g. on a category), and meal types are
Mealie's fixed ones (breakfast, lunch, dinner, …).

## Setup

You need Docker (with Compose), a running Tandoor or Mealie instance and an
API key for one AI provider.

1. **Get the code**

   ```bash
   git clone https://github.com/carllvin/recipe-bridge.git
   cd recipe-bridge
   ```

2. **Create `.env`**

   ```bash
   cp .env.example .env
   ```

   Fill in at least:
   - `AI_PROVIDER` – `anthropic` (Claude), `openai` (ChatGPT) or `gemini` (Google)
   - the matching API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `GEMINI_API_KEY`)
   - **Tandoor:** `TANDOOR_URL` (**without** a trailing slash) and
     `TANDOOR_TOKEN` (in Tandoor: *user menu → Settings → API → create new
     token*)
   - **or Mealie:** `RECIPE_MANAGER=mealie`, `MEALIE_URL` and `MEALIE_TOKEN`
     (in Mealie: *user profile → API tokens*)
   - `OUTPUT_LANGUAGE` – e.g. `Deutsch` (recipes **and** the app's UI)
   - `TZ` – e.g. `Europe/Berlin`, so automatic runs happen at your local time

3. **Start it**

   ```bash
   docker compose up -d --build
   ```

4. Open **http://localhost:8420** (change the port in `docker-compose.yml`
   if you like). The badge in the top right shows whether your recipe
   manager is reachable.

**Access from outside:** set `APP_PASSWORD` – each device then signs in once
and stays signed in. For the phone's share menu and app install the app
must be reachable via **HTTPS**, e.g. behind the same reverse proxy as your
recipe manager.

## Updating

```bash
git pull
docker compose up -d --build
```

Your data (settings, open suggestions, undo history …) lives in a Docker
volume and survives updates – see [Data and backups](#data-and-backups).

### Coming from "Tandoor Helper"

The app, its container, Compose service and data volume were renamed. To
keep your settings, open suggestions and history, move the old volume's
content over once:

```bash
docker compose down          # before pulling: stops the old container
git pull
docker volume ls | grep tandoor_helper_data   # the old volume, usually tandoor-helper_tandoor_helper_data
docker volume create recipe_bridge_data
docker run --rm -v tandoor-helper_tandoor_helper_data:/from -v recipe_bridge_data:/to alpine cp -a /from/. /to/
docker compose up -d --build --remove-orphans
```

Once everything works, the old volume can be removed
(`docker volume rm tandoor-helper_tandoor_helper_data`). If you changed the
repository's remote: `git remote set-url origin
https://github.com/carllvin/recipe-bridge` (GitHub also redirects the old
address). An installed phone app may keep the old name until reinstalled.

## Configuration

### In the app

*Automatic maintenance* and the *monthly AI budget* are set on the 🔧
Maintain page and stored in the data volume – no restart needed.

### `.env`

`.env.example` lists every option with a comment. The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic`, `openai` or `gemini` |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | – / `claude-sonnet-4-6` | Used when `AI_PROVIDER=anthropic` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | – / `gpt-4o` | Used when `AI_PROVIDER=openai` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-2.5-flash` | Used when `AI_PROVIDER=gemini` |
| `CLAUDE_TOOLS_MODEL` / `OPENAI_TOOLS_MODEL` / `GEMINI_TOOLS_MODEL` | `claude-haiku-4-5-20251001` / – / – | Cheaper model for the maintenance tools and the weekly plan. Cookbook extraction and recipe translation keep the main model. Empty = main model |
| `RECIPE_MANAGER` | `tandoor` | `tandoor` or `mealie` |
| `TANDOOR_URL` / `TANDOOR_TOKEN` | – | Your Tandoor instance and API token |
| `MEALIE_URL` / `MEALIE_TOKEN` | – | Your Mealie instance and API token |
| `OUTPUT_LANGUAGE` | `English` | Language of the recipes **and** of the app's UI (full UI translations: Deutsch, English, Français, Italiano, Español) |
| `APP_PASSWORD` | *(empty = none)* | Password for the web interface |
| `NOTIFY_NTFY_URL` / `NOTIFY_TELEGRAM_TOKEN` + `NOTIFY_TELEGRAM_CHAT_ID` | – | Push notifications via ntfy or Telegram; `APP_URL` makes them link to the app |
| `WATCH_DIR` | *(empty = off)* | Folder whose files are imported on their own (mount it, see `docker-compose.yml`) |
| `CONVERT_TO_METRIC` | `true` | Converts cups/oz/lb/°F/inch to g/ml/°C/cm |
| `CHECK_DUPLICATES` | `true` | Flags recipes whose title already exists |
| `REUSE_EXISTING_TAGS` | `true` | Asks the AI to prefer your existing tags |
| `CUSTOM_INSTRUCTIONS` | *(empty)* | Your own rules for the extraction, see below |
| `OCR_LANGUAGES` | `eng` | Tesseract languages, e.g. `eng+deu` (installed: eng, deu, fra, ita, spa) |
| `FORCE_OCR` | `false` | OCR every PDF page, even ones with a text layer (for garbled PDFs) |
| `IMAGE_GEN_ENABLED` / `IMAGE_GEN_PROVIDER` | `false` / `openai` | Generate a photo for recipes without one (`openai` or `gemini`; costs money per image) |
| `AUTO_PROCESS_INTERVAL_HOURS` | `0` (off) | Run *Process new recipes* every N hours |
| `TZ` | `UTC` | Time zone for automatic runs and the monthly budget |
| `JOB_RETENTION_HOURS` | `48` | Delete imports (and their uploaded files) after this many hours |
| `MAX_UPLOAD_MB` | `100` | Maximum upload size |

Further OCR fine-tuning (`OCR_DPI`, `OCR_PREPROCESS_ENABLED`,
`OCR_DOUBLE_PAGE_DETECTION`, …), image-generation and notification options
are documented in `.env.example`.

### Custom instructions

`CUSTOM_INSTRUCTIONS` appends your own rules to the extraction prompt and
takes precedence over the built-in guidance, e.g.:

```bash
CUSTOM_INSTRUCTIONS="Always add the tag 'family-recipe'. If servings aren't stated, assume 4."
```

### UI language

`OUTPUT_LANGUAGE` sets both the language the AI writes recipes in and the
language of the interface. Any language works for the recipes; the interface
is fully translated into Deutsch, English, Français, Italiano and Español and
falls back to English otherwise (add a language in
`backend/app/static/i18n.js` and `SUPPORTED_UI_LANGUAGES` in `config.py`).
If a cookbook is already in the target language, the translation step is
skipped automatically.

## AI providers and costs

Switch providers in `.env` – only the selected provider's key and model are
used:

```bash
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
```

```bash
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

The app behaves the same with all three; `backend/app/llm_provider.py`
translates to each API. Model names change regularly – see the current lists
at [Anthropic](https://docs.claude.com/en/docs/about-claude/models),
[OpenAI](https://platform.openai.com/docs/models) and
[Google](https://ai.google.dev/gemini-api/docs/models).

Keeping costs down:

- The maintenance tools and the weekly plan use the cheaper **tools model**,
  batch many items per request and send only what's needed (e.g. only likely
  duplicates, only recipes that really need a revision).
- The collection overview, duplicate detection, the website scan, the
  matching of *what can I cook today?* and the matching before import work
  **without AI**.
- A token badge in the top bar shows what the current import costs; the
  Maintain page shows the usage of the last 30 days per tool.
- Set a **monthly budget** on the Maintain page so automatic runs stop when
  it's used up.
- Only upload the pages that contain recipes rather than a whole book with
  foreword and index.

## Data and backups

Everything the app keeps lives in the Docker volume `recipe_bridge_data`
(mounted at `/app/data`):

- running and recent imports with their uploaded files (deleted after
  `JOB_RETENTION_HOURS`),
- tool runs and suggestions waiting for review, the background queue, the
  last weekly plan with its chat,
- the undo history (14 days), ignored entries, settings, AI usage, the
  ingredient index for *what can I cook today?*.

Your recipes themselves are always in Tandoor or Mealie – the app doesn't
have a recipe database of its own. To back up its state, back up the
volume.

## Command-line scripts

A few operations deliberately need a terminal – mostly destructive ones for
test setups. They work with **Tandoor** only, live in
[`backend/scripts/`](backend/scripts/) and run inside the container, e.g.:

```bash
docker compose exec recipe-bridge python scripts/manage_ingredients.py --help
```

| Script | What it does |
|---|---|
| `manage_ingredients.py` | Review ingredient names, fill in plural and category, estimate nutrition |
| `manage_tags.py` | Simplify and translate tags, add season tags, suggest more tags |
| `cleanup_units.py` | Merge units that mean the same ("TL" / "Teelöffel" / "tsp") |
| `retranslate_recipes.py` | Translate recipes already in Tandoor after changing `OUTPUT_LANGUAGE` |
| `purge_recipes.py`, `purge_ingredients.py`, `purge_ingredients_to_15.py` | Delete (almost) all recipes/ingredients – **for test instances only, no undo** |

All of them do a dry run unless told otherwise; read the header of each
script before using it.

## A note on the recipe managers' APIs

Both recipe managers keep evolving, and their APIs can differ slightly
between versions.

- **Tandoor** – the client (`backend/app/tandoor_client.py`) is written
  against the documented REST API and tries the known alternatives where
  Tandoor renamed endpoints (e.g. `/api/recipe-book/` vs. `/api/cookbook/`).
  Compare with your instance's schema at
  `https://YOUR-TANDOOR-URL/api/schema/swagger-ui/`.
- **Mealie** – the client (`backend/app/mealie_client.py`, planning in
  `mealie_plan.py`) follows Mealie's current API (households, shopping
  lists, ratings). Compare with `https://YOUR-MEALIE-URL/docs`.

If the recipe manager rejects something, the UI shows its error message
verbatim (e.g. *"Tandoor rejected the recipe (400): …"*) – adjust the payload
in the client, or paste the error into an AI assistant and let it adjust the
client.

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
    tools_new_recipes.py    "Process new recipes" workflow and its automatic runs
    mealie_maintenance.py, mealie_tools.py   The tiles and tools with Mealie
    health.py, duplicates.py, ignored.py   Collection overview (no AI)
    maintenance.py, app_settings.py, usage_log.py, notify.py   Automation, budget, usage, notifications

    # Plan
    tools_meal_plan.py      Weekly plan and its chat
    perishability.py        Which ingredients spoil quickly (order of the week)
    cook_today.py           "What can I cook today?" ingredient index, matching, fridge photo
    cook_feedback.py        "How was it?"
    mealie_plan.py          Meal plan, shopping list and ratings with Mealie
    seasonal.py             What's in season

    static/                 Frontend (index.html, app.js, i18n.js, style.css)
  scripts/                  Command-line scripts (see above)
  tests/                    pytest suite with in-memory fakes of Tandoor and Mealie
```

## Tests

The tricky parts – merging and undo against an in-memory fake Tandoor, the
Mealie import, tools and planning against a fake Mealie, the background
queue, duplicate detection, the weekly plan and its chat, *what can I cook
today?*, budget and schedule, the review inbox – have automated tests. They
need neither a recipe manager nor an AI key and run in a few seconds:

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

GitHub runs them on every pull request (`.github/workflows/tests.yml`).

## Known limitations

- **Undo** (Tandoor) restores the state from before the change; edits you
  made in Tandoor to the same recipes or ingredients since then are
  overwritten. Side effects Tandoor does on its own (e.g. shopping-list
  entries created with a meal-plan entry) are not undone. Changes in Mealie
  can't be undone from the app.
- The **Mealie** support was built against Mealie's API as published on
  GitHub and a fake of it – details like time formats may need adjusting
  for your version (see [the note on the APIs](#a-note-on-the-recipe-managers-apis)).
- Ingredient-to-step assignment and image matching are done by the AI or
  heuristically – worth a glance in the review screen for complex recipes.
- Duplicate recipe detection compares titles only, not ingredients.
- The freshness order of the weekly plan is based on ingredient names
  (German and English word lists) – unusual names may not be recognized.
- The website scan only finds pages that embed schema.org recipe data, and
  sites that load their content with JavaScript or block bots (e.g. behind
  Cloudflare) yield little or nothing. It is meant for picking recipes for
  your own collection – respect the site's terms.
- Imports that haven't been sent to your recipe manager yet are lost when
  the container restarts (open suggestions, the queue and the undo history
  are kept).
- `TOC_AWARE_CHUNKING` (aligning chunks to a PDF's table of contents) is
  experimental and off by default – it has made some PDFs yield no recipes.
