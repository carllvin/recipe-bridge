"""When an ingredient like "gemahlene Mandeln" is renamed to (or merged
into) "Mandeln", the preparation isn't lost: "gemahlen" becomes the note of
every recipe line that used it.

prep_note() derives it from the two names, no AI: the words of the old
name that the new name doesn't have - only when every word of the new
name is in the old one (so a translation like "ground almonds" -> "Mandeln"
adds nothing). German adjective endings are taken off ("gemahlene" ->
"gemahlen", "gehackte" -> "gehackt", "frische" -> "frisch")."""
from __future__ import annotations

import re

MAX_NOTE_WORDS = 3


def _words(name) -> list[str]:
    return [w for w in re.split(r"[\s,/]+", (name or "").strip()) if w]


def _same(a: str, b: str) -> bool:
    """Same word, allowing singular/plural ("Mandel" / "Mandeln")."""
    a, b = a.casefold(), b.casefold()
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 2)


def base_form(word: str) -> str:
    """The uninflected adjective / participle: gemahlene -> gemahlen."""
    w = word.casefold()
    if m := re.fullmatch(r"(.+en)(e|er|es|em|en)", w):
        if w.startswith("ge") or m.group(1).endswith(("hlen", "ssen", "eben", "önen", "den")):
            return m.group(1)  # gemahlen-e, gerieben-er, gebacken-en
    if m := re.fullmatch(r"(.+t)(e|er|es|em|en)", w):
        return m.group(1)      # gehackt-e, geschält-e, geröstet-e
    if w.startswith("ge") and w.endswith("en"):
        return w               # gemahlen (already uninflected)
    if m := re.fullmatch(r"(.{3,}?)(e|er|es|em|en)", w):
        return m.group(1)      # frisch-e, fein-er
    return w


def prep_note(old: str, new: str) -> str | None:
    old_words, new_words = _words(old), _words(new)
    if not old_words or not new_words:
        return None
    rest = list(old_words)
    for word in new_words:
        match = next((o for o in rest if _same(o, word)), None)
        if match is None:
            return None  # not a shortened form of the old name (e.g. a translation)
        rest.remove(match)
    if not rest or len(rest) > MAX_NOTE_WORDS:
        return None
    return " ".join(base_form(w) for w in rest)


def with_note(existing: str | None, note: str) -> str:
    """The row's note with `note` in front - unless it's already there."""
    existing = (existing or "").strip()
    if note.casefold() in existing.casefold():
        return existing
    return f"{note}, {existing}" if existing else note
