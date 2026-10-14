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


# "Butter oder Margarine", "Zitrone, Limette or Orange": the first one is the
# ingredient, the others go into the note ("oder Margarine") - the
# extraction asks the AI for that, this catches what slips through
_ALTERNATIVE = re.compile(r"\s+(or|oder|ou|o|oppure)\s+", re.IGNORECASE)

# A German shortened compound ("Romano- oder Wachsbohnen", "Zitronen- und
# Orangensaft"): the word before the hyphen shares the end of a later one
_ELIDED = re.compile(r"(?<![\w-])([^\W\d_]+)-(?=\s*(?:,|$|(?:or|oder|ou|o|oppure|und|and|bzw)\b))", re.IGNORECASE)
# the common ends of compound ingredient names, longest first when matching
_HEADS = sorted("""bohnen erbsen linsen saft mehl zucker käse öl essig pfeffer salz kraut salat nudeln beeren
milch sahne butter wurst schinken fleisch fisch brot brötchen schale kerne nüsse kohl rüben zwiebeln
pilze tomaten paprika sirup senf soße sauce brühe fond wein likör tee pulver flocken creme quark
joghurt mus mark blätter würfel streifen filet filets kartoffeln äpfel birnen trauben kürbis lauch
möhren karotten mandeln oliven chili chilis sprossen samen gewürz kräuter körner reis graupen""".split(), key=len, reverse=True)


def _compound_end(text: str) -> str | None:
    """What a shortened compound in text is completed with: a noun standing
    on its own at the end ("grüne Bohnen" -> "bohnen"), else the known end
    of the last compound ("Wachsbohnen" -> "bohnen"); None when unsure."""
    words = re.findall(r"[^\W\d_][\w]*", text)
    if not words:
        return None
    last = words[-1]
    before = text[:text.rfind(last)].rstrip()
    if len(words) >= 2 and last[0].isupper() and not before.endswith("-") and not _ALTERNATIVE.search(f" {words[-2]} "):
        return last.casefold()
    return next((h for h in _HEADS if last.casefold().endswith(h) and len(last) > len(h)), None)


def complete_compounds(text: str, end: str | None = None) -> str:
    """"Romano- oder Wachsbohnen" -> "Romanobohnen oder Wachsbohnen" - every
    word is written out (an ingredient is never called "Romano-")."""
    end = end or _compound_end(text)
    if not end:
        return text
    return _ELIDED.sub(lambda m: m.group(1) + end, text)


def split_alternatives(name: str) -> tuple[str, str | None]:
    """(the first ingredient, "or the others") - (name, None) when there are
    no alternatives. Shortened compounds are written out first; when that
    isn't possible the name stays whole rather than ending in "-"."""
    name = (name or "").strip()
    match = _ALTERNATIVE.search(name)
    if not match:
        return name, None
    name = complete_compounds(name)
    if _ELIDED.search(name):
        return name, None
    word = match.group(1)
    parts = [p.strip() for p in re.split(r"\s*,\s*|" + _ALTERNATIVE.pattern, name, flags=re.IGNORECASE)
             if p and p.strip() and p.strip().casefold() not in ("or", "oder", "ou", "o", "oppure")]
    if len(parts) < 2:
        return name, None
    return parts[0], f"{word.lower()} {', '.join(parts[1:])}"
