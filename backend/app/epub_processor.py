from __future__ import annotations

import logging
import os
import uuid

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

log = logging.getLogger("recipe-bridge")

SUPPORTED_EPUB_EXTENSIONS = {".epub"}


def process_epub(epub_path: str, images_dir: str) -> dict:
    """
    Reads an EPUB and returns the same shape as pdf_processor.process_pdf:
      - pages: list of {"page": n, "text": "..."} - one "page" per spine item
        (chapter/section), in reading order. EPUBs don't have fixed pages the
        way PDFs do, so a chapter is the natural unit here instead.
      - images: dict image_id -> {"page": n, "path": "...", "filename": "...",
        "width": w, "height": h} - images embedded in each chapter
      - metadata_title: the book's title from its metadata
      - toc_pages: always [] (each "page" is already a whole chapter, so there's
        no finer bookmark structure to align chunks to)
    EPUB text is already digital, so no OCR is needed here.
    """
    os.makedirs(images_dir, exist_ok=True)
    book = epub.read_epub(epub_path)

    metadata_title = ""
    try:
        titles = book.get_metadata("DC", "title")
        if titles:
            metadata_title = (titles[0][0] or "").strip()
    except Exception:  # noqa: BLE001
        pass

    pages = []
    images: dict[str, dict] = {}

    spine_ids = [item_id for item_id, _ in book.spine]
    ordered_docs = []
    for item_id in spine_ids:
        item = book.get_item_with_id(item_id)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            ordered_docs.append(item)

    if not ordered_docs:
        # Fall back to document order if the spine is empty/unreadable
        ordered_docs = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    for index, doc_item in enumerate(ordered_docs, start=1):
        try:
            soup = BeautifulSoup(doc_item.get_content(), "html.parser")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not parse EPUB chapter %s (%s) - skipping", doc_item.get_name(), exc)
            pages.append({"page": index, "text": ""})
            continue

        text = soup.get_text("\n").strip()
        pages.append({"page": index, "text": text})

        for img_tag in soup.find_all("img"):
            src = img_tag.get("src")
            if not src:
                continue
            image_item = _resolve_image_item(book, doc_item, src)
            if image_item is None:
                continue

            content = image_item.get_content()
            if len(content) < 5000:  # skip tiny icons/decoration (~5KB threshold)
                continue

            ext = os.path.splitext(image_item.get_name())[1].lstrip(".") or "jpg"
            image_id = uuid.uuid4().hex[:12]
            filename = f"{image_id}.{ext}"
            out_path = os.path.join(images_dir, filename)
            with open(out_path, "wb") as f:
                f.write(content)

            width, height = _image_dimensions(content)
            images[image_id] = {
                "page": index,
                "path": out_path,
                "filename": filename,
                "width": width,
                "height": height,
            }

    return {
        "pages": pages,
        "images": images,
        "page_count": len(pages),
        "metadata_title": metadata_title,
        "toc_pages": [],
    }


def _resolve_image_item(book: "epub.EpubBook", doc_item, src: str):
    """Resolves an <img src="..."> path (relative to the chapter file) to the
    matching image item in the EPUB package."""
    base_dir = os.path.dirname(doc_item.get_name())
    candidate = os.path.normpath(os.path.join(base_dir, src)).replace("\\", "/")
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if item.get_name() == candidate or item.get_name().endswith(src.lstrip("./")):
            return item
    return None


def _image_dimensions(content: bytes) -> tuple[int, int]:
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(content)) as img:
            return img.width, img.height
    except Exception:  # noqa: BLE001
        return 0, 0
