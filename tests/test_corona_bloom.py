"""Testy pro Corona Bloom ukazatel skóre (Milník 12 - přenos finálního
mobilního designu z Artifactu do appky).

Logika žije v JS (static/sunset_viz.js, zdvojená do static/app.js - viz
komentáře tam proč), takže projektová konvence čistě substring assertů
(viz test_web.py test_frontend_*) by u téhle funkce neověřila SKUTEČNÉ
chování (hranice kategorií, clamping, "chybí skóre" větev). Tyhle testy
proto skutečně SPOUŠTĚJÍ funkci přes Node (stejný sandbox jako
`node --check` v dev workflow) - přeskočí se, pokud Node není k
dispozici, aby nebyl nový tvrdý závislost na zbytku testovací sady.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js není k dispozici")

# `app.js` je CELOSTRÁNKOVÝ skript (čte `document`/`window` hned při
# načtení) - do Node VM se smí vytáhnout jen samotný Corona Bloom blok
# (stejné hranice markerů v obou souborech, viz jejich komentáře), ne
# celý soubor.
_START_MARKER = "const CORONA_BLOOM_TIERS = {"
_END_MARKERS = {
    "sunset_viz.js": "const HIKE_ACCESS_LABELS",
    "app.js": "function renderRatingRing(rating)",
}


def _extract_corona_bloom_block(source_file: str) -> str:
    text = (STATIC_DIR / source_file).read_text()
    start = text.index(_START_MARKER)
    end = text.index(_END_MARKERS[source_file], start)
    return text[start:end]


def _run_corona_bloom_probe(source_file: str) -> dict:
    """Vytáhne jen Corona Bloom blok z `source_file` (sunset_viz.js nebo
    app.js), spustí ho v Node VM a vrátí JSON se skutečným výstupem
    `coronaBloomTier`/`coronaBloomSvg`/`coronaBloomAccentColor` pro
    reprezentativní vstupy (zadání bod 6 - "ověř reprezentativní skóre ze
    všech čtyř kategorií, krajní hodnoty, nulu a chybějící skóre")."""
    block = _extract_corona_bloom_block(source_file)
    script = f"""
    const vm = require('vm');
    const src = {json.dumps(block)};
    const ctx = {{}};
    vm.createContext(ctx);
    vm.runInContext(src, ctx);
    const scores = [97, 85, 84, 72, 70, 69, 55, 50, 49, 10, 0, 100, -5, 150, null, undefined, NaN, "72"];
    const out = scores.map((s) => {{
      const svg = ctx.coronaBloomSvg(s, 52);
      return {{
        score: s === undefined ? "__undefined__" : (typeof s === "number" && Number.isNaN(s)) ? "__nan__" : s,
        tier: Number.isNaN(Number(s)) || s === null || s === undefined ? null : ctx.coronaBloomTier(Math.max(0, Math.min(100, Number(s)))),
        isMissing: svg.includes('Score not available'),
        hasNumber: /font-weight="700" fill="#fff"[^>]*>(\\d+)</.test(svg),
        accentColor: ctx.coronaBloomAccentColor(s),
      }};
    }});
    console.log(JSON.stringify(out));
    """
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("source_file", ["sunset_viz.js", "app.js"])
def test_corona_bloom_tier_boundaries_match_composite_label(source_file):
    # Hranice SCHVÁLNĚ stejné jako existující app/main.py::_composite_label()
    # (85/70/50) - zadání "přesné hranice kategorií převezmi z existujícího
    # návrhu, pokud jsou definované".
    rows = {r["score"]: r for r in _run_corona_bloom_probe(source_file) if r["score"] not in ("__undefined__",) and r["tier"] is not None}
    assert rows[97]["tier"] == "excellent"
    assert rows[85]["tier"] == "excellent"
    assert rows[84]["tier"] == "good"
    assert rows[70]["tier"] == "good"
    assert rows[69]["tier"] == "fair"
    assert rows[50]["tier"] == "fair"
    assert rows[49]["tier"] == "poor"
    assert rows[10]["tier"] == "poor"
    assert rows[0]["tier"] == "poor"
    assert rows[100]["tier"] == "excellent"


@pytest.mark.parametrize("source_file", ["sunset_viz.js", "app.js"])
def test_corona_bloom_clamps_out_of_range_scores(source_file):
    rows = {r["score"]: r for r in _run_corona_bloom_probe(source_file) if r["score"] not in ("__undefined__",) and r["tier"] is not None}
    assert rows[-5]["tier"] == "poor"  # clamp na 0
    assert rows[150]["tier"] == "excellent"  # clamp na 100


@pytest.mark.parametrize("source_file", ["sunset_viz.js", "app.js"])
def test_corona_bloom_coerces_numeric_strings(source_file):
    rows = {r["score"]: r for r in _run_corona_bloom_probe(source_file) if r["score"] not in ("__undefined__",) and r["tier"] is not None}
    assert rows["72"]["tier"] == "good"


@pytest.mark.parametrize("source_file", ["sunset_viz.js", "app.js"])
def test_corona_bloom_never_fabricates_missing_score(source_file):
    # Zadání "ošetři chybějící skóre... nezobrazuj skóre u míst, která ho
    # ještě nemají vypočtené" - null/undefined/NaN musí dát neutrální
    # placeholder BEZ čísla, nikdy vymyšlenou/nulovou hodnotu.
    rows = _run_corona_bloom_probe(source_file)
    by_score = {str(r["score"]): r for r in rows}
    for key in ("None", "__undefined__", "__nan__"):
        row = by_score[key]
        assert row["isMissing"] is True
        assert row["hasNumber"] is False


@pytest.mark.parametrize("source_file", ["sunset_viz.js", "app.js"])
def test_corona_bloom_real_scores_always_show_the_number(source_file):
    rows = _run_corona_bloom_probe(source_file)
    for row in rows:
        if row["tier"] is not None:
            assert row["hasNumber"] is True
            assert row["isMissing"] is False


def test_corona_bloom_logic_identical_between_app_js_and_sunset_viz_js():
    # Zadání bod 4 - "Nevytvářej několik odlišných implementací stejného
    # ukazatele... aby se při budoucích změnách nerozcházely barvy,
    # hranice skóre ani SVG." app.js si logiku ZÁMĚRNĚ duplikuje (Tonight
    # stránka nenačítá sunset_viz.js, viz komentáře v obou souborech),
    # ale výstup musí být BYTOVĚ identický pro stejný vstup - tenhle test
    # je jediná pojistka, že se obě kopie v budoucnu nerozejdou.
    probe_viz = _run_corona_bloom_probe("sunset_viz.js")
    probe_app = _run_corona_bloom_probe("app.js")
    assert probe_viz == probe_app
