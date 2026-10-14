"""Overlapping chunks read some recipes twice - with a drifted title or page
number; those become one recipe, real variants stay apart."""
from app import ai_extractor

METHOD = ("Die Zwiebeln würfeln und in Olivenöl glasig dünsten. Paprika und Tomaten dazugeben und "
          "15 Minuten schmoren. Mulden formen, die Eier hineinschlagen und zugedeckt garen, bis das Eiweiß stockt.")


def recipe(title, start, end, ingredients, method=METHOD):
    return {"title": title, "source_page_start": start, "source_page_end": end,
            "ingredients": [{"name": n, "amount": 1, "unit": None, "step_index": 0} for n in ingredients],
            "steps": [{"instruction": method}]}


SHAKSHUKA = ["Zwiebel", "Paprika", "Tomaten", "Eier", "Olivenöl", "Feta"]


def titles(raw):
    return [r.title for r in ai_extractor._dedupe_recipes(raw)]


def test_drifted_title_and_pages_become_one():
    # the second chunk translated the title differently and named the printed page
    raw = [recipe("Shakshuka mit Feta", 44, 45, SHAKSHUKA),
           recipe("Shakshuka mit Schafskäse", 42, 42, SHAKSHUKA[:4] + ["Olivenöl", "Schafskäse"])]
    assert titles(raw) == ["Shakshuka mit Feta"]


def test_a_partial_reading_is_merged_into_the_full_one():
    raw = [recipe("Shakshuka", 44, 44, SHAKSHUKA[:3], METHOD[:90]), recipe("Shakshuka", 44, 45, SHAKSHUKA)]
    (only,) = ai_extractor._dedupe_recipes(raw)
    assert len(only.ingredients) == 6 and (only.source_page_start, only.source_page_end) == (44, 45)


def test_variants_stay_apart():
    pancake = "Mehl, Milch und Eier glatt rühren, 20 Minuten quellen lassen und dünne Pfannkuchen backen."
    apple = "Äpfel schälen und in Spalten schneiden, in Butter karamellisieren, den Teig darübergießen und stocken lassen."
    raw = [recipe("Pfannkuchen", 10, 10, ["Mehl", "Milch", "Eier", "Salz", "Butter"], pancake),
           recipe("Apfelpfannkuchen", 11, 11, ["Mehl", "Milch", "Eier", "Salz", "Butter", "Äpfel"], apple)]
    assert titles(raw) == ["Pfannkuchen", "Apfelpfannkuchen"]


def test_same_title_far_apart_or_different_stays():
    dressing = ["Essig", "Öl", "Senf"]
    raw = [recipe("Dressing", 10, 10, dressing), recipe("Dressing", 80, 80, dressing)]
    assert titles(raw) == ["Dressing", "Dressing"]
    raw = [recipe("Dressing", 10, 10, dressing), recipe("Dressing", 11, 11, ["Joghurt", "Zitrone", "Dill"], "x" * 50)]
    assert titles(raw) == ["Dressing", "Dressing"]
