from __future__ import annotations

import logging

from PIL import Image
import pytesseract

from .config import settings
from .image_preprocessing import preprocess_for_ocr

log = logging.getLogger("recipe-bridge")


def ocr_image(img: "Image.Image") -> str:
    """Runs Tesseract OCR on a PIL image using the configured language(s)
    (OCR_LANGUAGES, e.g. "eng+deu"). Before OCR, the image goes through
    preprocess_for_ocr (deskew, double-page-spread splitting, crop/contrast
    cleanup - see image_preprocessing.py); if that's a double-page spread, both
    halves are OCR'd separately and their text is concatenated, so the caller
    never needs to know whether a split happened. Returns an empty string
    rather than raising if OCR fails or Tesseract isn't available, so callers
    can degrade gracefully (keep whatever text was already there, or none)."""
    try:
        pages = preprocess_for_ocr(img)
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR preprocessing failed (%s) - using the original image.", exc)
        pages = [img]

    texts = []
    for page_img in pages:
        try:
            texts.append(pytesseract.image_to_string(page_img, lang=settings.ocr_languages))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "OCR failed (%s) - continuing without OCR text for this part. Is Tesseract "
                "installed with the language pack(s) for OCR_LANGUAGES=%s?",
                exc, settings.ocr_languages,
            )

    return "\n\n".join(t for t in texts if t.strip())
