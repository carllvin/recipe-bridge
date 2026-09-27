"""What's in season: a calendar of local fruit and vegetables (Central
Europe, fresh from the field or from winter storage), month by month.

Used by the plan area ("in season now"), by "what can I cook today?"
(recipes with seasonal ingredients rank higher) and by the weekly plan
(the AI sees which recipes use seasonal produce).

Each entry: German name, English name, and word stems that recognize it in
an ingredient name in either language ("kürbis" also finds
"Butternutkürbis", "squash" finds "butternut squash")."""
from __future__ import annotations

import datetime as dt
import re

from .config import get_language_code, settings

# key: (German, English, stems[, excludes]) - an ingredient name containing
# an exclude isn't that produce ("Schnittlauch" is not leek).
PRODUCE = {
    "apple": ("Äpfel", "Apples", ["apfel", "äpfel", "apple"]),
    "pear": ("Birnen", "Pears", ["birne", "pear"]),
    "quince": ("Quitten", "Quinces", ["quitte", "quince"]),
    "plum": ("Zwetschgen", "Plums", ["zwetschg", "pflaume", "plum"], ["tomate", "tomato"]),
    "cherry": ("Kirschen", "Cherries", ["kirsch", "cherr"], ["tomate", "tomato"]),
    "strawberry": ("Erdbeeren", "Strawberries", ["erdbeer", "strawberr"]),
    "raspberry": ("Himbeeren", "Raspberries", ["himbeer", "raspberr"]),
    "blueberry": ("Heidelbeeren", "Blueberries", ["heidelbeer", "blaubeer", "blueberr"]),
    "blackberry": ("Brombeeren", "Blackberries", ["brombeer", "blackberr"]),
    "currant": ("Johannisbeeren", "Currants", ["johannisbeer", "currant"]),
    "gooseberry": ("Stachelbeeren", "Gooseberries", ["stachelbeer", "gooseberr"]),
    "apricot": ("Aprikosen", "Apricots", ["aprikose", "marille", "apricot"]),
    "peach": ("Pfirsiche", "Peaches", ["pfirsich", "peach"]),
    "grape": ("Trauben", "Grapes", ["weintraube", "trauben", "grape"], ["grapefruit"]),
    "rhubarb": ("Rhabarber", "Rhubarb", ["rhabarber", "rhubarb"]),
    "elder": ("Holunder", "Elderberries", ["holunder", "elderberr", "elderflower"]),
    "asparagus": ("Spargel", "Asparagus", ["spargel", "asparagus"]),
    "wild_garlic": ("Bärlauch", "Wild garlic", ["bärlauch", "wild garlic", "ramson"]),
    "spinach": ("Spinat", "Spinach", ["spinat", "spinach"]),
    "radish": ("Radieschen", "Radishes", ["radieschen", "radish"]),
    "spring_onion": ("Frühlingszwiebeln", "Spring onions", ["frühlingszwiebel", "lauchzwiebel", "spring onion", "scallion"]),
    "rocket": ("Rucola", "Rocket", ["rucola", "rauke", "rocket", "arugula"]),
    "lettuce": ("Salat", "Lettuce", ["kopfsalat", "eisbergsalat", "romanasalat", "lettuce"]),
    "kohlrabi": ("Kohlrabi", "Kohlrabi", ["kohlrabi"]),
    "chard": ("Mangold", "Chard", ["mangold", "chard"]),
    "peas": ("Erbsen", "Peas", ["erbse", "zuckerschote", "peas", "garden pea", "sugar snap"], ["kichererbse", "chickpea", "schälerbse", "split pea"]),
    "beans": ("Bohnen", "Green beans", ["grüne bohne", "buschbohne", "stangenbohne", "green bean"]),
    "zucchini": ("Zucchini", "Zucchini", ["zucchini", "courgette"]),
    "cucumber": ("Gurken", "Cucumbers", ["gurke", "cucumber"], ["gewürzgurke", "essiggurke", "pickle"]),
    "tomato": ("Tomaten", "Tomatoes", ["tomate", "tomato"]),
    "pepper": ("Paprika", "Bell peppers", ["paprikaschote", "spitzpaprika", "bell pepper"]),
    "eggplant": ("Auberginen", "Aubergines", ["aubergine", "eggplant"]),
    "corn": ("Mais", "Sweetcorn", ["maiskolben", "zuckermais", "sweetcorn", "corn on the cob"]),
    "fennel": ("Fenchel", "Fennel", ["fenchel", "fennel"]),
    "broccoli": ("Brokkoli", "Broccoli", ["brokkoli", "broccoli"]),
    "cauliflower": ("Blumenkohl", "Cauliflower", ["blumenkohl", "cauliflower"]),
    "new_potato": ("Frühkartoffeln", "New potatoes", ["frühkartoffel", "new potato"]),
    "pumpkin": ("Kürbis", "Pumpkin & squash", ["kürbis", "pumpkin", "squash"]),
    "mushroom": ("Pilze", "Mushrooms", ["pilz", "champignon", "pfifferling", "steinpilz", "mushroom", "chanterelle"]),
    "chestnut": ("Maronen", "Chestnuts", ["marone", "esskastanie", "chestnut"]),
    "walnut": ("Walnüsse", "Walnuts", ["walnuss", "walnüsse", "walnut"]),
    "beetroot": ("Rote Bete", "Beetroot", ["rote bete", "rote beete", "rote rübe", "beetroot", "beet"]),
    "celeriac": ("Knollensellerie", "Celeriac", ["knollensellerie", "sellerieknolle", "celeriac"]),
    "parsnip": ("Pastinaken", "Parsnips", ["pastinake", "parsnip"]),
    "salsify": ("Schwarzwurzeln", "Salsify", ["schwarzwurzel", "salsify"]),
    "jerusalem_artichoke": ("Topinambur", "Jerusalem artichokes", ["topinambur", "jerusalem artichoke", "sunchoke"]),
    "swede": ("Steckrüben", "Swedes", ["steckrübe", "swede", "rutabaga"]),
    "leek": ("Lauch", "Leeks", ["lauch", "porree", "leek"], ["bärlauch", "schnittlauch", "lauchzwiebel"]),
    "lambs_lettuce": ("Feldsalat", "Lamb's lettuce", ["feldsalat", "lamb's lettuce", "corn salad"]),
    "chicory": ("Chicorée", "Chicory", ["chicorée", "chicoree", "chicory"]),
    "kale": ("Grünkohl", "Kale", ["grünkohl", "kale"]),
    "brussels": ("Rosenkohl", "Brussels sprouts", ["rosenkohl", "brussels sprout"]),
    "savoy": ("Wirsing", "Savoy cabbage", ["wirsing", "savoy"]),
    "red_cabbage": ("Rotkohl", "Red cabbage", ["rotkohl", "blaukraut", "red cabbage"]),
    "white_cabbage": ("Weißkohl", "White cabbage", ["weißkohl", "weisskohl", "spitzkohl", "white cabbage"]),
}

WINTER = ["kale", "brussels", "savoy", "red_cabbage", "white_cabbage", "leek", "lambs_lettuce", "chicory",
          "salsify", "parsnip", "jerusalem_artichoke", "beetroot", "celeriac", "swede", "apple"]
CALENDAR = {
    1: WINTER + ["pear"],
    2: WINTER,
    3: ["leek", "lambs_lettuce", "chicory", "spinach", "wild_garlic", "beetroot", "parsnip", "apple", "white_cabbage"],
    4: ["wild_garlic", "spinach", "rhubarb", "radish", "spring_onion", "asparagus", "rocket", "lambs_lettuce", "leek"],
    5: ["asparagus", "rhubarb", "radish", "spinach", "kohlrabi", "spring_onion", "lettuce", "rocket", "strawberry",
        "chard", "cauliflower"],
    6: ["asparagus", "strawberry", "cherry", "peas", "zucchini", "kohlrabi", "broccoli", "cauliflower", "currant",
        "gooseberry", "lettuce", "cucumber", "chard", "new_potato", "raspberry", "rhubarb"],
    7: ["zucchini", "tomato", "cucumber", "beans", "peas", "pepper", "broccoli", "cauliflower", "cherry", "raspberry",
        "blueberry", "apricot", "currant", "chard", "fennel", "lettuce", "new_potato"],
    8: ["tomato", "zucchini", "pepper", "eggplant", "beans", "corn", "cucumber", "blackberry", "blueberry", "plum",
        "peach", "fennel", "chard", "mushroom", "raspberry"],
    9: ["pumpkin", "plum", "apple", "pear", "grape", "tomato", "pepper", "eggplant", "zucchini", "corn", "beetroot",
        "mushroom", "blackberry", "elder", "fennel", "leek", "beans"],
    10: ["pumpkin", "apple", "pear", "grape", "quince", "mushroom", "chestnut", "walnut", "beetroot", "brussels",
         "leek", "parsnip", "celeriac", "savoy", "lambs_lettuce", "kale"],
    11: ["pumpkin", "kale", "brussels", "savoy", "red_cabbage", "parsnip", "salsify", "jerusalem_artichoke",
         "lambs_lettuce", "leek", "apple", "pear", "quince", "chestnut", "chicory", "celeriac"],
    12: WINTER + ["chestnut"],
}


def in_season(month: int | None = None) -> list[str]:
    return CALENDAR[month or dt.date.today().month]


def display_names(keys) -> list[str]:
    german = get_language_code(settings.output_language) == "de"
    return [PRODUCE[k][0] if german else PRODUCE[k][1] for k in keys]


def _norm(text) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


# Processed or long-keeping forms don't count as fresh seasonal produce.
PROCESSED = ("essig", "vinegar", "saft", "juice", "pulver", "powder", "kerne", "seeds", "sirup", "syrup",
             "getrocknet", "dried", "konfitüre", "marmelade", "jam", "dose", "canned", "tiefgekühlt", "frozen", "(tk)")


def _is(key, name) -> bool:
    entry = PRODUCE[key]
    excludes = entry[3] if len(entry) > 3 else []
    if any(p in name for p in PROCESSED) or any(x in name for x in excludes):
        return False
    return any(stem in name for stem in entry[2])


def seasonal_in(food_names, month: int | None = None) -> list[str]:
    """Keys of the produce in season that appear among these ingredient
    names."""
    names = [_norm(n) for n in food_names if n]
    return [key for key in in_season(month) if any(_is(key, name) for name in names)]
