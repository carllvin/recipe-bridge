from __future__ import annotations

import logging

import numpy as np
from PIL import Image, ImageOps

from .config import settings

log = logging.getLogger("recipe-bridge")


def preprocess_for_ocr(img: "Image.Image") -> list["Image.Image"]:
    """Applies deskew, double-page-spread splitting, and crop/contrast cleanup
    before OCR. Returns a list of one image (a normal single page) or two
    images (a detected double-page spread, split into left/right halves),
    each ready to hand to Tesseract. Every step is best-effort: if a step
    fails, its input is used unmodified rather than failing the whole page."""
    working = img

    if settings.ocr_preprocess_enabled:
        try:
            working = _deskew(working)
        except Exception as exc:  # noqa: BLE001
            log.warning("Deskew failed (%s) - using the original orientation.", exc)

    pages = [working]
    if settings.ocr_double_page_detection:
        try:
            pages = _split_double_page(working)
        except Exception as exc:  # noqa: BLE001
            log.warning("Double-page split check failed (%s) - treating as a single page.", exc)
            pages = [working]

    if settings.ocr_preprocess_enabled:
        cleaned = []
        for page_img in pages:
            try:
                cleaned.append(_enhance_for_ocr(page_img))
            except Exception as exc:  # noqa: BLE001
                log.warning("Crop/contrast cleanup failed (%s) - using the unprocessed page.", exc)
                cleaned.append(page_img)
        pages = cleaned

    return pages


def _deskew(
    img: "Image.Image",
    max_angle: float = 5.0,
    angle_step: float = 0.5,
    downsample_width: int = 800,
) -> "Image.Image":
    """Estimates and corrects small rotation (a few degrees) typical of a
    slightly crooked book scan. Finds the angle in [-max_angle, max_angle] that
    maximizes the variance of horizontal ink-row sums (text lines line up
    tightly - and so produce sharp peaks/troughs in that profile - only at
    close to the true angle). The search itself runs on a small downsized copy
    for speed; the found angle is then applied to the full-resolution image."""
    gray = img.convert("L")
    scale = min(1.0, downsample_width / max(gray.width, 1))
    small = gray.resize(
        (max(1, int(gray.width * scale)), max(1, int(gray.height * scale)))
    ) if scale < 1.0 else gray

    arr = np.asarray(small, dtype=np.float64)
    threshold = arr.mean() - arr.std() * 0.5
    ink = (arr < threshold).astype(np.float64)
    ink_img = Image.fromarray((ink * 255).astype(np.uint8))

    best_angle = 0.0
    best_score = -1.0
    angle = -max_angle
    while angle <= max_angle:
        if angle == 0.0:
            rotated = ink
        else:
            rotated_img = ink_img.rotate(angle, resample=Image.BILINEAR, fillcolor=0, expand=False)
            rotated = np.asarray(rotated_img, dtype=np.float64) / 255.0
        row_sums = rotated.sum(axis=1)
        score = float(np.var(row_sums))
        if score > best_score:
            best_score = score
            best_angle = angle
        angle += angle_step

    if abs(best_angle) < angle_step:
        return img  # correction would be smaller than our own search resolution - not worth it

    fill = (255, 255, 255) if img.mode == "RGB" else 255
    return img.rotate(best_angle, resample=Image.BICUBIC, fillcolor=fill, expand=False)


def _split_double_page(img: "Image.Image") -> list["Image.Image"]:
    """A page scanned from an open book, flat on a scanner or photographed,
    often captures two facing pages as one wide image. Detected purely by
    aspect ratio (a single book page is portrait/near-square; a spread is
    clearly landscape) - if triggered, splits into left/right halves with a
    small overlap so text right at the gutter/spine isn't cut off."""
    width, height = img.size
    if height == 0:
        return [img]
    if (width / height) < settings.ocr_double_page_aspect_threshold:
        return [img]

    mid = width // 2
    overlap = max(4, int(width * 0.01))
    left = img.crop((0, 0, min(width, mid + overlap), height))
    right = img.crop((max(0, mid - overlap), 0, width, height))
    return [left, right]


def _enhance_for_ocr(img: "Image.Image") -> "Image.Image":
    """Auto-crops large uniform borders/margins (common on scans: scanner-bed
    black edges, excessive white margin) and boosts contrast - both generally
    improve OCR accuracy and, by removing dead space, cut down the pixel area
    (and therefore noise) Tesseract has to process."""
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    # Rough binarization: pixels noticeably darker than white count as "content".
    binarized = gray.point(lambda p: 255 if p < 235 else 0)
    bbox = binarized.getbbox()
    if bbox:
        pad = 12
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(img.width, bbox[2] + pad)
        bottom = min(img.height, bbox[3] + pad)
        if right - left > 20 and bottom - top > 20:  # sanity check: don't crop to near-nothing
            img = img.crop((left, top, right, bottom))
    return ImageOps.autocontrast(img, cutoff=1)
