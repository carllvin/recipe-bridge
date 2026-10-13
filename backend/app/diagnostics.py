"""Maintain -> Diagnosis: what the running container actually uses - version,
recipe manager, AI provider and models, the settings that change behaviour -
and a quick AI test. For finding out why something behaves unexpectedly
(e.g. an .env change that didn't reach the container) without reading
logs. Never shows a key or password - only whether one is set."""
from __future__ import annotations

import os
import shutil
import time

from . import app_settings, image_gen, llm_provider, target, usage_log
from .config import get_ui_language_code, settings


def _check(fn) -> tuple[bool, str]:
    try:
        fn()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _ocr() -> str:
    try:
        import pytesseract
        return f"Tesseract {pytesseract.get_tesseract_version()}, OCR_LANGUAGES={settings.ocr_languages}"
    except Exception as exc:  # noqa: BLE001
        return f"not available ({str(exc)[:80]})"


def info() -> list[dict]:
    """[{"group", "label", "value", "ok"?}] - "ok" False marks a problem."""
    provider = llm_provider._active_provider()
    manager_ok, manager_error = _check(lambda: target.client().test_connection())
    free = shutil.disk_usage(settings.data_dir if os.path.isdir(settings.data_dir) else "/")
    rows = [
        ("app", "Version", settings.app_version or "dev", None),
        ("app", "Recipe manager", f"{target.name()} – {target.base_url() or 'no URL set'}", None),
        ("app", "Reachable", "yes" if manager_ok else f"no: {manager_error}", manager_ok),
        ("app", "Password (APP_PASSWORD)", "set" if settings.app_password else "off", None),
        ("ai", "AI_PROVIDER", provider, None),
        ("ai", "Key / server", "set" if llm_provider.is_configured() else "missing", llm_provider.is_configured()),
        ("ai", "Main model", llm_provider._model(provider, tools=False), None),
        ("ai", "Tools model", llm_provider._model(provider, tools=True), None),
        ("ai", "Voice notes", llm_provider.audio_provider() or "not available", None),
        ("ai", "Image AI", (f"{settings.image_gen_provider}" if image_gen.is_configured()
                            else ("on, but no key" if settings.image_gen_enabled else "off")),
         None if not settings.image_gen_enabled else image_gen.is_configured()),
        ("ai", "Monthly budget", _budget(), None),
        ("ai", "IMPORT_CONFIRM", settings.import_confirm + (f" (from {settings.import_confirm_tokens:,} tokens)"
                                                         if settings.import_confirm == "large" else ""), None),
        ("lang", "OUTPUT_LANGUAGE", f"{settings.output_language} (interface: {get_ui_language_code(settings.output_language)})", None),
        ("lang", "TRANSLATE_RECIPES", str(app_settings.translating()).lower(), None),
        ("lang", "STEP_PHOTOS_AI", str(settings.step_photos_ai).lower(), None),
        ("lang", "SOURCE_TAG", str(settings.source_tag).lower(), None),
        ("lang", "CONVERT_TO_METRIC", str(settings.convert_to_metric).lower(), None),
        ("lang", "CUSTOM_INSTRUCTIONS", f"set ({len(settings.custom_instructions.strip())} characters)"
         if settings.custom_instructions.strip() else "none", None),
        ("system", "OCR", _ocr(), None),
        ("system", "Data folder", f"{settings.data_dir} – {free.free // 2**30} GB free", free.free > 2**30),
        ("ai", "UPLOAD_BATCH", str(settings.upload_batch).lower(), None),
        ("system", "Watched folder", (settings.watch_dir + (" (as AI batch, half price)" if settings.watch_batch else ""))
         if settings.watch_dir else "off", None),
        ("system", "Time zone (TZ)", os.environ.get("TZ") or time.strftime("%Z"), None),
    ]
    return [{"group": g, "label": label, "value": value, **({"ok": ok} if ok is not None else {})}
            for g, label, value, ok in rows]


def _budget() -> str:
    status = usage_log.budget_status()
    if not status.get("limit"):
        return "no limit"
    return f"{status.get('used', 0):,} of {status['limit']:,} tokens this month"


def ai_test() -> dict:
    """One tiny call to the main model: does the key work, how long does it take."""
    if not llm_provider.is_configured():
        return {"ok": False, "error": llm_provider.missing_key_hint()}
    started = time.monotonic()
    try:
        text, usage = llm_provider.complete_text(None, "Reply with exactly the word: OK", max_tokens=10)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "seconds": round(time.monotonic() - started, 1)}
    usage_log.record("diagnostics", getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
    return {"ok": True, "reply": (text or "").strip()[:60], "seconds": round(time.monotonic() - started, 1),
            "model": llm_provider._model(llm_provider._active_provider(), tools=False)}
