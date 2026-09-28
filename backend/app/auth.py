"""Optional password protection (APP_PASSWORD in .env; empty = off).

Once someone signs in, the browser keeps a cookie for a year - so on your
own phone you type the password once. The cookie holds an HMAC of the
password with a random secret from the data volume: changing the password
signs every device out.

Open without signing in: the login page itself, /api/login, /api/ping (the
container health check), and what the browser needs to show the login page
and install the app (style, manifest, icons). Everything else answers 401
(API) or redirects to /login (pages).

Wrong passwords are slowed down: after MAX_FAILURES failed attempts from
one address within LOCK_SECONDS, that address has to wait."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import settings

COOKIE = "th_auth"
COOKIE_MAX_AGE = 365 * 86400
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
OPEN_PATHS = {"/login", "/login.html", "/api/login", "/api/ping", "/logout", "/style.css", "/manifest.json",
              "/favicon.ico"}
OPEN_PREFIXES = ("/icons/",)

_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool(settings.app_password)


def _secret() -> bytes:
    path = os.path.join(settings.data_dir, "auth_secret")
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    os.makedirs(settings.data_dir, exist_ok=True)
    data = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(data)
    return data


def _token() -> str:
    return hmac.new(_secret(), settings.app_password.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_in(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE) or ""
    return bool(cookie) and hmac.compare_digest(cookie, _token())


def _client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "?")


def locked_for(request: Request) -> int:
    """Seconds this address still has to wait (0 = may try)."""
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(_client(request), []) if now - t < LOCK_SECONDS]
        _failures[_client(request)] = recent
        return int(LOCK_SECONDS - (now - recent[0])) + 1 if len(recent) >= MAX_FAILURES else 0


def check_password(request: Request, password: str) -> bool:
    ok = hmac.compare_digest((password or "").encode("utf-8"), settings.app_password.encode("utf-8"))
    if ok:
        with _lock:
            _failures.pop(_client(request), None)
    else:
        with _lock:
            _failures.setdefault(_client(request), []).append(time.time())
    return ok


def set_cookie(request: Request, response) -> None:
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    response.set_cookie(COOKIE, _token(), max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=https)


def _is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


async def middleware(request: Request, call_next):
    if not enabled() or _is_open(request.url.path) or signed_in(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Please sign in.", "login": True}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
