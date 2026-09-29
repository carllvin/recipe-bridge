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
