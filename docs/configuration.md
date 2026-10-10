# Configuration

[In the app](#in-the-app) · [`.env`](#env) · [Custom instructions](#custom-instructions) ·
[UI language](#ui-language) · [AI providers and costs](#ai-providers-and-costs) · [Local AI](#local-ai-ollama--co)

← [Back to the README](../README.md)

## In the app

*Automatic maintenance* and the *monthly AI budget* are set on the 🔧
Maintain page, the *household profile* on the 📅 Plan page – stored in the
data volume, no restart needed.

## `.env`

`.env.example` lists every option with a comment. The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic`, `openai`, `gemini` or `compatible` (any OpenAI-compatible API) |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | – / `claude-sonnet-4-6` | Used when `AI_PROVIDER=anthropic` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | – / `gpt-4o` | Used when `AI_PROVIDER=openai` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-2.5-flash` | Used when `AI_PROVIDER=gemini` |
| `COMPATIBLE_BASE_URL` / `COMPATIBLE_MODEL` / `COMPATIBLE_API_KEY` | – | Used when `AI_PROVIDER=compatible`, see [Local AI](configuration.md#local-ai-ollama--co) |
| `OPENAI_TRANSCRIBE_MODEL` / `COMPATIBLE_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` / – | Speech-to-text for voice notes (OpenAI, or a local server; Gemini transcribes with its main model) |
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
| `IMAGE_GEN_ENABLED` / `IMAGE_GEN_PROVIDER` | `false` / `openai` | Generate photos for recipes without one and improve existing ones (`openai` or `gemini`; costs money per image) |
| `AUTO_PROCESS_INTERVAL_HOURS` | `0` (off) | Run *Process new recipes* every N hours |
| `TZ` | `UTC` | Time zone for automatic runs and the monthly budget |
| `JOB_RETENTION_HOURS` | `48` | Delete imports (and their uploaded files) after this many hours |
| `MAX_UPLOAD_MB` | `100` | Maximum upload size |

Further OCR fine-tuning (`OCR_DPI`, `OCR_PREPROCESS_ENABLED`,
`OCR_DOUBLE_PAGE_DETECTION`, …), image-generation and notification options
are documented in `.env.example`.

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

### Local AI (Ollama & co.)

`AI_PROVIDER=compatible` works with any server that offers an
OpenAI-compatible API – Ollama, LM Studio, vLLM, LocalAI, or a service like
OpenRouter:

```bash
AI_PROVIDER=compatible
COMPATIBLE_BASE_URL=http://ollama:11434/v1
COMPATIBLE_MODEL=qwen2.5:14b
# optional: COMPATIBLE_TOOLS_MODEL (smaller model for the tools),
# COMPATIBLE_API_KEY, COMPATIBLE_TIMEOUT_SECONDS (default 600),
# COMPATIBLE_TRANSCRIBE_MODEL (speech-to-text for voice notes)
```

Small local models make more mistakes in long cookbook extractions; for
photos of pages and the fridge photo the model must be able to read images.
Everything is still reviewed before it changes your collection.

Keeping costs down:

- The maintenance tools and the weekly plan use the cheaper **tools model**,
  batch many items per request and send only what's needed (e.g. only likely
  duplicates, only recipes that really need a revision).
- The collection overview (including the checks for contradictions and
  weak photos), duplicate detection, the website scan, moving recipes from
  the other recipe manager, the matching of *what can I cook today?* and
  the matching before import work **without AI**.
- A token badge in the top bar shows what the current import costs; the
  Maintain page shows the usage of the last 30 days per tool.
- Set a **monthly budget** on the Maintain page so automatic runs stop when
  it's used up.
- Only upload the pages that contain recipes rather than a whole book with
  foreword and index.
