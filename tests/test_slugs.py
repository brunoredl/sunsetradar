"""Testy pro app/slugs.py (milestone "Complete Product Build",
pokračování - `/sunset/{slug}/` place pages, zadání bod 2)."""

from __future__ import annotations

from app.slugs import assign_slugs, find_by_slug, slugify


def test_slugify_strips_diacritics_and_lowercases():
    assert slugify("Radhošť") == "radhost"


def test_slugify_replaces_spaces_and_punctuation():
    assert slugify("Lysá hora") == "lysa-hora"
    assert slugify("Frýdek-Místek") == "frydek-mistek"


def test_slugify_never_returns_empty_string():
    assert slugify("!!!") == "misto"


def test_assign_slugs_gives_unique_slugs_to_same_name_candidates():
    candidates = [
        {"id": "node/2", "name": "Vyhlídka"},
        {"id": "node/1", "name": "Vyhlídka"},
    ]
    assign_slugs(candidates)
    slugs = {c["slug"] for c in candidates}
    assert len(slugs) == 2  # žádná kolize, i když name je identické
    assert "vyhlidka" in slugs  # jeden z nich dostane čistý slug


def test_assign_slugs_is_deterministic_regardless_of_input_order():
    a = [{"id": "node/1", "name": "Vyhlídka"}, {"id": "node/2", "name": "Vyhlídka"}]
    b = [{"id": "node/2", "name": "Vyhlídka"}, {"id": "node/1", "name": "Vyhlídka"}]
    assign_slugs(a)
    assign_slugs(b)
    a_by_id = {c["id"]: c["slug"] for c in a}
    b_by_id = {c["id"]: c["slug"] for c in b}
    assert a_by_id == b_by_id


def test_find_by_slug_returns_matching_candidate():
    candidates = [{"id": "node/1", "name": "Radhošť", "slug": "radhost"}]
    assert find_by_slug(candidates, "radhost")["id"] == "node/1"
    assert find_by_slug(candidates, "neexistuje") is None
