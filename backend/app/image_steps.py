"""Photos in the steps: besides a recipe's main photo, its further photos
(step-by-step pictures in a cookbook, the step images of a recipe page) go
into the step they show - always, the review is where it's changed: every
photo of a recipe gets a menu there (main photo / step n / not used).

All photos start out unassigned. The review's "place photos into the
steps" button (one recipe or all of them) places the ones not placed yet:
a recipe page names the step itself (the schema.org HowToStep's "image" -
mapped by the step's position, since the AI may have merged or split
steps), free; the others go to the image AI: one call per recipe looks at
them at once - a contact sheet of numbered thumbnails next to the step
texts - and says which step each one shows and whether it is sure. Only
sure answers are used; photos placed by hand stay where they are. Kept
cheap: the tools model, small thumbnails, the start of each step only.
STEP_PHOTOS_AI=false: only the free placement (and by hand).

Decoration never gets that far: an image repeated on several pages (a
logo, a frame), a duplicate or a narrow banner is marked when the import
is read and left out.

Photos of cookbook pages (the photo import) are the pages themselves, not
pictures in them - they never go into the steps."""
from __future__ import annotations

import hashlib
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

from . import json_answer, llm_provider, step_amounts
from .schemas import Job, TokenUsage

log = logging.getLogger("recipe-bridge")

MAX_PER_RECIPE = 12   # more is rarely a step-by-step series (a whole photo spread)
TILE = 220            # px, one thumbnail on the contact sheet - enough to tell the steps apart
COLUMNS = 4
STEP_CHARS = 160      # of each step's text - enough to recognise it
REPEATED_ON = 3       # the same image on this many pages is decoration
MAX_ASPECT = 3        # wider (or taller) than 3:1 is a banner or a border

PROMPT = """The picture is a contact sheet of {n} numbered photos (the number is
in the top left corner of each photo) found with a recipe in a cookbook.
These are the recipe's steps:

{steps}

Which step does each photo show? Only assign a photo when it clearly shows
what happens in that step (e.g. the dough being rolled out, the vegetables
being chopped). Photos of the finished dish, of ingredients laid out before
cooking, of something unrelated or of another recipe get null. "sure" is
true only when there is no real doubt which step it is - when two steps
could fit, or you are guessing, it is false.

Respond with ONLY a JSON object (no explanation, no markdown fence):
{{"photos": [{{"n": <photo number>, "step": <step number 1-{m}> | null, "sure": true | false}}, ...]}}"""


def decoration(job: Job, images_dir: str) -> set[str]:
    """Images that are no recipe photos: repeated on several pages, second
    copies of the same image, narrow banners - found without AI."""
    pages: dict[str, set] = {}
    first: dict[str, str] = {}
    out = set()
    for image_id, info in job.images.items():
        if info.get("kind") == "page" or info.get("ai"):
            continue
        path = os.path.join(images_dir, info["filename"])
        try:
            with open(path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
            with Image.open(path) as img:
                w, h = img.size
        except OSError:
            continue
        if max(w, h) > MAX_ASPECT * max(1, min(w, h)):
            out.add(image_id)
        pages.setdefault(digest, set()).add(info.get("page"))
        if digest in first:
            out.add(image_id)
        else:
            first[digest] = image_id
    out |= {first[d] for d, p in pages.items() if len(p) >= REPEATED_ON}
    return out


def extra_images(job: Job, recipe) -> list[str]:
    """The recipe's photos besides its main photo, in page order: those on
    its own pages (no neighbouring pages - they may belong to the next
    recipe), never photographed cookbook pages, AI images or decoration."""
    out = []
    for image_id, info in job.images.items():
        if image_id == recipe.selected_image_id or info.get("kind") in ("page", "decoration") or info.get("ai"):
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
    """{photo index: step index} for the photos the AI placed - and is sure about."""
    steps = "\n".join(f"{i}. {step_amounts.plain(s.instruction)[:STEP_CHARS]}" for i, s in enumerate(recipe.steps, 1))
    prompt = PROMPT.format(n=len(paths), steps=steps, m=len(recipe.steps))
    text, usage = llm_provider.transcribe_image(_contact_sheet(paths), prompt, max_tokens=600, tools=True)
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
        if step is not None and item.get("sure") is True and 1 <= n <= len(paths):
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


def assign_all(job: Job, images_dir: str) -> None:
    """While reading the import: only sort out decoration - nothing is
    placed into the steps until the review's button asks for it."""
    for image_id in decoration(job, images_dir):
        job.images[image_id]["kind"] = "decoration"
    for recipe in job.recipes:
        for step in recipe.steps:
            step.image_ids = []


def place(job: Job, recipe, images_dir: str, use_ai: bool) -> tuple[int, TokenUsage]:
    """The review's button: places the recipe's photos that aren't the main
    photo or in a step yet - a recipe page's own step photos by position
    (no AI), the others by the image AI (use_ai). Returns (photos placed,
    usage); raises when the AI call fails (e.g. a model that doesn't read
    images)."""
    if not recipe.steps:
        return 0, TokenUsage()
    in_steps = {iid for step in recipe.steps for iid in step.image_ids}
    ids = [iid for iid in extra_images(job, recipe) if iid not in in_steps]
    placed = _by_position(job, recipe, [iid for iid in ids if job.images[iid].get("step") is not None])
    rest = [iid for iid in ids if iid not in placed]
    usage = TokenUsage()
    if rest and use_ai:
        answer, usage = _ask_ai(recipe, [os.path.join(images_dir, job.images[iid]["filename"]) for iid in rest])
        placed.update({rest[i]: step for i, step in answer.items()})
    for iid in ids:  # page order within a step
        if iid in placed:
            recipe.steps[placed[iid]].image_ids.append(iid)
    return len(placed), usage
