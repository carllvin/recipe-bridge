"""'Butter oder Margarine': the first is the ingredient, the others its note."""
from app import ai_extractor
from app.prep_notes import split_alternatives


def test_split_alternatives():
    assert split_alternatives("Zutat 1, Zutat 2 oder Zutat 3") == ("Zutat 1", "oder Zutat 2, Zutat 3")
    assert split_alternatives("Butter oder Margarine") == ("Butter", "oder Margarine")
    assert split_alternatives("Honig oder Ahornsirup oder Agavendicksaft") == ("Honig", "oder Ahornsirup, Agavendicksaft")
    assert split_alternatives("lemon, lime or orange juice") == ("lemon", "or lime, orange juice")
    assert split_alternatives("burro o margarina") == ("burro", "o margarina")
    for plain in ("Salz und Pfeffer", "glass of water", "Mehl", "Oder-Barsch", "Tomaten, gehackt"):
        assert split_alternatives(plain) == (plain, None)


def test_extraction_keeps_only_the_first():
    raw = [{"title": "Kuchen", "steps": [{"instruction": "Backen."}], "ingredients": [
        {"amount": 100, "unit": "g", "name": "Butter oder Margarine", "note": "weich", "step_index": 0},
        {"amount": 200, "unit": "g", "name": "Mehl", "note": None, "step_index": 0}]}]
    recipe = ai_extractor._dedupe_recipes(raw)[0]
    first = recipe.ingredients[0]
    assert (first.name, first.note) == ("Butter", "oder Margarine, weich")
    assert recipe.ingredients[1].name == "Mehl" and not recipe.ingredients[1].note


def test_shortened_compounds_are_written_out():
    assert split_alternatives("frische Romano- oder Wachs-, grüne Bohnen") == (
        "frische Romanobohnen", "oder Wachsbohnen, grüne Bohnen")
    assert split_alternatives("Zitronen- oder Orangensaft") == ("Zitronensaft", "oder Orangensaft")
    assert split_alternatives("Hokkaido- oder Butternusskürbis") == ("Hokkaidokürbis", "oder Butternusskürbis")
    name, note = split_alternatives("Quitten- oder Xyzgelee")  # unknown end: kept whole, never "Quitten-"
    assert note is None and not name.endswith("-")


def test_a_name_the_ai_left_cut_off_is_completed():
    raw = [{"title": "Bohnensalat", "steps": [{"instruction": "Mischen."}], "ingredients": [
        {"amount": 400, "unit": "g", "name": "frische Romano-", "note": "oder Wachs-, grüne Bohnen", "step_index": 0}]}]
    ing = ai_extractor._dedupe_recipes(raw)[0].ingredients[0]
    assert (ing.name, ing.note) == ("frische Romanobohnen", "oder Wachsbohnen, grüne Bohnen")
