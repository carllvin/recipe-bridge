from __future__ import annotations

import base64
import logging
from typing import Optional

from .config import settings

log = logging.getLogger("recipe-bridge")


def _active_provider() -> str:
    provider = (settings.image_gen_provider or "openai").strip().lower()
    return provider if provider in ("openai", "gemini") else "openai"


def is_configured() -> bool:
    """True only when image generation is explicitly enabled AND the selected
    provider has an API key set. Anthropic has no image generation API, so
    IMAGE_GEN_PROVIDER must be openai or gemini regardless of AI_PROVIDER."""
    if not settings.image_gen_enabled:
        return False
    provider = _active_provider()
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    return bool(settings.openai_api_key)


def missing_key_hint() -> str:
    if not settings.image_gen_enabled:
        return "Image generation is disabled (set IMAGE_GEN_ENABLED=true in .env)."
    provider = _active_provider()
    var = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    return f"{var} is not set, but IMAGE_GEN_PROVIDER={provider}. Please configure it in the .env file."


def build_recipe_image_prompt(title: str, description: Optional[str], tags: list[str]) -> str:
    """Builds a food-photography prompt from the recipe's own text - no extra AI
    call needed, just string assembly."""
    parts = [f'A professional, appetizing food photograph of "{title}".']
    if description:
        parts.append(description.strip()[:300])
    if tags:
        parts.append("Style/context: " + ", ".join(tags[:5]) + ".")
    parts.append(
        "Overhead or three-quarter angle shot, natural daylight, on a simple "
        "plate or rustic wooden surface, shallow depth of field, photorealistic, "
        "editorial food-magazine style. No text, no watermark, no logo, no hands, "
        "no people."
    )
    if settings.image_gen_custom_instructions.strip():
        parts.append(settings.image_gen_custom_instructions.strip())
    return " ".join(parts)


ENHANCE_PROMPT = (
    "Enhance this food photograph like a professional photo editor: better lighting and exposure, "
    "natural colors and white balance (no yellow or blue cast), more sharpness and less noise, "
    "less glare, straightened if tilted. Keep the dish exactly as it is - the same food, the same "
    "arrangement, portion, plate and background; do not add, remove or change any ingredient or "
    "object. Photorealistic. No text, no watermark, no logo."
)
ENHANCE_MAX_SIDE = 1536


def enhance_image(data: bytes) -> bytes:
    """An improved version of an existing recipe photo (light, colors,
    sharpness - the dish itself stays unchanged). Raises RuntimeError with a
    clear message on any failure."""
    import io

    from PIL import Image, ImageOps

    if not is_configured():
        raise RuntimeError(missing_key_hint())
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((ENHANCE_MAX_SIDE, ENHANCE_MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    prompt = ENHANCE_PROMPT
    if settings.image_gen_custom_instructions.strip():
        prompt += " " + settings.image_gen_custom_instructions.strip()
    if _active_provider() == "gemini":
        return _enhance_gemini(buf.getvalue(), prompt)
    return _enhance_openai(buf.getvalue(), prompt)


def _enhance_openai(png: bytes, prompt: str) -> bytes:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    def edit(**extra):
        return client.images.edit(model=settings.openai_image_model, image=("photo.png", png, "image/png"),
                                  prompt=prompt, n=1, **extra)
    try:
        try:
            # keeps the dish's details as they are (gpt-image-1)
            response = edit(input_fidelity="high")
        except Exception as exc:  # noqa: BLE001
            if "fidelity" not in str(exc).lower():
                raise
            response = edit()  # a model without that option
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenAI image enhancement failed: {exc}") from exc
    b64 = getattr(response.data[0], "b64_json", None) if response.data else None
    if not b64:
        raise RuntimeError("OpenAI did not return any image data.")
    return base64.b64decode(b64)


def _enhance_gemini(png: bytes, prompt: str) -> bytes:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = client.models.generate_content(
            model=settings.gemini_image_model,
            contents=[types.Part.from_bytes(data=png, mime_type="image/png"), prompt],
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Gemini image enhancement failed: {exc}") from exc
    return _gemini_image(response)


def _gemini_image(response) -> bytes:
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data
    raise RuntimeError("Gemini did not return any image data.")


def generate_image(prompt: str) -> bytes:
    """Generates one image and returns its raw bytes. Raises RuntimeError with a
    clear message on any failure."""
    if not settings.image_gen_enabled:
        raise RuntimeError(missing_key_hint())
    provider = _active_provider()
    if provider == "gemini":
        return _generate_gemini(prompt)
    return _generate_openai(prompt)


def _generate_openai(prompt: str) -> bytes:
    from openai import OpenAI

    if not settings.openai_api_key:
        raise RuntimeError(missing_key_hint())

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.images.generate(
            model=settings.openai_image_model,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenAI image generation failed: {exc}") from exc

    b64 = getattr(response.data[0], "b64_json", None) if response.data else None
    if not b64:
        raise RuntimeError("OpenAI did not return any image data.")
    return base64.b64decode(b64)


def _generate_gemini(prompt: str) -> bytes:
    from google import genai

    if not settings.gemini_api_key:
        raise RuntimeError(missing_key_hint())

    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = client.models.generate_content(
            model=settings.gemini_image_model,
            contents=[prompt],
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Gemini image generation failed: {exc}") from exc
    return _gemini_image(response)
