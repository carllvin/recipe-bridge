"""Push notifications to the phone via ntfy and/or Telegram (NOTIFY_* in
.env; nothing is sent without them):
- an import that came in without you (phone share, watched folder) is ready
  - or failed,
- automatic runs (maintenance, new recipes, post-processing) prepared
  suggestions - collected for a while, so a night's maintenance is one
  message, not ten,
- the monthly AI budget reached 80 % / is used up (once each per month).
Sending happens in the background; a failure is only logged."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

import httpx

from .config import get_ui_language_code, settings

log = logging.getLogger("tandoor-helper")

BATCH_SECONDS = 120

TEXT = {
    "de": {
        "import_ready": ("Import fertig", "{name}: {n} Rezept(e) warten unter Prüfen → Neue Importe."),
        "import_failed": ("Import fehlgeschlagen", "{name}: {error}"),
        "suggestions": ("Neue Vorschläge", "Automatische Läufe haben {n} Vorschlag/Vorschläge vorbereitet – sie warten unter Prüfen."),
        "budget_warn": ("KI-Budget fast verbraucht", "{pct} % des monatlichen KI-Budgets sind verbraucht."),
        "budget_exceeded": ("KI-Budget aufgebraucht", "Automatische Läufe pausieren bis zum Monatsende."),
        "test": ("Test", "Benachrichtigungen vom Tandoor Helper kommen an."),
    },
    "en": {
        "import_ready": ("Import ready", "{name}: {n} recipe(s) waiting under Review → New imports."),
        "import_failed": ("Import failed", "{name}: {error}"),
        "suggestions": ("New suggestions", "Automatic runs prepared {n} suggestion(s) - they wait under Review."),
        "budget_warn": ("AI budget almost used up", "{pct} % of the monthly AI budget is used."),
        "budget_exceeded": ("AI budget used up", "Automatic runs pause until the end of the month."),
        "test": ("Test", "Notifications from Tandoor Helper arrive."),
    },
    "fr": {
        "import_ready": ("Import terminé", "{name} : {n} recette(s) à vérifier sous Vérifier → Nouveaux imports."),
        "import_failed": ("Import échoué", "{name} : {error}"),
        "suggestions": ("Nouvelles suggestions", "Les tâches automatiques ont préparé {n} suggestion(s) – elles attendent sous Vérifier."),
        "budget_warn": ("Budget IA presque épuisé", "{pct} % du budget IA mensuel est utilisé."),
        "budget_exceeded": ("Budget IA épuisé", "Les tâches automatiques sont en pause jusqu'à la fin du mois."),
        "test": ("Test", "Les notifications de Tandoor Helper arrivent."),
    },
    "it": {
        "import_ready": ("Importazione pronta", "{name}: {n} ricetta/e da controllare in Verifica → Nuove importazioni."),
        "import_failed": ("Importazione non riuscita", "{name}: {error}"),
        "suggestions": ("Nuovi suggerimenti", "Le esecuzioni automatiche hanno preparato {n} suggerimento/i – aspettano in Verifica."),
        "budget_warn": ("Budget IA quasi esaurito", "È stato usato il {pct} % del budget IA mensile."),
        "budget_exceeded": ("Budget IA esaurito", "Le esecuzioni automatiche sono in pausa fino a fine mese."),
        "test": ("Test", "Le notifiche di Tandoor Helper arrivano."),
    },
    "es": {
        "import_ready": ("Importación lista", "{name}: {n} receta(s) por revisar en Revisar → Nuevas importaciones."),
        "import_failed": ("Importación fallida", "{name}: {error}"),
        "suggestions": ("Nuevas sugerencias", "Las ejecuciones automáticas prepararon {n} sugerencia(s) – esperan en Revisar."),
        "budget_warn": ("Presupuesto de IA casi agotado", "Se ha usado el {pct} % del presupuesto mensual de IA."),
        "budget_exceeded": ("Presupuesto de IA agotado", "Las ejecuciones automáticas se pausan hasta fin de mes."),
        "test": ("Test", "Las notificaciones de Tandoor Helper llegan."),
    },
}

_batch = {"count": 0, "timer": None}
_lock = threading.Lock()


def channels() -> list[str]:
    out = []
    if settings.notify_ntfy_url:
        out.append("ntfy")
    if settings.notify_telegram_token and settings.notify_telegram_chat_id:
        out.append("telegram")
    return out


def _text(key, **values) -> tuple[str, str]:
    lang = get_ui_language_code(settings.output_language)
    title, body = TEXT.get(lang, TEXT["en"])[key]
    return f"Tandoor Helper: {title}", body.format(**values)


def _send_ntfy(title, message):
    parsed = urlparse(settings.notify_ntfy_url.rstrip("/"))
    server, _, topic = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rpartition("/")
    body = {"topic": topic, "title": title, "message": message, "tags": ["fork_and_knife"]}
    if settings.app_url:
        body["click"] = settings.app_url
    headers = {"Authorization": f"Bearer {settings.notify_ntfy_token}"} if settings.notify_ntfy_token else {}
    # JSON publishing to the server root keeps umlauts and emoji intact
    resp = httpx.post(server or f"{parsed.scheme}://{parsed.netloc}", json=body, headers=headers, timeout=15)
    resp.raise_for_status()


def _send_telegram(title, message):
    text = f"{title}\n{message}" + (f"\n{settings.app_url}" if settings.app_url else "")
    resp = httpx.post(f"https://api.telegram.org/bot{settings.notify_telegram_token}/sendMessage",
                      json={"chat_id": settings.notify_telegram_chat_id, "text": text,
                            "disable_web_page_preview": True}, timeout=15)
    resp.raise_for_status()


SENDERS = {"ntfy": _send_ntfy, "telegram": _send_telegram}


def _redact(text: str) -> str:
    """The Telegram bot token is part of the URL - never show it in errors."""
    for secret in (settings.notify_telegram_token, settings.notify_ntfy_token):
        if secret:
            text = text.replace(secret, "***")
    return text


def send_now(title, message) -> dict:
    """Sends right away; {"sent": [channels], "errors": [texts]}."""
    sent, errors = [], []
    for channel in channels():
        try:
            SENDERS[channel](title, message)
            sent.append(channel)
        except Exception as exc:  # noqa: BLE001
            error = _redact(f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else str(exc))
            log.warning("Notification via %s failed: %s", channel, error)
            errors.append(f"{channel}: {error}")
    return {"sent": sent, "errors": errors}


def _background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def send(key, **values) -> None:
    if not channels():
        return
    title, message = _text(key, **values)
    _background(send_now, title, message)


def test() -> dict:
    if not channels():
        return {"sent": [], "errors": ["No channel configured (NOTIFY_* in .env)."]}
    return send_now(*_text("test"))


# ---------- events ----------

def import_finished(job) -> None:
    """For imports that came in without you at the screen."""
    if not job.source:
        return
    if job.status == "ready":
        send("import_ready", name=job.filename, n=len(job.recipes))
    elif job.status == "error":
        send("import_failed", name=job.filename, error=(job.error or "")[:300])


def suggestions_ready(count: int) -> None:
    """Collected for BATCH_SECONDS, then one message."""
    if count <= 0 or not channels():
        return
    with _lock:
        _batch["count"] += count
        if _batch["timer"] is None:
            _batch["timer"] = threading.Timer(BATCH_SECONDS, _flush)
            _batch["timer"].daemon = True
            _batch["timer"].start()


def _flush() -> None:
    with _lock:
        count, _batch["count"], _batch["timer"] = _batch["count"], 0, None
    if count:
        send("suggestions", n=count)


def _state_path() -> str:
    return os.path.join(settings.data_dir, "notify_state.json")


def budget_changed(status: dict) -> None:
    """Once per month and level (80 % / used up)."""
    if not status.get("limit") or not channels():
        return
    level = "budget_exceeded" if status["exceeded"] else "budget_warn" if status["warn"] else None
    if level is None:
        return
    month = time.strftime("%Y-%m")
    try:
        with open(_state_path(), encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    sent = state.get(month, [])
    if level in sent:
        return
    state = {month: sent + [level]}
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        return
    send(level, pct=min(100, round(status["used"] / status["limit"] * 100)))
