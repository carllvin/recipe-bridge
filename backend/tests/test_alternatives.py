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
