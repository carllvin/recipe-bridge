"""How quickly a recipe's fresh ingredients spoil - so the weekly plan can
put fresh fish and leafy greens right after the shopping day and pantry or
frozen dishes at the end of the week. Word stems (German and English),
matched in the ingredient names like seasonal.py does; processed and
long-keeping forms ("Fischsauce", "TK-Spinat", "Thunfisch aus der Dose")
don't count.

Levels: 3 = use within 1-2 days, 2 = within 3-4 days, 0 = keeps (not
listed)."""
from __future__ import annotations

import re

LEVELS = {
    3: ["fisch", "fish", "lachs", "salmon", "forelle", "trout", "kabeljau", "dorsch", "cod", "seelachs", "zander",
        "scholle", "dorade", "wolfsbarsch", "sea bass", "garnele", "shrimp", "prawn", "scampi", "muschel", "mussel",
        "clam", "jakobsmuschel", "scallop", "tintenfisch", "calamar", "squid", "oktopus", "octopus",
        "hackfleisch", "rinderhack", "schweinehack", "lammhack", "putenhack", "hähnchenhack", "gemischtes hack",
        "gehacktes", "minced", "ground beef", "ground pork",
        "spinat", "spinach", "salat", "lettuce", "rucola", "arugula", "rocket", "mangold", "chard", "kresse", "cress",
        "basilikum", "basil", "koriander", "cilantro", "dill", "minze", "mint", "schnittlauch", "chives",
        "erdbeer", "strawberr", "himbeer", "raspberr", "heidelbeer", "blaubeer", "blueberr", "brombeer", "blackberr",
        "pilz", "champignon", "mushroom", "spargel", "asparagus", "sprossen", "sprouts"],
    2: ["hähnchen", "haehnchen", "huhn", "chicken", "pute", "truthahn", "turkey", "ente", "duck",
        "rind", "beef", "steak", "schwein", "pork", "lamm", "lamb", "kalb", "veal", "wurst", "sausage",
        "zucchini", "courgette", "brokkoli", "broccoli", "blumenkohl", "cauliflower", "paprika", "bell pepper",
        "tomate", "tomato", "gurke", "cucumber", "aubergine", "eggplant", "grüne bohnen", "green beans",
        "tofu", "mozzarella", "ricotta", "frischkäse", "cream cheese", "burrata", "sahne", "cream", "joghurt", "yogurt",
        "avocado", "mais", "corn", "lauch", "leek", "frühlingszwiebel", "spring onion", "fenchel", "fennel"],
}
# Processed or long-keeping forms.
KEEPS = ("sauce", "soße", "sosse", "brühe", "bruehe", "fond", "stock", "pulver", "powder", "getrocknet", "dried",
         "tiefkühl", "tiefgekühlt", "tk-", "(tk)", "frozen", "dose", "canned", "paste", "mark", "öl", "oil",
         "essig", "vinegar", "saft", "juice", "konfitüre", "marmelade", "jam", "gewürz", "spice", "räucher", "smoked",
         "salami", "schinken", "parmesan", "samen", "seeds", "mehl", "flour", "passiert")
# Stems that would match inside unrelated words.
NOT = {"salat": ("salatgurke",), "rind": ("zimtrinde", "brotrinde", "käserinde"), "mint": ("minute",), "dill": ("dille",), "mais": ("maisstärke",),
       "corn": ("cornstarch", "cornflakes", "popcorn", "cornichon"), "lamm": ("lammfell",)}


def _norm(text) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def level_of(name) -> int:
    name = _norm(name)
    if not name or any(k in name for k in KEEPS):
        return 0
    for level in (3, 2):
        for stem in LEVELS[level]:
            if stem in name and not any(x in name for x in NOT.get(stem, ())):
                return level
    return 0


def of_recipe(food_names) -> tuple[int, list[str]]:
    """(highest level, the ingredients with that level) for one recipe's
    ingredient names."""
    found = {}
    for name in food_names:
        if name:
            found.setdefault(level_of(name), []).append(name)
    top = max((lvl for lvl in found if lvl), default=0)
    return top, list(dict.fromkeys(found.get(top, []))) if top else []


# Fresh things usually bought in a larger pack than one recipe needs - what
# is left should go into another dish a day or two later. Stem -> matched
# like the levels above (KEEPS and NOT apply too).
LEFTOVERS = [
    "sahne", "cream", "schmand", "crème fraîche", "creme fraiche", "saure sahne", "sour cream", "buttermilch",
    "buttermilk", "mascarpone", "ricotta", "mozzarella", "feta", "frischkäse", "cream cheese", "joghurt", "yogurt",
    "quark", "kokosmilch", "coconut milk",
    "basilikum", "basil", "koriander", "cilantro", "petersilie", "parsley", "dill", "minze", "mint",
    "schnittlauch", "chives", "thymian", "thyme", "rosmarin", "rosemary", "salbei", "sage",
    "staudensellerie", "celery", "lauch", "leek", "frühlingszwiebel", "spring onion", "spinat", "spinach",
    "rucola", "arugula", "fenchel", "fennel", "ingwer", "ginger", "limette", "lime",
]
# "Leftover" stems that name the same thing (so Schlagsahne and Sahne match).
SAME = {"cream": "sahne", "schmand": "saure sahne", "sour cream": "saure sahne", "creme fraiche": "crème fraîche",
        "buttermilk": "buttermilch", "cream cheese": "frischkäse", "yogurt": "joghurt", "coconut milk": "kokosmilch",
        "basil": "basilikum", "cilantro": "koriander", "parsley": "petersilie", "mint": "minze", "chives": "schnittlauch",
        "thyme": "thymian", "rosemary": "rosmarin", "sage": "salbei", "celery": "staudensellerie", "leek": "lauch",
        "spring onion": "frühlingszwiebel", "spinach": "spinat", "arugula": "rucola", "fennel": "fenchel",
        "ginger": "ingwer", "lime": "limette"}


def leftovers_of(food_names) -> dict[str, str]:
    """Stem -> ingredient name for a recipe's ingredients that often leave a
    rest (LEFTOVERS). The longest stem wins: "saure sahne" before "sahne"."""
    found = {}
    for name in food_names:
        norm = _norm(name)
        if not norm or any(k in norm for k in KEEPS if k not in ("paste",)):
            continue
        stems = [st for st in LEFTOVERS if st in norm and not any(x in norm for x in NOT.get(st, ()))]
        if stems:
            stem = max(stems, key=len)
            found.setdefault(SAME.get(stem, stem), name)
    return found
