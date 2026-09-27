from app import duplicates, ignored


def keys(pairs):
    return {p["key"] for p in pairs}


def test_food_duplicates():
    foods = [
        {"id": 1, "name": "Zwiebel", "plural_name": "Zwiebeln"}, {"id": 2, "name": "Zwiebeln"},
        {"id": 3, "name": "Zucchini"}, {"id": 4, "name": "Zuchini"},
        {"id": 5, "name": "Paprika"}, {"id": 6, "name": "Paprikapulver"},
        {"id": 9, "name": "Olivenöl"}, {"id": 10, "name": "olivenöl "},
        {"id": 11, "name": "Rotwein"}, {"id": 12, "name": "Weißwein"},
        {"id": 13, "name": "Salz"}, {"id": 14, "name": "Salat"},
    ]
    assert keys(duplicates.food_duplicates(foods)) == {"1:2", "3:4", "9:10"}


def test_unit_duplicates_use_known_abbreviations():
    units = [{"id": 1, "name": "EL"}, {"id": 2, "name": "Esslöffel"}, {"id": 3, "name": "tbsp"},
             {"id": 4, "name": "g"}, {"id": 5, "name": "Gramm"}, {"id": 6, "name": "TL"},
             {"id": 7, "name": "Prise", "plural_name": "Prisen"}, {"id": 8, "name": "Prisen"}]
    assert keys(duplicates.unit_duplicates(units)) == {"1:2", "1:3", "2:3", "4:5", "7:8"}


def test_groups_skip_ignored_pairs():
    pairs = duplicates.unit_duplicates([{"id": 1, "name": "EL"}, {"id": 2, "name": "Esslöffel"},
                                        {"id": 4, "name": "g"}, {"id": 5, "name": "Gramm"}])
    ignored.add("units_duplicates", [{"key": "4:5", "name": "g ↔ Gramm"}])
    assert duplicates.open_duplicate_ids(pairs, "units_duplicates") == [[1, 2]]


def test_pack_groups_keeps_groups_together():
    assert duplicates.pack_groups([[1, 2, 3], [4, 5], [6, 7]], 4) == [[1, 2, 3], [4, 5, 6, 7]]


def test_merges_of_ignored_pairs_are_dropped():
    ignored.add("foods_duplicates", [{"key": "5:6", "name": "Paprika ↔ Paprikas"}])
    actions = [
        {"type": "merge", "keep_id": 5, "keep_name": "Paprika", "remove_ids": [6]},
        {"type": "merge", "keep_id": 1, "keep_name": "Zwiebel", "remove_ids": [2, 6]},
        {"type": "rename", "id": 3, "new_name": "Zucchini"},
    ]
    result = duplicates.drop_ignored_merges(actions, "foods_duplicates")
    assert result == [{"type": "merge", "keep_id": 1, "keep_name": "Zwiebel", "remove_ids": [2, 6]},
                      {"type": "rename", "id": 3, "new_name": "Zucchini"}]
