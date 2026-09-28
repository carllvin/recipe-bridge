from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import os
import shutil
import threading
import time
import uuid
from urllib.parse import urlparse, quote

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import app_settings, apply_queue, auth, cook_feedback, cook_today, seasonal, site_scan, tools_recipe_details, health, maintenance, undo, ignored, image_gen, import_matching, jobs, usage_log, watcher, recipe_restructure, tandoor_client, tool_jobs, tools_conversions, tools_meal_plan, tools_ingredients, tools_new_recipes, tools_recipes, tools_tags, tools_units
from .ai_extractor import extract_recipes_from_pages, guess_cookbook_title
from .config import settings, get_ui_language_code
from .epub_processor import SUPPORTED_EPUB_EXTENSIONS, process_epub
from .image_processor import SUPPORTED_IMAGE_EXTENSIONS, process_images
from .pdf_processor import process_pdf
from .url_processor import UrlImportError, links_from_text, process_url, validate_url
from .text_processor import looks_like_link_list, process_text, title_from_text
from .docx_processor import process_docx
from .schemas import ExtractedRecipe

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tandoor-helper")

app = FastAPI(title="Tandoor Helper")


@app.middleware("http")
async def revalidate_static_files(request, call_next):
    """The frontend (index.html, app.js, i18n.js, style.css) must always be
    revalidated: without this, a browser may keep an old i18n.js next to a
    new index.html after an update and show raw keys like
    'toolRecipesRestructureTitle'. StaticFiles answers revalidations with
    304 Not Modified (ETag), so this costs almost nothing."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Registered after the one above, so it runs first: nothing is served
# before the password check (see auth.py; off without APP_PASSWORD).
app.middleware("http")(auth.middleware)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

DUPLICATE_SIMILARITY_THRESHOLD = 0.82  # titles scoring at or above this (0-1) count as "similar"
CLEANUP_INTERVAL_SECONDS = 3600        # how often the background cleanup task runs
PDF_EXTENSIONS = {".pdf"}
TXT_EXTENSIONS = {".txt"}  # recipe links (one per line) or recipe text - decided by content
TEXT_EXTENSIONS = {".md", ".markdown"}
DOCX_EXTENSIONS = {".docx"}
MAX_SELECTED_LINKS = 200  # links picked from a site scan or bookmarks


def _job_dir(job_id: str) -> str:
    return os.path.join(settings.data_dir, job_id)


def _mark_duplicates(job) -> None:
    """Fetches all existing Tandoor recipe names once and flags recipes with an
    exact/very similar title match. Non-fatal: if the request fails (e.g. Tandoor
    not configured/reachable) it's simply skipped - the import itself doesn't
    depend on this."""
    if not settings.check_duplicates:
        return
    try:
        with tandoor_client.get_client() as client:
            existing_names = tandoor_client.fetch_all_recipe_names(client)
    except Exception as exc:  # noqa: BLE001
        log.info("Duplicate check skipped (Tandoor unreachable/not configured): %s", exc)
        return

    if not existing_names:
        return

    existing_lower = {n.strip().lower(): n for n in existing_names if n.strip()}

    for recipe in job.recipes:
        title_norm = recipe.title.strip().lower()
        if not title_norm:
            continue

        if title_norm in existing_lower:
            recipe.duplicate_match = existing_lower[title_norm]
            recipe.duplicate_exact = True
            recipe.selected = False  # deselect exact matches as a precaution
            continue

        best_ratio = 0.0
        best_name = None
        for norm, original in existing_lower.items():
            ratio = difflib.SequenceMatcher(None, title_norm, norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = original
        if best_ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            recipe.duplicate_match = best_name
            recipe.duplicate_exact = False
            # only a similar match: leave it selected, just warn


def _fetch_existing_tags() -> list[str] | None:
    """Fetches existing Tandoor keywords once, so the extraction prompt can ask the
    AI to prefer reusing them. Non-fatal: returns None if unavailable/disabled."""
    if not settings.reuse_existing_tags:
        return None
    try:
        with tandoor_client.get_client() as client:
            return tandoor_client.fetch_all_keyword_names(client)
    except Exception as exc:  # noqa: BLE001
        log.info("Tag reuse lookup skipped (Tandoor unreachable/not configured): %s", exc)
        return None


SINGLE_RECIPE_NOTE = (
    "NOTE FROM THE USER: the following {n} page(s) are photos of ONE single recipe that continues "
    "across the pages (e.g. the ingredients on one photo and the method on the next). Return exactly "
    "one recipe spanning all of them.\n\n"
)


def _run_extraction(job_id: str, source_paths: list[str], doc_type: str, force_ocr: bool = False,
                    single_recipe: bool = False) -> None:
    job = jobs.get_job(job_id)
    if job is None:
        return
    try:
        images_dir = os.path.join(_job_dir(job_id), "images")

        if doc_type == "pdf":
            job.progress_label = "Reading PDF …"
            jobs.save_job(job)
            result = process_pdf(source_paths[0], images_dir, force_ocr=force_ocr)
        elif doc_type == "epub":
            job.progress_label = "Reading EPUB …"
            jobs.save_job(job)
            result = process_epub(source_paths[0], images_dir)
        elif doc_type == "images":
            job.progress_label = "Running OCR on uploaded photo(s) …"
            jobs.save_job(job)
            result = process_images(source_paths, images_dir)
        elif doc_type == "url":
            job.progress_label = "Loading the recipe page …"
            jobs.save_job(job)
            result = process_url(source_paths[0], images_dir)
        elif doc_type == "txt":
            with open(source_paths[0], encoding="utf-8", errors="replace") as f:
                content = f.read()
            if looks_like_link_list(content):
                doc_type = "links"
                _run_link_list(job, links_from_text(content), images_dir)
                return
            doc_type = "text"
            result = process_text(content)
        elif doc_type == "links":  # links picked from a site scan or bookmarks (JSON list)
            with open(source_paths[0], encoding="utf-8") as f:
                _run_link_list(job, json.load(f)[:MAX_SELECTED_LINKS], images_dir)
            return
        elif doc_type == "text":
            job.progress_label = "Reading the text …"
            jobs.save_job(job)
            with open(source_paths[0], encoding="utf-8", errors="replace") as f:
                result = process_text(f.read())
        elif doc_type == "docx":
            job.progress_label = "Reading the Word document …"
            jobs.save_job(job)
            result = process_docx(source_paths[0], images_dir)
        else:
            raise ValueError(f"Unknown document type: {doc_type}")

        job.page_count = result["page_count"]
        job.images = {
            iid: {"page": info["page"], "filename": info["filename"]}
            for iid, info in result["images"].items()
        }
        jobs.save_job(job)

        def progress_cb(idx: int, total: int, page_start: int, page_end: int) -> None:
            job.progress_current = idx
            job.progress_total = total
            page_range = f"page {page_start}" if page_start == page_end else f"pages {page_start}-{page_end}"
            job.progress_label = f"Analyzed {page_range} ({idx}/{total})"
            jobs.save_job(job)

        existing_tags = _fetch_existing_tags()
        toc_pages = result.get("toc_pages") if settings.toc_aware_chunking else None
        if single_recipe and result["pages"]:
            first = result["pages"][0]
            first["text"] = SINGLE_RECIPE_NOTE.format(n=len(result["pages"])) + (first.get("text") or "")

        recipes: list[ExtractedRecipe]
        recipes, usage = extract_recipes_from_pages(
            result["pages"],
            on_progress=progress_cb,
            toc_pages=toc_pages,
            existing_tags=existing_tags,
        )
        if doc_type == "url":
            for recipe in recipes:
                recipe.source_url = source_paths[0]
        job.recipes = recipes
        job.token_usage.add(usage)
        jobs.match_images_to_recipes(job)

        if doc_type in ("url", "text"):
            # A web recipe or a pasted text doesn't belong to a cookbook by
            # default - the name field stays empty (the user can still type one).
            job.suggested_cookbook_name = None
            job.cookbook_name = None
        else:
            job.progress_label = "Determining cookbook title …"
            jobs.save_job(job)
            guess, title_usage = guess_cookbook_title(
                result["pages"], job.filename, metadata_title=result.get("metadata_title", "")
            )
            job.token_usage.add(title_usage)
            job.suggested_cookbook_name = guess
            job.cookbook_name = guess

        _finish_extraction(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Extraction failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
    finally:
        if doc_type != "links":  # _run_link_list records its own usage
            usage_log.record("import", job.token_usage.input_tokens, job.token_usage.output_tokens)
        jobs.save_job(job)


def _finish_extraction(job) -> None:
    """The steps after extraction shared by every import type."""
    job.progress_label = "Checking for duplicates already in Tandoor …"
    jobs.save_job(job)
    _mark_duplicates(job)

    job.progress_label = "Matching ingredients with Tandoor …"
    jobs.save_job(job)
    import_matching.match_job_ingredients(job)

    job.status = "ready"


def _run_link_list(job, links: list[str], images_dir: str) -> None:
    """Recipe links (a .txt file, or picked from a site scan / bookmarks):
    every link is loaded and extracted on its own (one web page = one AI
    call), and all recipes end up in one review. Each link's page number is
    its position in the list, which keeps the recipe's photo with it. Links
    that can't be loaded or contain no recipe are skipped and listed in
    job.notes."""
    try:
        if not links:
            raise ValueError("The text file contains no links (http:// or https://).")
        existing_tags = _fetch_existing_tags()
        job.progress_total = len(links)
        job.page_count = len(links)
        for number, url in enumerate(links, 1):
            job.progress_current = number
            job.progress_label = f"Link {number}/{len(links)}: {urlparse(url).netloc}"
            jobs.save_job(job)
            try:
                result = process_url(url, images_dir)
                for image_id, info in result["images"].items():
                    job.images[image_id] = {"page": number, "filename": info["filename"]}
                page = {"page": number, "text": result["pages"][0]["text"]}
                recipes, usage = extract_recipes_from_pages([page], existing_tags=existing_tags)
                job.token_usage.add(usage)
            except Exception as exc:  # noqa: BLE001 - one bad link must not stop the rest
                log.info("Link %s skipped: %s", url, exc)
                job.notes.append(f"{url} – {exc}")
                continue
            if not recipes:
                job.notes.append(f"{url} – no recipe found")
                continue
            for recipe in recipes:
                recipe.source_url = url
                recipe.source_page_start = recipe.source_page_end = number
            job.recipes.extend(recipes)
        if not job.recipes:
            raise ValueError("No recipe could be read from any of the links.")
        jobs.match_images_to_recipes(job)
        # Recipes from different websites don't belong to one cookbook by default.
        job.suggested_cookbook_name = job.cookbook_name = None
        _finish_extraction(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Link list import failed for job %s", job.id)
        job.status = "error"
        job.error = str(exc)
    finally:
        usage_log.record("import", job.token_usage.input_tokens, job.token_usage.output_tokens)
        jobs.save_job(job)


def _classify_upload(filenames: list[str]) -> tuple[str, str]:
    """Determines the document type from the uploaded filenames' extensions.
    Returns (doc_type, error_message) - error_message is '' when valid."""
    if not filenames:
        return "", "No file was uploaded."

    exts = [os.path.splitext(f)[1].lower() for f in filenames]

    if len(filenames) == 1 and exts[0] in PDF_EXTENSIONS:
        return "pdf", ""
    if len(filenames) == 1 and exts[0] in SUPPORTED_EPUB_EXTENSIONS:
        return "epub", ""
    if len(filenames) == 1 and exts[0] in TXT_EXTENSIONS:
        return "txt", ""
    if len(filenames) == 1 and exts[0] in TEXT_EXTENSIONS:
        return "text", ""
    if len(filenames) == 1 and exts[0] in DOCX_EXTENSIONS:
        return "docx", ""
    single_only = PDF_EXTENSIONS | SUPPORTED_EPUB_EXTENSIONS | TXT_EXTENSIONS | TEXT_EXTENSIONS | DOCX_EXTENSIONS
    if exts and all(e in SUPPORTED_IMAGE_EXTENSIONS for e in exts):
        return "images", ""
    if len(filenames) > 1 and any(e in single_only for e in exts):
        return "", "Only one document can be uploaded at a time (but multiple photos are fine)."

    supported = ", ".join(sorted(single_only | SUPPORTED_IMAGE_EXTENSIONS))
    return "", f"Unsupported file type. Supported: {supported}"


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), single_recipe: bool = Form(False)):
    """single_recipe: the uploaded photos all show one recipe (e.g. across a
    double page) - the AI is told to return exactly one."""
    _check_budget()
    filenames = [f.filename or "" for f in files]
    doc_type, error = _classify_upload(filenames)
    if error:
        raise HTTPException(400, error)

    job = jobs.create_job(_display_name(filenames))
    source_paths = await _save_uploads(files, _job_dir(job.id))
    _launch_extraction(job, source_paths, doc_type, single_recipe)
    return {"job_id": job.id}


def _display_name(filenames: list[str]) -> str:
    return filenames[0] if len(filenames) == 1 else f"{len(filenames)} photos"


async def _save_uploads(files, job_dir: str) -> list[str]:
    """Streams uploaded files into the job folder (within MAX_UPLOAD_MB)."""
    os.makedirs(job_dir, exist_ok=True)
    source_paths = []
    max_bytes = settings.max_upload_mb * 1024 * 1024
    total_size = 0
    for index, upload in enumerate(files):
        ext = os.path.splitext(upload.filename or "")[1].lower()
        dest_path = os.path.join(job_dir, f"source_{index:03d}{ext}")
        with open(dest_path, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                total_size += len(chunk)
                if total_size > max_bytes:
                    out.close()
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(413, f"Upload larger than {settings.max_upload_mb} MB.")
                out.write(chunk)
        source_paths.append(dest_path)
    return source_paths


def _launch_extraction(job, source_paths: list[str], doc_type: str, single_recipe: bool = False) -> None:
    jobs.save_job(job)
    threading.Thread(target=_run_extraction, args=(job.id, source_paths, doc_type, settings.force_ocr,
                                                    single_recipe and doc_type == "images"), daemon=True).start()


def _start_url_job(url: str, source: str | None = None):
    """Raises HTTPException(400) for an invalid address."""
    try:
        url = validate_url(url)
    except UrlImportError as exc:
        raise HTTPException(400, str(exc))
    job = jobs.create_job(urlparse(url).netloc or url)
    job.source = source
    os.makedirs(_job_dir(job.id), exist_ok=True)
    jobs.save_job(job)
    threading.Thread(target=_run_extraction, args=(job.id, [url], "url"), daemon=True).start()
    return job


def _start_text_job(text: str, source: str | None = None):
    text = (text or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "Please paste a recipe text.")
    job = jobs.create_job(title_from_text(text))
    job.source = source
    os.makedirs(_job_dir(job.id), exist_ok=True)
    path = os.path.join(_job_dir(job.id), "source.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    jobs.save_job(job)
    threading.Thread(target=_run_extraction, args=(job.id, [path], "text"), daemon=True).start()
    return job


@app.post("/api/import-url")
async def import_url(body: dict = Body(...)):
    """Imports a single recipe from a web page - same extraction, review
    and import flow as an uploaded document (see url_processor)."""
    _check_budget()
    return {"job_id": _start_url_job(body.get("url", "")).id}


@app.post("/api/import-text")
async def import_text(body: dict = Body(...)):
    """Pasted recipe text (e.g. from a message or a note) - same extraction,
    review and import flow as a document."""
    _check_budget()
    return {"job_id": _start_text_job(body.get("text") or "").id}


@app.post("/share")
async def share_target(request: Request):
    """Web Share Target of the installed app (see static/manifest.json): a
    page, text, photos or a file shared from another app on the phone.
    Starts the matching import and opens the app on it."""
    form = await request.form()
    files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    fields = {k: str(form.get(k) or "").strip() for k in ("title", "text", "url")}
    try:
        _check_budget()
        if files:
            filenames = [f.filename for f in files]
            doc_type, error = _classify_upload(filenames)
            if error:
                raise HTTPException(400, error)
            job = jobs.create_job(_display_name(filenames))
            job.source = "share"
            _launch_extraction(job, await _save_uploads(files, _job_dir(job.id)), doc_type)
        else:
            links = links_from_text(" ".join((fields["url"], fields["text"], fields["title"])))
            if links:
                job = _start_url_job(links[0], "share")
            else:
                job = _start_text_job("\n\n".join(v for v in (fields["title"], fields["text"]) if v), "share")
    except HTTPException as exc:
        return RedirectResponse(f"/?error={quote(str(exc.detail))}", status_code=303)
    return RedirectResponse(f"/?job={job.id}", status_code=303)


def _start_folder_import(files: list[str], name: str) -> str:
    """Watched folder (see watcher.py): copies the files into a new job and
    starts it. ValueError when the files can't be imported."""
    doc_type, error = _classify_upload([os.path.basename(f) for f in files])
    if error:
        raise ValueError(error)
    job = jobs.create_job(name if len(files) > 1 else os.path.basename(files[0]))
    job.source = "folder"
    job_dir = _job_dir(job.id)
    os.makedirs(job_dir, exist_ok=True)
    paths = []
    for index, src in enumerate(files):
        dest = os.path.join(job_dir, f"source_{index:03d}{os.path.splitext(src)[1].lower()}")
        shutil.copyfile(src, dest)
        paths.append(dest)
    _launch_extraction(job, paths, doc_type)
    return job.id


@app.post("/api/import-links")
async def import_links(body: dict = Body(...)):
    """{"urls": [...], "name"?} - links picked from a site scan or browser
    bookmarks, imported like a .txt link list."""
    _check_budget()
    urls = []
    for url in body.get("urls") or []:
        try:
            url = validate_url(str(url))
        except UrlImportError:
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        raise HTTPException(400, "No valid links selected.")
    job = jobs.create_job((body.get("name") or f"{len(urls)} links")[:80])
    os.makedirs(_job_dir(job.id), exist_ok=True)
    path = os.path.join(_job_dir(job.id), "links.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(urls[:MAX_SELECTED_LINKS], f)
    jobs.save_job(job)
    threading.Thread(target=_run_extraction, args=(job.id, [path], "links"), daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/scan")
async def start_site_scan(body: dict = Body(...)):
    """Scans a website for recipe pages (no AI) - poll GET /api/scan/{id}."""
    try:
        return {"scan_id": site_scan.start((body.get("url") or "").strip(), body.get("depth") or site_scan.DEFAULT_DEPTH)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/scan/{scan_id}")
async def get_site_scan(scan_id: str):
    state = site_scan.get(scan_id)
    if state is None:
        raise HTTPException(404, "Scan not found.")
    return state


@app.post("/api/scan/{scan_id}/cancel")
async def cancel_site_scan(scan_id: str):
    site_scan.cancel(scan_id)
    return site_scan.get(scan_id) or {}


@app.get("/api/jobs")
async def list_import_jobs():
    """Recent imports (newest first) for the import page. Import jobs live in
    memory, so this covers the time since the last restart."""
    items = []
    for job in sorted(jobs.list_jobs(), key=lambda j: -j.created_at)[:15]:
        items.append({
            "id": job.id, "filename": job.filename, "status": job.status, "created_at": job.created_at,
            "recipes": len(job.recipes),
            "imported": sum(1 for r in job.recipes if r.import_status == "imported"),
        })
    return items


@app.get("/api/usage")
async def token_usage(days: int = 30):
    return {**usage_log.summary(max(1, min(days, 365))), "budget": usage_log.budget_status()}


@app.get("/api/settings")
async def get_app_settings():
    return {**app_settings.get(), "budget_status": usage_log.budget_status(),
            "maintenance_status": maintenance.status(), "metrics": app_settings.MAINTENANCE_METRICS}


@app.put("/api/settings")
async def put_app_settings(body: dict = Body(...)):
    try:
        app_settings.update(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid settings: {exc}")
    return await get_app_settings()


@app.post("/api/maintenance/run")
async def run_maintenance_now():
    if not maintenance.start_now():
        raise HTTPException(409, "A maintenance run is already going.")
    return maintenance.status()


@app.get("/api/inbox")
async def inbox():
    """Every pending suggestion of every run, for the review inbox - plus the
    runs that are still scanning."""
    items, running = [], []
    queued = apply_queue.queued_keys()
    for job in sorted(tool_jobs.list_all_tool_jobs(), key=lambda j: j.created_at):
        if job.status == "scanning":
            running.append({"id": job.id, "tool": job.tool, "created_at": job.created_at,
                            "label": job.progress_label, "trigger": job.meta.get("trigger")})
            continue
        for s in job.suggestions:
            if s.status not in ("pending", "error"):
                continue
            items.append({
                "job_id": job.id, "tool": job.tool, "job_created_at": job.created_at,
                "trigger": job.meta.get("trigger"), "auto": bool(job.meta.get("auto")),
                "id": s.id, "kind": s.kind, "entity": s.detail.get("entity"),
                "summary": s.summary, "preview": s.preview, "queued": (job.id, s.id) in queued,
                # Failed ones stay visible until retried or dismissed.
                "failed": s.status == "error", "error": s.error,
                "retryable": s.status == "error" and _retryable(job, s),
            })
    imports = _pending_imports()
    return {"items": items, "running": running, "count": len(items) + len(imports), "queued": len(queued),
            "imports": imports}


def _pending_imports() -> list[dict]:
    """Imports that arrived without you at the screen (shared from the phone,
    dropped into the watched folder) and still have recipes to review."""
    out = []
    for job in sorted(jobs.list_jobs(), key=lambda j: j.created_at):
        if not job.source or job.status not in ("ready", "processing", "error"):
            continue
        pending = sum(1 for r in job.recipes if r.import_status in ("pending", "error"))
        if job.status == "ready" and not pending:
            continue
        out.append({"job_id": job.id, "filename": job.filename, "source": job.source, "status": job.status,
                    "error": job.error,
                    "recipes": pending, "titles": [r.title for r in job.recipes if r.import_status != "imported"][:5],
                    "created_at": job.created_at})
    return out


@app.post("/api/jobs/{job_id}/dismiss")
async def dismiss_import(job_id: str):
    """Removes an import from the review inbox (the job itself stays)."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    job.source = None
    jobs.save_job(job)
    return {"ok": True}


def _retryable(job, suggestion) -> bool:
    # The new-recipes run translates right away - there's no apply step to
    # repeat for a failed translation (the next run tries again).
    return not (job.tool == "new_recipes" and suggestion.kind == "translate_recipe")


@app.get("/api/health")
async def health_overview():
    return health.cached()


@app.post("/api/health/refresh")
async def health_refresh():
    health.start_refresh()
    return health.cached()


@app.get("/api/health/items/{metric}")
async def health_items(metric: str):
    try:
        return health.items(metric)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/health/ignore")
async def health_ignore(body: dict = Body(...)):
    """{"metric": ..., "items": [{"key", "name"}]} - ignore these entries."""
    try:
        ignored.add(body.get("metric", ""), body.get("items") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return health.cached()


@app.post("/api/health/unignore")
async def health_unignore(body: dict = Body(...)):
    """{"metric": ..., "keys": [...]} - count these entries again."""
    ignored.remove(body.get("metric", ""), body.get("keys") or [])
    return health.cached()


@app.get("/api/inbox/count")
async def inbox_count():
    count = sum(
        1 for job in tool_jobs.list_all_tool_jobs() if job.status != "scanning"
        for s in job.suggestions if s.status in ("pending", "error")
    )
    running = sum(1 for job in tool_jobs.list_all_tool_jobs() if job.status == "scanning")
    return {"count": count + sum(1 for i in _pending_imports() if i["status"] != "processing"), "running": running}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job.model_dump()


@app.get("/api/jobs/{job_id}/images/{image_id}")
async def get_job_image(job_id: str, image_id: str):
    job = jobs.get_job(job_id)
    if job is None or image_id not in job.images:
        raise HTTPException(404, "Image not found.")
    filename = job.images[image_id]["filename"]
    path = os.path.join(_job_dir(job_id), "images", filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Image file is missing.")
    return FileResponse(path)


@app.put("/api/jobs/{job_id}/recipes/{recipe_id}")
async def update_recipe(job_id: str, recipe_id: str, payload: dict = Body(...)):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    for i, r in enumerate(job.recipes):
        if r.id == recipe_id:
            # Validate, not model_copy(update=...): that would store edited
            # ingredients/steps as plain dicts, which the import can't read.
            updated = ExtractedRecipe.model_validate({**r.model_dump(), **payload})
            job.recipes[i] = updated
            jobs.save_job(job)
            return updated.model_dump()
    raise HTTPException(404, "Recipe not found.")


@app.put("/api/jobs/{job_id}")
async def update_job(job_id: str, payload: dict = Body(...)):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    allowed_fields = {"cookbook_name"}
    for key, value in payload.items():
        if key in allowed_fields:
            setattr(job, key, value)
    jobs.save_job(job)
    return job.model_dump()


@app.post("/api/jobs/{job_id}/import")
async def import_selected(job_id: str, body: dict = Body(default={})):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    recipe_ids = body.get("recipe_ids")  # None = import all selected recipes
    to_import = [
        r for r in job.recipes
        if (recipe_ids is None and r.selected) or (recipe_ids is not None and r.id in recipe_ids)
    ]

    cookbook_name = (body.get("cookbook_name") or job.cookbook_name or "").strip()
    if cookbook_name:
        job.cookbook_name = cookbook_name
        jobs.save_job(job)

    results = []
    cookbook_warning = None

    try:
        with tandoor_client.get_client() as client:
            cookbook_id = None
            cookbook_endpoint = None
            if cookbook_name:
                try:
                    cookbook_id, cookbook_endpoint = tandoor_client.get_or_create_cookbook(client, cookbook_name)
                except tandoor_client.TandoorError as exc:
                    cookbook_warning = str(exc)
                    log.warning("Could not create cookbook: %s", exc)

            for recipe in to_import:
                recipe.import_status = "importing"
                jobs.save_job(job)

                image_path = None
                if recipe.selected_image_id and recipe.selected_image_id in job.images:
                    image_path = os.path.join(
                        _job_dir(job_id), "images", job.images[recipe.selected_image_id]["filename"]
                    )

                warnings: list[str] = []
                try:
                    tandoor_id = tandoor_client.create_recipe(client, recipe)
                    recipe.tandoor_recipe_id = tandoor_id

                    if image_path and os.path.exists(image_path):
                        try:
                            tandoor_client.upload_image(client, tandoor_id, image_path)
                        except tandoor_client.TandoorError as exc:
                            warnings.append(f"Image upload failed: {exc}")

                    if cookbook_id is not None:
                        try:
                            tandoor_client.add_recipe_to_cookbook(client, cookbook_id, cookbook_endpoint, tandoor_id)
                        except tandoor_client.TandoorError as exc:
                            warnings.append(str(exc))

                    recipe.import_status = "imported"
                    recipe.import_error = "; ".join(warnings) if warnings else None
                except Exception as exc:  # noqa: BLE001
                    recipe.import_status = "error"
                    recipe.import_error = str(exc)
                    log.warning("Import failed for recipe %s: %s", recipe.title, exc)

                results.append({
                    "id": recipe.id,
                    "status": recipe.import_status,
                    "tandoor_recipe_id": recipe.tandoor_recipe_id,
                    "error": recipe.import_error,
                })
                jobs.save_job(job)

    except tandoor_client.TandoorError as exc:
        # e.g. TANDOOR_URL/TANDOOR_TOKEN missing entirely -> mark all affected recipes as failed
        for recipe in to_import:
            recipe.import_status = "error"
            recipe.import_error = str(exc)
            results.append({
                "id": recipe.id, "status": "error",
                "tandoor_recipe_id": None, "error": str(exc),
            })
        jobs.save_job(job)

    # Straight into post-processing (nutrition, conversions, matching, tags
    # ...) - suggestions then wait under Tools. Never blocks the import.
    post_processing_job_id = None
    imported_ids = [r["tandoor_recipe_id"] for r in results if r["status"] == "imported" and r["tandoor_recipe_id"]]
    if imported_ids:
        health.mark_changed()
        try:
            post_processing_job_id = await asyncio.to_thread(tools_new_recipes.start_after_import, imported_ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not start post-processing after import: %s", exc)

    return {"results": results, "cookbook_name": cookbook_name or None, "cookbook_warning": cookbook_warning,
            "post_processing_job_id": post_processing_job_id}


@app.post("/api/jobs/{job_id}/recipes/{recipe_id}/undo-import")
async def undo_import(job_id: str, recipe_id: str):
    """Deletes a previously imported recipe from Tandoor again and resets its
    status to 'pending' here, so it can be edited and re-imported if desired."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    recipe = next((r for r in job.recipes if r.id == recipe_id), None)
    if recipe is None:
        raise HTTPException(404, "Recipe not found.")
    if recipe.import_status != "imported" or not recipe.tandoor_recipe_id:
        raise HTTPException(400, "This recipe hasn't been imported (or was already undone).")

    try:
        with tandoor_client.get_client() as client:
            tandoor_client.delete_recipe(client, recipe.tandoor_recipe_id)
    except tandoor_client.TandoorError as exc:
        raise HTTPException(502, f"Could not undo the import: {exc}")

    recipe.import_status = "pending"
    recipe.tandoor_recipe_id = None
    recipe.import_error = None
    jobs.save_job(job)

    return recipe.model_dump()


@app.post("/api/jobs/{job_id}/undo-all-imports")
async def undo_all_imports(job_id: str):
    """Bulk version of undo-import: deletes every imported recipe in this job
    from Tandoor again and resets each back to 'pending'. Recipes that failed
    to undo keep their 'imported' status so nothing is silently lost."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    imported = [r for r in job.recipes if r.import_status == "imported" and r.tandoor_recipe_id]
    if not imported:
        raise HTTPException(400, "No imported recipes to undo in this job.")

    results = []
    try:
        with tandoor_client.get_client() as client:
            for recipe in imported:
                try:
                    tandoor_client.delete_recipe(client, recipe.tandoor_recipe_id)
                    recipe.import_status = "pending"
                    recipe.tandoor_recipe_id = None
                    recipe.import_error = None
                    results.append({"id": recipe.id, "status": "undone", "error": None})
                except tandoor_client.TandoorError as exc:
                    results.append({"id": recipe.id, "status": "error", "error": str(exc)})
    except tandoor_client.TandoorError as exc:
        raise HTTPException(502, f"Could not undo imports: {exc}")

    jobs.save_job(job)
    return {"results": results}


@app.post("/api/jobs/{job_id}/recipes/{recipe_id}/generate-image")
async def generate_recipe_image(job_id: str, recipe_id: str):
    """Generates an AI recipe photo (for recipes with no photo from the source
    document, or simply as an alternative) and adds it as a candidate image."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    recipe = next((r for r in job.recipes if r.id == recipe_id), None)
    if recipe is None:
        raise HTTPException(404, "Recipe not found.")

    if not image_gen.is_configured():
        raise HTTPException(400, image_gen.missing_key_hint())

    prompt = image_gen.build_recipe_image_prompt(recipe.title, recipe.description, recipe.tags)
    try:
        image_bytes = image_gen.generate_image(prompt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Image generation failed: {exc}")

    images_dir = os.path.join(_job_dir(job_id), "images")
    os.makedirs(images_dir, exist_ok=True)
    image_id = uuid.uuid4().hex[:12]
    filename = f"{image_id}.png"
    out_path = os.path.join(images_dir, filename)
    with open(out_path, "wb") as f:
        f.write(image_bytes)

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.width, img.height
    except Exception:  # noqa: BLE001
        width, height = 0, 0

    job.images[image_id] = {"page": recipe.source_page_start, "filename": filename}
    recipe.candidate_image_ids = list(recipe.candidate_image_ids) + [image_id]
    recipe.selected_image_id = image_id
    jobs.save_job(job)

    return {"image_id": image_id, "recipe": recipe.model_dump()}


# ---------- Maintenance tools (ingredients/tags/units cleanup against live Tandoor data) ----------

# tool name -> (job kind saved on the ToolJob, the scan function to run in a background thread)
_TOOL_SCANS = {
    "ingredients_review": tools_ingredients.run_scan,
    "ingredients_enrich": tools_ingredients.run_enrich_scan,
    "tags_cleanup": tools_tags.run_cleanup_scan,
    "tags_simplify": tools_tags.run_simplify_scan,
    "tags_translate": tools_tags.run_translate_scan,
    "tags_season": tools_tags.run_season_scan,
    "tags_suggest_more": tools_tags.run_suggest_more_scan,
    "units_review": tools_units.run_scan,
    "recipes_translate": tools_recipes.run_scan,
    "new_recipes": tools_new_recipes.run_scan,
    "conversions": tools_conversions.run_scan,
    "recipes_restructure": recipe_restructure.run_scan,
    "meal_plan": tools_meal_plan.run_scan,
    "recipes_servings": tools_recipe_details.run_servings_scan,
    "recipes_images": tools_recipe_details.run_images_scan,
}

# tool name -> the apply_suggestion(job_id, suggestion_id) function for that tool
_TOOL_APPLY = {
    "ingredients_review": tools_ingredients.apply_suggestion,
    "ingredients_enrich": tools_ingredients.apply_enrich_suggestion,
    "tags_cleanup": tools_tags.apply_suggestion,
    "tags_simplify": tools_tags.apply_suggestion,
    "tags_translate": tools_tags.apply_suggestion,
    "tags_season": tools_tags.apply_suggestion,
    "tags_suggest_more": tools_tags.apply_suggestion,
    "units_review": tools_units.apply_suggestion,
    "recipes_translate": tools_recipes.apply_suggestion,
    "new_recipes": tools_new_recipes.apply_suggestion,
    "conversions": tools_conversions.apply_suggestion,
    "recipes_restructure": recipe_restructure.apply_suggestion,
    "meal_plan": tools_meal_plan.apply_suggestion,
    "recipes_servings": tools_recipe_details.apply_servings_suggestion,
    "recipes_images": tools_recipe_details.apply_image_suggestion,
}


def _check_budget() -> None:
    """Refuses a manual start when the monthly AI budget is used up and the
    settings say manual starts are blocked too."""
    if not usage_log.manual_runs_allowed():
        raise HTTPException(409, "The monthly AI budget is used up (see Maintain → Automation & budget).")


def _start_tool_job(tool: str, meta: dict | None = None):
    _check_budget()
    job = tool_jobs.create_tool_job(tool)
    if meta:
        job.meta.update(meta)
        tool_jobs.save_tool_job(job)
    threading.Thread(target=_TOOL_SCANS[tool], args=(job.id,), daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/tools/ingredients/review")
async def start_ingredients_review(body: dict | None = Body(None)):
    """Optional body {"focus": "duplicates"}: only the likely duplicates
    listed in the health overview instead of every entry."""
    focus = (body or {}).get("focus")
    return _start_tool_job("ingredients_review", {"focus": focus} if focus == "duplicates" else None)


@app.post("/api/tools/ingredients/enrich")
async def start_ingredients_enrich():
    return _start_tool_job("ingredients_enrich")


@app.post("/api/tools/tags/cleanup")
async def start_tags_cleanup():
    return _start_tool_job("tags_cleanup")


@app.post("/api/tools/tags/simplify")
async def start_tags_simplify():
    return _start_tool_job("tags_simplify")


@app.post("/api/tools/tags/translate")
async def start_tags_translate():
    return _start_tool_job("tags_translate")


@app.post("/api/tools/tags/season")
async def start_tags_season():
    return _start_tool_job("tags_season")


@app.post("/api/tools/tags/suggest-more")
async def start_tags_suggest_more():
    return _start_tool_job("tags_suggest_more")


@app.post("/api/tools/units/review")
async def start_units_review(body: dict | None = Body(None)):
    """Optional body {"focus": "duplicates"}: only the likely duplicates
    listed in the health overview instead of every entry."""
    focus = (body or {}).get("focus")
    return _start_tool_job("units_review", {"focus": focus} if focus == "duplicates" else None)


@app.post("/api/tools/recipes/servings")
async def start_recipes_servings():
    return _start_tool_job("recipes_servings")


@app.post("/api/tools/recipes/images")
async def start_recipes_images():
    return _start_tool_job("recipes_images")


@app.post("/api/tools/recipes/translate")
async def start_recipes_translate():
    return _start_tool_job("recipes_translate")


@app.get("/api/cook-today")
async def cook_today_search(have: str = ""):
    return await asyncio.to_thread(cook_today.suggest, have)


@app.get("/api/season")
async def in_season_now():
    """Local fruit and vegetables in season this month (for the plan area)."""
    return {"month": time.localtime().tm_mon, "produce": seasonal.display_names(seasonal.in_season())}


@app.post("/api/cook-today/plan")
async def cook_today_plan(body: dict = Body(...)):
    """{"recipe": {"id", "name"}, "meal_type": {"id", "name"}, "date"?, "add_to_shopping"?} -
    puts the recipe on today's (or the given day's) meal plan."""
    recipe, meal_type = body.get("recipe") or {}, body.get("meal_type") or {}
    if not recipe.get("id") or not meal_type.get("id"):
        raise HTTPException(400, "Recipe and meal type are required.")
    date = body.get("date") or time.strftime("%Y-%m-%d")

    def create():
        with tandoor_client.get_client() as client:
            return tools_meal_plan.create_plan_entry(client, recipe, date, meal_type, bool(body.get("add_to_shopping")))
    try:
        await asyncio.to_thread(create)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))
    return {"ok": True, "date": date}


@app.get("/api/cooked/pending")
async def cooked_pending():
    try:
        return {"items": await asyncio.to_thread(cook_feedback.pending)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))


@app.post("/api/cooked")
async def cooked_answer(body: dict = Body(...)):
    """{"plan_id", "recipe_id", "date", "servings", "rating": 1-5 | null (not cooked)}"""
    try:
        await asyncio.to_thread(cook_feedback.answer, body["plan_id"], int(body["recipe_id"]), body["date"],
                                body.get("servings"), body.get("rating"))
    except KeyError as exc:
        raise HTTPException(400, f"Missing field {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))
    health.mark_changed()
    return {"ok": True}


@app.get("/api/tools/meal-plan/options")
async def meal_plan_options():
    try:
        return await asyncio.to_thread(tools_meal_plan.options)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc), "meal_types": []}, status_code=200)


@app.post("/api/tools/meal-plan")
async def start_meal_plan(body: dict = Body(...)):
    meal_type = body.get("meal_type") or {}
    if not meal_type.get("id"):
        raise HTTPException(400, "Please choose a meal type.")
    _check_budget()
    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {
        "start_date": body.get("start_date"),
        "days": body.get("days") or 7,
        "meal_type": {"id": meal_type["id"], "name": meal_type.get("name", "")},
        "wishes": (body.get("wishes") or "")[:500],
        "add_to_shopping": bool(body.get("add_to_shopping")),
    }
    tool_jobs.save_tool_job(job)
    threading.Thread(target=tools_meal_plan.run_scan, args=(job.id,), daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/tools/meal-plan/{job_id}/reroll")
async def meal_plan_reroll(job_id: str, body: dict = Body(...)):
    try:
        job = await asyncio.to_thread(tools_meal_plan.reroll_day, job_id, body.get("date", ""))
    except tandoor_client.TandoorError as exc:
        raise HTTPException(400, str(exc))
    return job.model_dump()


@app.post("/api/tools/recipes/restructure")
async def start_recipes_restructure():
    return _start_tool_job("recipes_restructure")


@app.post("/api/tools/conversions")
async def start_conversions():
    return _start_tool_job("conversions")


@app.get("/api/tools/new-recipes/status")
async def new_recipes_status():
    """Number of recipes added since the last run. On the very first call
    this records every existing recipe as already handled (the baseline)."""
    try:
        return await asyncio.to_thread(tools_new_recipes.status)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=200)


@app.post("/api/tools/new-recipes/process")
async def start_new_recipes():
    return _start_tool_job("new_recipes")


@app.get("/api/tools/jobs")
async def list_open_tool_jobs():
    """Runs that still have suggestions to review (newest first) - so they
    can be reopened after a reload or a container restart."""
    open_jobs = [
        job for job in tool_jobs.list_all_tool_jobs()
        if job.status in ("ready", "cancelled", "scanning")
        and (job.status == "scanning" or any(s.status == "pending" for s in job.suggestions))
    ]
    return [
        {"id": job.id, "tool": job.tool, "status": job.status, "created_at": job.created_at,
         "pending": sum(1 for s in job.suggestions if s.status == "pending")}
        for job in sorted(open_jobs, key=lambda j: -j.created_at)
    ]


@app.get("/api/tools/jobs/{job_id}")
async def get_tool_job(job_id: str):
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise HTTPException(404, "Tool job not found.")
    queued = apply_queue.queued_keys()
    return {**job.model_dump(), "queued_ids": [s.id for s in job.suggestions if (job.id, s.id) in queued]}


@app.post("/api/tools/jobs/{job_id}/cancel")
async def cancel_tool_job(job_id: str):
    """Cooperative cancel: just sets a flag on the job. The running scan loop
    (see tool_jobs.check_cancelled) checks it after each chunk/item and stops
    itself there - a Python thread can't be killed from the outside, and
    stopping mid-AI-call would risk leaving the job in a half-written state,
    so this only takes effect at the next safe checkpoint rather than
    instantly."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise HTTPException(404, "Tool job not found.")
    if job.status == "scanning":
        job.cancel_requested = True
        tool_jobs.save_tool_job(job)
    return job.model_dump()


def _perform_suggestion_action(job_id: str, suggestion_id: str, action: str):
    """Applies or skips one suggestion - used by the endpoints below and by
    the background queue (apply_queue). Raises LookupError if the run or
    suggestion is gone."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise LookupError("Tool job not found.")
    if action == "retry":
        # A failed suggestion goes back to pending and is applied again.
        suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
        if suggestion is None:
            raise LookupError("Suggestion not found.")
        if suggestion.status == "error" and _retryable(job, suggestion):
            suggestion.status, suggestion.error = "pending", None
            tool_jobs.save_tool_job(job)
        action = "apply"
    if action == "apply":
        apply_fn = _TOOL_APPLY.get(job.tool)
        if apply_fn is None:
            raise LookupError(f"Unknown tool: {job.tool}")
        before = next((s.status for s in job.suggestions if s.id == suggestion_id), None)
        with undo.recording() as journal:
            suggestion = apply_fn(job_id, suggestion_id)
        if suggestion.status == "applied" and before != "applied":
            _remember_applied(job, suggestion, journal)
            health.mark_changed()
        return suggestion
    if action == "undo":
        return _undo_suggestion(job, suggestion_id)
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise LookupError("Suggestion not found.")
    if suggestion.status in ("pending", "error"):  # also dismisses a failed one
        suggestion.status = "skipped"
        tool_jobs.save_tool_job(job)
        tools_new_recipes.after_action(job)
    return suggestion


def _remember_applied(job, suggestion, journal) -> None:
    suggestion.applied_at = time.time()
    if journal:
        undo.save(job.id, suggestion.id, journal)
        suggestion.undoable = True
    tool_jobs.save_tool_job(job)


def _undo_suggestion(job, suggestion_id):
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise LookupError("Suggestion not found.")
    if suggestion.status != "applied" or not suggestion.undoable:
        return suggestion
    try:
        with tandoor_client.get_client() as client:
            undo.revert(client, job.id, suggestion.id)
    except Exception as exc:  # noqa: BLE001
        suggestion.error = f"Undo failed: {exc}"
        tool_jobs.save_tool_job(job)
        raise
    suggestion.status, suggestion.undoable, suggestion.error = "undone", False, None
    tool_jobs.save_tool_job(job)
    health.mark_changed()
    return suggestion


apply_queue.configure(_perform_suggestion_action)


@app.post("/api/tools/jobs/{job_id}/suggestions/{suggestion_id}/apply")
async def apply_tool_suggestion(job_id: str, suggestion_id: str):
    try:
        suggestion = await asyncio.to_thread(_perform_suggestion_action, job_id, suggestion_id, "apply")
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return suggestion.model_dump()


@app.post("/api/tools/jobs/{job_id}/suggestions/{suggestion_id}/skip")
async def skip_tool_suggestion(job_id: str, suggestion_id: str):
    try:
        suggestion = _perform_suggestion_action(job_id, suggestion_id, "skip")
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return suggestion.model_dump()


@app.get("/api/history")
async def applied_history():
    """Applied changes that can still be undone, newest first."""
    queued = apply_queue.queued_keys()
    items = [
        {"job_id": job.id, "tool": job.tool, "id": s.id, "kind": s.kind, "summary": s.summary,
         "applied_at": s.applied_at, "error": s.error, "queued": (job.id, s.id) in queued}
        for job in tool_jobs.list_all_tool_jobs()
        for s in job.suggestions
        if s.status == "applied" and s.undoable and undo.exists(job.id, s.id)
    ]
    items.sort(key=lambda i: -(i["applied_at"] or 0))
    return {"items": items[:300], "retention_days": undo.RETENTION_DAYS}


@app.post("/api/tools/actions")
async def queue_suggestion_actions(body: dict = Body(...)):
    """{"action": "apply"|"skip", "items": [{"job_id", "id"}, ...]} - done in
    the background, one after another, even if the page is closed."""
    try:
        batch_id = apply_queue.enqueue(body.get("action", ""), body.get("items") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"batch_id": batch_id, **apply_queue.status(batch_id)}


@app.get("/api/tools/actions")
async def suggestion_actions_status(batch: str | None = None):
    return apply_queue.status(batch)


@app.get("/api/ping")
async def ping():
    """Open without signing in: the container health check, and the login
    page's language."""
    return {"ok": True, "language_code": get_ui_language_code(settings.output_language)}


@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"), headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(request: Request, body: dict = Body(...)):
    if not auth.enabled():
        return {"ok": True}
    wait = auth.locked_for(request)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords - please wait {wait // 60 + 1} minutes.")
    if not auth.check_password(request, str(body.get("password") or "")):
        await asyncio.sleep(1)  # slows down guessing
        raise HTTPException(401, "Wrong password.")
    response = JSONResponse({"ok": True})
    auth.set_cookie(request, response)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/config")
async def get_config():
    return {
        "language_code": get_ui_language_code(settings.output_language),
        "output_language": settings.output_language,
        "convert_to_metric": settings.convert_to_metric,
        "check_duplicates": settings.check_duplicates,
        "supported_extensions": sorted(PDF_EXTENSIONS | SUPPORTED_EPUB_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS),
        "image_extensions": sorted(SUPPORTED_IMAGE_EXTENSIONS),
        "image_gen_available": image_gen.is_configured(),
        "auth_enabled": auth.enabled(),
        "watch_dir": settings.watch_dir if settings.watch_dir and os.path.isdir(settings.watch_dir) else None,
        # Base URL only (never the token) - lets the UI link straight to an
        # imported recipe in Tandoor. None when Tandoor isn't configured at all.
        "tandoor_url": settings.tandoor_url.rstrip("/") if settings.tandoor_url else None,
    }


@app.get("/api/tandoor/status")
async def tandoor_status():
    try:
        tandoor_client.test_connection()
        return {"connected": True}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"connected": False, "error": str(exc)}, status_code=200)


# ---------- Housekeeping: periodically delete old jobs and their uploaded files ----------

async def _cleanup_loop() -> None:
    while True:
        try:
            jobs.cleanup_old_jobs(settings.data_dir, settings.job_retention_hours)
            tool_jobs.cleanup_old_tool_jobs(settings.job_retention_hours)
            undo.cleanup({job.id for job in tool_jobs.list_all_tool_jobs()})
        except Exception:  # noqa: BLE001
            log.exception("Background cleanup failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    # Run once immediately (covers jobs left over from a previous container run),
    # then keep running in the background for as long as the app is up.
    jobs.cleanup_old_jobs(settings.data_dir, settings.job_retention_hours)
    loaded = tool_jobs.load_tool_jobs()
    if loaded:
        log.info("Restored %d tool run(s) from disk", loaded)
    tool_jobs.cleanup_old_tool_jobs(settings.job_retention_hours)
    asyncio.create_task(_cleanup_loop())
    apply_queue.start()
    maintenance.configure(_TOOL_SCANS)
    asyncio.create_task(maintenance.loop())
    if settings.auto_process_interval_hours > 0:
        asyncio.create_task(tools_new_recipes.auto_run_loop())
    if settings.watch_dir:
        asyncio.create_task(watcher.loop(_start_folder_import))


# Mount the static frontend last, so /api/* routes take precedence
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
