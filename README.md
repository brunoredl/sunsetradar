# Sunset Radar

Geospatial engine: z GPS bodu + skutečného terénního modelu (DEM) určí,
kdy bude Slunce při západu (nebo východu) skutečně viditelné a kdy ho
zakryje okolní terén — ne jen tabulkový čas.

Technický plán, zdůvodnění knihoven/datových zdrojů, citlivostní analýza
a validace: viz [`PLAN.md`](PLAN.md).

## Instalace

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # requirements.txt + pytest
```

## Stažení testovacích dat (DEM)

Zdroj: [Copernicus DEM GLO-30](https://registry.opendata.aws/copernicus-dem/)
(zdarma, bez API klíče). Pro ruční stažení dvou testovacích dlaždic:

```bash
mkdir -p data
for TILE in Copernicus_DSM_COG_10_N50_00_E013_00_DEM Copernicus_DSM_COG_10_N50_00_E012_00_DEM; do
  curl -sL -o "data/${TILE#Copernicus_DSM_COG_10_}.tif" \
    "https://copernicus-dem-30m.s3.amazonaws.com/${TILE}/${TILE}.tif"
done
mv "data/N50_00_E013_00_DEM.tif" data/milesovka_dem.tif
mv "data/N50_00_E012_00_DEM.tif" data/krusne_hory_dem.tif
```

Nebo automaticky přes `app/tile_manager.py` (stahuje a cachuje podle
GPS bodu, viz níže) - to je preferovaný způsob pro cokoliv nového.

(~28 MB/dlaždice, licence: volné použití včetně komerčního, vyžaduje
atribuci Copernicus/ESA.)

## Spuštění

```bash
source .venv/bin/activate

python scripts/poc_sunset.py           # jeden bod, západní sektor, textový výstup + graf
python scripts/poc_360_horizon.py      # 360° horizont + kartézský a polární (panoramatický) graf
python scripts/find_test_points.py     # programové hledání testovacích bodů z DEM dat
python scripts/test_point_network.py   # síť 5 bodů v různém terénu, srovnávací graf
```

Použití DEM přes zdroj-agnostickou abstrakci (`DEMProvider`, viz
`app/dem_provider.py`) - terrain engine nikdy neví, jestli data přišla z
Copernicus nebo ČÚZK:

```python
from app.dem_provider import open_copernicus_dem, open_cuzk_dmr5g_dem

dem = open_copernicus_dem(lat=50.5552, lon=13.9312, radius_m=30_000)   # globální, 30m, DSM
# nebo:
dem = open_cuzk_dmr5g_dem(lat=50.5552, lon=13.9312, radius_m=30_000)   # jen ČR, ~2-20m, skutečné DTM
print(dem.elevation(50.5552, 13.9312))
```

**Doporučený způsob** (automatický výběr + fallback, viz sekce "DEM Strategy" níže):

```python
from app.dem_strategy import resolve_dem

dem, metadata = resolve_dem(lat=50.5552, lon=13.9312, analysis_range_m=50_000)
print(metadata.describe())   # "ČÚZK DMR 5G (DTM) · 10 m"
print(dem.elevation(50.5552, 13.9312))
```

Porovnání obou zdrojů na síti testovacích bodů:

```bash
python scripts/compare_dem_sources.py
```

## Veřejné API: `analyze_location()`

Jedna funkce, která vezme místo + čas a vrátí kompletní strukturovanou
fyzikální analýzu (žádné HTML, žádný prezentační text - čistá data pro
budoucí frontend):

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from app.analyze_location import analyze_location

result = analyze_location(
    lat=50.5552, lon=13.9312,
    when=datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),  # MUSÍ být timezone-aware
    analysis_range_m=50_000,  # výchozí, konfigurovatelné
)

print(result.dem)  # provider, dataset (DTM/DSM), rozlišení, dosah, fallback_used, quality_flag
print(result.solar.astronomical_sunset, result.solar.sunset_azimuth_deg)
print(result.visibility.terrain_obstruction, result.visibility.terrain_obscured_sunset)
print(result.visibility.delay_minutes, result.visibility.visibility_duration)
print(result.terrain.horizon_at(result.solar.sunset_azimuth_deg))  # 360° profil, ne jen jedno číslo
```

**Vstupy**: `lat`, `lon`, `when` (timezone-aware datetime - naivní datetime
je odmítnuto s jasnou chybou, žádná implicitní lokální zóna stroje),
volitelně `analysis_range_m` (výchozí 50 km), `azimuth_step_deg`,
`horizon_step_m`, `observer_eye_height_m`, `pixel_size_m`,
`refraction_coefficient`, `visibility_search_window`, `include_sunrise`.

**Výstup** (`LocationAnalysis`): `location` (lat/lon/elevation),
`dem` (provider/dataset/rozlišení/fallback/kvalita), `terrain` (celý 360°
profil azimut→úhel horizontu + `horizon_at(azimut)`, min/max, slope/aspect),
`solar` (aktuální i sunset/sunrise pozice Slunce + terénní horizont v
daném azimutu), `visibility` (`terrain_obstruction`, `terrain_obscured_sunset`,
`delay_minutes`, `visibility_duration` + `_is_lower_bound` flag),
`diagnostics` (varování + použité parametry výpočtu).

**Terminologie** (přesně, viz PLAN.md Krok 29): `astronomical_sunset` je
idealizovaný okamžik západu (astral, s refrakcí); `terrain_obscured_sunset`
je skutečný okamžik zmizení za terénem - ROVNÁ SE `astronomical_sunset`,
pokud terén nepřekáží (žádný falešný posun); `visibility_duration` je
doba od POSLEDNÍHO vzestupu Slunce nad terén do `terrain_obscured_sunset`
- pokud se do prohledávaného okna nevejde, je to jen dolní odhad
(`visibility_duration_is_lower_bound=True`, s varováním).

**Timezone**: `when` musí nést `tzinfo` - UTC i pojmenovaná zóna
(`ZoneInfo("Europe/Prague")`) fungují správně včetně DST přechodů
(testováno na jarním i podzimním přechodu 2026). Stejný fyzický okamžik v
různých zónách dá identický `astronomical_sunset`.

**Výkon**: ~0,3-0,8 s s DEM v cache (i na výchozích 50 km), ~13-21 s při
prvním dotazu na novou oblast (síť, ne výpočet - viz PLAN.md Krok 31).

## DEM Strategy

- **V ČR**: primárně **ČÚZK DMR 5G** (skutečné DTM, ~2-20 m podle dosahu analýzy) přes veřejný ArcGIS ImageServer - žádný API klíč.
- **Mimo ČR**: **Copernicus GLO-30** (jediná možnost, globální pokrytí, DSM).
- **Fallback**: pokud ČÚZK selže z JAKÉHOKOLIV důvodu (timeout, chyba serveru, NODATA, podezřelá kvalita, mimo pokrytí), systém automaticky a transparentně přejde na Copernicus - viz `app/dem_strategy.py::resolve_dem()`. Nikdy není potřeba zásah uživatele.
- **Metadata**: každý výsledek nese `DEMMetadata` (provider, dataset včetně DTM/DSM, skutečné rozlišení, dosah analýzy, CRS, zda byl použit fallback, quality flag) - viz `.describe()`.
- **Výchozí dosah analýzy**: **50 km** (ne 30 km) - citlivostní analýza ukázala, že přesnost se zlepšuje i mezi 30 a 50 km bez jasného zploštění křivky; priorita je fyzikální správnost, ne rychlost. Konfigurovatelné přes `analysis_range_m`.
- **Terénní vs. vizuální horizont**: počítá se **skutečný terénní horizont** (upřednostňuje DTM). Vegetace/budovy (DSM) záměrně NEJSOU teď zahrnuté - architektura (`DEMMetadata.dataset` rozlišuje DTM/DSM) je ale připravená na budoucí kombinaci obou.
- **Známá omezení**: ČÚZK je při prvním (necachovaném) dotazu na velkou oblast ~4-8× pomalejší než Copernicus (dynamické renderování na serveru vs. statický soubor); jemnější rozlišení může zesílit efekt nepřesné vstupní souřadnice (viz PLAN.md Krok 23).

Plné zdůvodnění (7.1-7.6, včetně srovnávací tabulky a analýzy příčin rozdílů): [`PLAN.md`, sekce "DEM Strategy"](PLAN.md#26-dem-strategy-krok-7---finální-rozhodnutí).

## Testy

```bash
source .venv/bin/activate
python -m pytest tests/ -m "not integration" -v   # rychlé, bez sítě (výchozí pro běžný vývoj)
python -m pytest tests/ -m integration -v         # potřebují už stažené reálné DEM dlaždice
python -m pytest tests/ -v                        # úplně všechny
```

186 testů celkem (176 jednotkových + 10 integračních). Jednotkové testy
používají syntetický DEM (`tests/conftest.py`) se ZNÁMOU geometrií (rovina,
uměle vložená "zeď", nakloněná rovina se známým sklonem) nebo mockovaný
`analyze_location()` (webová vrstva) - ověřují matematiku proti uzavřeným
vzorcům, ne jen "vypadá to rozumně". Integrační testy potřebují reálné
dlaždice v `data/`, nebo běží proti reálnému DEM přes síť (viz stažení výše).

## Struktura

```
app/dem.py                - GeoTIFF DEM, bilineární sampling, OutOfBoundsError/NaN pro chybějící data
app/dem_provider.py         - DEMProvider protokol - terrain engine je zdroj-agnostický
app/geo.py                   - geodetické výpočty (bod ve vzdálenosti+azimutu)
app/solar.py                  - poloha Slunce (azimut, výška) pro GPS+čas
app/horizon.py                 - terénní horizont (i 360°), korekce zakřivení Země
app/visibility.py               - obecné is_sun_visible + odhad zakrytí (sunset i sunrise, bisekce)
app/tile_manager.py             - Copernicus DEM cache/download/merge podle GPS bodu + poloměru
app/cuzk_dem.py                  - ČÚZK DMR 5G přes ArcGIS ImageServer (jen ČR, vyšší rozlišení, skutečné DTM)
app/dem_strategy.py               - automatický výběr zdroje + fallback + metadata (viz "DEM Strategy" výše)
app/dem_merge.py                  - sdílené sloučení víc GeoTIFF do mozaiky
app/terrain_attributes.py         - slope/aspect (informační, zatím nepoužito pro viditelnost)
app/experimental_score.py         - experimentální geometrický indikátor otevřenosti (Fáze 3, NE AI skóre)
app/analyze_location.py           - veřejné API: analyze_location() - orchestrace celého enginu
app/sunset_candidate.py           - Fáze 6 příprava: SunsetCandidate datový model + geometry score (žádné vyhledávání)
app/main.py                       - FastAPI web vrstva (Fáze 5) - ČISTĚ prezentační, jen volá analyze_location()
templates/index.html              - hlavní stránka (3D mapa, plovoucí panely, polar plot canvas)
static/app.js                      - frontend logika (fetch, vykreslení, MapLibre 3D mapa) - žádné výpočty
static/style.css                   - styl
scripts/                           - CLI nástroje, viz "Spuštění" výše
```

## Stav projektu

Ověřené geospatial jádro (audit, 360°, síť testovacích bodů v 5 různých
terénních situacích, citlivostní analýza, tile cache, výkon změřen a
optimalizován, slope/aspect, experimentální skóre), druhý reálný zdroj
DEM dat (ČÚZK DMR 5G) za zdroj-agnostickou abstrakcí s automatickým
fallbackem, a jedna veřejná orchestrační funkce `analyze_location()` nad
tím vším, s přesnou terminologií a testovaným timezone/DST chováním.
Kompletní přehled: viz [`PLAN.md`](PLAN.md#33-stav-po-fázi-4).

Zatím vědomě NEobsahuje: počasí, kvalitu světla, přístupnost, AI, ranking
míst, POI, routing, login, databázi, monetizaci.

## Spuštění webové aplikace (Fáze 5 / 6.5 / 6.6 / 7)

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

Otevři `http://localhost:8000` v prohlížeči.

**Hlavní obrazovka (Fáze 7 - "Najdi svůj dokonalý západ slunce")**: stránka
je teď běžná scrollovací stránka, ne fullscreen mapa. Sekce shora dolů:

1. **Hero** - nadpis + skutečný dnešní kontext ("Dnes · 24. září · západ
   slunce 19:02"), napojený na živá data.
2. **Best Sunset For You** - kruhový Sunset Rating, hero statistiky
   (sunset/terrain horizon/visible sunset/elevation) a "Proč právě toto
   místo?" checklist pro AKTUÁLNĚ vybrané místo (výchozí: Milešovka).
   **Důležité**: tohle NENÍ automaticky vybrané "nejlepší místo ze
   všech" - candidate search (vyhledávání přes síť bodů) v týhle fázi
   neexistuje, jde o reálnou analýzu jednoho vybraného místa prezentovanou
   jako produktový koncept (viz `dev-note` přímo pod nadpisem v UI).
3. **Sunset Panorama** - interaktivní horizontální pohled na terénní
   horizont (táhni myší/prstem), viz "Sunset Panorama" níže.
4. **Other Sunsets** - karty s dalšími testovacími lokalitami (výslovně
   ukázková/development data, ne výsledek automatického vyhledávání).
5. **Explore the map** - 3D terénní mapa (MapLibre, beze změny z Fáze
   6.5/6.6), teď jako sekce stránky místo fullscreen plochy.
6. **Technical details** - sbalený panel s 360° polar chartem, tabulkou
   terénních/DEM parametrů a syrovým JSON - schovaný, aby aplikace
   nepůsobila jako debugovací nástroj.

Nastavení lokace/data/časové zóny je ve slide-in panelu za tlačítkem
"☰" v horní liště (dřív plovoucí panel nad mapou) - obsahuje dropdown
**Test locations** (Milešovka, Krušné hory/hřeben, Labská niva/údolí,
úbočí Milešovky/otevřenější terén, Paříž/zahraniční lokalita -
souřadnice převzaté z už dřív validovaných bodů, žádné nové odhady) a
sbalitelné "Manuální souřadnice" pro ruční zadání.

**Zadání lokace** (3 rovnocenné cesty, všechny volají stejnou
`/api/analyze`): klik do mapy v sekci "Explore the map", výběr z
dropdownu / manuální souřadnice v "☰" panelu, nebo tlačítko "Prozkoumat"
na kartě v "Other Sunsets". Cokoliv z toho aktualizuje VŠECHNY sekce
stránky najednou (hero kontext, Best Sunset, panorama, mapu, technické
detaily) - jeden zdroj pravdy (`renderAll()` v `static/app.js`).

**Otáčení/naklánění/zoom mapy NIKDY nespouští analýzu** - jen explicitní
kliknutí na konkrétní bod. Mapová interakce a terénní analýza jsou
záměrně oddělené (viz zadání Fáze 6.5 bod 14) - MapLibre vyvolává
`click` jen na skutečné kliknutí/tap, nikdy během gest.

**3D terén na mapě je jen průzkumný podklad** (veřejná "Terrarium" DEM
data, viz sekce "Mapa" níže) - slouží k tomu, aby uživatel poznal kopec
od údolí a zorientoval se v krajině. Jakmile ale klikne na konkrétní bod,
skutečný sunset/terrain výpočet vždy běží přes stejné
`/api/analyze` → `analyze_location()` nad přesnými ČÚZK/Copernicus daty
- 3D podkladová dlaždice se pro samotný výpočet nikde nepoužívá.

## Sunset Panorama (Fáze 7)

Interaktivní horizontální vizualizace terénního horizontu (canvas,
`drawPanoramaFrame()` v `static/app.js`) - postavená nad STEJNÝMI 360°
daty jako "Technical details" polar chart (`terrain.azimuths_deg`/
`horizon_altitudes_deg`), jen jiná projekce (skyline místo radaru), a
interaktivní: tažením myší/prstem se mění zobrazený výsek azimutů
(field of view 100°), tlačítko "🌅 Recenter" vrátí pohled na skutečný
`sunset_azimuth_deg`. Sky gradient a teplá záře kolem pozice slunce jsou
čistě VIZUÁLNÍ EFEKT (dekorace, ne fyzikální nebe); silueta terénu a
poloha sunset markeru jsou přímo ANALYTICKÁ DATA z `/api/analyze` -
žádné vymyšlené hory.

## Other Sunsets (Fáze 7) - ukázková data, ne candidate search

Karty pro zbylé testovací lokality (`TEST_LOCATIONS` z `app/main.py`) -
každá volá stejné `/api/analyze` jako zbytek aplikace. **Donačítají se
POSTUPNĚ** (ne najednou) a **až když sekce vstoupí do viewportu** (lazy
load) - audit během vývoje zjistil, že souběžné načítání všech karet
najednou konkurovalo o CPU s uživatelovou vlastní interakcí (terénní
výpočet je CPU-náročný a nekešuje se per konkrétní bod), viz PLAN.md
Fáze 7 Krok 67 pro detaily a přiznané zbytkové omezení.

**Sunset direction na mapě**: po analýze se od zvoleného bodu vykreslí
čára + jemný teplý "světelný kužel" + malý 🌅 marker směrem ke skutečnému
azimutu západu Slunce (`solar.sunset_azimuth_deg` z API, žádný hardcoded
směr). Čára nese ANALYTICKÝ význam - zelená pokud je horizont otevřený,
červená pokud terén brání výhledu; kužel je čistě ATMOSFÉRICKÝ (vždy
teplý, viz Fáze 6.6 níže). Terén na mapě (hillshade) se zároveň
přeorientuje na stejný azimut - jde o **vizualizační orientační pomůcku**,
NE o fyzikálně přesnou simulaci slunečního svitu (to je v UI i kódu
explicitně okomentované).

**"Sunset atmosphere" vizuální styl (Fáze 6.6)**: terén má teplé
zlatavé nasvícené svahy / hluboké fialovo-modré stíny (MapLibre
`hillshade-highlight-color`/`hillshade-shadow-color`, statická stylová
volba), voda a pozadí mapy jsou přebarvené do tmavšího modro-fialového
tónu, a přes celou obrazovku leží jemný, obrazovkově ukotvený
teplý/chladný gradient (`#sunset-atmosphere`, `mix-blend-mode: soft-light`,
nízká opacita) - vše čistě stylistické, terén a popisky zůstávají čitelné.

**Datum**: zadej datum, časová zóna je samostatné pole (výchozí
`Europe/Prague`) - musí být platný IANA název (např. `UTC`,
`America/New_York`). Čas dne se interně nastaví na poledne v zadané zóně
- `analyze_location()` z něj použije hlavně kalendářní den.

**Interpretace výsledku**:
- **Sunset Rating** (0-100, Fáze 6.6) - kruhový indikátor v sekci "Best
  Sunset For You" (Fáze 7), viz sekce "Sunset Rating" níže.
- **Sunset** - astronomický západ (idealizovaný, jako v kalendáři).
- **Terrain horizon** - výškový úhel terénního horizontu přesně v azimutu
  západu (záporné = terén je pod astronomickým horizontem = otevřeno,
  kladné = terén blokuje).
- **Visible sunset** - kolik minut před terénním zakrytím bylo Slunce
  nad terénem viditelné (nebo "Open horizon", pokud terén nepřekáží).
- **Elevation** - nadmořská výška terénu ve zvoleném bodě.

## Sunset Rating (Fáze 6.6)

**Co to je**: deterministická 0-100 metrika TECHNICKÉ VHODNOSTI místa pro
VIDITELNOST západu Slunce - NE hodnocení krásy/kvality místa (k tomu
chybí počasí, cloud cover, fotografie, crowd data - viz
`app/sunset_rating.py` docstring a transparentní poznámka přímo v UI
pod ratingem: "Sunset Rating currently represents terrain geometry
only..."). Počítá se JEDNOU na backendu (`compute_sunset_rating()`),
frontend jen zobrazuje.

**Vstupy** (všechny přímo z `LocationAnalysis`, nic se neodhaduje):
1. `sunset_terrain_horizon_deg` - horizont PŘESNĚ v azimutu západu.
2. `delay_minutes` - o kolik terén posouvá zmizení Slunce.
3. Otevřenost sektoru ±20° KOLEM SKUTEČNÉHO `sunset_azimuth_deg` (ne
   fixního pásma - viz níže).
4. `visibility_duration` - jak dlouho je Slunce vidět nad horizontem,
   než zmizí.

Váhy (35/25/25/15 %) a meze převodu na 0-100 jsou zdokumentované přímo
v `app/sunset_rating.py` - rating je deterministický a reprodukovatelný
(stejný vstup vždy dá stejný výsledek, žádná náhoda/síť).

**Popisky** (zadání bod 9, žádné subjektivní "beautiful"/"best"):
90-100 Excellent terrain visibility, 75-89 Very open horizon, 50-74
Partial obstruction, 25-49 Strong terrain obstruction, 0-24 Sunset
largely obstructed.

**Vztah k `compute_geometry_score()`** (Fáze 6, `app/sunset_candidate.py`):
`compute_sunset_rating()` staví na stejné myšlence (otevřenost západního
sektoru + zpoždění), ale zamčenou experimentální funkci `compute_geometry_score()`
neupravuje (má vlastní testovaný kontrakt) - je to nová, přesnější metrika
s dynamickým sektorem centrovaným na skutečný azimut západu (AUDIT NÁLEZ:
`compute_geometry_score()` mělo fixní pásmo 240-300°, které pro krajní
sezónní azimuty míjí relevantní terén - viz PLAN.md Fáze 6.6).

**Architektura pro budoucí weather vrstvu**: `SunsetRating` má pole
`weather_score`/`visibility_score`/`accessibility_score` - zatím vždy
`None`, žádné API se nevolá. Až přibudou, `overall_0_100` se stane jejich
váženou kombinací s `geometry_score`.

**Polar plot** (360° terénní horizont): sever nahoře, po směru
hodinových ručiček, hnědá vyplněná plocha = terénní horizont v daném
azimutu. Radiální škála se přizpůsobuje skutečnému rozsahu dat dané
lokality (ne fixní 0-90°, viz audit v PLAN.md Krok 48 - fixní zenitová
vzdálenost natlačila realistické výškové úhly do úzkého pásu u okraje a
graf byl vizuálně nečitelný), s vlastním zvýrazněným referenčním kruhem
pro 0° (astronomický horizont bez terénu). Silná barevná čára = azimut
západu Slunce (zelená/červená podle toho, jestli je obstrukce), tečkovaná
čára = azimut východu; legenda pod grafem popisuje všechny prvky.
Vykresluje se přímo ze skutečných 360° dat z `LocationAnalysis.terrain` -
frontend nic nepřepočítává.

**Technical JSON** (dole, sbalitelné) - celý serializovaný
`LocationAnalysis` pro ladění.

**Cache**: opakovaná analýza stejné lokace + stejného dosahu použije
existující DEM cache (`app/tile_manager.py`/`app/cuzk_dem.py`) -
ověřeno, že web vrstva nezpůsobuje žádné duplicitní stahování.

**Chybové stavy**: neplatné souřadnice/datum/timezone vrátí jasnou chybu
ihned (validace na vstupu, HTTP 422); nedostupná/nepokrytá DEM data
(např. oceán) vrátí srozumitelnou zprávu (HTTP 502), ne prázdný nebo
vymyšlený výsledek.

## Mapa (Fáze 5.5 → 3D ve Fázi 6.5 → 2D outdoor mapa ve Fázi 8)

Interaktivní mapa ([MapLibre GL JS](https://maplibre.org/), BSD-3-Clause,
`static/app.js`) - klik do mapy okamžitě spustí analýzu pro daný bod
(viz sekce výše).

**AUDIT NÁLEZ + rozhodnutí (Fáze 8, zadání bod 7/8)**: dřívější 3D verze
(OpenFreeMap vektorový styl `liberty` + Terrarium DEM terén/hillshade,
Fáze 6.5/6.6) vizuálně pořád vypadala jako běžná silniční/GIS mapa -
čísla silnic, desítky názvů vesnic, žádné turistické trasy ani
vrstevnice. Pro "outdoor exploration" účel a zobrazování VÍCE kandidátů
najednou (Fáze 8 - candidate discovery) je čistá 2D mapa čitelnější než
nakloněná 3D kamera. Nahrazeno:

**[OpenTopoMap](https://opentopomap.org/)** (raster dlaždice,
`{a,b,c}.tile.opentopomap.org`) - veřejný turistický mapový styl nad OSM
daty + SRTM, licence CC-BY-SA, **zdarma, bez API klíče, bez účtu**.
Obsahuje vrstevnice, turistické trasy, lesy, vodní plochy - a v defaultní
stylizaci ŽÁDNÉ komerční POI (restaurace/obchody/bankomaty). Stejná
poznámka jako u předchozích mapových zdrojů: vhodné pro lehké lokální
vývojové/demo použití (viz [OpenTopoMap Usage Policy](https://opentopomap.org/about)),
pro produkční nasazení s vysokým provozem by bylo potřeba vlastní tile
server. 3D terén/hillshade (Terrarium dlaždice, Fáze 6.6) byl při týhle
změně ZRUŠEN - žádná závislost na druhém externím DEM zdroji jen pro
vizuál mapy.

**DŮLEŽITÉ - mapa (dřív 3D reliéf, teď 2D vrstevnice) se používá jen pro
VIZUÁLNÍ orientaci**. Pro samotný sunset/terrain výpočet konkrétního bodu
se nikdy nepoužívá - to jde vždy přes `/api/analyze` → `analyze_location()`
nad přesnými ČÚZK DMR 5G / Copernicus GLO-30 daty (viz "DEM Strategy"
výše). Frontend žádnou terrain/solar geometrii sám nepočítá; jediná
geometrie v `app.js` je `destinationPoint()` - čistě vizuální promítnutí
už hotového azimutu západu Slunce do bodu na mapě, ne přepočet analýzy.

Samotná knihovna MapLibre GL JS se načítá z `unpkg.com` CDN - vyžaduje
síť při načtení stránky.

## Candidate Discovery (Fáze 8, rozšířeno Fáze 11) - "Find the best sunset in Czechia"

**Cíl**: první krok k reálnému vyhledávání nejlepších míst pro západ
Slunce - zatím jen pro **Beskydy + Krkonoše** (ne celá ČR - audit ve
Fázi 11 ukázal 43 062 kandidátů pro celou ČR, mimo rozsah sekvenční
terrain analýzy; region-po-regionu architektura je ale připravená na
další rozšíření beze změny kódu, viz `app/main.py::REGIONS`).

**Zdroj dat** (audit v zadání bod 8/10, rozšířeno Fáze 11): [OpenStreetMap](https://www.openstreetmap.org/)
přes [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) -
veřejné, bez API klíče, licence ODbL. Dotazované tagy: `natural=peak`,
`natural=hill`, `tourism=viewpoint`, a od Fáze 11 i rozhledny
(`man_made=tower`+`tower:type=observation` - musí být OBA tagy
zároveň, samotné `man_made=tower` chytá i vysílače/vodojemy) -
`app/candidate_discovery.py`. Endpoint je nastavitelný - hlavní veřejná
instance `overpass-api.de` byla z tohohle vývojového prostředí trvale
nedostupná (síťové omezení sandboxu), použito oficiální zrcadlo
`overpass.openstreetmap.fr`. Audit Fáze 11 také ukázal, že `natural=ridge`
(hřebeny) v české OSM datech prakticky neexistuje (0 výsledků pro celou
ČR) - hřebeny proto NEJSOU implementovány, musely by se odvodit z DEM.

**Cache**: syrová Overpass odpověď se ukládá do `data/overpass_cache/<region>_combined.json`
(`discover_candidates_cached()`) - další běh skriptu už negeneruje nový
síťový požadavek. Pro Beskydy je v repozitáři reálná, skutečně stažená
odpověď (241 vrcholů + 112 vyhlídek/rozhleden), ne vymyšlená data.

**Filtrace**: `deduplicate()` (odstraní body blíž než 150 m k sobě),
`filter_within_bbox()` (mimo ČR pryč) - `app/candidate_discovery.py`.

**Terrain analýza kandidátů**: `app/candidate_batch.py::analyze_candidates()`
volá STEJNÉ `analyze_location()`/`compute_sunset_rating()` jako zbytek
appky - SEKVENČNĚ, ne souběžně (zadání bod 14). Naměřeno reálně (viz
PLAN.md Fáze 8): **~18 s/kandidát v průměru** (11-32 s rozptyl), tedy
~220 s pro 12 kandidátů. Extrapolace: 25 kandidátů ≈ 7,5 min, 50 ≈ 15 min
- jednoznačně mimo rozumný timeout jednoho HTTP requestu.

**Proto**: terrain analýza běží OFFLINE, ručně spouštěným skriptem
(`scripts/build_candidate_dataset.py`), který zapíše seřazený výsledek do
`data/candidates_beskydy.json`. Webová vrstva (`GET /api/candidates?region=beskydy`)
tenhle soubor jen ČTE - žádný živý DEM výpočet na požadavek. Pro
aktualizaci (nové datum, čerstvá data) je potřeba skript spustit znovu -
zatím žádné automatické plánování (mimo rozsah týhle fáze).

**Ranking**: `rank_by_rating()` - deterministické seřazení sestupně podle
`SunsetRating.overall_0_100` (výpočet ratingu beze změny, viz Fáze 6.6).
"Best Sunset For You" na hlavní stránce teď zobrazuje SKUTEČNÉHO vítěze
téhle analýzy (dřív natvrdo Milešovka) - pokud dataset chybí, aplikace
se čistě vrátí k původnímu chování (Milešovka + testovací lokality).

## "Where? / Search within / Find my sunset" (Fáze 11)

Uživatel teď zadá **místo** (textem) a **radius** (5-100 km, výchozí
25 km) a aplikace najde nejlepší západ v okolí - z PŘEDPOČÍTANÝCH
candidate datasetů (viz výše), ne živou terrain analýzou stovek bodů.

- `GET /api/geocode?q=<název>` - textová lokalita -> souřadnice přes
  [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap,
  zdarma, ODbL). Volá se ze serveru (kvůli `User-Agent`/CORS) jen na
  explicitní odeslání (klik/Enter), NIKDY jako autocomplete-při-psaní -
  respektuje [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
  (max 1 req/s, žádné hromadné dotazy).
- `GET /api/search?lat=&lon=&radius_km=` - sloučí kandidáty ze
  VŠECH registrovaných regionů (`app/main.py::REGIONS`), filtruje
  haversine vzdáleností (`app/candidate_discovery.py::haversine_m`,
  žádné routing/travel-time), řadí podle PŘEDPOČÍTANÉHO ratingu. Tenhle
  rating slouží jen k výběru "koho vůbec zobrazit/analyzovat" - finální
  zobrazený výsledek se vždy dopočítá naživo pro AKTUÁLNÍ datum přes
  existující `/api/analyze`, takže "Best Sunset" nikdy nezobrazuje
  zastaralý (např. včerejší) rating.
- Mimo pokrytí registrovaných regionů (např. Brno) vrátí čestně
  `candidate_count: 0`, ne fake/nejbližší náhradní výsledek.
- Na mapě se zobrazí čistě vizuální kruh hledané oblasti (klientský
  GeoJSON polygon, žádný network request navíc) - posun slideru radiusu
  kruh jen překreslí, nespouští žádnou analýzu.
