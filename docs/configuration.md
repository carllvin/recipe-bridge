# Configuration

[In the app](#in-the-app) · [`.env`](#env) · [Custom instructions](#custom-instructions) ·
[UI language](#ui-language) · [AI providers and costs](#ai-providers-and-costs) · [Local AI](#local-ai-ollama--co)

← [Back to the README](../README.md)

## In the app

*Automatic maintenance* and the *monthly AI budget* are set on the 🔧
Maintain page, the *household profile* on the 📅 Plan page – stored in the
data volume, no restart needed.

## `.env`

`.env.example` holds what you need to start. Everything below is optional
and uses the default when left out. After changing `.env`, re-create the
container (`docker compose up -d`, in Dockge *Update*) – a plain restart
keeps the old environment. Maintain → **Diagnosis** shows what the running
app actually uses.

### Recipe manager

| Variable | Default | Meaning |
|---|---|---|
| `RECIPE_MANAGER` | `tandoor` | `tandoor` or `mealie` – with Mealie, nutrition, supermarket categories, unit conversions and tag groups aren't available, and changes can't be undone |
| `TANDOOR_URL` / `TANDOOR_TOKEN` | – | Your Tandoor (without a trailing slash) and an API token |
| `MEALIE_URL` / `MEALIE_TOKEN` | – | Your Mealie and an API token. With **both** Tandoor and Mealie filled in, the import page offers moving recipes from the other one |

### AI

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic`, `openai`, `gemini` or `compatible` (see [Local AI](#local-ai-ollama--co)) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | – | The key for the chosen provider |
| `CLAUDE_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` | `claude-sonnet-4-6` / `gpt-4o` / `gemini-2.5-flash` | Main model: reading cookbooks, translating recipes |
| `CLAUDE_TOOLS_MODEL` / `OPENAI_TOOLS_MODEL` / `GEMINI_TOOLS_MODEL` | `claude-haiku-4-5-20251001` / – / – | Cheaper model for the maintenance tools and the weekly plan; empty = main model |
| `COMPATIBLE_BASE_URL` / `COMPATIBLE_MODEL` | – | `AI_PROVIDER=compatible`: the server's OpenAI-compatible API and model |
| `COMPATIBLE_API_KEY` / `COMPATIBLE_TOOLS_MODEL` | – | Only if the server needs a key / a smaller model for the tools |
| `COMPATIBLE_TIMEOUT_SECONDS` | `600` | How long to wait for a local model's answer |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Speech-to-text for voice notes (needs an OpenAI or Gemini key, also with Claude) |
| `COMPATIBLE_TRANSCRIBE_MODEL` | – | Or a local Whisper-style server at `COMPATIBLE_BASE_URL` |

### Recipes

| Variable | Default | Meaning |
|---|---|---|
| `OUTPUT_LANGUAGE` | `English` | Language of the recipes **and** of the app – see [UI language](#ui-language) |
| `TRANSLATE_RECIPES` | `true` | `false` keeps every recipe in the language it was written in |
| `CONVERT_TO_METRIC` | `true` | cups/oz/lb/°F/inch to g/ml/°C/cm |
| `SOURCE_TAG` | `true` | A tag with the source: the website (`nytcooking`), the video channel, the cookbook's author (`ottolenghi`) |
| `STEP_PHOTOS_AI` | `true` | *Place photos into the steps* may ask the image AI; `false` = only a recipe page's own step photos, the rest by hand |
| `CHECK_DUPLICATES` | `true` | Flags recipes already in the collection (by title and by address) |
| `REUSE_EXISTING_TAGS` | `true` | The AI prefers your existing tags |
| `CUSTOM_INSTRUCTIONS` | – | Your own rules for the extraction, see [below](#custom-instructions) |
| `TOC_AWARE_CHUNKING` | `false` | Experimental: split PDFs along their bookmarks – has made some PDFs yield no recipes |

### Import

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_DIR` | – | Watched folder: files put there are imported on their own (mount it, see `docker-compose.yml`). Done files move to `processed/`, unusable ones to `failed/` |
| `WATCH_INTERVAL_SECONDS` | `30` | How often the folder is checked |
| `WATCH_BATCH` | `true` | Watched-folder imports go to the AI as a batch: half price, ready within up to 24 hours (Claude, OpenAI, Gemini). Imports in the app have a checkbox for it |
| `MAX_UPLOAD_MB` | `100` | Largest upload |
| `JOB_RETENTION_HOURS` | `48` | Imports (and their files) are deleted after this long |

### OCR (scanned pages, photos)

| Variable | Default | Meaning |
|---|---|---|
| `OCR_LANGUAGES` | `eng` | Tesseract languages, `+`-joined, e.g. `eng+deu` (installed: eng, deu, fra, ita, spa) |
| `FORCE_OCR` | `false` | OCR every PDF page, also ones with a text layer (for garbled PDFs) |
| `OCR_MIN_CHARS_PER_PAGE` | `20` | A PDF page with less text counts as a scan |
| `OCR_DPI` | `300` | Render resolution – higher is more accurate, slower |
| `OCR_PREPROCESS_ENABLED` | `true` | Deskew, crop and sharpen before OCR |
| `OCR_DOUBLE_PAGE_DETECTION` / `OCR_DOUBLE_PAGE_ASPECT_THRESHOLD` | `true` / `1.3` | Split a photographed double page (wider than 1.3 : 1) into two |

### Photos by the image AI

| Variable | Default | Meaning |
|---|---|---|
| `IMAGE_GEN_ENABLED` | `false` | Generate photos for recipes without one and improve weak ones – costs per image |
| `IMAGE_GEN_PROVIDER` | `openai` | `openai` or `gemini` (Claude has no image generation) |
| `OPENAI_IMAGE_MODEL` / `GEMINI_IMAGE_MODEL` | `gpt-image-1` / `gemini-2.5-flash-image` | The image models |
| `IMAGE_GEN_CUSTOM_INSTRUCTIONS` | – | Extra wishes for every generated photo |

### Access, notifications, automation

| Variable | Default | Meaning |
|---|---|---|
| `APP_PASSWORD` | – | Password for the web interface; each device signs in once. Changing it signs every device out |
| `NOTIFY_NTFY_URL` / `NOTIFY_NTFY_TOKEN` | – | Push messages via an ntfy topic (token only for a protected topic) – when an import from the phone, the folder or at half price is ready, automatic runs prepared suggestions, the budget runs low |
| `NOTIFY_TELEGRAM_TOKEN` / `NOTIFY_TELEGRAM_CHAT_ID` | – | The same via a Telegram bot (@BotFather) |
| `APP_URL` | – | The app's public address – notifications link to it |
| `AUTO_PROCESS_INTERVAL_HOURS` | `0` (off) | Run *Process new recipes* every N hours |
| `TZ` | `UTC` | Time zone for automatic runs and the monthly budget, e.g. `Europe/Berlin` |

## Custom instructions

`CUSTOM_INSTRUCTIONS` appends your own rules to the extraction prompt and
takes precedence over the built-in guidance, e.g.:

```bash
CUSTOM_INSTRUCTIONS="Always add the tag 'family-recipe'. If servings aren't stated, assume 4."
```

## UI language

`OUTPUT_LANGUAGE` sets both the language the AI writes recipes in and the
language of the interface. Any language works for the recipes; the interface
is fully translated into Deutsch, English, Français, Italiano and Español and
falls back to English otherwise (add a language in
`backend/app/static/i18n.js` and `SUPPORTED_UI_LANGUAGES` in `config.py`).
If a cookbook is already in the target language, the translation step is
skipped automatically. To keep every recipe in the language it was written
in, set `TRANSLATE_RECIPES=false` – `OUTPUT_LANGUAGE` then only sets the
interface.

## AI providers and costs

Only the chosen provider's key and model are used; the app behaves the same
with all of them. Model names change regularly – see the current lists at
[Anthropic](https://docs.claude.com/en/docs/about-claude/models),
[OpenAI](https://platform.openai.com/docs/models) and
[Google](https://ai.google.dev/gemini-api/docs/models).

### Local AI (Ollama & co.)

`AI_PROVIDER=compatible` works with any server that offers an
OpenAI-compatible API – Ollama, LM Studio, vLLM, LocalAI, or a service like
OpenRouter:

```bash
AI_PROVIDER=compatible
COMPATIBLE_BASE_URL=http://ollama:11434/v1   # on the host: http://host.docker.internal:11434/v1
COMPATIBLE_MODEL=qwen2.5:14b
```

Small local models make more mistakes in long cookbooks; for photos of
pages and the fridge photo the model must read images (e.g. `qwen2.5vl`,
`llava`). Everything is still reviewed before it changes your collection.

### Keeping costs down

- **Half price:** with Claude, OpenAI or Gemini an import can go to the AI as a
  batch – ready within up to 24 hours. On by default for the watched folder
  (`WATCH_BATCH`), a checkbox on the import page otherwise; a large import
  without it asks once more.
- With Claude, a cookbook's instructions are **cached** after its first
  part; the amounts in the steps are written while the recipe is read,
  without a second AI call.
- The maintenance tools and the weekly plan use the cheaper **tools
  model** and send only what's needed (likely duplicates, recipes that
  really need a revision).
- Without AI: the collection overview, duplicate detection, the website
  scan, moving recipes between Tandoor and Mealie, *what can I cook today?*
  and the matching before import.
- The top bar shows what the current import costs, Maintain the usage of
  the last 30 days per tool; a **monthly budget** there stops automatic
  runs when it's used up.
- With a cookbook, pick only the recipes you want before the AI reads it –
  the pages of the others aren't sent.
