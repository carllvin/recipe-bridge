"""Reading JSON answers from the AI.

Models now and then answer with almost-JSON: a code fence around it, a
sentence before it, a double quote inside a text that isn't escaped
('Harissa "light"', '2" pieces'), a raw line break inside a string or a
trailing comma. parse() tries the answer as is, then the JSON part of it,
then a repaired version; only if all of that fails it raises the original
error (callers may then ask the AI once more)."""
from __future__ import annotations

import json
import re

_LITERALS = ("true", "false", "null")


def _strip(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _outer(text: str) -> str | None:
    """The part from the first { or [ to the matching last } or ]."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    return text[start:end + 1] if end > start else None


def _closes_string(text: str, i: int) -> bool:
    """Is the quote at text[i] the end of a string (and not a quote inside
    the text)? Judged by what follows it."""
    j = i + 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j >= len(text) or text[j] in "}]:":
        return True
    if text[j] != ",":
        return False
    k = j + 1
    while k < len(text) and text[k] in " \t\r\n":
        k += 1
    if k >= len(text):
        return True
    rest = text[k:]
    return rest[0] in '"{[]}-0123456789' or rest.startswith(_LITERALS)


def repair(text: str) -> str:
    """Escapes quotes and line breaks inside strings, drops trailing commas."""
    out, in_string, escape = [], False, False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
                out.append(ch)
            elif ch == "\\":
                escape = True
                out.append(ch)
            elif ch == '"':
                if _closes_string(text, i):
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                continue
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def parse(text: str):
    text = _strip(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = exc
    candidates = [c for c in (_outer(text),) if c and c != text] + [text]
    for candidate in candidates:
        for attempt in (candidate, repair(candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise first_error


RETRY_NOTE = ("\n\nIMPORTANT: your previous answer was not valid JSON. Answer with valid JSON only - "
              "escape every double quote inside a text as \\\" and don't put line breaks inside strings.")


def complete(ask, system_prompt: str):
    """ask(system_prompt) -> (text, usage). Parses the answer; if it can't
    be read even after repair, asks once more with a note about valid JSON.
    Returns (parsed, usage of the last call)."""
    text, usage = ask(system_prompt)
    try:
        return parse(text), usage
    except json.JSONDecodeError:
        text, usage2 = ask(system_prompt + RETRY_NOTE)
        for field in ("input_tokens", "output_tokens"):
            if hasattr(usage2, field) and hasattr(usage, field):
                setattr(usage2, field, (getattr(usage, field) or 0) + (getattr(usage2, field) or 0))
        return parse(text), usage2
