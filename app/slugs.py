"""Milestone "Complete Product Build" (pokračování) - stabilní URL slug
pro `/sunset/{slug}/` (zadání bod 2 - "MUSÍ implementovat place pages").

Slug se počítá z `name` (diakritika pryč, lowercase, mezery/interpunkce
na pomlčky). Kolize (dva kandidáti stejného jména, běžné u "Vyhlídka"
apod.) se řeší deterministickým appendem kratšího otisku `id` - stejný
katalog vždy vygeneruje STEJNÉ slugy (žádná závislost na pořadí zpracování
kromě první-vyhrává u čistých duplicit jména, což je vzácné a neškodné -
druhý kandidát dostane jen delší, pořád stabilní slug)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub("-", ascii_only.lower()).strip("-")
    return slug or "misto"


def _short_id_suffix(candidate_id: str) -> str:
    return hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:6]


def assign_slugs(candidates: list[dict]) -> None:
    """Mutuje `candidates` na místě, přidá klíč `slug` do každého dict.
    Deterministické pořadí zpracování (podle `id`, ne podle pořadí v
    seznamu) - katalog je jinak sestavován z dict/set operací
    (`deduplicate()`), jejichž pořadí není garantované napříč běhy."""
    used: set[str] = set()
    for c in sorted(candidates, key=lambda c: c["id"]):
        # Milestone "Data Foundation" - celostátní katalog má hodně
        # kandidátů BEZ OSM `name` (celý smysl name-resolution pipeline,
        # viz app/place_names.py) - slug se pak generuje ze skutečně
        # zobrazovaného `display_name`, ne z prázdného/chybějícího `name`.
        base = slugify(c.get("display_name") or c["name"])
        slug = base
        if slug in used:
            slug = f"{base}-{_short_id_suffix(c['id'])}"
        used.add(slug)
        c["slug"] = slug


def find_by_slug(candidates: list[dict], slug: str) -> dict | None:
    for c in candidates:
        if c.get("slug") == slug:
            return c
    return None
