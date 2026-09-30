from __future__ import annotations

from typing import Optional

from .config import settings
from .schemas import TokenUsage

_PROVIDER_KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # any OpenAI-compatible API: Ollama, LM Studio, vLLM, LocalAI, OpenRouter ...
    "compatible": "COMPATIBLE_BASE_URL and COMPATIBLE_MODEL",
}
_ALIASES = {"ollama": "compatible", "local": "compatible", "openai_compatible": "compatible",
            "openai-compatible": "compatible", "lmstudio": "compatible", "openrouter": "compatible"}


def _active_provider() -> str:
    provider = (settings.ai_provider or "anthropic").strip().lower()
    provider = _ALIASES.get(provider, provider)
    return provider if provider in _PROVIDER_KEY_VARS else "anthropic"


def is_configured() -> bool:
    """Checks whether an API key is set for the currently selected provider."""
    provider = _active_provider()
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "compatible":
        return bool(settings.compatible_base_url and settings.compatible_model)
    return bool(settings.anthropic_api_key)


def _openai_client(provider: str):
    """An OpenAI SDK client - for OpenAI itself or an OpenAI-compatible
    server (COMPATIBLE_BASE_URL, e.g. http://ollama:11434/v1)."""
    from openai import OpenAI
    if provider == "compatible":
        # local servers usually take any key; the SDK needs a non-empty one
        return OpenAI(api_key=settings.compatible_api_key or "not-needed", base_url=settings.compatible_base_url,
                      timeout=settings.compatible_timeout_seconds)
    return OpenAI(api_key=settings.openai_api_key)


def missing_key_hint() -> str:
    provider = _active_provider()
    var = _PROVIDER_KEY_VARS[provider]
    return (
        f"{var} is not set, but AI_PROVIDER={provider} is selected. "
        f"Please configure it in the .env file."
    )


def _model(provider: str, tools: bool) -> str:
    """The main model, or - for tools=True - the *_TOOLS_MODEL if one is set."""
    main = {"openai": settings.openai_model, "gemini": settings.gemini_model,
            "compatible": settings.compatible_model}.get(provider, settings.claude_model)
    if not tools:
        return main
    cheap = {"openai": settings.openai_tools_model, "gemini": settings.gemini_tools_model,
             "compatible": settings.compatible_tools_model}.get(provider, settings.claude_tools_model)
    return (cheap or "").strip() or main


def complete_text(
    system_prompt: Optional[str], user_content: str, max_tokens: int = 4096, tools: bool = False
) -> tuple[str, TokenUsage]:
    """Sends a prompt to the configured AI provider and returns (response_text, token_usage).
    system_prompt may be empty/None (in which case no system prompt is sent).
    tools=True uses the cheaper *_TOOLS_MODEL (if set) - for the maintenance
    tools' small, structured tasks, not for cookbook extraction."""
    provider = _active_provider()
    model = _model(provider, tools)
    if provider in ("openai", "compatible"):
        return _complete_openai(system_prompt, user_content, max_tokens, model, provider)
    if provider == "gemini":
        return _complete_gemini(system_prompt, user_content, max_tokens, model)
    return _complete_anthropic(system_prompt, user_content, max_tokens, model)


def _complete_anthropic(system_prompt: Optional[str], user_content: str, max_tokens: int, model: str) -> tuple[str, TokenUsage]:
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError(missing_key_hint())

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    kwargs = {}
    if system_prompt:
        kwargs["system"] = system_prompt

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_content}],
        **kwargs,
    )
    text = "".join(block.text for block in response.content if block.type == "text")

    usage = TokenUsage()
    if getattr(response, "usage", None) is not None:
        usage.input_tokens = getattr(response.usage, "input_tokens", 0) or 0
        usage.output_tokens = getattr(response.usage, "output_tokens", 0) or 0
    return text, usage


def _complete_openai(system_prompt: Optional[str], user_content: str, max_tokens: int, model: str,
                     provider: str = "openai") -> tuple[str, TokenUsage]:
    if not is_configured():
        raise RuntimeError(missing_key_hint())

    client = _openai_client(provider)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})

    # Newer OpenAI models (gpt-5/o-series) require 'max_completion_tokens' instead of
    # 'max_tokens'. We try both so the app works regardless of the chosen model,
    # without the user having to figure that out themselves.
    try:
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            messages=messages,
        )
    except Exception:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
    text = response.choices[0].message.content or ""

    usage = TokenUsage()
    if getattr(response, "usage", None) is not None:
        usage.input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
        usage.output_tokens = getattr(response.usage, "completion_tokens", 0) or 0
    return text, usage


def _complete_gemini(system_prompt: Optional[str], user_content: str, max_tokens: int, model: str) -> tuple[str, TokenUsage]:
    from google import genai
    from google.genai import types

    if not settings.gemini_api_key:
        raise RuntimeError(missing_key_hint())

    client = genai.Client(api_key=settings.gemini_api_key)
    config_kwargs = {"max_output_tokens": max_tokens}
    if system_prompt:
        config_kwargs["system_instruction"] = system_prompt

    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    text = response.text or ""

    usage = TokenUsage()
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
        usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
    return text, usage


def complete_tool_text(system_prompt: Optional[str], user_content: str, max_tokens: int = 4096) -> tuple[str, TokenUsage]:
    """complete_text() with the cheaper tools model - used by the maintenance
    tools and scripts (matching, tagging, plurals, nutrition, seasons)."""
    return complete_text(system_prompt, user_content, max_tokens, tools=True)


def transcribe_image(jpeg: bytes, prompt: str, max_tokens: int = 4000) -> tuple[str, TokenUsage]:
    """Sends one photo (JPEG bytes) with an instruction to the configured
    provider's main model - all three read images. Used to transcribe
    handwritten recipes, which Tesseract reads poorly."""
    import base64

    provider = _active_provider()
    model = _model(provider, tools=False)
    b64 = base64.b64encode(jpeg).decode("ascii")
    usage = TokenUsage()
    if provider in ("openai", "compatible"):
        if not is_configured():
            raise RuntimeError(missing_key_hint())
        # (with a local server this needs a model that reads images, e.g. llava, qwen2.5-vl)
        client = _openai_client(provider)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}]
        try:
            response = client.chat.completions.create(model=model, max_completion_tokens=max_tokens, messages=messages)
        except Exception:
            response = client.chat.completions.create(model=model, max_tokens=max_tokens, messages=messages)
        if getattr(response, "usage", None) is not None:
            usage.input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            usage.output_tokens = getattr(response.usage, "completion_tokens", 0) or 0
        return response.choices[0].message.content or "", usage
    if provider == "gemini":
        from google import genai
        from google.genai import types
        if not settings.gemini_api_key:
            raise RuntimeError(missing_key_hint())
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), prompt],
            config=types.GenerateContentConfig(max_output_tokens=max_tokens),
        )
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        return response.text or "", usage
    import anthropic
    if not settings.anthropic_api_key:
        raise RuntimeError(missing_key_hint())
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    if getattr(response, "usage", None) is not None:
        usage.input_tokens = getattr(response.usage, "input_tokens", 0) or 0
        usage.output_tokens = getattr(response.usage, "output_tokens", 0) or 0
    return "".join(block.text for block in response.content if block.type == "text"), usage
