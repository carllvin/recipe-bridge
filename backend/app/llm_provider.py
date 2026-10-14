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
    system_prompt: Optional[str], user_content: str, max_tokens: int = 4096, tools: bool = False,
    cache: bool = False,
) -> tuple[str, TokenUsage]:
    """Sends a prompt to the configured AI provider and returns (response_text, token_usage).
    system_prompt may be empty/None (in which case no system prompt is sent).
    tools=True uses the cheaper *_TOOLS_MODEL (if set) - for the maintenance
    tools' small, structured tasks, not for cookbook extraction.
    cache=True: the system prompt comes again in the next calls (a
    cookbook's chunks) - Claude caches it (a cache read costs about a tenth;
    OpenAI and Gemini cache repeated prompts on their own)."""
    provider = _active_provider()
    model = _model(provider, tools)
    if provider in ("openai", "compatible"):
        return _complete_openai(system_prompt, user_content, max_tokens, model, provider)
    if provider == "gemini":
        return _complete_gemini(system_prompt, user_content, max_tokens, model)
    return _complete_anthropic(system_prompt, user_content, max_tokens, model, cache)


def _anthropic_usage(response) -> TokenUsage:
    """Input as billed: cache writes count 1.25x, cache reads a tenth - so
    the budget and "AI usage" show what caching saves."""
    usage = TokenUsage()
    u = getattr(response, "usage", None)
    if u is not None:
        written = getattr(u, "cache_creation_input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        usage.input_tokens = (getattr(u, "input_tokens", 0) or 0) + round(written * 1.25) + round(read * 0.1)
        usage.output_tokens = getattr(u, "output_tokens", 0) or 0
    return usage


def _complete_anthropic(system_prompt: Optional[str], user_content: str, max_tokens: int, model: str,
                        cache: bool = False) -> tuple[str, TokenUsage]:
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError(missing_key_hint())

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    kwargs = {}
    if system_prompt and cache:  # the same instructions for every chunk: read from the cache after the first
        kwargs["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    elif system_prompt:
        kwargs["system"] = system_prompt

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_content}],
        **kwargs,
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, _anthropic_usage(response)


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


def transcribe_image(jpeg: bytes, prompt: str, max_tokens: int = 4000, tools: bool = False) -> tuple[str, TokenUsage]:
    """Sends one photo (JPEG bytes) with an instruction to the configured
    provider's main model (or, tools=True, the cheaper tools model) - all
    three read images. Used to transcribe handwritten recipes, which
    Tesseract reads poorly, and to place photos into steps."""
    import base64

    provider = _active_provider()
    model = _model(provider, tools=tools)
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


# ---------- voice notes ----------

AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
               ".oga": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm", ".aac": "audio/aac",
               ".flac": "audio/flac", ".amr": "audio/amr", ".3gp": "audio/3gpp", ".mp4": "audio/mp4"}
TRANSCRIBE_PROMPT = ("Transcribe this voice recording word for word in its original language. It is someone "
                     "dictating a recipe. Write numbers and amounts as digits. Respond with the transcript only.")


def audio_provider() -> str | None:
    """Who transcribes voice notes: the active provider if it can (OpenAI,
    Gemini, a compatible server with TRANSCRIBE support), otherwise any
    OpenAI or Gemini key that is set - Claude has no audio input."""
    active = _active_provider()
    if active in ("openai", "gemini") and is_configured():
        return active
    if active == "compatible" and settings.compatible_transcribe_model:
        return "compatible"
    if settings.openai_api_key:
        return "openai"
    if settings.gemini_api_key:
        return "gemini"
    return None


def transcribe_audio(data: bytes, filename: str) -> tuple[str, TokenUsage]:
    """The spoken text of a voice note."""
    import os
    provider = audio_provider()
    if provider is None:
        raise RuntimeError("Voice notes need an OpenAI or Gemini API key (OPENAI_API_KEY or GEMINI_API_KEY) - "
                           "Claude can't listen to audio.")
    ext = os.path.splitext(filename)[1].lower()
    mime = AUDIO_TYPES.get(ext, "audio/mpeg")
    usage = TokenUsage()
    if provider == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=settings.gemini_api_key)
        model = settings.gemini_model if _active_provider() == "gemini" else "gemini-2.5-flash"
        response = client.models.generate_content(
            model=model, contents=[types.Part.from_bytes(data=data, mime_type=mime), TRANSCRIBE_PROMPT])
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        return (response.text or "").strip(), usage
    client = _openai_client(provider)
    model = settings.compatible_transcribe_model if provider == "compatible" else settings.openai_transcribe_model
    result = client.audio.transcriptions.create(model=model, file=(filename or f"note{ext}", data, mime))
    return (getattr(result, "text", "") or "").strip(), usage


# ---------- batches: half price, the answer within up to 24 hours ----------
#
# For imports nobody waits for (the watched folder, WATCH_BATCH=true): all of
# a cookbook's chunks go to the provider as one batch, billed at half price;
# the import waits (polling once a minute) until every answer is there.
# Claude (Message Batches), OpenAI (Batch API) and Gemini (Batch Mode) - a
# local model answers one by one as usual.

BATCH_POLL_SECONDS = 60
BATCH_MAX_SECONDS = 25 * 3600
GEMINI_INLINE_MAX = 18 * 1024 * 1024


def batch_supported() -> bool:
    return _active_provider() in ("anthropic", "openai", "gemini") and is_configured()


def complete_batch(system_prompt, contents: list[str], max_tokens: int = 4096,
                   on_wait=None, on_submit=None) -> list[tuple[str, TokenUsage]]:
    """One answer per content, in their order. system_prompt: one for all,
    or a list with one per content. on_submit(ref) right after the batch
    is sent - keeping ref lets collect_batch() pick it up again after a
    restart. on_wait(done, total) while waiting. Raises when the batch
    fails or takes too long - the caller then asks one by one."""
    ref = submit_batch(system_prompt, contents, max_tokens)
    if on_submit:
        on_submit(ref)
    return collect_batch(ref, on_wait)


def submit_batch(system_prompt, contents: list[str], max_tokens: int = 4096) -> dict:
    """Sends the batch - {"provider", "id", "n"}."""
    provider = _active_provider()
    model = _model(provider, tools=False)
    systems = _systems(system_prompt, len(contents))
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        def params(system, content):
            p = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
            if system:  # mostly the same for every request: cached inside the batch too
                p["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            return p
        batch = client.messages.batches.create(requests=[
            {"custom_id": f"c{n}", "params": params(system, content)}
            for n, (system, content) in enumerate(zip(systems, contents))])
        return {"provider": "anthropic", "id": batch.id, "n": len(contents)}
    if provider == "openai":
        import json

        client = _openai_client("openai")
        lines = []
        for n, (system, content) in enumerate(zip(systems, contents)):
            messages = ([{"role": "system", "content": system}] if system else []) + \
                       [{"role": "user", "content": content}]
            lines.append(json.dumps({"custom_id": f"c{n}", "method": "POST", "url": "/v1/chat/completions",
                                     "body": {"model": model, "max_completion_tokens": max_tokens,
                                              "messages": messages}}))
        uploaded = client.files.create(file=("chunks.jsonl", "\n".join(lines).encode("utf-8")), purpose="batch")
        batch = client.batches.create(input_file_id=uploaded.id, endpoint="/v1/chat/completions",
                                      completion_window="24h")
        return {"provider": "openai", "id": batch.id, "n": len(contents)}
    if provider == "gemini":
        from google import genai

        # sent inline - Gemini takes up to 20 MB that way; a bigger batch is
        # asked one by one by the caller
        if sum(len(c.encode("utf-8")) + len((s or "").encode("utf-8")) for s, c in zip(systems, contents)) > GEMINI_INLINE_MAX:
            raise RuntimeError("Too large for a Gemini batch.")
        client = genai.Client(api_key=settings.gemini_api_key)

        def request(system, content):
            config = {"max_output_tokens": max_tokens}
            if system:
                config["system_instruction"] = system
            return {"contents": [{"role": "user", "parts": [{"text": content}]}], "config": config}
        job = client.batches.create(model=model, src=[request(s, c) for s, c in zip(systems, contents)],
                                    config={"display_name": "recipe-bridge"})
        return {"provider": "gemini", "id": job.name, "n": len(contents)}
    raise RuntimeError(f"Batches aren't available with AI_PROVIDER={provider}.")


def collect_batch(ref: dict, on_wait=None) -> list[tuple[str, TokenUsage]]:
    """Waits for a sent batch and returns its answers in order."""
    if ref["provider"] == "anthropic":
        return _collect_anthropic(ref, on_wait)
    if ref["provider"] == "openai":
        return _collect_openai(ref, on_wait)
    if ref["provider"] == "gemini":
        return _collect_gemini(ref, on_wait)
    raise RuntimeError(f"Unknown batch provider {ref['provider']!r}.")


def _wait(check, on_wait):
    import time
    started = time.monotonic()
    while True:
        done, total, finished = check()
        if on_wait:
            on_wait(done, total)
        if finished:
            return
        if time.monotonic() - started > BATCH_MAX_SECONDS:
            raise RuntimeError("The AI batch didn't finish within 25 hours.")
        time.sleep(BATCH_POLL_SECONDS)


def _half(usage: TokenUsage) -> TokenUsage:
    """Batches are billed at half price - counted that way, like cache reads."""
    return TokenUsage(input_tokens=round(usage.input_tokens / 2), output_tokens=round(usage.output_tokens / 2))


def _systems(system_prompt, n: int) -> list:
    return list(system_prompt) if isinstance(system_prompt, (list, tuple)) else [system_prompt] * n


def _collect_anthropic(ref, on_wait):
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    total = ref["n"]

    def check():
        b = client.messages.batches.retrieve(ref["id"])
        counts = b.request_counts
        done = (counts.succeeded or 0) + (counts.errored or 0) + (counts.canceled or 0) + (counts.expired or 0)
        return done, total, b.processing_status == "ended"
    _wait(check, on_wait)
    answers = {}
    for item in client.messages.batches.results(ref["id"]):
        if item.result.type == "succeeded":
            message = item.result.message
            answers[item.custom_id] = ("".join(b.text for b in message.content if b.type == "text"),
                                       _half(_anthropic_usage(message)))
        else:
            answers[item.custom_id] = None
    failed = [cid for cid in (f"c{n}" for n in range(total)) if not answers.get(cid)]
    if failed:
        raise RuntimeError(f"{len(failed)} of {total} batch request(s) failed.")
    return [answers[f"c{n}"] for n in range(total)]


def _collect_openai(ref, on_wait):
    import json

    client = _openai_client("openai")
    total = ref["n"]
    state = {}

    def check():
        b = client.batches.retrieve(ref["id"])
        state["batch"] = b
        counts = b.request_counts
        done = ((counts.completed or 0) + (counts.failed or 0)) if counts else 0
        return done, total, b.status in ("completed", "failed", "expired", "cancelled")
    _wait(check, on_wait)
    b = state["batch"]
    if b.status != "completed" or not b.output_file_id:
        raise RuntimeError(f"The AI batch ended as {b.status}.")
    answers = {}
    for line in client.files.content(b.output_file_id).text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        body = (row.get("response") or {}).get("body") or {}
        if (row.get("response") or {}).get("status_code") != 200 or not body.get("choices"):
            continue
        usage = body.get("usage") or {}
        answers[row["custom_id"]] = (body["choices"][0]["message"].get("content") or "",
                                     _half(TokenUsage(input_tokens=usage.get("prompt_tokens", 0) or 0,
                                                      output_tokens=usage.get("completion_tokens", 0) or 0)))
    failed = [n for n in range(total) if f"c{n}" not in answers]
    if failed:
        raise RuntimeError(f"{len(failed)} of {total} batch request(s) failed.")
    return [answers[f"c{n}"] for n in range(total)]


def _collect_gemini(ref, on_wait):
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    total = ref["n"]
    state = {}
    ended = ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED",
             "JOB_STATE_PARTIALLY_SUCCEEDED")

    def check():
        job = client.batches.get(name=ref["id"])
        state["job"] = job
        stats = job.completion_stats
        done = ((stats.successful_count or 0) + (stats.failed_count or 0)) if stats else 0
        name = getattr(job.state, "name", str(job.state))
        return done, total, name in ended
    _wait(check, on_wait)
    job = state["job"]
    responses = (job.dest.inlined_responses if job.dest else None) or []
    answers = []
    for item in responses:
        response = item.response
        if item.error or response is None:
            answers.append(None)
            continue
        usage = TokenUsage()
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        answers.append((response.text or "", _half(usage)))
    failed = total - sum(1 for a in answers[:total] if a)
    if failed:
        name = getattr(job.state, "name", str(job.state))
        raise RuntimeError(f"{failed} of {total} batch request(s) failed ({name}).")
    return answers[:total]
