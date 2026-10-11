"""Photos in the steps: besides a recipe's main photo, its further photos
(step-by-step pictures in a cookbook, the step images of a recipe page) go
into the steps - the import option "photos in the steps"
(app_settings.step_images):

- "off"   - only the main photo, as before
- "steps" - each photo into the step it shows
- "first" - all of them into step 1 (when the order doesn't matter or the
            assignment would be a guess)

Which step a photo belongs to: a recipe page names the step itself (the
schema.org HowToStep's "image" - mapped by the step's position, since the
AI may have merged or split steps); for cookbook pages one image AI call
per recipe looks at all its photos at once - a contact sheet of numbered
thumbnails next to the step texts - and says which step each one shows
(or none: the finished dish, a picture of another recipe). Without an
answer they go into step 1.

Photos of cookbook pages (the photo import) are the pages themselves, not
pictures in them - they never go into the steps. Everything can still be
changed in the review before the import."""
from __future__ import annotations

import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

from . import app_settings, json_answer, llm_provider
from .schemas import Job, TokenUsage

log = logging.getLogger("recipe-bridge")

MAX_PER_RECIPE = 12   # more is rarely a step-by-step series (a whole photo spread)
TILE = 300            # px, one thumbnail on the contact sheet
COLUMNS = 4

PROMPT = """The picture is a contact sheet of {n} numbered photos (the number is
in the top left corner of each photo) found with a recipe in a cookbook.
These are the recipe's steps:

{steps}

Which step does each photo show? Only assign a photo when it clearly shows
what happens in that step (e.g. the dough being rolled out, the vegetables
being chopped). Photos of the finished dish, of ingredients laid out before
cooking, of something unrelated or of another recipe get null.

Respond with ONLY a JSON object (no explanation, no markdown fence):
{{"photos": [{{"n": <photo number>, "step": <step number 1-{m}> | null}}, ...]}}"""


def extra_images(job: Job, recipe) -> list[str]:
    """The recipe's photos besides its main photo, in page order: those on
    its own pages (no neighbouring pages - they may belong to the next
    recipe), never photographed cookbook pages or AI images."""
    out = []
    for image_id, info in job.images.items():
        if image_id == recipe.selected_image_id or info.get("kind") == "page" or info.get("ai"):
            continue
        if recipe.source_page_start <= (info.get("page") or 0) <= recipe.source_page_end:
            out.append(image_id)
    out.sort(key=lambda iid: (job.images[iid].get("page") or 0, job.images[iid].get("order", 0)))
    return out[:MAX_PER_RECIPE]


def _contact_sheet(paths: list[str]) -> bytes:
    rows = (len(paths) + COLUMNS - 1) // COLUMNS
    sheet = Image.new("RGB", (COLUMNS * TILE, rows * TILE), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=44)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    for n, path in enumerate(paths):
        x, y = (n % COLUMNS) * TILE, (n // COLUMNS) * TILE
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((TILE - 8, TILE - 8))
            sheet.paste(img, (x + (TILE - img.width) // 2, y + (TILE - img.height) // 2))
        except Exception as exc:  # noqa: BLE001
            log.info("Photo %s not readable for the contact sheet: %s", path, exc)
        draw.rectangle([x + 4, y + 4, x + 64, y + 58], fill="black")
        draw.text((x + 12, y + 6), str(n + 1), fill="white", font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


def _ask_ai(recipe, paths: list[str]) -> tuple[dict[int, int], TokenUsage]:
    """{photo index: step index} for the photos the AI could place."""
    steps = "\n".join(f"{i}. {s.instruction[:300]}" for i, s in enumerate(recipe.steps, 1))
    prompt = PROMPT.format(n=len(paths), steps=steps, m=len(recipe.steps))
    text, usage = llm_provider.transcribe_image(_contact_sheet(paths), prompt, max_tokens=800)
    try:
        answer = json_answer.parse(text)
    except Exception:  # noqa: BLE001
        answer = None
    placed = {}
    for item in (answer or {}).get("photos", []) if isinstance(answer, dict) else []:
        try:
            n, step = int(item.get("n")), item.get("step")
        except (TypeError, ValueError, AttributeError):
            continue
        if step is not None and 1 <= n <= len(paths):
            try:
                step = int(step)
            except (TypeError, ValueError):
                continue
            if 1 <= step <= len(recipe.steps):
                placed[n - 1] = step - 1
    return placed, usage or TokenUsage()


def _by_position(job: Job, recipe, ids: list[str]) -> dict[str, int]:
    """Step images of a recipe page: the k-th of n original steps -> the
    step at the same relative position among the extracted ones."""
    out = {}
    for iid in ids:
        info = job.images[iid]
        total = info.get("of") or 1
        out[iid] = min(len(recipe.steps) - 1, int(info["step"] * len(recipe.steps) / total))
    return out


def assign(job: Job, recipe, images_dir: str, mode: str) -> TokenUsage:
    usage = TokenUsage()
    for step in recipe.steps:
        step.image_ids = []
    ids = extra_images(job, recipe)
    if mode == "off" or not ids or not recipe.steps:
        return usage
    if mode == "first":
        recipe.steps[0].image_ids = ids
        return usage
    placed: dict[str, int] = {}
    hinted = [iid for iid in ids if job.images[iid].get("step") is not None]
    placed.update(_by_position(job, recipe, hinted))
    rest = [iid for iid in ids if iid not in placed]
    if rest and len(recipe.steps) > 1 and llm_provider.is_configured():
        try:
            answer, usage = _ask_ai(recipe, [os.path.join(images_dir, job.images[iid]["filename"]) for iid in rest])
            placed.update({rest[i]: step for i, step in answer.items()})
            # the AI saw them and found no step: the finished dish, another recipe ...
            rest = []
        except Exception as exc:  # noqa: BLE001 - e.g. a local model that doesn't read images
            log.info("Placing the photos of %r failed (%s) - they go into step 1", recipe.title, exc)
    for iid in ids:
        if iid in placed:
            recipe.steps[placed[iid]].image_ids.append(iid)
        elif iid in rest:
            recipe.steps[0].image_ids.append(iid)
    return usage


def assign_all(job: Job, images_dir: str) -> None:
    mode = app_settings.step_images()
    if mode == "off":
        return
    for n, recipe in enumerate(job.recipes, 1):
        if len(job.recipes) > 1:
            job.progress_label = f"Placing photos into the steps ({n}/{len(job.recipes)}) …"
        job.token_usage.add(assign(job, recipe, images_dir, mode))
