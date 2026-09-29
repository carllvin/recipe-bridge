"""Word documents (.docx) as an import source - read directly from the
file's XML (a .docx is a zip), no extra dependency. Paragraphs and tables
become text, split into pages like pasted text; embedded pictures become
candidate recipe images on the page where they appear."""
from __future__ import annotations

import io
import logging
import os
import posixpath
import uuid
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image

from .text_processor import MAX_PAGE_CHARS

log = logging.getLogger("recipe-bridge")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MIN_IMAGE_SIDE = 150  # skip icons and decorations


def _paragraph_text(p) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{W}t":
            parts.append(node.text or "")
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _blocks(body):
    """(text, [image relationship ids]) per paragraph, tables row by row."""
    for child in body:
        if child.tag == f"{W}p":
            images = [b.get(f"{R}embed") for b in child.iter(f"{A}blip") if b.get(f"{R}embed")]
            yield _paragraph_text(child), images
        elif child.tag == f"{W}tbl":
            for row in child.iter(f"{W}tr"):
                cells = [" ".join(_paragraph_text(p) for p in cell.iter(f"{W}p")).strip() for cell in row.iter(f"{W}tc")]
                yield " | ".join(c for c in cells if c), []


def process_docx(path: str, images_dir: str) -> dict:
    os.makedirs(images_dir, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(f"{W}body")
        rels = {}
        if "word/_rels/document.xml.rels" in z.namelist():
            for rel in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(f"{PR}Relationship"):
                rels[rel.get("Id")] = posixpath.normpath(posixpath.join("word", rel.get("Target", "")))
        title = ""
        if "docProps/core.xml" in z.namelist():
            core = ET.fromstring(z.read("docProps/core.xml"))
            node = core.find("{http://purl.org/dc/elements/1.1/}title")
            title = (node.text or "").strip() if node is not None else ""

        pages, images, current = [], {}, ""
        for text, image_rels in _blocks(body if body is not None else []):
            if text:
                if current and len(current) + len(text) + 1 > MAX_PAGE_CHARS:
                    pages.append(current)
                    current = ""
                current = f"{current}\n{text}" if current else text
            for rel_id in image_rels:
                target = rels.get(rel_id)
                if not target or target not in z.namelist():
                    continue
                try:
                    img = Image.open(io.BytesIO(z.read(target))).convert("RGB")
                except Exception as exc:  # noqa: BLE001
                    log.info("Picture %s in the document not usable: %s", target, exc)
                    continue
                if min(img.size) < MIN_IMAGE_SIDE:
                    continue
                image_id = uuid.uuid4().hex[:12]
                filename = f"{image_id}.jpg"
                img.save(os.path.join(images_dir, filename), "JPEG", quality=90)
                images[image_id] = {"page": len(pages) + 1, "path": os.path.join(images_dir, filename),
                                    "filename": filename, "width": img.width, "height": img.height}
        if current:
            pages.append(current)

    if not pages:
        raise ValueError("The document contains no text.")
    return {"pages": [{"page": i, "text": t} for i, t in enumerate(pages, 1)], "images": images,
            "page_count": len(pages), "metadata_title": title, "toc_pages": []}
