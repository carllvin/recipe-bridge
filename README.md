# Tandoor Helper

Upload a PDF cookbook, let an AI (Claude, ChatGPT, or Gemini) detect the
individual recipes — including images, ingredients, times, and tags — review
and adjust them in a web UI, and import the selected ones into
[Tandoor](https://tandoor.dev/) via its API.

The web UI has four areas:

- **📥 Import** – a cookbook (PDF/EPUB), photos of pages (collected and
  ordered first, optionally as one single recipe), or a single recipe from a
  web page; reviewed before it goes into Tandoor, and post-processed
  automatically afterwards.
- **✅ Review** – one inbox for every suggestion waiting for approval
  (merges, ingredient details, conversions, recipe revisions, tags, meal
  plan), grouped by kind. Selected suggestions are applied in the
  background (closing the page doesn't stop it); failed ones can be retried,
  and applied changes can be undone for 14 days.
- **📅 Plan** – "What can I cook today?" from the ingredients you have, a
  weekly meal plan from your own recipes (using ratings and cooking history)
  added to Tandoor's meal plan and shopping list, and "How was it?" to rate
  what you cooked.
- **🔧 Maintain** – a health overview of the collection (likely duplicate
  ingredients/units, missing nutrition/categories/conversions, untranslated
  or unstructured recipes, missing season/tags): each tile starts the tool
  that fixes it, and single entries can be ignored. Tools without a count
  (full ingredient/unit review, tag translate & simplify) are under "More
  tools". Automatic maintenance on a schedule and a monthly AI token budget
  are set up there too, next to the AI usage of the last 30 days. A few
  destructive operations are standalone scripts that intentionally require
  a terminal - see [`backend/scripts/`](backend/scripts/).

<p align="center">
  <img src="screenshots/main_page.png" alt="Main page" width="85%">
</p>

<p align="center">
  <img src="screenshots/preview_page_1.png" alt="Recipe preview" width="85%">
</p>

<p align="center">
  <img src="screenshots/preview_page_2.png" alt="Recipe preview" width="85%">
</p>

<p align="center">
  <img src="screenshots/succesful_import.png" alt="Successful Tandoor import" width="85%">
</p>

## Setup

1. **Create `.env`**

   ```bash
   cp .env.example .env
   ```

   Fill in:
   - `AI_PROVIDER` – `anthropic` (Claude), `openai` (ChatGPT), or `gemini` (Google) – see [Switching AI providers](#switching-ai-providers) below
   - the API key for the provider you chose (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`)
   - `TANDOOR_URL` – the URL of your running Tandoor instance, **without** a trailing slash
   - `TANDOOR_TOKEN` – API token from Tandoor: *user menu → settings → API → "create new token"*

2. **Start it**

   ```bash
   docker compose up --build -d
   ```

3. The app runs at **http://localhost:8420** (change the port in `docker-compose.yml` if you like).

## How it works

1. Upload a PDF (drag & drop or click)
2. The AI reads the PDF page by page (in overlapping chunks so large cookbooks
   can be processed too), detects recipes including their page range,
   **translates every text field into the configured language**
   (`OUTPUT_LANGUAGE`), and **converts US/UK units to metric**
   (`CONVERT_TO_METRIC`)
3. A **cookbook name is suggested** once (from the PDF's title metadata, an AI
   guess at the first pages, or the filename) — editable at the top of the
   review screen
4. Images found in the PDF are automatically matched to recipes by page number
   — you can switch or deselect the image per recipe in the UI
5. In the review screen: select recipes individually or in bulk, edit title,
   description, ingredients, steps, and times directly. If the source text
   contains important side notes (shelf life, freezing, variations, "prepare a
   day ahead" …), they're automatically added as an extra, final step titled
   "Note". Each ingredient is assigned to the step that actually needs it
   (so in Tandoor, ingredients end up attached to the right step instead of
   all dumped into the first one) — a small dropdown next to each ingredient
   lets you correct that if needed. A season tag (Spring/Summer/Autumn/Winter)
   is added automatically when the recipe clearly fits one
6. Click **"Import to Tandoor"** — each recipe gets a status (imported / error
   with a message), gets added to the cookbook named above (created
   automatically if it doesn't exist yet), and a summary popup appears at the
   end with the option to upload the next PDF right away. If some recipes
   failed, a **"Retry failed"** button lets you re-attempt just those without
   touching anything already imported

While analyzing, the app shows progress (e.g. "Analyzed pages 25-36 (3/8)"),
since the AI processes the PDF in overlapping sections. Before import, the app
also checks once against recipes already in Tandoor: an (almost) exact title
match gets the recipe auto-deselected and clearly flagged; a merely similar
title stays selected but shows a warning. Existing Tandoor tags are fetched
once too, so the AI prefers reusing them (e.g. "vegetarian") instead of
creating near-duplicates.

A small badge in the top bar shows how many tokens the current job has used so
far across all AI calls (hover it for the input/output breakdown) — handy for
keeping an eye on cost.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | Which AI provider handles recipe extraction: `anthropic`, `openai`, or `gemini` |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | – / `claude-sonnet-4-6` | Only relevant when `AI_PROVIDER=anthropic` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | – / `gpt-4o` | Only relevant when `AI_PROVIDER=openai` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-2.5-flash` | Only relevant when `AI_PROVIDER=gemini` |
| `CLAUDE_TOOLS_MODEL` / `OPENAI_TOOLS_MODEL` / `GEMINI_TOOLS_MODEL` | `claude-haiku-4-5-20251001` / – / – | Cheaper model for the maintenance tools (matching, tags, seasons, plurals, nutrition). Cookbook extraction and recipe translation keep the main model. Empty = main model |
| `OUTPUT_LANGUAGE` | `English` | Target language for title, description, ingredients, steps, tags **and this app's UI language** — independent of the source PDF's language |
| `CONVERT_TO_METRIC` | `true` | Converts cups/oz/lb/°F/inch to g/ml/°C/cm automatically |
| `CHECK_DUPLICATES` | `true` | Checks recipe titles against those already in Tandoor before import |
| `REUSE_EXISTING_TAGS` | `true` | Fetches existing Tandoor tags and asks the AI to prefer reusing them |
| `CUSTOM_INSTRUCTIONS` | *(empty)* | Free-text instructions appended to the extraction prompt — see [Custom instructions](#custom-instructions) below |
| `JOB_RETENTION_HOURS` | `48` | Deletes jobs (and their uploaded PDF/images) from disk after this many hours |
| `AUTO_PROCESS_INTERVAL_HOURS` | `0` (off) | Runs **Process new recipes** automatically every N hours: new recipes are translated right away, all other suggestions wait under ✅ Review. Costs AI tokens only when there are new recipes |
| `MAX_UPLOAD_MB` | `100` | Maximum PDF upload size |

### Switching AI providers

By default the app uses Claude (Anthropic). To use ChatGPT (OpenAI) or Gemini
(Google) instead, in `.env`:

```bash
# Example: ChatGPT instead of Claude
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
```

or

```bash
# Example: Gemini instead of Claude
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

Only the key/model of the provider you select needs to be set; the other two
providers' values are then ignored and can stay empty or remain in `.env`.
Model names change regularly for all three providers — current lists:
[Anthropic](https://docs.claude.com/en/docs/about-claude/models),
[OpenAI](https://platform.openai.com/docs/models),
[Google](https://ai.google.dev/gemini-api/docs/models). The app itself
(prompt, JSON parsing, image/cookbook logic) behaves identically across all
three providers — only `backend/app/llm_provider.py` translates to each
provider's specific API format, including reading back its token usage.

### Custom instructions

`CUSTOM_INSTRUCTIONS` lets you append your own free-text rules to the
extraction prompt without touching the code, e.g.:

```bash
CUSTOM_INSTRUCTIONS="Always add the tag 'family-recipe'. If servings aren't stated, assume 4. Keep step instructions under two sentences."
```

These are appended verbatim and explicitly take precedence over the built-in
guidance when they conflict, so use them to override defaults you don't like
(e.g. a different tagging convention) rather than just to add minor notes.

### About the UI language

`OUTPUT_LANGUAGE` controls **both**: the language the AI writes/translates
recipes into, AND the language of the web interface itself (buttons, labels,
messages). Complete UI translations exist for **Deutsch, English, Français,
Italiano, Español** (`backend/app/static/i18n.js`). If you set a different
language name (e.g. `Polski`), recipe translation still works (the AI
understands essentially any language), but the interface itself falls back to
English since there's no translation table for it. Adding another UI language
is straightforward: add an entry to `TRANSLATIONS` in `i18n.js` and to
`SUPPORTED_UI_LANGUAGES` in `config.py`.

If the source PDF is already (detectably) written in the target language, the
system automatically skips the "translate this" instruction to the AI
(language detection via `langdetect`, local, no extra API call) — this avoids
needlessly rephrased text and saves a bit of time/tokens.

### Reducing token usage

- Increase `pages_per_chunk` in `ai_extractor.py` — fewer, larger requests
  means less repeated prompt overhead (trade-off: higher risk a recipe spans a
  chunk boundary)
- Pick a cheaper model (e.g. `gpt-4o-mini`, `gemini-2.5-flash`,
  `claude-haiku-4-5`)
- Language detection (see above) already avoids paying for unnecessary
  "translation" when the source is already in the target language
- Only upload the pages that actually contain recipes rather than a whole book
  including foreword/index
- Watch the token badge in the top bar to see what a given cookbook actually
  costs before committing to a bigger one

## ⚠️ A note on the Tandoor API

Tandoor keeps evolving, and the exact field names of its API (`/api/recipe/`,
`/api/food/`, `/api/unit/`, `/api/keyword/`) can differ slightly between
versions. `backend/app/tandoor_client.py` is written against the common,
documented REST API (ingredients/units/tags are created via "get-or-create",
then referenced by ID **and** name — some Tandoor versions require both for
nested objects). For the cookbook feature, both `/api/recipe-book/` and
`/api/cookbook/` are tried (Tandoor renamed this endpoint across versions), as
well as for the recipe association (`/api/recipe-book-entry/`,
`/api/cookbook-recipe/`, `/api/cookbookrecipe/`). If none of those match your
version, the recipe is still imported — the cookbook assignment then just
shows up as a warning in the import popup / on the recipe row.

If import fails with an error like *"Tandoor rejected the recipe (400): ..."*:

1. Open your own instance's Swagger UI: `https://YOUR-TANDOOR-URL/api/schema/swagger-ui/`
   (or `/api/docs/`) and check the exact schema of `POST /api/recipe/`.
2. Tandoor's error message is shown verbatim in the UI (it usually names the
   offending field) — use that to adjust `_build_recipe_payload()` in
   `tandoor_client.py`.
3. If you're working with an AI assistant on this, just paste the exact error
   message or the Swagger schema and have it adjust the client accordingly.

## Architecture

```
backend/
  app/
    main.py           FastAPI endpoints; also serves the frontend
    pdf_processor.py  Text/image/TOC extraction from the PDF (PyMuPDF)
    ai_extractor.py   AI-based recipe extraction: prompt, chunking, dedup, language detection
    llm_provider.py   Abstraction over Anthropic/OpenAI/Gemini (AI_PROVIDER switch), token usage
    tandoor_client.py Tandoor API client (get-or-create, recipe import, image upload, cookbook, duplicate check, tag reuse)
    jobs.py           In-memory job/state store, old-job cleanup
    schemas.py         Pydantic data models
    config.py          Settings + central language-name-to-code mapping
    static/           Vanilla JS frontend (index.html, app.js, i18n.js, style.css)
```

There's deliberately **no database** — jobs live only in the container's
memory (PDFs/images sit in the `cookbook_data` volume under
`/app/data/<job_id>`, cleaned up automatically after `JOB_RETENTION_HOURS`).
For the intended use (upload a PDF → review → import, then done) that's
enough; restarting the container loses any job that hasn't been imported yet.

## Known limitations / possible next steps

- `TOC_AWARE_CHUNKING` (aligning chunk boundaries to a PDF's bookmarks) is
  **off by default** — it has been observed to make some PDFs yield 0 recipes
  for reasons not yet fully isolated. The code has diagnostic logging for it
  (`docker compose logs -f tandoor-helper` shows per-chunk recipe counts and
  raw AI output snippets on parse failures) if you want to help debug it with
  `TOC_AWARE_CHUNKING=true`; otherwise leave it off
- Ingredient/step assignment is done by the AI reading the instructions, so it
  can occasionally misjudge which step an ingredient belongs to — correct it
  via the per-ingredient step dropdown in the UI
- Image matching is heuristic (closest image within the page range) — for
  cookbooks with lots of decorative/ingredient photos per spread, it's worth a
  manual look in the UI
- Duplicate detection only compares titles (exact + similarity via `difflib`),
  not ingredients/content — two recipes with completely different names but
  the same content won't be flagged
- No auth/login for the web UI itself — if the app isn't only running
  locally, put a reverse proxy with basic auth (or similar) in front of it
- No merging/splitting of recipes in the UI yet (in case the AI incorrectly
  splits one recipe into two, or the reverse)
- Deliberately out of scope for now: OCR for un-OCR'd scans, EPUB/JPEG/PNG/HEIC
  upload support, double-page-spread detection with image deskew/crop, and
  AI-generated recipe images when the source has none — each is a large
  enough subsystem (new dependencies: Tesseract, ebooklib, an image-gen API,
  OpenCV/PIL-based image processing) to warrant its own dedicated pass


## Tests

The tricky parts (merging and undo against an in-memory fake Tandoor, the
background queue, duplicate detection, plural filter, "what can I cook
today?", budget and schedule, the review inbox) have automated tests. They
need no Tandoor and no AI key and run in a few seconds:

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

GitHub runs them on every pull request (`.github/workflows/tests.yml`).
