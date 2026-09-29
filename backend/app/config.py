from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Which AI provider is used for recipe extraction: anthropic | openai | gemini
    ai_provider: str = "anthropic"

    # Anthropic / Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    # Cheaper model for the maintenance tools (matching, tagging, plurals...);
    # empty = use claude_model for those too
    claude_tools_model: str = "claude-haiku-4-5-20251001"

    # OpenAI / ChatGPT
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_tools_model: str = ""       # cheaper model for the maintenance tools; empty = openai_model

    # Google / Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_tools_model: str = ""       # cheaper model for the maintenance tools; empty = gemini_model

    # Where imports go: "tandoor" (default) or "mealie" - with Mealie only the import works
    recipe_manager: str = "tandoor"
    mealie_url: str = ""       # e.g. https://mealie.myserver.com (no trailing slash)
    mealie_token: str = ""     # Mealie: user profile -> API tokens

    # Tandoor
    tandoor_url: str = ""      # e.g. https://recipes.myserver.com (no trailing slash)
    tandoor_token: str = ""    # Tandoor API token: user settings -> API

    # Output settings
    output_language: str = "English"   # target language for title/description/ingredients/steps AND the UI language
    convert_to_metric: bool = True     # convert US/UK units (cups, oz, lb, °F, inch) to metric
    check_duplicates: bool = True      # look up existing Tandoor recipes by title before import and warn on matches
    reuse_existing_tags: bool = True   # fetch existing Tandoor keywords and ask the AI to prefer reusing them
    custom_instructions: str = ""      # extra instructions appended to the extraction prompt (see .env.example)
    toc_aware_chunking: bool = False   # align chunk boundaries to the PDF's bookmarks/TOC when present (EXPERIMENTAL - has caused some PDFs to yield 0 recipes, off by default until fixed; set true to opt in)

    # Housekeeping
    job_retention_hours: int = 48      # delete jobs (and their uploaded PDF/images) older than this many hours
    auto_process_interval_hours: float = 0  # run "Process new recipes" automatically every N hours; 0 = off

    # OCR (for scanned PDF pages with no text layer, and for directly uploaded photos)
    ocr_languages: str = "eng"         # Tesseract language code(s), '+'-joined, e.g. "eng+deu". Installed by default: eng, deu, fra, ita, spa
    ocr_min_chars_per_page: int = 20   # a PDF page with less extracted text than this is treated as a scan and sent through OCR
    ocr_dpi: int = 300                 # render resolution for OCR - higher is more accurate but slower
    ocr_preprocess_enabled: bool = True       # deskew + auto-crop + contrast enhancement before OCR
    ocr_double_page_detection: bool = True    # detect a two-page spread scanned/photographed as one image and OCR each half separately
    ocr_double_page_aspect_threshold: float = 1.3  # width/height ratio above which an image counts as a double-page spread
    force_ocr: bool = False  # OCR every page even if it already has a text layer - use if extracted text looks garbled/wrong despite the PDF having text

    # AI image generation (optional, for recipes with no photo in the source document)
    image_gen_enabled: bool = False          # off by default - generating images costs money per recipe
    image_gen_provider: str = "openai"       # openai | gemini (Anthropic/Claude has no image generation API)
    openai_image_model: str = "gpt-image-1"  # dall-e-3 was retired March 2026 - use a gpt-image-* model
    gemini_image_model: str = "gemini-2.5-flash-image"  # Google's Imagen models were retired in 2026 - use a Gemini image ("Nano Banana") model
    image_gen_custom_instructions: str = ""  # extra instructions appended to the image-generation prompt (see .env.example)

    # Watched folder: files put here are imported on their own and wait in the review inbox; empty = off
    watch_dir: str = ""
    watch_interval_seconds: int = 30

    # Push notifications (see notify.py) - ntfy topic URL (e.g. https://ntfy.sh/my-secret-topic) and/or Telegram bot
    notify_ntfy_url: str = ""
    notify_ntfy_token: str = ""          # only for a protected ntfy topic
    notify_telegram_token: str = ""
    notify_telegram_chat_id: str = ""
    app_url: str = ""                    # public address of the helper - notifications link to it

    # Password for the web interface; empty = no password (e.g. only reachable in your home network)
    app_password: str = ""

    # App
    data_dir: str = "/app/data"
    app_version: str = "dev"             # set in the Docker image (APP_VERSION)
    max_upload_mb: int = 100

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


# ---------- Language mapping (name from .env -> ISO 639-1 code) ----------
# Used both for the translation instruction sent to the AI and for the UI
# language, so both stay consistent and follow from a single OUTPUT_LANGUAGE.
LANGUAGE_NAME_TO_CODE = {
    "deutsch": "de", "german": "de", "allemand": "de", "tedesco": "de", "aleman": "de", "alemán": "de",
    "englisch": "en", "english": "en", "anglais": "en", "inglese": "en", "ingles": "en", "inglés": "en",
    "französisch": "fr", "franzosisch": "fr", "french": "fr", "français": "fr", "francais": "fr",
    "francese": "fr", "frances": "fr", "francés": "fr",
    "italienisch": "it", "italian": "it", "italien": "it", "italiano": "it",
    "spanisch": "es", "spanish": "es", "español": "es", "espanol": "es", "espagnol": "es", "spagnolo": "es",
    "niederländisch": "nl", "niederlaendisch": "nl", "dutch": "nl", "nederlands": "nl", "néerlandais": "nl",
    "polnisch": "pl", "polish": "pl", "polski": "pl",
    "portugiesisch": "pt", "portuguese": "pt", "português": "pt", "portugues": "pt",
}

# Languages that have a complete UI translation in the frontend.
# Anything else falls back to English (still works, just not localized).
SUPPORTED_UI_LANGUAGES = {"de", "en", "fr", "it", "es"}


def get_language_code(name: str) -> str | None:
    """Returns the ISO code for a language name, or None if unknown (no guessing)."""
    return LANGUAGE_NAME_TO_CODE.get((name or "").strip().lower())


def get_ui_language_code(name: str) -> str:
    """Returns the UI language: a known, supported language -> its code, otherwise fallback 'en'."""
    code = get_language_code(name)
    return code if code in SUPPORTED_UI_LANGUAGES else "en"
