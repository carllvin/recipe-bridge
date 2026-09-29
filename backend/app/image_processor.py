from __future__ import annotations

import io
import logging
import os
import uuid

from PIL import Image, ImageOps
import pillow_heif

from . import llm_provider
from .ocr import ocr_image
from .schemas import TokenUsage

log = logging.getLogger("recipe-bridge")

pillow_heif.register_heif_opener()  # lets Pillow open .heic/.heif via Image.open()

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# Handwriting: the photo goes to the AI (which reads images) instead of
# Tesseract, which reads handwriting poorly. Resized first - the AI doesn't
# need more to read a card, and a smaller image costs fewer tokens.
HANDWRITING_MAX_SIDE = 2000
HANDWRITING_PROMPT = (
    "This is a photo of a recipe - often handwritten (a recipe card, a notebook page, a note), maybe with "
    "printed parts. Transcribe ALL text on it exactly as written, line by line, in its original language. "
    "Keep amounts, units and fractions exactly as written. Read crossed-out words as crossed out (leave them "
    "out) and include notes in the margin. Mark a word you really cannot read as [?]. Answer with the "
    "transcribed text only - no comments, no translation, no formatting."
)


def _transcribe(img: Image.Image) -> tuple[str, TokenUsage]:
    small = img.copy()
    small.thumbnail((HANDWRITING_MAX_SIDE, HANDWRITING_MAX_SIDE))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return llm_provider.transcribe_image(buf.getvalue(), HANDWRITING_PROMPT)


def process_images(image_paths: list[str], images_dir: str, handwriting: bool = False) -> dict:
    """
    Treats a set of directly-uploaded photos (e.g. phone photos of cookbook
    pages) as one "document": each image becomes one page (OCR'd with
    Tesseract) AND is saved as a candidate recipe image for that page, since a
    photo of a recipe page often doubles as a perfectly good recipe photo.
    Returns the same shape as pdf_processor.process_pdf / epub_processor.process_epub.
    handwriting=True: the AI transcribes each photo instead of Tesseract (its
    token usage is returned as "usage"); a photo it can't read falls back to OCR.
    """
    os.makedirs(images_dir, exist_ok=True)

    usage = TokenUsage()
    pages = []
    images: dict[str, dict] = {}

    for index, path in enumerate(image_paths, start=1):
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)  # respect phone-camera rotation metadata
            img = img.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open image %s (%s) - skipping this page", path, exc)
            pages.append({"page": index, "text": ""})
            continue

        text = None
        if handwriting:
            try:
                text, page_usage = _transcribe(img)
                usage.input_tokens += page_usage.input_tokens
                usage.output_tokens += page_usage.output_tokens
            except Exception as exc:  # noqa: BLE001
                log.warning("Handwriting transcription failed for photo %d (%s) - using OCR", index, exc)
                text = None
        if text is None:
            text = ocr_image(img)
        pages.append({"page": index, "text": text})

        image_id = uuid.uuid4().hex[:12]
        filename = f"{image_id}.jpg"
        out_path = os.path.join(images_dir, filename)
        img.save(out_path, "JPEG", quality=90)

        images[image_id] = {
            "page": index,
            "path": out_path,
            "filename": filename,
            "width": img.width,
            "height": img.height,
        }

    if pages:
        log.info("%s used for %d uploaded photo(s)", "AI transcription" if handwriting else "OCR", len(pages))

    return {
        "pages": pages,
        "images": images,
        "page_count": len(pages),
        "metadata_title": "",
        "toc_pages": [],
        "usage": usage,
    }
