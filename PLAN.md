# Sunset Radar — technický plán a shrnutí

Cíl: z GPS bodu + skutečného terénního modelu určit, kdy bude Slunce při
západu (nebo východu) skutečně viditelné a kdy ho zakryje okolní terén —
ne jen vrátit tabulkový čas. Tento dokument pokrývá jak první PoC
(jeden bod, západní sektor), tak druhou fázi (audit, 360°, síť bodů,
obecná architektura, cache, výkon, slope/aspect, experimentální skóre).

## 1. Zvolený technologický stack

| Účel | Knihovna | Proč |
|---|---|---|
| Čtení GeoTIFF/rastr | `rasterio` | Standard, GDAL-backed |
| CRS/geodetické výpočty | `pyproj` (`Geod`) | Přesný geodetický výpočet nad WGS84 elipsoidem |
| Numerika | `numpy`, `scipy` | Vektorizace, interpolace |
| Poloha Slunce | `astral` | Viz Krok 6 níže - **empiricky ověřeno, ne jen předpokládáno** |
| Vizualizace | `matplotlib` | Kartézský i polární (panoramatický) graf |
| Stahování DEM | `requests` | Přímé S3 stažení dlaždic |

## 2. Datové zdroje — DEM

### ČÚZK DMR 5G (ČR) — **implementováno** (viz Krok 20 níže)
Nativní LAZ mračno bodů nepoužito. Místo toho **ArcGIS ImageServer
`exportImage`** (`https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer`) -
žádný API klíč, žádné ruční GIS zpracování, server umí na požádání rovnou
reprojektovat do EPSG:4326. Nativní rozlišení **2 m** (ne 5 m - "5G" je
generace produktu, ne pixel size), skutečné **DTM** (bare-earth, LiDAR
2009-2013, přesnost 0,18 m/0,3 m les), licence CC BY 4.0. Podrobnosti,
nezdokumentované limity serveru a výkonové srovnání s Copernicus: Krok 20-25.

### Copernicus DEM GLO-30 (30m, globální) — použito
Zdarma, žádný API klíč, S3 `copernicus-dem-30m`, přímo GeoTIFF v EPSG:4326.
Dlaždice `Copernicus_DSM_COG_10_<N/S+lat>_00_<E/W+lon>_00_DEM` (pozor:
prefix `COG_10`, ne `COG_30`). Licence volná i komerčně, nutná atribuce.
**Je to DSM** (zahrnuje vegetaci/budovy), ne bare-earth DTM.

## 3. Matematický princip

```
Observer:                 50.5552, 13.9312 (Milešovka)
Astronomical sunset:      19:06:45
Sunset azimuth:           272.0°
Terrain horizon (azimut): -0.95°
Terrain obstruction:      ANO (o 1.7 min dřív než astronomický západ)
```

## 4. Terénní horizont — algoritmus a dosah

Pro každý azimut: jdi od pozorovatele krokem `step_m` do `max_distance_m`,
v každém bodě spočítej výškový úhel k terénu (korigovaný na zakřivení
Země), zapamatuj MAXIMUM.

**Krok 6 citlivostní analýza (empiricky změřeno na Milešovce, ne odhad):**

| Parametr | Test | Vliv na výsledný úhel |
|---|---|---|
| `step_m` (10-500 m) | -0.938° až -1.003° | **Malý** (0.065°) - výsledek je robustní vůči kroku vzorkování |
| `refraction_coefficient` k (0-0.25) | -0.971° až -0.937° | **Malý** (0.034°) |
| `max_distance_m` (5-30 km) | **-4.551° až -0.953°** | **OBROVSKÝ (3.6°!)** - zdaleka nejvlivnější parametr |
| Rozlišení DEM (30m/90m/270m) | -0.953° / -0.966° / **+17.181°** | **Zanedbatelné 30→90m, KATASTROFICKÉ na 270m** u úzkých izolovaných vrcholů |

**Závěr**: `max_distance_m` musí být dost velký (30 km+ pro tenhle typ
terénu) - krátký dosah systematicky PODHODNOTÍ otevřenost (nezachytí
vzdálené "propadnutí" terénu kvůli zakřivení Země). DEM rozlišení je
kritické konkrétně pro úzké, ostře ohraničené terénní útvary (izolované
sopečné kužely apod.) - hrubý grid je "rozmázne" a při výpočtu vlastní
nadmořské výšky pozorovatele vznikne stejný typ chyby, jako nepřesná
souřadnice (viz Krok 1 níže). Pro širší/pozvolnější terén (hřeben, svah,
údolí) očekáváme menší citlivost na rozlišení, ale to nebylo systematicky
otestováno pro všechny typy terénu - jen pro izolovaný vrchol.

Výchozí hodnoty PoC: `max_distance_m=30000`, `step_m=90` (~3× pixel).

## 5. Poloha Slunce

`astral.sun.azimuth/elevation`. `astronomical_sunset()`/`sunrise()`
počítá i "dip" horizontu kvůli výšce pozorovatele.

## 6. Astral vs. pvlib — empirické ověření (Krok 6)

Předpoklad "pvlib je přesnější u horizontu" byl **testován, ne přijat na
víru**. Výsledek byl překvapivý:

- Porovnání apparent (refrakcí korigované) výšky Slunce mezi `astral` a
  `pvlib.solarposition.spa_python` v jemném čase (20s krok) kolem
  astronomického západu na Milešovce.
- `astral`: hladká, monotónní křivka bez skoků.
- `pvlib` (`spa_python`, výchozí `atmos_refract`): **skutečný skok ~0.67°
  během 20 sekund** v pásmu přibližně -0.2° až -0.9° zdánlivé výšky -
  přesně v oblasti, která nás nejvíc zajímá.
- Nebylo prozkoumáno, jestli je to laditelné přes `atmos_refract` parametr
  nebo jde o known limitation té konkrétní pvlib funkce - to je otevřená
  otázka pro budoucnost, pokud by `astral` přestal stačit.

**Rozhodnutí: ZŮSTÁVÁME u `astral`.** Empiricky se v tomhle konkrétním
testu chová lépe než výchozí `pvlib` volání, navzdory obecné pověsti SPA
algoritmu jako "referenčního". Tohle je přesně případ, kdy by slepé
"upgradování na renomovanější knihovnu" bez měření zavedlo regresi.

## 7. Astronomická refrakce vs. terestrická refrakce - dva různé jevy

Audit (Krok 1) ověřil, že `astral.elevation()` už defaultně
(`with_refraction=True`) počítá s atmosférickou refrakcí Slunce
(Meeus/NOAA polynom, `astral/__init__.py::refraction_at_zenith`) - to je
JINÝ fyzikální jev než zakřivení Země/terestrická refrakce podél linie k
terénu (`app/horizon.py::earth_curvature_drop_m`, k=0.13). Oba jsou teď
správně a nezávisle pokryté, nedochází k duplicitě ani k opomenutí.

## 8. Sklon a expozice (slope/aspect) — Krok 9, HOTOVO

`app/terrain_attributes.py::compute_slope_aspect()` - centrální diference
ve 4 směrech (sever/jih/východ/západ) přes existující `DEMSource.elevation()`
sampling. Ověřeno na syntetických nakloněných rovinách se ZNÁMÝM
sklonem/expozicí (5 testů, včetně přesné shody velikosti sklonu s
analytickým vzorcem `atan(gradient)`).

**Důležité, striktně dodrženo**: NENÍ zapojeno nikde do `visibility.py`
ani do skóre jako primární faktor - je to jen připravený, samostatný
údaj pro budoucí scoring. Hlavním faktorem viditelnosti zůstává skutečný
terénní horizont.

## 9. Architektura — obecný terrain engine (Krok 4)

```
terrain_horizon = compute_horizon_profile(dem, lat, lon, ...)   # app/horizon.py
sun_pos = sun_position(observer, when)                            # app/solar.py
visible = is_sun_visible(sun_pos.altitude_deg, sun_pos.azimuth_deg, terrain_horizon)  # app/visibility.py
```

`is_sun_visible()` je čistá, obecná funkce nezávislá na "je to východ
nebo západ" - `estimate_sunset_visibility()` a `estimate_sunrise_visibility()`
(Krok 4+5) jsou tenké obálky nad sdíleným `find_horizon_crossing()`
(bisekce, viz Krok 10). Ověřeno testem, že sunrise/sunset fungují
symetricky nad stejným horizontem. Stejný `HorizonProfile` lze použít i
pro "je vidět konkrétní azimut" (fotografie) nebo hiking planner - bez
úpravy jádra.

## 10. Časová přesnost - bisekce (Krok 5)

`find_horizon_crossing()`: (1) hrubý lineární sken (60s krok) najde
INTERVAL, kde dochází k přechodu viditelné/zakryté - robustní i vůči
nemonotónnímu horizontu (Slunce může nakrátko zmizet za bližším hrbolem a
zase se objevit), (2) bisekce uvnitř intervalu zpřesní na `tolerance_seconds`
(výchozí 1s). Ověřeno testem proti nezávislému jemnému (1s) lineárnímu
skenu - shoda do 2 sekund.

## 11. Audit nalezené chyby a opravy (Krok 1)

1. **`DEMSource.elevation()` tiše "clampovala" body mimo hranice DEM** na
   okrajový pixel místo chyby - přidán `OutOfBoundsError`. Regresní testy
   v `tests/test_dem.py`.
2. **NODATA pixely se vůbec nekontrolovaly** - bilineárně by se
   zprůměrovaly do "věrohodně" vypadající, ale smyšlené výšky. Teď vrací
   `NaN`, `terrain_angle` NODATA vzorek uprostřed paprsku přeskočí (nepočítá
   ho jako výšku 0).
3. **Sentinel `-90.0°` pro "žádná překážka" byl nerozlišitelný od "nemohli
   jsme nic ověřit"** (pozorovatel mimo DEM / paprsek okamžitě mimo
   pokrytí) - nahrazeno `NaN` s jasnou sémantikou "nevím".
4. **`HorizonProfile.altitude_at()` neuměla wraparound přes šev 359°/0°**
   u plného 360° profilu (Krok 2) - opraveno přidáním "obalujícího" bodu.
5. **Výkonový bug** (Krok 8, nalezeno profilováním, ne odhadem): `rasterio`
   `.bounds` se přepočítávalo z GDAL transformace při KAŽDÉM volání
   `contains()` - **~70 % celého výpočetního času** 360° horizontu.
   Cachováno při otevření souboru → **2.5× zrychlení** (704ms → 284ms
   pro 360°/1° na 30km).

## 12. Síť testovacích bodů (Krok 3) — validace napříč terénem

Body nalezeny PROGRAMOVĚ z DEM dat (ne odhadem souřadnic z paměti - to
způsobilo problém #10 níže), klasifikace podle měřitelné lokální
reliéfní charakteristiky (`scripts/find_test_points.py`).

| Bod | Situace | Souřadnice | Horizont (rozsah) | Zpoždění západu |
|---|---|---|---|---|
| A | Vysoký otevřený vrchol (Milešovka) | 50.5552, 13.9312 | -6.63° až +0.08° | **1.4 min** |
| B | Hřeben (Krušné hory, u Klínovce) | 50.3972, 12.9479 | -2.72° až +12.81° | **5.7 min** |
| C | Svah | 50.6556, 13.9417 | +0.17° až +5.05° | **18.3 min** |
| D | Údolí | 50.5764, 13.7167 | +3.35° až +9.81° | **62.8 min** |
| E | Kopec přímo na západě (Milešovka, východní úbočí) | 50.5537, 13.9337 | -5.27° až +33.47° | **84.2 min** |

**Krásně monotónní gradient přesně v očekávaném pořadí terénu** (otevřený
vrchol < hřeben < svah < údolí < přímo blokované) - silná evidence, že
algoritmus funguje konzistentně napříč terénem, ne jen na jednom vybraném
bodě. Graf: `data/test_point_network.png`.

Poznámka k bodu B: 30km paprsky směrem na východ z tohoto bodu (blízko
západní hranice dlaždice) by přesáhly pokrytí jedné dlaždice - řeší Krok 7
(tile manager), viz níže.

## 13. Nalezený problém a jeho řešení (PoC, bod A)

První běh s odhadnutými souřadnicemi (50.5537, 13.9337, z paměti) vrátil
terénní horizont **+22°** - fyzikálně nesmyslné. Jemné vzorkování (po 20 m)
odhalilo hladký, skutečný profil hory: souřadnice ležely ~160 m na
VÝCHODNÍM SVAHU, ne na vrcholu. Algoritmus SPRÁVNĚ "viděl" vlastní horu
stoupající před pozorovatelem. Není to bug ve výpočtu, ale nepřesnost
vstupu. Opraveno na skutečné lokální maximum z DEM (835.3 m, sedí na
837 m z Wikipedie). **Tenhle bod je teď záměrně recyklovaný jako testovací
bod E** ("kopec přímo na západě") - ukázalo se, že to je skvělý příklad
přesně téhle terénní situace.

## 14. DEM tile manager a cache (Krok 7)

`app/tile_manager.py`:
- `tile_id_for(lat, lon)` - určí Copernicus 1°×1° dlaždici.
- `tiles_needed_for_area(lat, lon, radius_m)` - konzervativní obalový
  obdélník kolem kruhu daného poloměru, vrátí VŠECHNY dlaždice, které by
  mohly být potřeba (ověřeno testy na hranicích: bod 4.8 km od hranice
  dlaždice + 30km dosah správně zabere sousední dlaždici, krátký 3km dosah
  ne).
- `DEMTileCache.ensure_local()` - stáhne, jen pokud soubor ještě není v
  cache (atomický zápis přes `.part` + rename, aby se nikdy nezůstal
  napůl stažený soubor pod finálním jménem).
- Víc dlaždic → `rasterio.merge.merge()` sloučí do jedné mozaiky, uložené
  taky do cache (nepočítá se znovu při opakovaném dotazu na stejnou
  kombinaci) - **žádná změna nebyla potřeba v `DEMSource`**, mozaika se
  otevírá úplně stejně jako jedna stažená dlaždice.

Ověřeno end-to-end živým stažením (3.1s první volání, 0.000s druhé z
cache) + sloučením dvou reálných dlaždic (Milešovka + Krušné hory) s
ověřením správné výšky na obou stranách švu.

Testy rozdělené (Krok 11): rychlé jednotkové (`tile_id_for`,
`tiles_needed_for_area` - čisté funkce, bez I/O) běží vždy, síťové/
integrační testy jsou označené `@pytest.mark.integration` a spouští se
zvlášť (`pytest -m integration`) - nezávisí na "the internet must be up"
pro běžný vývojářský cyklus.

## 15. Výkon (Krok 8)

Naměřeno (ne odhadnuto), s dlaždicí v cache:

| Fáze | Čas |
|---|---|
| Rozřešení dlaždice z cache | 0.1 ms |
| Načtení DEM (rasterio open + read) | 98 ms |
| 360° horizont (360 azimutů, krok 1°, 30 km) | 425 ms |
| Odhad viditelnosti (bisekce) | 1.5 ms |
| **CELKEM (dlaždice v cache)** | **~525 ms** |
| Stažení chybějící dlaždice (síť) | ~3-6 s |

Cíl "výsledek za pár sekund po kliknutí na mapu" je s dlaždicí v cache
**splněn s velkou rezervou** (0.5s). Jediné pomalé místo je STAŽENÍ nové
dlaždice - to je I/O (síť), ne výpočet, a řeší ho přesně cache z Kroku 7
pro jakýkoliv opakovaný dotaz do stejné oblasti.

**Nebylo potřeba** multiprocessing ani další vektorizace `terrain_angle` -
jeden cílený fix (cachování `bounds`, nalezený PROFILOVÁNÍM, ne odhadem)
dal 2.5× zrychlení a současný výkon cíl splňuje. Pokud by v budoucnu
360° na mnoha bodech najednou (např. předpočítání pro síť bodů na mapě)
bylo pomalé, další krok by byl vektorizovat `terrain_angle` přes numpy
(dnes je to Python smyčka přes azimuty) - ale to je předčasné řešit bez
změřeného skutečného požadavku.

## 16. Experimentální geometrický indikátor (Krok 10)

`app/experimental_score.py::compute_experimental_openness()` - **NENÍ to
AI skóre ani skóre "kvality" (barevnost, počasí)** - kombinuje jen dvě
už vypočtené geometrické veličiny (podíl otevřeného západního sektoru +
časové zpoždění západu) s váhami "od oka", explicitně označeno
`is_experimental=True`.

Na síti bodů z Kroku 3 vyšlo přesně očekávané pořadí: A=92.4, B=72.7,
C=34.7, D=0.0, E=0.0 (D a E se dotýkají nuly kvůli stropu na 60 min
zpoždění - známý kalibrační artefakt, ne chyba).

## 17. Validace (Krok 12) — co JE a co NENÍ ověřeno

**Ověřeno:**
- Nadmořská výška bodu A shoduje s Wikipedií (835.3 vs 837 m).
- Syntetický test s uměle vloženou "zdí" známé geometrie - `terrain_angle`
  vrátil úhel odpovídající `atan(výška/vzdálenost)` v toleranci 1°.
- Syntetické nakloněné roviny se ZNÁMÝM sklonem/expozicí - `compute_slope_aspect`
  souhlasí s analytickým vzorcem.
- Bisekce (Krok 5) souhlasí s nezávislým jemným lineárním skenem do 2s.
- Zakřivení Země souhlasí s uzavřeným vzorcem `d²/(2R_eff)`.
- Kvalitativní shoda s pověstí Milešovky (extrémně otevřený výhled).
- Monotónní, fyzikálně smysluplné pořadí napříč 5 různými terénními
  situacemi (Krok 3).
- Live download + multi-tile merge (Krok 7) funguje end-to-end na
  reálných datech.

**NENÍ ověřeno (řečeno na rovinu, ne zamlčeno):**
- Žádné srovnání s reálnou fotografií západu Slunce ani s Google Earth 3D
  pohledem - nejsilnější možná validace, ale vyžaduje externí referenční
  materiál, který jsem k dispozici neměl.
- Absolutní přesnost DEM (Copernicus DSM) vs. skutečnost - víme jen, že
  numericky sedí na Wikipedii u JEDNOHO bodu, ne systematicky přes víc bodů.
- Citlivost na DEM rozlišení byla testována jen pro izolovaný vrchol (bod
  A) - ne pro hřeben/svah/údolí, kde by efekt mohl být jiný (pravděpodobně
  menší, ale netestováno).
- Přesnost bodů B-E ze sítě (Krok 3) nebyla nezávisle ověřena mimo
  vlastní DEM - jsou to reálná data, ale nemám externí potvrzení, že
  odpovídají realitě na místě.
- **"Výsledek vypadá rozumně" ≠ "výsledek byl validován"** - tohle
  rozlišení je vědomě dodržené v celém dokumentu.

## 18. Struktura projektu

```
sunset-radar/
├── app/
│   ├── dem.py                # GeoTIFF DEM + bilineární sampling, OutOfBoundsError, NaN pro NODATA
│   ├── geo.py                 # geodetické výpočty (pyproj)
│   ├── solar.py                # poloha Slunce (astral)
│   ├── horizon.py              # terénní horizont, 360° wraparound, zakřivení Země
│   ├── visibility.py            # is_sun_visible, bisekce, sunset+sunrise
│   ├── tile_manager.py          # DEM tile cache/download/merge
│   ├── terrain_attributes.py    # slope/aspect (informační, nepoužito ve visibility)
│   └── experimental_score.py    # experimentální geometrický indikátor
├── scripts/
│   ├── poc_sunset.py            # PoC #1 - jeden bod, západní sektor
│   ├── poc_360_horizon.py       # PoC #2 - 360°, kartézský + polární graf
│   ├── find_test_points.py      # programové hledání testovacích bodů z DEM
│   └── test_point_network.py    # síť 5 bodů, diagnostický graf
├── tests/                        # 51 testů (49 unit + 2 integration), syntetické DEM
├── data/                          # stažené DEM dlaždice + grafy (gitignored)
├── requirements.txt / requirements-dev.txt
├── pytest.ini                     # marker "integration"
├── PLAN.md
└── README.md
```

## 19. Stav a další krok

**Hotovo a validováno (v mezích Kroku 17)**: DEM sampling s ošetřenými
edge cases, 360° terénní horizont s wraparound interpolací, panoramatická
vizualizace, obecný (ne na sunset natvrdo navázaný) terrain engine se
sunrise i sunset, bisekce pro přesný čas zakrytí, DEM tile cache/merge,
slope/aspect, experimentální indikátor, 51 testů, empiricky podložená
volba `astral` nad `pvlib`, změřený a jednou opravou 2.5× zrychlený výkon.

**Nehotovo (vědomě, mimo rozsah)**: skutečná ČÚZK DMR 5G integrace, web/
mapa, počasí, kvalita světla, přístupnost, kalibrace experimentálního
skóre na reálných preferencích, systematická validace přes víc bodů
externím zdrojem (foto/Google Earth).

**Nejlogičtější další krok** (v době psaní tohohle odstavce): vizuálně
ověřit aspoň jeden z vygenerovaných grafů proti reálné fotografii nebo
Google Earth 3D pohledu. **Aktualizace**: místo toho byla další fází
zvolena kvalita a infrastruktura DEM dat (viz níže, Krok 20+) - vizuální
validace zůstává otevřená.

---

# FÁZE 3 — Kvalita a datová infrastruktura DEM

## 20. ČÚZK DMR 5G — praktická implementace (Krok 1)

Teoretický průzkum z Kroku 2 (fáze 2) byl **ověřen prakticky, ne jen
zdokumentován**. Klíčová nová zjištění (`?f=json` metadata služby,
skutečné `exportImage` volání):

- **Nativní rozlišení je 2 m**, ne 5 m (`pixelSizeX/Y=2` v metadatech) -
  "5G" značí generaci produktu (5. generace DMR), ne velikost pixelu.
- **Server umí přímou reprojekci do EPSG:4326** (`imageSR=4326` parametr)
  - žádná ruční reprojekce z S-JTSK/Krovak není potřeba, výstup jde rovnou
  do `app.dem.DEMSource` beze změny.
- Popis služby explicitně potvrzuje **DTM charakter**: "digitální model
  reliefu... přirozeného... zemského povrchu", LiDAR 2009-2013, přesnost
  0,18 m (odkrytý terén) / 0,3 m (zalesněný terén).
- Žádný API klíč, žádné přihlášení - `GET` na veřejnou URL.
- **DVA nezdokumentované praktické limity zjištěné EMPIRICKY (binárním
  hledáním, ne z dokumentace):**
  1. `maxImageWidth=15000, maxImageHeight=4100` (tohle bylo v metadatech).
  2. **Server navíc padá s `500 Internal Server Error` nad ~9-10 milionů
     pixelů CELKEM** - výrazně dřív, než by dovolily samotné rozměrové
     limity (ty by povolily až 61,5M px). Ověřeno: 3000×3000 (9M px) OK
     za 27s, 3200×3200 (10,24M px) selhalo okamžitě (500, ~8s, bez
     renderování). Řešeno automatickým zhrubením rozlišení (`_effective_pixel_size`
     v `app/cuzk_dem.py`), aby jeden požadavek vždy zůstal pod touhle
     nezdokumentovanou hranicí (`MAX_TOTAL_PIXELS = 8_000_000`, bezpečná
     rezerva).

**Rozhodnutí: implementováno**, `app/cuzk_dem.py` - analogie
`app/tile_manager.py`, ale bez pevných dlaždic (ČÚZK přes tenhle přístup
žádné nemá) - misto toho jeden přímý požadavek na přesný bbox, s cache
podle hash(bbox, rozlišení).

## 21. DEM abstrakce (Krok 2)

`app/dem_provider.py` - `DEMProvider` jako `typing.Protocol` (structural
typing, ne nutná explicitní dědičnost) s `elevation(lat, lon)` a
`contains(lat, lon)`. **`app.dem.DEMSource` už tenhle protokol splňovala
beze změny** - jak Copernicus, tak ČÚZK produkují GeoTIFF v EPSG:4326,
takže existující sampling kód je zdroj-agnostický od začátku (viz Krok 7
z fáze 2). Terrain engine (`app/horizon.py`) má teď typové anotace na
`DEMProvider`, ne na `DEMSource` - ověřeno testem s fake implementací
protokolu (`test_horizon_engine_works_with_any_demprovider_not_just_demsource`),
že `terrain_angle()` funguje se STEJNÝM chováním na cizí implementaci.

`open_copernicus_dem(lat, lon, radius_m)` a `open_cuzk_dmr5g_dem(lat, lon, radius_m)`
jsou tenké factory funkce - obě vrací `DEMSource`, terrain engine nikdy
neví, který zdroj to byl.

Sdílená pomůcka `app/dem_merge.py` (sloučení víc GeoTIFF do mozaiky) je
teď použitá jak Copernicus tile manažerem, tak (potenciálně) ČÚZK cache -
nepsalo se to dvakrát.

## 22. Srovnání GLO-30 vs. DMR 5G na síti bodů (Krok 3)

Stejných 5 testovacích bodů z fáze 2, dosah 30 km, stejné datum:

| Místo | GLO-30 horizon | DMR5G horizon | rozdíl | GLO-30 zpoždění | DMR5G zpoždění |
|---|---:|---:|---:|---:|---:|
| A - vrchol | -0.96° | -0.98° | -0.03° | 1.4 min | 1.3 min |
| B - hřeben | -0.02° | +5.67° | **+5.69°** | 5.7 min | **36.3 min** |
| C - svah | +2.04° | +2.03° | -0.01° | 18.3 min | 18.1 min |
| D - údolí | +8.93° | +4.88° | **-4.05°** | 62.8 min | **35.5 min** |
| E - blokováno | +22.12° | +22.54° | +0.43° | 84.2 min | 88.9 min |

(`scripts/compare_dem_sources.py`)

## 23. Analýza rozdílů — PROČ, ne jen KOLIK (Krok 4)

**Body A, C, E (vrchol, svah, silná překážka): rozdíl zanedbatelný**
(<0,5°, <5 min). Jednoduchý, hladký nebo dominantně velký terénní útvar -
oba zdroje ho zachytí prakticky stejně bez ohledu na DSM/DTM rozdíl nebo
rozlišení (21,6-36 m u ČÚZK po automatickém zhrubení pro 30km dotaz vs.
30 m Copernicus - podobný řád).

**Bod B (hřeben): rozdíl +5,69° - VYŠETŘENO, není to "DMR5G je
přesnější".** Jemný radiální profil (krok 90 m) ukázal, že maximum úhlu u
DMR5G vzniká **na úplně první vzorek (90 m od pozorovatele)**, kde terén
vystoupá o ~10,6 m nad bod, který `find_test_points.py` (fáze 2) označil
za "hřeben" hrubým gridem (krok ~450 m) - **stejný mechanismus jako
původní chyba u Milešovky** (fáze 1): nepřesná souřadnice pozorovatele +
blízký hrbol. Copernicus (30 m) tenhle lokální hrbol prostě "rozmázne"
průměrováním, ČÚZK (jemnější) ho ukáže. **Není to tedy o tom, že by ČÚZK
"objevilo skutečnou překážku" - je to artefakt přesnosti umístění
pozorovatele interagující s rozlišením.** Poučení: jemnější DEM
NEZARUČUJE lepší výsledek, pokud vstupní souřadnice není přesná na
srovnatelné škále.

**Bod D (údolí): rozdíl -4,05° a rozdíl elevace pozorovatele +48,8 m -
VYŠETŘENO, vypadá to na chybu v Copernicus datech, ne na DTM/DSM rozdíl.**
Kontrola 200m okolí bodu ukázala SYSTEMATICKÝ rozdíl v celé oblasti
(Copernicus 44-67 m, ČÚZK 95-114 m) - není to jednopixelový skok, je to
konzistentní posun přes desítky metrů okolí. Bod leží v labské nivě -
pravděpodobné vysvětlení je artefakt Copernicus DSM (TanDEM-X, radarové
snímkování) nad vodní plochou/mokřinou, kde radarový signál chová
nepředvídatelně. ČÚZK (LiDAR) je pro tenhle typ terénu důvěryhodnější.
**Tohle je konkrétní, empiricky podložený příklad, kde DMR 5G dat kvalita
skutečně je lepší** - na rozdíl od bodu B, kde šlo jen o artefakt
přesnosti bodu.

**Shrnutí Kroku 4**: rozdíly NEJSOU rovnoměrně rozložené - u 3 z 5 bodů
jsou zanedbatelné, u 2 z 5 velké, a KAŽDÝ velký rozdíl měl jinou
příčinu (jeden byl artefakt souřadnice, druhý pravděpodobná datová chyba
zdroje). To přesně odpovídá zadání "nechci pouze konstatování, že DMR5G
je přesnější" - realita je nuancovanější.

## 24. Citlivost na dosah - napříč oběma zdroji (Krok 5)

| Dosah | GLO-30 (bod A) | DMR5G (bod A) | rozdíl mezi zdroji |
|---|---:|---:|---:|
| 5 km | -4.551° | -4.452° | 0.10° |
| 10 km | -3.298° | -3.277° | 0.02° |
| 20 km | -1.662° | -1.630° | 0.03° |
| 30 km | -0.953° | -0.988° | 0.04° |
| 50 km | -0.050° | -0.031° | 0.02° |

**Citlivost na dosah je prakticky STEJNÁ u obou zdrojů** (rozdíly mezi
zdroji <0,1° při libovolném dosahu) - potvrzuje to, že jde o čistě
geometrický jev (zakřivení Země přes vzdálenost), ne o vlastnost
konkrétních dat. **Žádný jasný "plateau" nebyl nalezen ani na 50 km** -
přechod 30→50 km pořád mění výsledek o ~0,9° (víc, než přechod 20→30 km,
~0,7°) - u tohohle konkrétního (extrémně otevřeného) bodu se citlivost
na dosah zmírňuje, ale nezanikne úplně ani na 50 km. **Doporučení: 30 km
zůstává rozumný default pro rychlost/přesnost kompromis, ale pro
nejotevřenější místa (izolované vrcholy) by přesnější odhad chtěl 50 km+.**
Nejde o arbitrární volbu - je to zdokumentovaný kompromis s změřenými čísly.

## 25. Výkon ČÚZK vs. Copernicus (Krok 6)

| Metrika | Copernicus GLO-30 | ČÚZK DMR 5G |
|---|---:|---:|
| Velikost dat (30km dosah) | ~29 MB (celá 1° dlaždice, i pro malé dotazy) | ~10-20 MB (přesně ořezáno na dotaz) |
| Stažení, dlaždice v cache | 0,1 ms | 0,1 ms (stejný princip cache) |
| Stažení, chybí v cache (30km) | ~3-6 s (S3, statický soubor) | **~23 s** (dynamické renderování na serveru) |
| Stažení, chybí v cache (3km) | ~3-6 s (pořád celá dlaždice) | **~4,6 s** (jen požadovaná oblast, žádné plýtvání) |
| Auto-degradace rozlišení u velkých oblastí | Ne (pevná 30m dlaždice) | Ano (nezdokumentovaný limit ~9M px, viz Krok 20) |

**ČÚZK je při STUDENÉM požadavku na velkou oblast (30 km) ~4-8× pomalejší
než Copernicus** (dynamické generování mozaiky + reprojekce na serveru
vs. čtení statického předrenderovaného souboru z S3). Pro MALÉ oblasti
(3 km) je naopak ČÚZK efektivnější (stahuje jen to, co je potřeba, ne
celou 1° dlaždici). **Oprava nalezená měřením** (ne odhadem): první verze
(vlastní virtuální dlaždice 0,25°, sloučené přes `merge`) potřebovala pro
30 km dotaz 12 samostatných požadavků a **206,71 s** - přepsáno na jeden
přímý požadavek s automatickým zhrubením rozlišení → **23,39 s (8,8×
zrychlení)**. Po prvním stažení je cache hit u obou zdrojů stejně rychlý
(~0 ms).

**Závěr Kroku 6**: ČÚZK je prakticky použitelný, ale ne "zadarmo" oproti
Copernicus - první dotaz na novou oblast je znatelně pomalejší. Pro
produkční nasazení by dávalo smysl ČÚZK data předpočítat/cachovat
dopředu pro oblíbené lokality, ne spoléhat na "on-demand" fetch při každém
kliknutí uživatele.

## 26. DEM Strategy (Krok 7 - finální rozhodnutí)

### 7.1 Srovnání obou zdrojů

| Kritérium | Copernicus GLO-30 | ČÚZK DMR 5G |
|---|---|---|
| Přesnost horizontu | Dobrá pro jednoduchý/dominantní terén; hrubší (30 m) rozmázne malé lokální útvary | Lepší PŘI přesné souřadnici pozorovatele; jemnější rozlišení ale zesiluje chybu nepřesné souřadnice (viz Krok 23, bod B) |
| Typ dat | **DSM** (povrch - včetně vegetace/budov) | **DTM** (skutečný terén, LiDAR) |
| Prostorové pokrytí | Globální | Jen ČR |
| Dostupnost/spolehlivost | Vysoká (statické soubory na AWS S3, průmyslový standard) | Nejistá ve velkém měřítku (jediný státní ArcGIS server, žádné zdokumentované SLA, nezdokumentovaný pixelový limit vedoucí k 500 chybám - viz Krok 20) |
| Rychlost (studený dotaz, 30 km) | ~3-6 s | ~23 s (server renderuje mozaiku dynamicky) |
| Velikost dat | Pevná 1° dlaždice (~29 MB) bez ohledu na velikost dotazu | Přesně podle dotazu (efektivnější pro malé oblasti) |
| Náročnost stahování | Jednoduchá (pevná mřížka dlaždic, predikovatelné) | Složitější (dynamické vyjednávání bbox+rozlišení, nutná ochrana proti nezdokumentovaným limitům) |
| Chování při chybě | Běžné HTTP chyby | Může vrátit JSON chybu s HTTP 200 (zavádějící - řešeno explicitní kontrolou `Content-Type`) |
| Vhodnost pro produkci | Vysoká - prověřená infrastruktura | Střední - funguje, ale potřebuje robustní zpracování chyb (implementováno, viz 7.3) |
| Vhodnost pro ČR | Adekvátní, ale hrubší + nalezené artefakty DSM (Krok 23, bod D) | Lepší - jemnější, skutečné DTM |
| Vhodnost mimo ČR | Jediná možnost | Nepoužitelné (bez pokrytí) |

**Důležitá nuance** (explicitně požadovaná v zadání): Sunset Radar je citlivý na **horizontální profil terénu**, ne jen na výšku jednoho bodu. To znamená, že "jemnější rozlišení = lepší" NENÍ automaticky pravda - záleží na přesnosti vstupní souřadnice relativně k rozlišení (viz Krok 23, kde jemnější ČÚZK data zviditelnila artefakt způsobený nepřesnou testovací souřadnicí, ne "objevila skutečnou překážku"). V produkci, kde souřadnice pochází z přesného kliknutí na mapu (ne z hrubého gridového hledání jako naše testovací body), tenhle konkrétní rizikový scénář nastává méně - jemnější rozlišení pak skutečně pomáhá.

### 7.2 Zvolená strategie: **A) ČÚZK v ČR, Copernicus mimo ČR - rozšířeno o povinný automatický fallback**

Zvoleno **A s vestavěným fallbackem** (efektivně A+B dohromady): v ČR se
vždy PRIMÁRNĚ zkusí ČÚZK DMR 5G (přesnější DTM), ale při JAKÉMKOLIV
problému systém automaticky a transparentně přejde na Copernicus - nikdy
není systém závislý čistě na dostupnosti ČÚZK serveru. Mimo ČR se ČÚZK
vůbec nezkouší (věděli bychom předem, že selže na `OutsideCoverageError`
- zbytečná síťová operace navíc).

**Proč ne čisté "B" (Copernicus vždy výchozí, ČÚZK jen volitelný
override)**: přesnost je prioritní podle zadané priority "správnost →
validace → robustnost → výkon". ČÚZK je demonstrovatelně přesnější zdroj
(skutečné DTM, jemnější rozlišení, zachytilo pravděpodobnou chybu v
Copernicus u bodu D) - nedávalo by smysl ho degradovat na "volitelné
vylepšení", když je výchozí volbou automaticky dostupný a jen o něco
pomalejší při prvním dotazu.

Výběr je čistě podle GPS souřadnice (`is_roughly_in_cz()` - hrubý
obalový obdélník ČR) - žádný API klíč, žádný zásah uživatele.

### 7.3 Fallback mechanismus

Implementováno v `app/dem_strategy.py::resolve_dem()` jako deterministický rozhodovací strom:

```
v ČR:
    zkus ČÚZK DMR 5G
        úspěch + elevace v rozumném rozsahu pro ČR (90-1650 m) -> použij ČÚZK
        cokoliv jiné -> fallback na Copernicus GLO-30:
            - OutsideCoverageError (bod těsně u hranice obalového obdélníku)
            - requests.exceptions.RequestException (timeout, spojení, HTTP 4xx/5xx)
            - RuntimeError (server vrátil JSON chybu maskovanou jako HTTP 200)
            - NaN na pozici pozorovatele (NODATA)
            - elevace mimo 90-1650 m (podezřelá kvalita)
mimo ČR:
    rovnou Copernicus (ČÚZK by stejně selhalo na coverage)
```

**Pokud selže i Copernicus, chyba se NEPOTLAČUJE** - žádný třetí zdroj
neexistuje, takže tichý fallback na "nic" by byl horší než jasná chyba
volajícímu. To je vědomá hranice systému, ne opomenutí.

Ověřeno 9 testy (`tests/test_dem_strategy.py`) - každá větev
rozhodovacího stromu zvlášť, s mockovaným selháním (rychlé, deterministické).

### 7.4 Metadata o kvalitě DEM

`app.dem_strategy.DEMMetadata` (dataclass): `provider`, `dataset`
(včetně DTM/DSM v názvu), `spatial_resolution_m` (skutečně použité, po
případném zhrubení), `analysis_range_m`, `source_crs`, `acquired_at`,
`used_fallback`, `fallback_reason`, `quality_flag`. Metoda `.describe()`
vrátí lidsky čitelný řetězec přesně v duchu zadání:

```
"ČÚZK DMR 5G (DTM) · 10 m"
"Copernicus GLO-30 (DSM) · 30 m · fallback"
```

### 7.5 Analysis range - výchozí hodnota

**Zvoleno: 50 km, KONFIGUROVATELNÉ** (parametr `analysis_range_m` v
`resolve_dem()`, výchozí `DEFAULT_ANALYSIS_RANGE_M = 50_000.0`).

Zdůvodnění: Krok 24 ukázal, že rozdíl mezi 30 km a 50 km je pořád ~0,9°
u otevřených míst - žádný jasný "plateau" nebyl nalezen ani na 50 km.
Zadání tohohle kroku explicitně říká "nechci optimalizovat výkon za
každou cenu, důležitější je fyzikálně smysluplný výsledek" - proto je
výchozí hodnota teď 50 km (dřív 30 km ve fázi 2). Cena je jen delší
STAŽENÍ při prvním dotazu do nové oblasti (jde o síť, ne o výpočet -
samotný výpočet horizontu zůstává rychlý i na 50 km, řádově stovky ms).

**Ne adaptivní** (zatím) - adaptivní rozšiřování dosahu "dokud se výsledek
nepřestane měnit" by bylo elegantnější, ale přidává komplexitu nad rámec
tohohle kroku ("uzavřít DEM infrastrukturu", ne vymýšlet nové algoritmy).
Zůstává jako rozumné budoucí vylepšení, ne current-scope rozhodnutí.

### 7.6 DSM vs. DTM - vizuální vs. terénní horizont

**Rozhodnutí: Sunset Radar v týhle fázi počítá se skutečným TERÉNNÍM
horizontem** (upřednostňuje DTM, kde je dostupné - ČÚZK v ČR), ne s
vizuálním horizontem včetně objektů. Důsledky pro jednotlivé kategorie:

- **Les**: DSM (Copernicus) zahrnuje korunu stromů (+10-25 m typicky) -
  to JE reálná překážka pro pozorovatele stojícího pod stromy, ale mění
  se sezónně (opadavý les) a při těžbě - není to stabilní geometrická
  vlastnost terénu.
- **Budovy**: DSM zahrnuje střechy - reálná překážka pro pozorovatele ve
  vesnici/městě, ale DTM ji ignoruje (mohla by podhodnotit skutečnou
  překážku poblíž zástavby).
- **Stožáry/věže**: přesně tohle vysvětluje rozdíl ~3-4 m mezi Copernicus
  (835,3 m) a ČÚZK (832,7 m) na vrcholu Milešovky - meteorologická
  observatoř na vrcholu.
- **Malé skalní útvary**: obojí by je mělo zachytit, pokud je rozlišení
  dost jemné - je to skutečný terén, ne objekt na povrchu.

**Architektura je připravená na budoucí rozšíření** beze změny principu:
`DEMMetadata.dataset` už teď rozlišuje DTM/DSM explicitně v názvu, takže
budoucí "vizuální horizont" funkce by mohla kombinovat OBOJÍ (DTM jako
geometrický základ + DSM jako korekce pro vegetaci/budovy) - není potřeba
nic měnit v současném kódu, jen přidat novou funkci nad existující data.

## 27. Stav po Fázi 3 (Krok 7 dokončen)

**Hotovo**: kompletní DEM infrastruktura - dva reálné zdroje dat za
jednotnou abstrakcí, automatický výběr podle lokality, deterministický
fallback pokrývající všechny scénáře ze zadání, metadata o kvalitě,
zdůvodněný výchozí dosah analýzy (50 km), explicitní DTM/DSM rozhodnutí
s připravenou architekturou na budoucí rozšíření. **70 testů, všechny
procházejí** (57 unit + 3 integration z fáze 3, + 10 nových pro Krok 7 -
viz `tests/test_dem_strategy.py`).

**Změněné/nové soubory v Kroku 7**:
- `app/dem_strategy.py` (nový) - `resolve_dem()`, `DEMMetadata`, fallback logika
- `app/cuzk_dem.py` (upraveno) - `is_roughly_in_cz`/`effective_pixel_size` zveřejněny (bez `_`) pro použití napříč moduly
- `tests/test_dem_strategy.py` (nový) - 10 testů
- `tests/test_cuzk_dem.py` (upraveno) - přejmenování po zveřejnění funkcí
- `PLAN.md`, `README.md` (aktualizováno)

**Nejmenší další krok**: podle zadání se tímhle krokem DEM infrastruktura
UZAVÍRÁ - další fáze (frontend/mapa/UI) čeká na explicitní pokyn, jak
bylo požadováno ("po dokončení se zastav").

---

# FÁZE 4 — Analyze Location Engine

Jedna veřejná funkce `analyze_location()` (`app/analyze_location.py`),
která orchestruje všechny existující komponenty (DEM strategie, terénní
horizont, astronomie, viditelnost) do jednoho strukturovaného výsledku.
**Čistá orchestrace** - žádná výpočetní logika se nepřepisovala ani
neduplikovala, každá komponenta zůstává nezávisle použitelná a testovaná.

## 28. Veřejné API

```python
def analyze_location(
    lat: float,
    lon: float,
    when: datetime,                     # POVINNĚ timezone-aware
    analysis_range_m: float = 50_000.0,
    azimuth_step_deg: float = 1.0,
    horizon_step_m: float = 90.0,
    observer_eye_height_m: float = 1.7,
    pixel_size_m: float = 10.0,
    refraction_coefficient: float = 0.13,
    visibility_search_window: timedelta = timedelta(hours=3),
    include_sunrise: bool = True,
) -> LocationAnalysis
```

Vrací `LocationAnalysis` (dataclass): `location`, `dem`, `terrain`,
`solar`, `visibility`, `diagnostics` - přesně podle konceptu ze zadání,
přizpůsobené existujícím datovým typům (`DEMMetadata` z `app.dem_strategy`,
`HorizonProfile` z `app.horizon`, `SunsetVisibilityResult`/`SunriseVisibilityResult`
z `app.visibility`). Výstup je čistě strukturovaná data (dataclassy) -
žádné HTML, žádný prezentační text - viz zadání bod 9.

## 29. Terminologie - přesně podle zadání bodu 3

- **`astronomical_sunset`**: okamžik, kdy Slunce dosáhne standardní
  astronomické výšky pro západ (`app.solar.astronomical_sunset` - astral,
  včetně refrakce a "dip" podle nadmořské výšky pozorovatele).
- **`terrain_obscured_sunset`** (pole `visibility.terrain_obscured_sunset`):
  okamžik, kdy Slunce přestane být geometricky viditelné kvůli terénnímu
  horizontu v příslušném azimutu. **Rovná se přesně `astronomical_sunset`,
  pokud `terrain_obstruction=False`** - ověřeno testem, žádný falešný posun.
- **`visibility_duration`**: doba od okamžiku, kdy Slunce NAPOSLEDY
  vystoupilo nad lokální terénní horizont, do `terrain_obscured_sunset`.
  Hledá se POSLEDNÍ (ne první) vzestupný přechod v prohledávaném okně
  (`app.visibility.find_horizon_crossing(..., occurrence="last")`) -
  nutné pro nemonotónní horizont (Slunce se může krátce schovat za bližší
  hrbol a znovu vynořit, než definitivně zapadne). Pokud vzestupný
  přechod NENÍ nalezen uvnitř prohledávaného okna (Slunce bylo nad
  horizontem po celou dobu), hodnota je **jen dolní odhad**
  (`visibility_duration_is_lower_bound=True`) s jasným varováním v
  `diagnostics.warnings` - NIKDY se to nepředstírá jako přesné (viz
  zadání: "pokud algoritmus neumí veličinu korektně určit, neupravuj
  výsledek odhadem").

## 30. Nalezené a opravené problémy (audit před orchestrací)

Než vznikla samotná orchestrace, tři reálné nedodělky z předchozích fází:

1. **`app/horizon.py` mělo zastaralé typové anotace** (`dem: DEMSource`),
   i když import byl už dřív změněn na `DEMProvider` - fungovalo to jen
   díky `from __future__ import annotations` (anotace se nevyhodnocují za
   běhu), ale bylo to zavádějící. Opraveno.
2. **Opakovaný výpočet** (zadání bod 10 - "pokud se stejná data počítají
   vícekrát, oprav to"): `terrain_angle()` si při KAŽDÉM volání znovu
   četlo nadmořskou výšku pozorovatele z DEM - u 360° sweepu 360× stejná
   hodnota. Profilování ukázalo, že šlo jen o ~0,3 % celkového času (ne
   hlavní bottleneck), ale princip "nepočítej stejnou věc dvakrát" platí
   nezávisle na tom, jak moc to bolí - přidán volitelný parametr
   `observer_elevation_m` (zpětně kompatibilní, výchozí `None` zachovává
   přesně původní chování), `compute_horizon_profile` ho teď počítá
   JEDNOU a předává dál.
3. **`compute_horizon_profile` vůbec nepřijímala `refraction_coefficient`**
   parametr (jen `terrain_angle` per-azimut ho měla) - `analyze_location`
   by ho tiše IGNOROVALA, i kdyby ho volající explicitně nastavil. Opraveno
   přidáním průchozího parametru (výchozí hodnota zachovává původní chování).

Všechny tři opravy mají regresní testy (`tests/test_horizon.py`).

## 31. Výkon (zadání bod 10)

| Scénář | Čas |
|---|---|
| `analyze_location()`, 20 km dosah, DEM v cache | 0,29 s |
| `analyze_location()`, 50 km dosah (výchozí), DEM v cache | 0,68-0,76 s |
| `analyze_location()`, 50 km dosah, NOVÝ bod (studený DEM požadavek, ČÚZK) | 21,05 s |
| `analyze_location()`, 20 km dosah, NOVÝ bod (studený DEM požadavek) | 13,78 s |

Naměřeno na reálném bodě (Brno pro "nový bod" test, aby cache byla
opravdu prázdná). Dominantní náklad je STAŽENÍ DEM (síť), ne výpočet -
samotný `compute_horizon_profile()` běží pod 1s i na 50 km (viz Fáze 3,
Krok 25). Žádná databáze nebyla potřeba - existující souborová cache
(`app/tile_manager.py`/`app/cuzk_dem.py`) stačí.

## 32. Otestované edge cases (zadání bod 8)

| Edge case | Jak otestováno |
|---|---|
| Otevřený horizont (bez obstrukce) | `test_real_open_peak_no_obstruction_no_false_shift` (reálná Milešovka, 10 km) |
| Vysoký hřeben / blízká překážka | `test_high_wall_causes_obstruction` (syntetická "zeď") |
| Hluboké údolí, svah | pokryto v Fázi 3 (`scripts/test_point_network.py`) - `analyze_location` používá stejné jádro |
| Azimut kolem 0°/360° | `test_azimuth_wraparound_near_zero_does_not_crash` |
| Velmi nízký/vysoký terrain horizon na rovině | `test_flat_terrain_horizon_stays_small_regardless_of_range` |
| Chybějící/NODATA DEM (poslední zdroj selže) | `test_nodata_at_final_source_raises_clear_error` - jasná `RuntimeError`, ne tichý výsledek |
| ČÚZK fallback | `test_fallback_metadata_propagates_to_result` |
| Místo mimo ČR | `test_real_foreign_location_uses_copernicus` |
| DST (jarní i podzimní přechod) | `test_dst_spring_forward_transition_no_crash`, `test_dst_fall_back_transition_no_crash` |
| UTC i pojmenovaná zóna, stejný fyzický okamžik | `test_different_timezones_same_instant_give_consistent_sunset` |
| Naivní datetime (bez timezone) | `test_naive_datetime_rejected` |
| Determinismus | `test_deterministic_result_for_same_inputs` |
| 360° konzistence horizontu | `test_360_horizon_integrity` |
| horizon ↔ solar azimut shoda | `test_horizon_matches_solar_azimuth_lookup` |

**Zajímavý vedlejší nález při psaní testů**: dokonale plochý syntetický
terén (pozorovatel jen ve výšce očí 1,7 m, bez převýšení) NEMŮŽE
matematicky dosáhnout `terrain_obstruction=False` vůči astral -0,833°
referenci - dip horizontu daný jen výškou očí je řádově 0,04°
(odpovídá standardnímu geodetickému vzorci `dip≈0,0293°·√výška_m`).
"Opravdu otevřený horizont" vyžaduje SKUTEČNÉ převýšení pozorovatele nad
okolím (jako Milešovka), ne jen dostatečný dosah analýzy na rovině. Není
to bug - je to důsledek toho, že terénní horizont a astral -0,833° jsou
dva nezávisle odvozené modely, které se numericky přesně neshodují pro
každý myslitelný scénář.

## 33. Stav po Fázi 4

**Hotovo**: jedna veřejná, dobře zdokumentovaná orchestrační funkce nad
celým dosavadním enginem, přesná terminologie (astronomical/terrain-
obscured/visibility_duration), correct timezone handling (žádná implicitní
lokální zóna, testováno na DST přechodech), 360° horizon data zachovaná v
plné podobě pro budoucí UI, 3 reálné nedodělky nalezené a opravené
(zastaralé typy, zbytečný opakovaný výpočet, tichy ignorovaný parametr),
výkon změřen. **96 testů, všechny procházejí** (89 unit + 7 integration).

**Změněné/nové soubory**:
- `app/analyze_location.py` (nový) - `analyze_location()` + `LocationAnalysis` a související dataclassy
- `app/horizon.py` (opraveno) - `DEMProvider` anotace, `observer_elevation_m`, `refraction_coefficient` pass-through
- `app/visibility.py` (rozšířeno) - `find_horizon_crossing(..., occurrence="first"|"last")`
- `tests/test_analyze_location.py` (nový) - 21 testů
- `tests/test_horizon.py`, `tests/test_visibility.py` (rozšířeno) - regresní testy pro opravy
- `PLAN.md`, `README.md` (aktualizováno)

**Nejmenší další krok**: podle zadání se engine teď považuje za
spolehlivé jádro - další krok (frontend/mapa) čeká na explicitní pokyn.

---

# FÁZE 5 — první použitelné UI

## 34. Audit před implementací

`app/` v době auditu neobsahoval žádný web framework ani template kód
(`app/main.py` existoval jako prázdný soubor od začátku projektu).
Žádné FastAPI/Flask/Jinja2 nebyly nainstalované. Zvoleno **FastAPI +
Jinja2 + uvicorn** (ne Flask) - hlavní důvod: Pydantic validace vstupu
"zadarmo" (`AnalyzeRequest` s `field_validator` pro lat/lon/date/timezone)
přímo pokrývá zadání bod 19 (bezpečnost/robustnost vstupu) s minimem kódu,
a čistá JSON serializace je jednodušší než ve Flasku. Frontend: **žádný
build systém, žádný React** - vanilla HTML/CSS/JS, polar chart vlastní
implementací na `<canvas>` (žádná externí grafová knihovna).

## 35. Architektura (dodrženo přesně podle zadání bodu 8/16)

```
Browser → POST /api/analyze → analyze_location() → (DEM strategy → DEM provider → terrain/solar/visibility)
```

`app/main.py` obsahuje JEN: (1) Pydantic validaci vstupu, (2) JEDNO volání
`analyze_location()`, (3) serializaci výsledku do JSON (`_to_json` -
rekurzivní převod dataclass/datetime/timedelta/NaN na JSON-kompatibilní
typy, NENÍ to výpočet). `static/app.js` čte jen už hotová pole z
odpovědi a vykresluje je - žádný přepočet horizontu, azimutu ani
viditelnosti ve frontendu.

## 36. Nalezený a opravený bug (Fáze 5)

**Bod bez pokrytí posledního DEM zdroje (např. oceán) způsoboval
neošetřenou `requests.exceptions.HTTPError` a syrové HTTP 500 bez
jakékoliv zprávy.** Reprodukováno (`lat=0, lon=-140`, Copernicus nemá pro
oceán dlaždici na S3 → 404 → `HTTPError`, subclass `RequestException`,
nezachycená v `app/main.py`). `app/analyze_location.py`/`app/dem_strategy.py`
tuhle chybu ZÁMĚRNĚ nepotlačují (žádný třetí DEM zdroj neexistuje - viz
Fáze 3 Krok 26), takže oprava patří do webové vrstvy, ne do enginu: přidán
`except requests.exceptions.RequestException` v `app/main.py::api_analyze`,
vrací srozumitelnou zprávu + HTTP 502. Regresní test
(`tests/test_web.py::test_dem_network_failure_returns_clear_error_not_raw_500`
+ živý integrační test se stejným bodem).

## 37. UI - hlavní obrazovka

- **Location**: lat/lon pole + dropdown **Test locations** (5 lokalit,
  souřadnice převzaté z už programově nalezených/validovaných bodů z Fáze
  3 - žádné nové odhady) + klikací mapa (Fáze 5.5).
- **Date** + **Timezone** (textové pole, IANA název, výchozí
  `Europe/Prague`) - datum+timezone se na serveru spojí do `datetime`
  s polednem jako referenčním časem (žádná implicitní timezone stroje,
  viz Fáze 4 Krok 29).
- **Hero sekce**: Sunset / Terrain obstruction / Visible sunset - velkým
  písmem, přímo ze skutečných hodnot (`visibility.astronomical_sunset`,
  `visibility.terrain_obscured_sunset`, `visibility.visibility_duration`).
- **Barevný badge**: Open (zelená) / Delayed (oranžová) / Strong terrain
  obstruction (červená) - čistě podle `delay_minutes` (práh 15 min,
  vědomě "od oka", ŽÁDNÉ skóre kvality - viz zadání "nejde o hodnocení
  kvality sunsetu").
- **Terrain & calculation details**: observer elevation, DEM
  provider/dataset/resolution/analysis range/fallback, solar azimuth,
  terrain horizon at sunset azimuth, slope/aspect.
- **Warnings** sekce (jen když `diagnostics.warnings` není prázdné).
- **Technical JSON** (collapsible) - celý serializovaný výsledek.

## 38. 360° polar plot

Vlastní implementace na `<canvas>` (`static/app.js::drawPolarChart`) -
0°=sever nahoře, po směru hodinových ručiček (`x = cx + r·sin(θ), y = cy - r·cos(θ)`).
Radiální osa: zenitová vzdálenost (`90 - altitude`), stejná konvence jako
`scripts/poc_360_horizon.py` (Fáze 2) - horizont (0°) blízko okraje,
zenit uprostřed. Vykresluje se PŘÍMO z `terrain.azimuths_deg`/
`terrain.horizon_altitudes_deg` (celých 360 hodnot) - žádná ztráta dat
převodem na jedno číslo. Azimut západu zvýrazněný silnou barevnou čarou
(zelená/červená podle obstrukce), azimut východu tečkovanou čarou,
konkrétní bod na horizontu v azimutu západu zvýrazněný značkou.

## 39. Mapa (Fáze 5.5)

Leaflet.js (CDN `unpkg.com`) + OpenStreetMap tile server - žádný API
klíč, otevřená data. **Explicitně zdokumentováno** (viz README "Mapa"):
OSM tile server je vhodný jen pro lehké lokální vývojové použití (viz
[OSM Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/)),
ne pro produkci s vyšším provozem. Klik do mapy nastaví lat/lon pole -
mapová logika nezasahuje do výpočtového enginu (splněno zadání "mapová
logika nesmí zasahovat do výpočtového engine").

## 40. Výkon a cache přes web vrstvu (ověřeno, ne jen předpokládáno)

| Scénář | Čas |
|---|---|
| Cold request (nová oblast, výchozí 50 km) přes `/api/analyze` | 21-33 s (síť, ČÚZK) |
| Cached request (stejná oblast) přes `/api/analyze` | 0,79-0,86 s |

Web vrstva NEVYTVÁŘÍ žádné vlastní duplicitní DEM požadavky - `/api/analyze`
je tenká obálka nad `analyze_location()`, cache mechanismus (Fáze 3) funguje
beze změny. Ověřeno opakovaným voláním stejného bodu přes skutečný HTTP request.

## 41. Testy (Fáze 5)

29 nových testů (`tests/test_web.py`) - většina mockuje `analyze_location()`
přímo (rychlé, deterministické): validace vstupu (lat/lon/date/timezone),
serializace (NaN→null, interní `_profile` pole nesmí uniknout do JSON),
všechny chybové větve (OutOfBoundsError/OutsideCoverageError/RuntimeError/
RequestException/Timeout/ValueError), fallback info v odpovědi. 2 integrační
testy proti reálnému enginu (validní request, oceánský bod bez pokrytí).

## 42. UX review (zadání bod 13)

Proveden strukturální/API-level review (bez interaktivního prohlížeče,
který nemám k dispozici) - ověřeno: (a) JSON odpověď obsahuje přesně ta
pole, která UI potřebuje bez dalšího přepočtu, (b) všechna ID použitá v
`app.js` existují v `index.html` (křížová kontrola), (c) chybové stavy
vrací srozumitelné zprávy, ne prázdné/spadlé odpovědi, (d) barevné
rozdělení Open/Delayed/Blocked je jednoznačné a nezávislé na žádném
skrytém skóre. **Otevřená položka**: skutečné vizuální/interaktivní
projití v prohlížeči (jak vypadá polar plot na reálných datech, jestli
je rozložení čitelné) NEBYLO provedeno - nemám k dispozici nástroj pro
ovládání prohlížeče. Doporučeno provést ručně po návratu.

---

# FÁZE 6 — příprava datového modelu pro "Best Sunset" (BEZ vyhledávání)

## 43. `SunsetCandidate` datový model

`app/sunset_candidate.py::SunsetCandidate` - postavený nad
`LocationAnalysis` (`from_location_analysis()` factory), obsahuje
lat/lon/elevation, terrain horizon při západu, sunset azimuth, obstrukci/
zpoždění/dobu viditelnosti, DEM metadata, a **explicitně `None`** pole
pro budoucí počasí (`cloud_cover_pct`, `cloud_base_m`, `visibility_km`,
`precipitation_probability_pct`, `wind_speed_ms`) - připravená struktura,
ŽÁDNÉ vyhledávání napříč body zatím neexistuje.

## 44. Experimentální "Sunset Geometry Score" (návrh + lehká implementace)

`app/sunset_candidate.py::compute_geometry_score()` - STEJNÝ princip jako
`app.experimental_score` (Fáze 3, Krok 16), jen počítáno přímo nad
`LocationAnalysis` místo nad nižšími komponentami. Kombinuje:
- podíl otevřeného západního sektoru (240°-300°, horizont pod 0°),
- skutečné časové zpoždění západu (`delay_minutes`, capped na 60 min).

`score = 100 · (0,5 · open_fraction + 0,5 · (1 - min(delay/60, 1)))`

**Explicitně NENÍ**: hodnocení krásy/kvality západu, produkční skóre,
"nejlepší místo" žádný ranking. Je to čistě technická metrika pro budoucí
porovnávání (viz zadání bod 14 - "žádné subjektivní hodnocení").

## 45. Architektura pro budoucí weather vrstvu (bez API)

```
Terrain analysis (analyze_location, hotovo, nezávislé na počasí)
                        +
Weather analysis (BUDOUCÍ modul, zatím neexistuje)
                        ↓
              Sunset forecast (BUDOUCÍ, kombinuje obojí)
```

`SunsetCandidate` už má pole pro `cloud_cover_pct`/`cloud_base_m`/
`visibility_km`/`precipitation_probability_pct`/`wind_speed_ms` -
záměrně `None`, dokud weather vrstva nevznikne. **Terrain engine
(`analyze_location`) zůstává 100% nezávislý na počasí** - žádný modul
z Fáze 1-5 o počasí neví a nemusí vědět. Weather vrstva by byla
samostatný modul (např. `app/weather.py`), který by `SunsetCandidate`
jen DOPLNIL, ne měnil.

## 46. Audit před koncem session (zadání bod 21)

- **Duplikovaná výpočetní logika**: žádná nenalezena - web vrstva jen
  serializuje, `compute_geometry_score` je nová (menší) implementace
  stejného principu jako Fáze 3, ne kopie kódu.
- **Mrtvé dependencies nalezené a odstraněné**: `scipy` (nikdy nebylo
  importováno nikde v `app/`/`scripts/`), `python-multipart` (endpoint
  používá JSON body, ne `Form()` - `python-multipart` byl nadbytečný).
- **Hardcoded výsledky**: žádné - testovací lokality jsou jen předvyplněná
  vstupní data, ne výsledky.
- **Falešné weather informace**: žádné - všechna weather pole v
  `SunsetCandidate` jsou `None`.
- **API klíče/placené služby**: žádné (ČÚZK, Copernicus, OpenStreetMap
  tiles, Leaflet.js - všechno zdarma bez klíče).
- **Frontend jako čistě prezentační vrstva**: ověřeno (Krok 35).
- **`analyze_location()` zůstává čistý**: nezměněn v týhle fázi.

## 47. Stav po Fázi 5/5.5/6

**Implementováno**: FastAPI web UI (formulář, hero výsledky, barevná
interpretace, 360° polar plot, technical JSON, chybové stavy, loading
state), Leaflet/OSM mapa pro výběr souřadnic, `SunsetCandidate` datový
model, experimentální geometry score.

**Pouze připraveno (ne implementováno)**: skutečné vyhledávání "best
sunset spots" přes síť bodů, weather vrstva (jen datová hranice
navržena).

**Testy**: 96 → **130** (+34: 29 web, 5 sunset_candidate). Všechny
procházejí (121 unit + 9 integration).

**Zbývá na další fázi**: skutečné vizuální projití v prohlížeči (Krok 42),
weather API integrace (až bude explicitně požadována), skutečné
vyhledávání nad `SunsetCandidate`.

## 48. Validační fáze — skutečný browser test (Playwright/Chromium)

Krok 42 (výše) je teď hotový: nainstalován Playwright + Chromium, spuštěn
skutečný headless browser test (screenshoty, computed visibility, click
simulace, console error capture) proti běžící appce. Nalezené a opravené
reálné bugy:

1. **Loading indikátor viditelný hned po načtení stránky** —
   `#loading { display: flex }` (ID selektor) mělo vyšší CSS specificitu
   než výchozí `[hidden] { display: none }` prohlížeče, takže `hidden`
   atribut byl efektivně ignorován. Oprava: `#loading:not([hidden])`.
   Regresní test: `tests/test_web.py::test_loading_indicator_css_does_not_override_hidden_attribute`
   (strukturální kontrola CSS, protože `TestClient` CSS nevykresluje).
2. **Polar plot vizuálně nečitelný pro realistický terén** — radiální osa
   0-90° (zenitová vzdálenost) natlačila reálné výškové úhly terénu
   (typicky -10° až +30°) do úzkého pásu u okraje; graf pak vypadal jako
   plný vyplněný kruh bez viditelné variace. Oprava: radiální škála se teď
   počítá z reálného rozsahu dat dané lokality (s min. rozpětím 20°),
   0° (astronomický horizont) má vlastní zvýrazněný referenční kruh,
   legenda rozlišuje všechny 4 prvky (terén/západ/východ/0° reference).
3. **Nekonzistentní časové pásmo** — "Sunrise (astronomical)" v detailech
   se dřív zobrazoval v UTC, zatímco hero časy v lokální zóně, bez
   jasného označení který je který. Oprava: jednotné lokální zobrazení +
   explicitní `#tz-note` ("Všechny časy na této stránce jsou v časovém
   pásmu: …").
4. **Chybová hláška pro chybějící DEM pokrytí (např. oceán) prosakovala
   syrovou technickou výjimku** (interní S3 URL) do uživatelského UI.
   Oprava: technický detail jde jen do server logu (`logger.warning`),
   uživatel dostává čistě lidskou zprávu. Regresní test:
   `test_dem_network_failure_message_does_not_leak_raw_exception`.
5. Chybný popisek pod grafem ("Oranžová čára = azimut západu Slunce") byl
   věcně nesprávný (ta čára je červená/zelená, oranžová je terénní
   profil) — opraveno na odkaz na legendu.

Zkontrolováno a **potvrzeno v pořádku** (žádná změna nebyla potřeba):
klik na mapu správně mění lat/lon i marker a Analyze použije kliknutou
pozici (ne zastaralou hodnotu z dropdownu); `analyze_location()` volané
přímo vs. přes `/api/analyze` dávají bit-přesně identické výsledky pro
3 lokality (otevřený vrchol, blokovaný hřeben, zahraniční bod); mimo-rozsahové
souřadnice blokuje nativní HTML5 validace (`min`/`max`) ještě před
odesláním formuláře; experimentální geometry score se nikde v UI
neobjevuje.

**Testy**: 130 → **132** (+2 regresní). Všechny procházejí (123 unit +
9 integration).

# FÁZE 6.5 — 3D Sunset Terrain Explorer

## 49. Audit před implementací

Projito: `app/main.py`, `templates/index.html`, `static/app.js`,
`static/style.css`, `analyze_location()`/terrain/solar/visibility engine,
DEM provider abstrakce, `SunsetCandidate`, testy, cache, README/PLAN.
Závěr: `/api/analyze` už vrací úplně všechno, co nová 3D mapa potřebuje
(`solar.sunset_azimuth_deg`, `location.elevation_m`, atd.) - **žádná
změna backendu nebyla nutná**. Původní 2D Leaflet mapa (Fáze 5.5) jen
čte/nastavuje lat/lon, nezasahuje do enginu - stejný princip zachován.

## 50. Výběr technologie (zdůvodněno, ne odhadnuto)

Porovnáno: **MapLibre GL JS**, CesiumJS, Three.js + vlastní mesh.

- **CesiumJS** zamítnuto: výchozí terén (Cesium World Terrain) vyžaduje
  Cesium ion účet/token - v rozporu se zadáním "žádný placený API klíč".
- **Three.js + vlastní terrain mesh** zamítnuto: Three.js je obecný 3D
  engine, ne mapa - "prohlížecí" slippy mapa (pan/zoom/rotate přes celou
  zemi) by musela být postavena od nuly. Neúměrné množství práce k
  přínosu pro tuhle fázi.
- **MapLibre GL JS** (BSD-3-Clause, `unpkg.com/maplibre-gl@4.7.1`)
  vybráno: nativní podpora `raster-dem` terénu (`map.setTerrain()`),
  hillshade s nastavitelným směrem osvětlení, žádný API klíč, standardní
  slippy-map interakce (pan/zoom/rotate/pitch) zdarma z krabice.

Datové zdroje (oba ověřeny `curl` - HTTP 200 bez klíče, žádná
autentizace):
- Základní vektorová mapa: [OpenFreeMap](https://openfreemap.org/) styl
  `liberty` (`https://tiles.openfreemap.org/styles/liberty`).
- 3D terén: veřejné "Terrarium" DEM dlaždice
  (`https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`,
  AWS Open Data / Tilezen Joerd).

Stejná poznámka jako u OSM dlaždic ve Fázi 5.5: vhodné pro lokální
vývoj/demo, ne pro produkční provoz s vysokým provozem.

## 51. Architektura - oddělení "explore" a "analyze"

3D mapa je čistě **průzkumný podklad** (rozeznat kopec od údolí,
zorientovat se) - pro samotný sunset/terrain výpočet konkrétního bodu se
terrain-dem dlaždice nikde nepoužívají. Klik na mapu pořád volá jen
`/api/analyze` → `analyze_location()` nad přesnými ČÚZK/Copernicus daty,
stejně jako ve Fázi 5. Jediná nová geometrie na frontendu je
`destinationPoint()` v `static/app.js` - čistě vizuální projekce už
hotového azimutu (z `solar.sunset_azimuth_deg`) do bodu na mapě pro
vykreslení směrové čáry, ne přepočet terrain/solar geometrie.

**Mapová interakce vs. analýza jsou striktně oddělené** (zadání bod 14):
pan/zoom/rotate/tilt nikdy nevyvolá `/api/analyze` - jen explicitní
kliknutí na bod (nebo výběr z dropdownu / tlačítko "Analyze
coordinates"). Tohle plyne přímo z MapLibre event modelu (`click` se
vyvolá jen na skutečné kliknutí, ne během gest), ověřeno Playwright
testem (viz Krok 53).

## 52. Nové UX

- Mapa je teď fixní na celý viewport, dominantní prvek (zadání bod 4/21).
- Formulář nahrazen plovoucím panelem "⚙ Location & date" (skrytý po
  načtení, po spuštění analýzy se navíc automaticky zavře - viz audit
  níže). Obsahuje dropdown testovacích lokalit, datum/timezone,
  a sbalitelné "Manuální souřadnice" pro technické zadání.
  Kliknutí do mapy zůstává hlavní cestou k výběru místa.
- Výsledek se zobrazuje jako plovoucí karta: desktop = panel vpravo,
  mobil = bottom sheet zezdola (`@media (max-width: 719.98px)`).
- **Sunset direction na mapě**: čára od zvoleného bodu ve směru
  `sunset_azimuth_deg` (zelená/červená podle obstrukce), s "sun" hintem
  vysvětlujícím, že jde o skutečnou hodnotu z výpočtu.
- **Hillshade illumination direction** terénu se po každé analýze
  přenastaví na stejný azimut (`map.setPaintProperty(...)`) -
  explicitně okomentováno a v UI popsáno jako vizualizační orientační
  pomůcka, NE fyzikálně přesná simulace slunečního svitu (zadání bod 6).
- **Discover → Analyze → Inspect** (zadání bod 12): 360° polar horizon
  graf (z Fáze 5, beze změny logiky) se přesunul do sbalitelné sekce
  "Explore horizon ▾" uvnitř výsledkové karty - není to už hlavní
  obrazovka, otevírá se až po výběru místa.
- Barevné štítky přejmenovány na kanonické stavy ze zadání (bod 13):
  **OPEN** / **DELAYED** / **OBSCURED** (dřív "Strong terrain
  obstruction") - čistě textová změna, prahy a logika beze změny.

## 53. Reálné ověření (Playwright/Chromium, ne jen strukturální kontrola)

Server spuštěn lokálně, ověřeno skutečným headless browserem:

- Mapa se načte, MapLibre canvas + navigační ovládání existují.
- Výběr testovací lokality (Krušné hory) → `#result-card` se zobrazí se
  správnými daty (sunset 19:09, badge "OBSCURED... 47.4 min", odpovídá
  dřív ověřeným hodnotám z validační fáze).
- `sunset-line` GeoJSON zdroj obsahuje přesně 1 linii, barva odpovídá
  `terrain_obstruction` (červená), koncový bod geometricky odpovídá
  azimutu 271.46° (ověřeno destinačním vzorcem - lon klesá, lat mírně
  roste, přesně jak se pro azimut těsně nad 270° očekává).
- `hillshade-illumination-direction` po analýze = přesně
  `271.46126897179994` (stejná hodnota jako `sunset_azimuth_deg` z API,
  ne hardcoded).
- Rotace/naklonění/zoom mapy (pravé tažení myší + kolečko) → **0**
  volání `/api/analyze`. Kliknutí jinam na mapu → **přesně 1** volání.
- "Explore horizon" rozklikne polar graf se zachovanou logikou z Fáze 5.
- Mobilní viewport (390×844): bottom-sheet karta se zobrazí, žádné
  JS console chyby.
- Reálný 3D reliéf (skutečné kopce/hřebeny/údolí, ne placeholder)
  vizuálně potvrzen přiblížením na Krušné hory - viditelné hřebeny,
  perspektivní zkreslení při `pitch: 60`.

### Bug nalezený a opravený při validaci (mapová/vizuální vrstva)

Při výchozím zoomu 10 (~100 km napříč) byl reálný výškový rozdíl terénu
(řádově stovky metrů) vzhledem k šířce záběru natolik malý, že 3D reliéf
i s nakloněnou kamerou vypadal skoro jako plochá 2D mapa - `pitch`/
`terrain` byly ve skutečnosti aktivní (ověřeno `map.getPitch()`/
`map.getTerrain()`), šlo čistě o vizuální nečitelnost v tomhle měřítku,
potvrzeno přiblížením (viditelný reliéf). Opraveno zvýšením výchozího
zoomu (10 → 11.5), zoomu po přeletu na vybranou lokalitu (11 → 13) a
mírným zvýšením terrain exaggeration (1.4 → 1.6), aby byl 3D charakter
patrný hned po načtení, ne až po ručním přiblížení.

Druhý nález: na mobilním viewportu zůstával plovoucí nastavovací panel
otevřený i po spuštění analýzy a spolu s bottom-sheet výsledkovou kartou
zabíral skoro celou obrazovku - mapa (hlavní prvek podle zadání) nezbyla
skoro vidět. Opraveno: panel se po spuštění analýzy automaticky zavře.

## 54. Stav po Fázi 6.5

**Implementováno**: 3D terénní mapa (MapLibre GL JS, veřejná
Terrarium/OpenFreeMap data, žádný API klíč), klik → okamžitá analýza,
sunset direction čára + hillshade orientovaný podle skutečného azimutu,
plovoucí UI (panel nastavení + výsledková karta, responsive
desktop/mobil), horizon graf přesunutý za "Explore horizon". Backend,
`analyze_location()`, DEM strategie, cache a všech 132 testů beze změny
v logice (2 čistě textové přejmenování badge stavů).

**Vědomě NEimplementováno** (zadání bod 22/18): candidate search, weather
API, routing, Google Maps, žádný nový terrain/analysis engine.

**Zbývá na další fázi**: Fáze 7 (candidate search) - viz zadání bod 23,
"STOP" po dokončení Fáze 6.5.

# FÁZE 6.6 — Sunset Visual Identity + Sunset Rating

## 55. Vizuální identita ("sunset color grading")

Beze změny backendu/enginu (zadání bod 21) - jen `static/app.js`/`.css`.

- `hillshade-highlight-color` (#ffb066, teplá zlatá) / `hillshade-shadow-color`
  (#1b1533, hluboká fialovo-modrá) / `hillshade-accent-color` na terénní
  vrstvě - statická stylová volba (barvy), NE odvozeno z dat. Jediná
  hodnota, která zůstává odvozená ze skutečných dat, je
  `hillshade-illumination-direction` = `solar.sunset_azimuth_deg` (beze
  změny z Fáze 6.5) - **analytická data (směr) vs. vizuální efekt
  (barvy) jsou v kódu explicitně odděleny komentářem** (zadání bod 3).
- Voda a pozadí základní vektorové mapy přebarveny do tmavšího
  modro-fialového tónu - hledáno podle `layer.type`/`source-layer`, ne
  podle natvrdo vypsaného ID vrstvy (robustní vůči budoucí změně stylu).
- `#sunset-atmosphere` - jemný, obrazovkově ukotvený (ne mapově otočený)
  teplý/chladný gradient přes celou obrazovku, `mix-blend-mode: soft-light`,
  nízká opacita (0.05-0.16) - čistě dekorativní aproximace "west=warm/
  east=cool", žádná fyzika. `pointer-events: none`, takže neblokuje klik
  na mapu pod sebou.
- Sky/fog atmosféra (zadání bod 4, "pokud je bezpečně podporovaný")
  VĚDOMĚ VYNECHÁNA - MapLibre 4.7.1 `Sky`/fog API nebylo v týhle session
  možné bezpečně ověřit (riziko tichého selhání/vizuální regrese bez
  jistoty podpory), takže radši žádná nefunkční přidaná vrstva než
  neověřená.

## 56. Sunset direction - vizuální vylepšení

Beze změny zdrojových dat (`solar.sunset_azimuth_deg`), jen vykreslení:
- **Čára** (beze změny z Fáze 6.5) - barva NESE analytický význam
  (červená/zelená podle `terrain_obstruction`).
- **Nový "glow" sektor** (`sunset-glow` zdroj/vrstva) - trojúhelníkový
  výsek ±10° kolem azimutu, vždy teplá barva (#ffb066, opacity 0.16) -
  čistě atmosférický, barva NENESE analytický význam (na rozdíl od čáry).
- **Nový 🌅 marker** na konci směrové čáry (`maplibregl.Marker` s vlastním
  DOM elementem) - vizuální ukazatel, ne fyzikální paprsek (viz hint
  v UI: "NE o fyzikálně přesnou simulaci...").

## 57. Sunset Rating - algoritmus

Nový modul `app/sunset_rating.py`, funkce `compute_sunset_rating()`,
dataclass `SunsetRating`/`SunsetRatingComponents`. Vědomě SAMOSTATNÁ
funkce vedle `compute_geometry_score()` (viz `app/sunset_candidate.py`),
ne jeho úprava in-place - `compute_geometry_score()` má vlastní zamčený
test kontrakt (`western_sector_open_fraction` nad fixním pásmem 240-300°,
viz `tests/test_sunset_candidate.py`) a zůstává nedotčené, aby nedošlo
k tiché změně chování pro cokoliv, co už na něj spoléhá.

4 komponenty (0-100 každá), váhy 35/25/25/15 %:
1. **horizon_at_azimuth** - `sunset_terrain_horizon_deg` lineárně
   mapované z [-3°, +25°] na [100, 0].
2. **delay** - `delay_minutes` lineárně mapované z [0, 60] min na [100, 0].
3. **sector_openness** - frakce vzorků v okně ±20° KOLEM SKUTEČNÉHO
   `sunset_azimuth_deg` (ne fixního pásma) s výškovým úhlem pod 0°
   (astronomický horizont) - AUDIT NÁLEZ: starší `compute_geometry_score()`
   mělo fixní západní pásmo 240-300°, které pro sunset azimuth mimo tohle
   rozmezí (reálně možné v zimě/létě na vyšších zeměpisných šířkách) míjí
   relevantní terén. Nová verze centruje okno na aktuální azimut, funguje
   správně pro libovolné roční období.
4. **duration** - `visibility_duration` lineárně mapované z [0, 60] min
   na [0, 100].

`overall_0_100 = round(0.35*horizon + 0.25*delay + 0.25*sector + 0.15*duration)`,
deterministické, žádná síť/náhoda. Popisky přesně podle zadání bodu 9
(Excellent/Very open/Partial/Strong/Largely obstructed).

## 58. Architektura pro budoucí weather (zadání bod 18)

`SunsetRating.geometry_score` vždy vyplněné; `weather_score`/
`visibility_score`/`accessibility_score` zatím vždy `None` - žádné
počasí API se v týhle fázi nevolá. `overall_0_100` je zatím čistě
`geometry_score` - až přibudou další skóre, stane se jejich váženou
kombinací beze změny tvaru modelu. Transparentní poznámka
(`SunsetRating.note`) je v UI vždy viditelná pod ratingem (zadání bod 17).

## 59. UI - rating card nahrazuje starý badge

Dřívější OPEN/DELAYED/OBSCURED badge (Fáze 5) odstraněn - rating label
teď nese stejnou informaci podrobněji zdůvodněnou (číslo + popisek +
progress bar, barevně podle pásma). Hero řádky změněny z "Terrain
obstruction" (čas) na "Terrain horizon" (stupně, přímý vstup ratingu) a
přidán čtvrtý řádek "Elevation" (dřív samostatný text pod nadpisem) -
podle mockupu ze zadání bodu 16. Detailní rozpad ratingu (4 komponenty)
přidán do "Terrain & calculation details" pro vysvětlitelnost (zadání
bod 8).

## 60. Reálné ověření (Playwright/Chromium)

- Krušné hory (blokovaný hřeben): rating **41** "Strong terrain
  obstruction" (červená), Milešovka (otevřený vrchol): rating **92**
  "Excellent terrain visibility" (zelená) - směr i velikost rozdílu
  odpovídá dřív ověřeným datům (delay 47.4 min vs. 5.5 min).
  `/api/analyze` odpověď obsahuje `rating` se všemi komponentami.
- `hillshade-highlight-color`/`hillshade-shadow-color` potvrzeny nastavené
  (#ffb066/#1b1533), `sunset-glow` GeoJSON zdroj obsahuje polygon, 🌅
  marker se vykresluje na konci směrové čáry.
- Terén viditelně čitelný i s barevným gradingem (hřebeny/údolí
  rozeznatelné na screenshotech), mapa nepůsobí přeplácaně.
- Rotace/naklonění/zoom stále nespouští `/api/analyze` (0 volání),
  klik stále spouští přesně 1 volání - beze změny chování z Fáze 6.5.
- Mobilní bottom-sheet (390×844) zobrazuje rating kartu čitelně,
  žádné console chyby na desktopu ani mobilu.

## 61. Stav po Fázi 6.6

**Implementováno**: sunset color grading (hillshade barvy, přebarvená
voda/pozadí, obrazovkový atmosférický gradient), vylepšená sunset
direction vizualizace (čára + glow kužel + 🌅 marker), nový deterministický
Sunset Rating (0-100, 4 vysvětlitelné komponenty, transparentní
poznámka o čistě geometrickém základu), UI hierarchie podle zadání bodu
12. Backend/engine/cache/DEM strategie beze změny logiky.

**Vědomě NEimplementováno**: weather/visibility/accessibility skóre
(pole existují, vždy `None`), candidate markers pro víc míst najednou
(UI/vrstvy jsou na to připravené, žádné skutečné vyhledávání).

**Testy**: 132 → **144** (+12 pro `app/sunset_rating.py`). Všechny
procházejí.

**Zbývá na další fázi**: Fáze 7 (candidate search) - "STOP" po
dokončení Fáze 6.6, žádná další fáze v týhle session.

# FÁZE 7 — kompletní redesign ("Najdi svůj dokonalý západ slunce")

Poznámka k číslování: zadání pro tuhle fázi bylo označené "FÁZE 7", ale
skutečný candidate search (viz zadání Fáze 6/6.6 "budoucí Fáze 7") tu
VĚDOMĚ NENÍ implementovaný - zadání to explicitně zakazuje ("NEIMPLEMENTUJ
candidate search"). Jde o redesign produktového rozhraní nad stávajícím
enginem; skutečné vyhledávání kandidátů zůstává budoucí prací.

## 62. Audit před redesignem

Backend (`app/main.py`, `app/sunset_rating.py`, `analyze_location()`,
DEM strategie, cache) beze změny logiky - `/api/analyze` už vracel
všechno potřebné (rating, terrain 360° profil, solar azimuty). Celý
redesign je `templates/index.html` + `static/app.js` + `static/style.css`.

## 63. Architektura - jeden zdroj pravdy, víc sekcí

Dřív: mapa byla fullscreen plocha, výsledek se zobrazoval v jedné
plovoucí kartě. Teď: běžná scrollovací stránka se sekcemi (Hero → Best
Sunset → Panorama → Other Sunsets → Explore the map → Technical
details) - `renderAll()` je jediné místo, které po každé analýze
aktualizuje VŠECHNY sekce najednou (hero kontext, rating ring, why-list,
statistiky, panorama, mapové vrstvy, technické detaily). Klik na mapu,
výběr z dropdownu, tlačítko "Analyze coordinates" i klik na kartu
"Other Sunsets" všechny volají stejnou `selectLocation()` →
`runAnalysis()` → `renderAll()` cestu - žádná duplicitní logika.

## 64. Best Sunset For You (zadání bod 4)

Zobrazuje reálnou analýzu AKTUÁLNĚ vybraného místa (výchozí: Milešovka),
NE automaticky vybrané "nejlepší místo ze všech" - candidate search
neexistuje. Explicitní `dev-note` pod nadpisem tohle jasně říká ("Zatím
designový koncept nad reálnou analýzou vybraného místa..."). Mockup ze
zadání obsahoval `☁ 18% clouds` a `👁 31 km visibility` - tahle pole
JSME VĚDOMĚ VYNECHALI, protože by to bylo vymyšlené počasí (zadání bod
20 - "Nevymýšlej data", "Neimplementuj fake weather"); místo toho hero
statistiky ukazují jen reálná pole (`sunset`, `terrain horizon`,
`visible sunset`, `elevation`).

**"Proč toto místo?"**: `renderWhyList()` generuje věty z už hotových
`rating.components` (práh 70/100 na `horizon_at_azimuth`/`sector_openness`/
`duration`/`delay`) + `location.elevation_m` (práh 800 m) - žádný nový
scoring algoritmus, žádný AI text, čistě deterministické věty nad
existujícími čísly. Pokud nic neprojde prahem, zobrazí se jedna poctivá
negativní věta místo prázdného seznamu.

## 65. Sunset Panorama (zadání bod 7/8) - nová komponenta

`drawPanoramaFrame()` v `static/app.js` - horizontální "skyline" pohled
na terénní horizont, postavený nad STEJNÝMI daty jako `drawPolarChart()`
(`terrain.azimuths_deg`/`horizon_altitudes_deg`) - žádný nový 3D engine,
žádné vymyšlené hory. Interaktivní: pointer events (myš i dotyk) mění
`panoramaViewCenter` a překreslují viditelné okno (FOV 100°); tlačítko
Recenter vrátí pohled na `solar.sunset_azimuth_deg`. Sky gradient a warm
glow kolem pozice slunce jsou VIZUÁLNÍ EFEKT (dekorace); silueta terénu
a poloha sunset markeru jsou ANALYTICKÁ DATA z `/api/analyze` - v kódu
explicitně okomentováno.

AUDIT NÁLEZ při implementaci: `wrap.setPointerCapture()` na
`pointerdown` potlačovalo nativní `click` vnořeného tlačítka Recenter
(capture na rodiči přebije cílení kliku na dítě) - tlačítko vypadalo
mrtvé. Opraveno: capture/drag se spouští jen když `pointerdown` cíl
NENÍ tlačítko Recenter.

## 66. Mapa - změna role (zadání bod 9)

`#map` už není `position: fixed` přes celý viewport - žije uvnitř
`#map-frame` (`height: min(70vh, 620px)`, zaoblené rohy) jako sekce
"Explore the map" v běžném dokumentovém flow. Technicky beze změny
(stejný MapLibre styl/terén/hillshade/sunset-line/sunset-glow z Fáze
6.5/6.6) - `map.resize()` navíc po `map.on("load")`, protože kontejner
teď dostává rozměr z CSS layoutu stránky, ne z fullscreen `inset:0`.

## 67. Other Sunsets (zadání bod 11/20) - ukázková data

Karty pro zbylé `TEST_LOCATIONS` (server-side pole z `app/main.py`,
beze změny) - explicitní hint pod nadpisem: "Ukázková data z testovacích
lokalit — skutečné vyhledávání okolních míst přijde až s candidate
search (další fáze), tohle nejsou automaticky nalezená 'nejlepší místa
v okolí'." Klik na "Prozkoumat" přepne aktuální analýzu na tu lokalitu
(stejná `selectLocation()` cesta).

### AUDIT NÁLEZ a oprava (výkon/souběžnost)

Původní implementace odesílala všechny karty NAJEDNOU (4 souběžné
`/api/analyze` požadavky) při KAŽDÉM načtení stránky. Terénní výpočet
(`compute_horizon_profile`) je CPU-náročný a NEKEŠUJE SE per konkrétní
bod (jen podkladová DEM dlaždice) - souběžné požadavky si navzájem
konkurovaly o CPU (Python GIL), takže běžný klik uživatele na mapu
mohl čekat frontou desítky sekund za nimi. Nalezeno skutečným Playwright
testem (ne teoreticky). Oprava, dvě vrstvy:
1. **Sekvenční fetch** - karty se donačítají jedna po druhé (`for await`
   smyčka), ne `Promise.all`/paralelně.
2. **Lazy load** - `IntersectionObserver` spustí fetch až když sekce
   "Other Sunsets" opravdu vstoupí do viewportu, PŘIPOJENÝ AŽ PO PRVNÍM
   vykreslení výsledku (dřívější verze observeru se připojovala hned při
   načtení skriptu, kdy `#best-sunset`/`#panorama-section` mají `hidden`
   atribut a nulovou výšku - stránka byla v tu chvíli uměle krátká a
   observer nahlásil "už je to ve viewportu" okamžitě, čímž lazy load
   fakticky nefungoval).

Zbytkové omezení (poctivě přiznané, ne vyřešené): pokud uživatel
scrolluje AŽ K MAPĚ (prochází přitom kolem "Other Sunsets", což lazy
load legitimně spustí) a HNED klikne na nový, dosud nenačtený bod, může
se souběžnost 2 požadavků (běžící "other sunset" fetch + uživatelův
klik) projevit jako pomalejší odezva než obvyklé "~1-20 s". Toto je
vlastnost CPU-bound výpočtu a Python GIL, ne regrese zavedená touhle
fází - řešit by to šlo jen změnou `analyze_location()`/cache
architektury, což zadání pro tuhle fázi výslovně zakazuje.

Backend NEBYL měněn kvůli tomuhle nálezu (žádný nový endpoint, žádná
změna `analyze_location()`) - řešení je čistě na frontendu.

## 68. Sunset Rating - prezentace (výpočet beze změny)

`compute_sunset_rating()` (Fáze 6.6) beze změny - potvrzeno diffem
`app/sunset_rating.py` proti stavu před touhle fází (0 řádků změněno).
Prezentace: kruhový SVG indikátor (`stroke-dasharray`/`stroke-dashoffset`,
animovaný přechod při načtení výsledku) místo dřívějšího lineárního
pruhu, barva podle stejných pásem (open/delayed/blocked) jako dřív.

## 69. Vizuální redesign - shrnutí

Typografie: eyebrow/h1/h2 hierarchie s výrazně větším spacingem
("hodně prostoru", zadání bod 15), tmavší/hlubší barevná paleta (navy/
fialová základna, jantarová/oranžová vyhrazená pro slunce/západní
horizont - ne "všechno oranžové"). `prefers-reduced-motion` respektováno
(CSS `@media` pravidlo vypíná animace/transitions, `scroll-behavior:
auto` místo `smooth`). Nastavovací panel (lokace/datum) přesunut z
plovoucího "GIS" panelu do slide-in "☰" drawer, aby nepůsobil jako
technický formulář na hlavní ploše.

## 70. Testy a validace

Backend: **144/144** beze změny (redesign nezasáhl žádný `.py` soubor
kromě žádného - čistě frontend). Žádné nové pytest testy nebyly potřeba
(zadání bod 22 - "testy jen tam, kde redesign vyžaduje nový backend
behavior" - tady žádný nebyl).

Reálná Playwright/Chromium validace (desktop 1440×1000, mobil 390×844):
hero s reálným dnešním datem/časem, Best Sunset sekce, rating ring,
why-list, panorama (drag i touch-friendly recenter tlačítko po opravě
pointer-capture bugu), Other Sunsets (sekvenční/lazy načítání i klik →
přepnutí analýzy), Explore map (rotate/zoom = 0 volání `/api/analyze`,
klik = přesně 1), settings drawer, nav "Explore" scroll. Žádné console
chyby na desktopu ani mobilu. Screenshoty prohlédnuty vizuálně - both
prošly kritickým testem "vypadá to jako produkt, co bych spustil pro
veřejnost" lépe než předchozí fáze (viz report v konverzaci).

Vedlejší zjištění během validace (session hygiena, ne aplikační bug):
opakované restarty dev serveru bez spolehlivého ukončení předchozí
instance nechaly běžet víc uvicorn procesů najednou na stejném portu,
což zkreslovalo časování při testování - po pořádném ukončení všech
starých procesů (`pkill`/`kill -9` podle PID) časování odpovídalo dřív
naměřeným hodnotám (~1s teplý cache hit, ~20s studený požadavek).

## 71. Audit skutečným použitím + opravy (post-Fáze 7)

Aplikace prošla skutečným ručním/Playwright auditem (ne jen čtením
kódu) - klikání, změna lokace/data, mapa, panorama, mobil. Nalezené a
opravené problémy:

1. **Kritický bug - změna data neměla žádný efekt.** Uživatel změnil
   datum v "☰" panelu, ale nic se nepřepočítalo, dokud nerozkliknul
   skrytou "Manuální souřadnice" a nestiskl "Analyze coordinates"
   (název tlačítka na datum vůbec neodkazuje) - reálně nepoužitelné.
   Opraveno: `change` na `#date`/`#timezone` teď vždy okamžitě
   přeanalyzuje aktuálně vybrané místo.
2. Hero neukazoval, jaké místo se analyzuje, a chybělo dominantní CTA -
   přidán `#hero-location` řádek a tlačítko "Najít nejlepší západ"
   (scrolluje na Best Sunset).
3. Statistika "-0.1° horizon" byla syrová GIS hodnota bez významu pro
   běžného uživatele - nahrazena kompasovým směrem západu ("W (271°)"),
   reálná data (`sunset_azimuth_deg`), jen srozumitelnější prezentace.
4. `dev-note` pod nadpisem lokality vypadal jako vývojářské varování
   (přerušovaný rámeček) - zjemněno na jednořádkovou poznámku s 💡.
5. Panorama u opravdu otevřeného horizontu (výchozí Milešovka) splývalo
   s pozadím oblohy a působilo jako prázdný/nenačtený obrázek - přidána
   zřetelná linka profilu terénu (i pro skoro plochý horizont).

Beze změny: backend, `analyze_location()`, `compute_sunset_rating()`,
DEM strategie. Testy beze změny (**144/144**), oprava je čistě
frontendová (`templates/index.html`, `static/app.js`, `static/style.css`).

## 72. Druhé kolo auditu - race condition, chybové stavy, barvy

Druhý důkladný průchod aplikací (Playwright, flow A-E ze zadání)
odhalil a opravil další reálné problémy:

1. **Race condition při rychlém přepínání míst.** `runAnalysis()`
   neměl žádnou ochranu proti tomu, že starší (pomalejší) požadavek
   dorazí AŽ PO novějším a přepíše už vykreslený aktuální výsledek
   zastaralými daty. Opraveno monotónním tokenem požadavku
   (`latestRequestId`) - vykreslí/zobrazí chybu jen odpověď na
   POSLEDNÍ vyslaný požadavek, starší se tiše zahodí.
2. **Chyba vedle starého výsledku.** Po neúspěšné analýze (např.
   oceán) zůstávala "Best Sunset"/panorama sekce zobrazená se starým,
   už neplatným výsledkem přímo pod chybovou hláškou - protichůdný
   stav. `showError()` teď schová i staré výsledky.
3. **Oranžová všude** (zadání bod 16 - "oranžová = sunset/důležitá
   akce, ne univerzální barva"). Section-eyebrow labely ("OTHER
   SUNSETS", "EXPLORE THE MAP") byly stejně oranžové jako "BEST SUNSET
   FOR YOU"/"SUNSET PANORAMA" - teď mají neutrální `--muted` barvu,
   oranžová zůstala jen u sekcí přímo o západu Slunce + CTA tlačítek.
4. Rating ukazoval jen holé číslo bez jednotky - přidáno "/100" vedle
   čísla (zadání bod 6 - "92 /100 EXCELLENT" styl).

Ověřeno reálným Playwright testem: FLOW A (výchozí analýza), FLOW B
(klik na mapu - funguje, ale při souběžném "Other Sunsets" fetchu může
trvat ~15 s místo obvyklé ~1 s, viz Krok 67 - stejné, dřív zdokumentované
omezení, ne nová regrese), FLOW C (změna data - okamžitě přepočítá
sunset/azimut/rating), FLOW D (změna lokace - plná aktualizace), FLOW E
(mobil - hero → best sunset → panorama → other sunsets → mapa →
settings, vše funkční). Chybový stav ověřen na reálném "oceán" bodu -
lidsky čitelná zpráva, žádný raw traceback. Testy beze změny
(**144/144**), žádná úprava backendu.

# FÁZE 8 — Premium vizuál + candidate discovery pro Beskydy

## 73. Audit současného stavu (před změnami)

Fresh screenshot desktop/mobil (Playwright). Dobré: hero hierarchie,
rating ring, why-list, panorama. Problém: pozadí prakticky černé přes
celou stránku bez vizuální variace mezi sekcemi (chybí "teplá horská
atmosféra"), sekce oddělené jen tenkou linkou (působí jako formulář),
a hlavně **mapa pořád vypadala jako GIS/silniční mapa** - čísla silnic,
desítky názvů vesnic, žádné turistické trasy/vrstevnice.

## 74. Vizuální redesign (Priorita 2)

Nová paleta (`static/style.css` `:root`): `--bg` (#10141f, ne černá),
`--surface`/`--surface-2` (odlišené plochy sekcí), `--accent-soft`.
`#hero`/`#best-sunset`/`#panorama-section` dostaly jemný teplý
radiální/lineární gradient (jen tyhle komponenty - zadání bod 3
"oranžová ne všude"); `#other-sunsets`/`#explore-map` mají chladnější
`--surface-2` bez teplé zóny. Tenké `border-top` linky nahrazeny
zaoblenými panely s vlastním paddingem/stínem - sekce teď mají jasnou
vizuální hloubku místo jedné dlouhé tmavé plochy.

## 75. Audit mapových dat a providerů (Priorita 3, zadání bod 8)

Zvažováno: ponechat MapLibre vektorový styl (OpenFreeMap `liberty`) +
3D terén (Fáze 6.5/6.6), nebo přejít na čistou 2D outdoor mapu.

**Rozhodnutí: [OpenTopoMap](https://opentopomap.org/)** (raster
dlaždice, `{a,b,c}.tile.opentopomap.org/{z}/{x}/{y}.png`) - veřejné,
zdarma, bez API klíče, licence CC-BY-SA (data OSM + SRTM). Nahrazuje
JAK vektorový styl, TAK 3D terén/hillshade (Terrarium DEM dlaždice) -
oboje zrušeno, `pitch: 0` (čistě 2D). Důvod: vrstevnice/turistické
trasy/lesy jsou v OpenTopoMap defaultně viditelné a ŽÁDNÉ komerční POI
(restaurace/obchody), přesně podle zadání bodu 7. Pro zobrazování
DESÍTEK candidate markerů najednou (Fáze 8 hlavní cíl) je čitelnější
plochá mapa než nakloněná 3D kamera - navíc odpadá závislost na druhém
externím DEM zdroji jen pro vizuál. Implementace: `static/app.js`,
inline MapLibre style objekt s jedním raster zdrojem (žádný externí
`.json` styl soubor, žádný vektorový styling).

Ověřeno `curl` před implementací: OpenTopoMap tile HTTP 200 bez
autentizace.

## 76. Audit zdrojů kandidátů (Priorita 4, zadání bod 8/10)

**Zvoleno: OpenStreetMap přes Overpass API** - veřejné, zdarma, bez
API klíče, licence ODbL, standardní zdroj přesně pro tenhle účel
(desítky tisíc aplikací). Tagy: `natural=peak`, `natural=hill`,
`tourism=viewpoint` (`app/candidate_discovery.py` `DEFAULT_TAGS`).

**AUDIT NÁLEZ (dostupnost sítě)**: hlavní veřejná instance
`overpass-api.de` vracela z tohohle vývojového prostředí trvale HTTP
406 (ověřeno `curl` i `requests`, s různými hlavičkami - jde o síťové
prostředí/edge proxy mezi sandboxem a serverem, ne o chybu dotazu).
Zrcadlo `overpass.openstreetmap.fr` fungovalo (HTTP 200, reálná data),
ale po několika dotazech během vývoje začalo vracet
HTTP 403 "This service is only available to white-listed usages".
Zrcadlo `overpass.kumi.systems` vracelo HTTP 504 (timeout). Tohle je
reálné, zdokumentované omezení TÉHLE vývojové session/sandboxu - kód
(`discover_candidates()`) je funkčně správný a otestovaný (17
jednotkových testů nad mockovanými odpověďmi + 1 `@integration` test),
ale živé volání je nespolehlivé z tohohle prostředí.

**Řešení**: `discover_candidates_cached()` - perzistentní cache syrové
Overpass odpovědi na disk (`data/overpass_cache/<region>_combined.json`).
Pro Beskydy je v repozitáři SKUTEČNÁ odpověď, kterou se podařilo stáhnout
před tím, než zrcadlo začalo blokovat (241 vrcholů + 112 vyhlídek,
staženo 2026-09-24 z `overpass.openstreetmap.fr`) - ne vymyšlená data,
jen uložená kvůli téhle dostupnostní bariéře. Při dalším spuštění na
jiném stroji/síti s funkčním Overpass přístupem se cache normálně
vytvoří napoprvé živým dotazem.

## 77. Prototyp pro Beskydy (Priorita 5/6)

`discover_candidates_cached("beskydy", (49.4, 18.3, 49.7, 18.7))` →
**353 syrových kandidátů** → `filter_within_bbox()` + `deduplicate()`
(150 m) → **323 kandidátů**. Reálná, rozpoznatelná jména (Lysá hora,
Smrk, Kněhyně, Ondřejník, Ropice...) - žádné hardcoded názvy.

## 78. Terrain analýza kandidátů - REÁLNÉ MĚŘENÍ (Priorita 6/7, zadání bod 14)

Vzorek 12 kandidátů (top podle OSM nadmořské výšky) analyzováno
SEKVENČNĚ přes `app/candidate_batch.py::analyze_candidates()` →
`scripts/build_candidate_dataset.py`. Naměřeno (2026-09-24, ne
odhadnuto):

```
[1/12]  Lysá hora        31.8s  rating=98
[2/12]  Smrk              22.8s  rating=97
[3/12]  Kněhyně           14.0s  rating=81
[4/12]  Smrk J            15.9s  rating=95
[5/12]  Malchor           16.3s  rating=97
[6/12]  Čertův mlýn       21.1s  rating=96
[7/12]  Travný            11.4s  rating=91
[8/12]  Malý Smrk         16.0s  rating=72
[9/12]  Čertův mlýn J     17.1s  rating=95
[10/12] Smrčina           17.6s  rating=66
[11/12] Malý Travný       24.1s  rating=58
[12/12] Ropice            11.7s  rating=97

Total: 219.7s pro 12 kandidátů (průměr 18.3s/kandidát)
Úspěšně analyzováno: 12/12 (žádné selhání)
```

**Rozptyl 11-32 s** potvrzuje, že kandidáti rozptýlení po celé oblasti
(ne blízko sebe) nesdílí DEM cache - každý potřebuje vlastní tile
merge. Extrapolace na základě reálného průměru (18.3 s/kandidát):
**25 kandidátů ≈ 7,5 min, 50 kandidátů ≈ 15 min** - jednoznačně mimo
rozumný timeout jednoho HTTP requestu/prohlížeče (zadání bod 14
potvrzeno naměřenými daty, ne odhadem).

**Řešení (zadání bod 14, "persistent cache/background processing")**:
terrain analýza běží OFFLINE (`scripts/build_candidate_dataset.py`,
spouští se ručně), výsledek (rating, azimut, elevation, čas) se zapíše
do `data/candidates_beskydy.json`. Webová vrstva
(`GET /api/candidates?region=beskydy`, `app/main.py`) tenhle soubor
JEN ČTE - nulová terrain zátěž na request. `analyze_location()`/
terrain engine beze změny (zadání bod 20) - candidate_batch.py je
čistá orchestrace nad existujícími funkcemi.

## 79. Ranking a Best Sunset (Priorita 7/9, zadání bod 15/17)

`rank_by_rating()` - deterministické sestupné řazení podle
`SunsetRating.overall_0_100`. Skutečný výsledek pro Beskydy
(2026-09-24): **Lysá hora vyhrává s 98/100** ("Excellent terrain
visibility"), následují Smrk/Malchor/Ropice (97), Čertův mlýn (96)...
`static/app.js::initApp()` teď načte `/api/candidates?region=beskydy`
PŘED první analýzou a jako výchozí místo použije vítěze žebříčku -
"Best Sunset For You" už NENÍ natvrdo Milešovka, je to výsledek
algoritmu (zadání bod 17, "ŽÁDNÉ fake rankingy" - splněno, `dev-note`
text se dynamicky mění na "N vyhlídek analyzováno... vítěz skutečného
žebříčku"). Pokud dataset pro region chybí, aplikace se čistě vrátí
k původnímu chování (Milešovka + testovací lokality) - žádný pád.

"Other Sunsets" sekce teď zobrazuje ZBÝVAJÍCÍ ranked kandidáty (ne
starou `TEST_LOCATIONS` placeholder sadu) - s reálným ratingem rovnou
z předpočítaného datasetu, BEZ jakéhokoliv nového síťového volání (na
rozdíl od Fáze 7, kde každá karta volala vlastní `/api/analyze`) - tohle
navíc úplně odstraňuje dřív zdokumentovaný "Other Sunsets kontence"
problém (Krok 67) pro tenhle flow.

## 80. Mapa + kandidáti (Priorita 8, zadání bod 19/20)

`renderCandidateMarkers()` - malé kruhové odznaky s číslem ratingu,
barva podle stejného pásma jako zbytek appky (open/delayed/blocked),
vítěz žebříčku vizuálně výraznější (větší, zlatý okraj). Klik na
marker → `selectLocation()` → stejná analýza jako klik na prázdnou
mapu. Žádné clustery/desítky ikon zatím netřeba (12 kandidátů).

### Bug nalezený a opravený (race condition v inicializaci mapy)

`if (map.loaded()) placeMarkers(); else map.on("load", placeMarkers);`
mělo race condition - `map.loaded()` může vrátit `false` i POTÉ, co
jednorázová událost `"load"` už proběhla (loaded() čeká i na dokreslení
dlaždic, ne jen na inicializaci stylu) - `.on("load", cb)` pak čekalo
na událost, která se znovu nikdy nespustí, a candidate markery se
nevykreslily (nedeterministicky, cca 50/50 podle časování). Nalezeno
opakovaným Playwright testem (5× stejný scénář, různý výsledek).
Opraveno vlastním flagem (`mapReady`/`whenMapReady()`) nastaveným
uvnitř JEDINÉHO existujícího `map.on("load", ...)` handleru - ověřeno
5/5 konzistentních běhů po opravě.

## 81. Testy (Priorita 24)

Nové: `tests/test_candidate_discovery.py` (14 + 1 integration),
`tests/test_candidate_batch.py` (5), `tests/test_web.py` (+3 pro
`/api/candidates`). Pokrývá: parsování Overpass odpovědi, přeskočení
way/relation/chybějících souřadnic/nerozpoznaných tagů, fallback jméno,
neplatná elevace, prázdná množina, deduplikace, bbox filtr, cache
hit/miss, chyba sítě → `CandidateDiscoveryError`, sekvenční batch
analýza, přeskočení selhaného kandidáta, deterministický ranking,
API endpoint (200/404×2). **144 → 167** (+23), všechny procházejí
(157 unit + 10 integration, včetně 1 nového pro Overpass - integration
testy se spouští jen explicitně `pytest -m integration`, ne v běžném
běhu, takže nezávisí na momentální dostupnosti Overpass mirroru).

## 82. Browser validace (Priorita 25)

Desktop (1440×1000), tablet (768×1024), mobil (390×844) - hero
s reálným vítězem (Lysá hora), rating, candidate mapa (12 markerů,
barevně odlišené, konzistentní po opravě race condition), "Other
Sunsets" s reálnými daty bez zbytečných requestů, klik na marker/kartu
→ přesně 1 `/api/analyze` volání a plná aktualizace stránky, rotate/zoom
mapy stále 0 volání. Žádné console chyby na žádném zařízení.

## 83. Stav po Fázi 8

**Implementováno**: nová "premium outdoor" paleta a layout, 2D
OpenTopoMap mapa (bez POI clutteru), `app/candidate_discovery.py` +
`app/candidate_batch.py` (discovery/filtrace/batch analýza/ranking,
plně otestováno), `GET /api/candidates`, reálný dataset pro Beskydy
(12 kandidátů, měřeno ~18s/kandidát), "Best Sunset"/"Other Sunsets"/
mapa napojené na skutečný ranking s fallbackem na Fázi 7 chování.

**Vědomě NEimplementováno** (zadání bod 22): weather, AI doporučení,
routing, celostátní pokrytí, accounts/login/platby. `analyze_location()`/
terrain/solar/DEM/cache beze změny.

**Zbývající omezení**: (1) živý Overpass přístup z tohohle prostředí je
nespolehlivý (403/406/504) - řešeno cache, ale nový region by dnes
vyžadoval spustit discovery z prostředí s funkčním přístupem; (2)
dataset je pro JEDNO datum (2026-09-24) - změna data v UI přepočítá
aktuálně vybrané místo živě, ale ranking/Best Sunset zůstává podle
data, pro které byl dataset vygenerován, dokud se skript nespustí znovu;
(3) jen Beskydy, ne celá ČR (záměrně, zadání bod 11).

**Testy**: 167/167 (157 unit + 10 integration).

**Doporučení pro další fázi**: (a) rozšířit `discover_candidates_cached()`
na víc regionů ČR postupně (stejný vzor, jiný bbox); (b) promyslet
plánované/pravidelné přegenerování datasetu (dnešní datum) - buď ruční
připomínka, nebo jednoduchý cron mimo request cyklus; (c) až bude víc
regionů, přidat výběr regionu do UI (zatím jen Beskydy natvrdo).

# FÁZE 9 — Razantní vizuální redesign (grafická identita)

Backend/engine beze změny - čistě `templates/index.html`/`static/app.js`/
`static/style.css`.

## 84. Nový grafický motiv - horská silueta

Vlastní inline SVG (`templates/index.html`, `.hero-silhouette`) - 3
vrstvy zvlněných hřebenů (klesající sytost/rostoucí opacita směrem
dopředu = pocit hloubky) + radiální "sunset glow" gradient + sluneční
kotouč napůl schovaný za předním hřebenem. Full-bleed přes celou šířku
obrazovky (`width:100vw` breakout z `main` max-width), i když obsah
zůstává v čitelné šířce. VÝSLOVNĚ dekorativní - nejde o skutečná terénní
data (ta zůstávají jen v Sunset Panorama).

## 85. Vlastní ikonový systém (zadání bod 12/18)

`.icon-*` třídy v `static/style.css` - tenké line-style SVG ikony přes
CSS `mask-image` (data URI, žádný externí request/knihovna, dědí
`currentColor`). Nahrazeno: 📍→pin, 🌅→sunset, 🧭→compass, ⏱→clock,
🏔→mountain, ☰→menu, ✕→close. Emoji zůstalo jen v drobných textových
detailech (legendy grafů, "🏆" v eyebrow labelu, karty) - zadání
explicitně dovoluje "emoji jen jako textový detail".

## 86. Méně karet - full-bleed pásy místo boxů

`#best-sunset`/`#panorama-section`/`#other-sunsets`/`#explore-map`
ztratily viditelný `border` a staly se horizontálními PÁSY přes celou
šířku obrazovky (`::before` pseudo-element s full-bleed pozadím, stejný
trik jako u siluety) - stránka je teď sled otevřených barevných zón, ne
sloupec ohraničených krabic. `.sunset-card` (grid v Other Sunsets)
zůstal jako karta - tam to dává smysl (mřížka odlišných míst).

## 87. Candidate markery s vlastním vizuálním jazykem (zadání bod 11/12)

`.candidate-marker.type-peak` (trojúhelníkový `clip-path`) vs.
`.type-viewpoint` (diamant, `rotate: 45deg` na moderní CSS individual
transform property, ne `transform` shorthand - viz audit níže) vs.
výchozí kruh. Filter chips (All/Peaks/Viewpoints/Best sunset) přepínají
`display` markerů podle `data-type`/`data-best` atributu - žádný nový
request, čistě klientská filtrace.

**Dataset přegenerován** (`scripts/build_candidate_dataset.py`) se
záměrným MIXEM typů (8 vrcholů + 4 vyhlídky), protože první verze
(čistě podle OSM nadmořské výšky) vybrala 12/12 vrcholů - vyhlídky v
OSM skoro nikdy nemají tag výšky, takže by z takového řazení vypadly
úplně. Vedlejší postřeh: tenhle běh trval jen 32.8s/12 kandidátů (dřív
219.7s) - 8 vrcholů mělo DEM dlaždice už cachované z minulého běhu,
jen 4 nové vyhlídky vyžadovaly čerstvé stažení (5-9s každá).

### Bug nalezený a opravený (CSS specificity, `transform` konflikt)

`.type-viewpoint { transform: rotate(45deg) }` společně s
`:hover { transform: scale(1.15) }` by se navzájem přepisovaly (jeden
`transform` shorthand nemůže nést obojí najednou) - opraveno použitím
nezávislých CSS vlastností `rotate`/`scale` (moderní CSS, podporováno v
testovaném Chromiu), místo jednoho společného `transform`.

## 88. Scroll-reveal animace (zadání bod 5/27)

Sekce se jemně "fade-in" při vstupu do viewportu (`IntersectionObserver`
+ `.reveal-pending`/`.is-visible` třídy). `prefers-reduced-motion`
zkracuje transition na 0.01ms globálně (beze změny JS).

### Bug nalezený a opravený (STEJNÁ třída chyby jako dřív u `[hidden]`)

Původní CSS `main > section:not(#hero) { opacity: 0 }` má specificitu
ID selektoru (`:not(#hero)` počítá specificitu svého argumentu) a
porazilo by `main > section.is-visible { opacity: 1 }` (třídní
specificita) BEZ OHLEDU na pořadí v souboru - `classList` mělo
`is-visible` správně přidané, ale `computed opacity` zůstávalo 0.
Objeveno full-page screenshotem (celá stránka pod hero byla černá).
Opraveno přechodem na čistě třídní pár selektorů (`.reveal-pending` +
`.is-visible`), žádné ID uvnitř `:not()`. Přidána i bezpečnostní pojistka
(`setTimeout` 2.5s) - i kdyby pozorovač pro nějakou sekci nikdy
nespustil, obsah se nesmí natrvalo schovat jen kvůli animaci.

**Poučení pro tenhle projekt**: tohle je TŘETÍ nezávislý výskyt stejné
třídy chyby v týhle session (`#loading[hidden]` ve Fázi 5, `map.loaded()`
race condition ve Fázi 8, teď CSS specificita `:not(#ID)`) - všechny tři
objeveny jen SKUTEČNÝM vizuálním/browser testem, ne čtením kódu.

## 89. Testy a validace

Beze změny testů (**157 unit + 10 integration**, backend nedotčen).
Ověřeno reálným browser testem: hero, Best Sunset (bez borderu, otevřený
layout), Sunset Panorama, Other Sunsets (mřížka karet s reálnými typy),
Explore Map (filtr chips funkční, markery odlišené tvarem), klik na
marker → plná aktualizace, změna data → okamžitý přepočet, chybový stav
(oceán) → lidská zpráva + schování starého výsledku, request-token guard
proti race condition. Desktop (1440×1000), tablet (768×1024), mobil
(390×844) - responsive bez horizontálního scrollu, žádné console chyby.

## 90. Stav po Fázi 9

**Implementováno**: vlastní grafická identita (horská silueta + sunset
glow), vlastní ikonový systém (ne emoji), redukce "kartovosti" (full-bleed
pásy), diferencované candidate markery + filtr chips, jemný scroll-reveal.

**Vědomě NEimplementováno**: clustering (zatím jen 12 kandidátů, netřeba),
obousměrné propojení panorama↔mapa (zadání to označilo jako nepovinné),
weather/AI/routing/accounts - beze změny z předchozích fází.

**Testy**: 167/167 (157 unit + 10 integration - 1 integration test pro
Overpass zůstává nespolehlivý kvůli síťovému omezení prostředí, viz
Fáze 8 Krok 76, nesouvisí s touhle fází).

# FÁZE 10 — Finální art direction

Backend beze změny - čistě CSS/HTML/JS.

## 91. Audit odhalil: velké nevyužité boční pásy na 1440px+

`main { max-width: 1080px }` na širokém monitoru nechávalo výrazné
ploché okraje kolem mapy/mřížky karet - vypadalo jako "container
uprostřed stránky", ne přirozená kompozice. Opraveno: `main` rozšířeno
na 1240px + nová `.bleed-wide` třída (stejný "breakout" trik jako hero
silueta, strop 1520px) aplikovaná na `#map-frame`, `.cards-grid`,
`.panorama-wrap` - vizuálně těžké prvky teď využívají mnohem víc šířky,
textové/hero prvky zůstávají v čitelném `main` max-width.

## 92. Horská silueta jako motiv napříč produktem (ne jen hero)

Nová `.scene-silhouette` (tlumenější 2vrstvá varianta hero SVG) přidána
za "Best Sunset" - výsledek teď působí jako pohled na krajinu s
výsledkem popředu, ne izolovaná datová karta. Stejný vizuální jazyk
(barvy, tvar hřebenů) jako hero - čistě dekorativní, `z-index:-1` za
textovým obsahem, žádný zásah do skutečných dat.

## 93. Interaktivita - vlastní hover tooltip na candidate markerech

Nativní `title` atribut (pomalý, nestylovaný) nahrazen `.marker-tooltip`
- název + rating se objeví okamžitě nad markerem při hoveru (zadání bod
7/9: "hover nad viewpointem: objeví se název, zobrazí se rating").
`.candidate-marker-wrap` nese hover stav odděleně od `.candidate-marker`
(který nese tvar/rotaci), aby se tooltip u diamantových markerů
nezkosil spolu s rotací.

"Best sunset" marker dostal jemnou pulzující glow animaci
(`@keyframes best-pulse`), vypnutou přes `prefers-reduced-motion`.

Hero silueta dostala jemný parallax podle pozice myši (3 vrstvy, různá
rychlost = pocit hloubky) - `mousemove`/`mouseleave` na `#hero`, úplně
vypnuto (ne jen zkráceno) při `prefers-reduced-motion`.

## 94. Ověření - Fáze 9 vs. Fáze 10 (zadání bod "porovnej před dokončením")

Fáze 9: obsah zaboxovaný do 1080px sloupce uprostřed široké obrazovky,
statická silueta jen v hero, nativní tooltip na markerech, žádný
parallax.

Fáze 10: mapa/mřížka/panorama využívají mnohem víc reálné šířky
obrazovky (viditelný rozdíl na první pohled na screenshotu), horský
motiv se opakuje i za Best Sunset výsledkem, markery mají vlastní
okamžitý hover tooltip, hero má jemnou hloubku při pohybu myší. Rozdíl
je vidět přímo na screenshotu bez potřeby srovnávat kód.

## 95. Testy a validace

Beze změny testů (**157 unit + 10 integration**, backend nedotčen).
Ověřeno reálným browser testem: date change, map click (funguje, občas
pomalejší kvůli CPU-bound DEM kontenci - už dřív zdokumentované, ne
nová regrese), error stav, request-token race guard (funguje i pod
zátěží dvou rychlých kliknutí), filtr chips, hover tooltip. Desktop
(1440×1000), tablet (768×1024), mobil (390×844) - žádné console chyby,
mapa/mřížka teď full-bleed na všech třech.

## 96. Stav po Fázi 10

**Implementováno**: širší intelligentní layout (bez mrtvých ploch),
silueta jako opakovaný motiv, vlastní hover tooltip na markerech,
pulzující "best" marker, jemný mouse parallax v hero.

**Vědomě NEimplementováno**: obousměrné propojení panorama↔mapa (bylo
nepovinné), plovoucí viewpoint detail panel místo scroll-to-section
(zachováno stávající scroll chování - funkčně ekvivalentní, nižší
riziko nové regrese v pozdní fázi session).

**Testy**: 167/167 nedotčeno.

## 97. Doplnění - plovoucí viewpoint detail (zadání bod 9, druhé kolo)

Uživatel zopakoval stejné zadání Fáze 10 beze změny textu - vzato jako
signál, že dřív přiznaný "zbývající problém" (plovoucí detail panel
místo scroll-to-section) se má dotáhnout. `.marker-tooltip` rozšířen
z "jméno + rating" na plný floating detail podle mockupu ze zadání:
typ (PEAK/VIEWPOINT), jméno, nadmořská výška (`measured_elevation_m` -
skutečná DEM hodnota, ne OSM tag), velké číslo ratingu, skutečný sunset
směr (`azimuthToCompass(sunset_azimuth_deg)` - stejná funkce jako hero
statistiky, žádný nový výpočet), a textová výzva "Explore →" (klik na
marker samotný, žádné vnořené tlačítko v malém popoveru). Zůstává čistě
hover-only preview, klik beze změny (analýza + scroll na Best Sunset).
Ověřeno reálným browser testem, 157/157 unit testů beze změny.

## 98. Fáze 11a - hero doladění + layout check (audit-first checkpoint)

Uživatel explicitně požádal PAUZU dalšího velkého vizuálního redesignu
a zadal přesný postup: lehké doladění hero -> kontrola bočních
prostorů -> audit zdrojů kandidátů -> návrh dalšího kroku -> teprve
implementace.

Hero: přidán `#hero-stat` badge (jen jednou, ne "redesign") se
SKUTEČNÝMI daty z existujícího datasetu (`candidate_count` + rating
vítěze) - "🔎 12 vyhlídek analyzováno v Beskydech · nejlepší skóre
98/100". Cestou nalezen a opraven bug: `scripts/build_candidate_dataset.py`
mělo natvrdo `datetime(2026, 9, 24, ...)` místo `datetime.now()` -
dataset byl den stará data. Opraveno, ověřeno `grep` že žádné další
hardcoded datum nikde v `app/`/`scripts/` nezůstalo.

Layout: screenshot na 1920px - `main` (1240px) + `.bleed-wide` (1520px)
mají už jen mírné okraje, fix z Fáze 10 drží. Bez další změny.

## 99. Fáze 11b - Audit zdrojů kandidátů (PŘED jakýmkoliv novým scraperem)

Živě otestováno (ne jen teoreticky):
- `overpass.openstreetmap.fr` funguje (rate limit z minula se přes noc
  resetoval); `overpass-api.de` pořád 406, `kumi.systems` timeout -
  nezměněno, sandboxové omezení.
- Rozhledny = reálný OSM tag `man_made=tower`+`tower:type=observation`
  (5 v Beskydech, některé BEZ `tourism=viewpoint` - skutečně nové
  kandidáty, ne duplicity).
- `natural=ridge` vrátil 0 výsledků pro celou ČR - hřebeny jako
  OSM prvek prakticky neexistují, musely by se odvodit z DEM (mimo
  scope, ridges vědomě NEimplementovány).
- Celá ČR (peak+hill+viewpoint+tower) = 43 062 uzlů, dotaz s
  `timeout:50` vypršel (prošlo až s `timeout:90`) - potvrzuje, že
  "jeden dotaz na celou ČR + naživo analyzovat vše" není schůdné.
  Krkonoše ověřeny jako druhý validní region (823 raw kandidátů,
  kvalita dat srovnatelná s Beskydami).

Návrh (odsouhlaseno uživatelem): rozšířit `DEFAULT_TAGS` o tower tag,
přidat Krkonoše jako druhý region STEJNOU architekturou (žádný nový
scraper), zbytek (víc regionů, ridges, spatial DB) odložit.

## 100. Fáze 11c - implementace: towers + Krkonoše + regionální architektura

`app/candidate_discovery.py`: `DEFAULT_TAGS` změněn ze seznamu
`(key,value)` párů na seznam SKUPIN párů (`_build_overpass_query`
teď generuje `node["k1"="v1"]["k2"="v2"]` AND-selektor na skupinu) -
rozhledna potřebuje DVA tagy najednou. `_infer_type()` dostal
precedenci: tower > peak/hill > viewpoint (hodně rozhleden má i
`tourism=viewpoint` zároveň - Overpass union stejně vrátí uzel jen
JEDNOU, `_infer_type` pak rozhodne o JEDNOM výsledném typu, žádná
duplicita). `_haversine_m` přejmenováno na veřejné `haversine_m`
(sdílí ho teď i `app/main.py`).

`scripts/build_candidate_dataset.py`: `REGION`/`BESKYDY_BBOX`
nahrazeno `REGIONS` dict (region -> bbox), `main()` bere regiony z
CLI argumentů (bez argumentů = všechny). Sample-výběr rozšířen o
"vezmi všechny dostupné towers (max 3), pak named viewpoints (max 4),
zbytek peaks" - towers jsou v OSM řídké, jinak by se do 12-vzorku
nikdy nedostaly.

`app/main.py`: `REGIONS` registry (`name`/`bbox`/`dataset_path` na
region, zadání bod B - připraveno pro další regiony beze změny kódu).
Nové `GET /api/geocode?q=` (Nominatim, server-side proxy kvůli
User-Agent/CORS, jen na explicitní odeslání - NE autocomplete, souhlasí
s Nominatim usage policy). Nové `GET /api/search?lat=&lon=&radius_km=`
- slučuje VŠECHNY registrované datasety, filtruje haversine
vzdáleností, řadí podle PŘEDPOČÍTANÉHO ratingu (jen pro výběr "koho
analyzovat", finální zobrazený výsledek se vždy dopočítá naživo pro
AKTUÁLNÍ datum přes existující `/api/analyze`). `MAX_SEARCH_RESULTS=200`
obranný strop (při současné velikosti dat se nikdy neuplatní - žádná
komplexní infrastruktura navíc, zadání bod T "neměř naslepo").

Vygenerováno: Beskydy 12/12 (5 peaks+4 viewpoints+3 towers, 351 raw ->
327 po dedup, 88.7s/12=7.4s prům.), Krkonoše 12/12 (5+4+3, 823 raw ->
734 po dedup, **636.4s/12 = 53s prům.** - výrazně pomalejší kvůli
velmi otevřeným vysokým vrcholům u polské hranice, kde horizon-scan
algoritmus potřebuje mnohem delší dohled - reálný, poctivě naměřený
nález pro Fázi T, ne bug). 0 chyb v analýze, obě data pro 2026-09-25.

## 101. Fáze 11d - frontend "Where? / Search within / Find my sunset"

`templates/index.html`: hero dostal `.hero-search` blok (Where text
input, Search within slider 5-100km/25 výchozí, Find my sunset CTA) -
NAHRADIL starý `#hero-cta` ("Najít nejlepší západ", jen scrolloval),
aby nevznikly dvě konkurenční CTA. Přidán "Towers" filter chip.

`static/app.js`: `drawSearchCircle()`/`circlePolygon()` - 64-bodový
geodetický kruh přes stejnou `destinationPoint()` funkci jako sunset
direction výseč (žádná nová knihovna), kreslí se na nový `search-area`
GeoJSON zdroj/vrstvu (jen `setData()` na slider `input` event - ŽÁDNÝ
network request na tah slideru, zadání bod D/E). `findMySunset()`:
geocode -> `/api/search` -> vítěz přes STÁVAJÍCÍ `selectLocation()`/
`runAnalysis()` (vždy aktuální datum) -> zbytek přes STÁVAJÍCÍ
`renderCandidateMarkers()`/`renderOtherSunsetsFromCandidates()`
(rozšířené o `distance_km` a typ "tower"). Progres jen ze SKUTEČNÝCH
sekvenčních kroků (geocode/filtr/analýza), žádné fake procento.

BUG NALEZENÝ A OPRAVENÝ (browser test): sticky `#site-header` (65px,
poloprůhledný + blur) po `scrollIntoView({block:"start"})` překrýval
horní okraj sekce - nadpis byl vizuálně rozmazaný/nečitelný pod
headerem (existovalo už před Fází 11, jen teď víc scrollIntoView
volání). Oprava: `main > section { scroll-margin-top: 80px; }`.

Menší nález: search-area kruh byl při původním `fill-opacity:0.08`/
`line-width:1.5` na mapě prakticky neviditelný - zvýšeno na 0.14/2.5px
+ výraznější dash, ověřeno screenshotem že je teď jasně vidět.

Menší nález: `setSearchError()` nechávalo starý "Filtruji vyhlídky…"
status vedle chybové hlášky současně - opraveno, `setSearchError()` s
neprázdným textem teď vždy nejdřív vyčistí status.

## 102. Fáze 11e - testy a browser ověření

Nové testy: `haversine_m` (shoda se známou referenční vzdáleností,
identické body = 0, stabilita napříč opakovanými voláními),
`deduplicate()` na přesné hranici `min_distance_m` (bod zůstává -
porovnání hodnoty se sebou samou, žádná nejistota z zaokrouhlení),
klasifikace rozhledny (`man_made=tower`+`tower:type=observation`,
včetně duálně otagovaného uzlu -> jeden kandidát, ne dva),
`/api/search` (kandidát uvnitř/mimo/přesně na hranici radiusu, řazení
podle ratingu přes víc regionů, prázdný výsledek bez chyby, chybějící
souřadnice, neplatné souřadnice, chybějící datasety, radius clamp),
`/api/geocode` (úspěch, prázdný dotaz, žádný výsledek, síťová chyba).
**176/176 unit testů** (bylo 157).

Browser test (Chromium, Playwright) - desktop 1440×1000, tablet
768×1024, mobil 390×844: celý tok Where -> radius -> Find my sunset ->
Best Sunset -> Top Results -> mapa s kruhem -> tower filtr -> klik na
marker -> panorama, i "no candidates" (Brno, 25km - čestně 0 výsledků,
zatím pokrýváme jen 2 regiony) a "no location" chybové stavy. 0
console chyb na všech třech viewportech.

Zjištěná (ne opravená, jde o test-script artefakt) flakiness:
`whenMapReady()`/`mapReady` flag (Fáze 8 fix) funguje SPRÁVNĚ a
deterministicky, pokud test na něj explicitně čeká
(`wait_for_function("() => mapReady === true")`) - fixní
`wait_for_timeout()` bez tohoto čekání občas (cold browser launch)
neposkytne dost času na "load" event před kontrolou markerů. Pro
příští testy: vždy čekat na `mapReady`, ne na fixní timeout.

## 103. Stav po Fázi 11

**Implementováno**: rozhledny jako 4. typ kandidáta (vlastní tvar na
mapě/marker tooltip/filtr chip), Krkonoše jako druhý region (stejná
architektura, 12 reálně analyzovaných kandidátů), `REGIONS` registry
připravený pro další regiony beze změny kódu, `/api/geocode`
(Nominatim), `/api/search` (multi-region haversine filtr + řazení
podle předpočítaného ratingu), hero "Where? / Search within / Find my
sunset" tok napojený na existující selectLocation/runAnalysis/
panorama/mapu beze změny enginu, vizuální search-area kruh na mapě.

**Vědomě NEimplementováno** (přiznáno, ne zamlčeno): ridges (OSM je
nemá), víc než 2 regiony (architektura připravená, ale "neplň
automaticky"), komplexní pre-filter pro velké množství kandidátů
(zatím zbytečné - max. desítky kandidátů na region), precompute/
background job infrastruktura (zatím zbytečná při současné velikosti
dat, zadání bod T).

**Testy**: 176/176 (bylo 157) + browser test na 3 viewportech, 0
console chyb.

## 104. Fáze 11f - reálná uživatelská zpětná vazba: "30 km od Valašského Meziříčí -> Smrk?"

Uživatel (skutečný znalec terénu, chodí na Smrk) reálně vyzkoušel
search a nedůvěřoval výsledku - Smrk 97/100 mu jako sunset spot
nepřišel dobrý, a chyběly mu konkrétní známé rozhledny (Vartovna,
Helfštýn, Maruška, Kunovice). Vyšetřeno, ne jen opraveno naslepo:

1. **Skutečná diera v pokrytí**: Valašské Meziříčí (17.9714°E) leží
   ZÁPADNĚ od Beskydské bbox (west=18.3°E) - naprostá většina 30km
   okruhu neměla ŽÁDNÉ kandidáty v databázi, Smrk vyhrál prostě proto,
   že byl skoro jediná dostupná možnost, ne protože reálně přesvědčivě
   vyhrál nad konkurencí. Overpass audit potvrdil `Vartovna` jako
   reálný `natural=peak` (651m, zdroj `cuzk`) přesně tam, kde ji
   uživatel čekal - jen mimo pokrytou oblast.
2. **Nový region "vsetinsko"** (49.15-49.6°N, 17.5-18.3°E, stejná
   architektura/pipeline, žádný nový scraper) - 1013 raw kandidátů,
   924 po dedup (771 peaks, 132 viewpoints, 12 towers, 9 hills).
   Vzorek 12 analyzován 12/12, **vítěz Radhošť 97/100**. Zajímavý nález:
   avg 37s/kandidát (podobně pomalé jako Krkonoše - hraniční/velmi
   otevřené vrcholy, ne bug).
3. **Vartovna konkrétně doanalyzována** - byla v syrových datech, ale
   NEPROŠLA do malého 12-vzorku (sample bere primárně nejvyšší vrcholy;
   Vartovna 651m prohrává s Radhoštěm 1129m). Protože šlo o SKUTEČNÁ,
   už objevená data (ne fabrikace), doanalyzována zvlášť stejným
   enginem (`analyze_candidates()`) a zapojena do datasetu -
   **86/100, "Very open horizon"**, reálný výsledek, ne fake.
   `Maruška` v OSM není `man_made=tower`+`tower:type=observation` (jen
   `monitoring_station` - meteostanice), `Helfštýn` je hrad (jiná
   kategorie kandidátů, mimo zadání), `Kunovice` v okolí VM nenalezeno
   (existuje stejnojmenná obec u Uh. Hradiště, ~70 km jinam) - přiznáno
   uživateli, nevymyšleno.
4. **BUG NALEZEN A OPRAVEN**: `/api/search` řadil shodu ratingu podle
   pořadí registrace regionu (`REGIONS` dict), ne podle vzdálenosti -
   Smrk (97, 29km) vyhrával nad Radhoštěm (97, 18km) jen protože
   Beskydy jsou v dictu dřív. Oprava: `sort(key=(-rating, distance_km))`
   - při shodě vyhrává BLIŽŠÍ kandidát. Nový test
   `test_search_ties_on_rating_broken_by_distance_not_region_order`.
5. **Přiznaná, dřív nezmíněná limitace terénního modelu**: ČÚZK DMR 5G
   je DTM (holý terén), NE DSM - stromy/lesní porost NEJSOU v analýze
   zahrnuté (bylo už zmíněno v README, ale ne v UI). Smrk je zalesněný
   vrchol - "otevřený horizont" v analýze může neodpovídat realitě kvůli
   stromům, které DTM nevidí. Přidán čestný disclaimer do "Proč právě
   toto místo?" bloku (`templates/index.html`, `.hint` pod `#why-list`).

Search z Valašského Meziříčí (30 km) teď vrací 16 kandidátů (bylo
prakticky jen Beskydy okraj), vítěz Radhošť (blíž, stejný rating jako
Smrk). **177/177 unit testů** (bylo 176).

**BUG NALEZEN A OPRAVEN #2** (reálný browser test PO opravě #4): hero/
Best Sunset nadpis pro vítěze z `/api/search` mimo Beskydy (např.
Radhošť z Vsetínska) se zobrazil jako holé "49.4916, 18.2231" místo
jména - `currentLocationLabel()` znala jména jen ze statického
výchozího Beskydy datasetu. Oprava: sdílená `knownLocationNames` mapa
naplňovaná KAŽDÝM zdrojem kandidátů (`initApp()` i `findMySunset()`),
ne jen jedním konkrétním datasetem. Ověřeno: "Radhošť" se teď zobrazuje
správně.

## 105. Milestone "Real Candidate Catalog + Spatial Search" (Fáze 12)

Audit před implementací potvrdil: žádné `if region == "X"` větvení v
kódu (dobré), ale `REGIONS` bbox/name bylo duplikováno ve DVOU
souborech (app/main.py i scripts/build_candidate_dataset.py), a
`/api/search` uměl hledat jen v malé (12-13/region) RUČNĚ analyzované
podmnožině - syrová Overpass data pro ~2000 kandidátů už na disku
ležela (`data/overpass_cache/*_combined.json`), jen nebyla nikdy
sloučená do dotazovatelného celku. To byla přesně ta mezera, kterou
tenhle milestone řeší.

**Nové soubory**: `app/regions.py` (jediný zdroj pravdy pro
bbox/name - main.py i build_candidate_dataset.py teď importují
odsud), `app/candidate_catalog.py` (`build_catalog()` +
`load_candidate_catalog()` s in-memory cache),
`scripts/build_candidate_catalog.py` (offline sloučení + GLOBÁLNÍ
deduplikace přes všechny regiony najednou - běží < 1s, protože jen
čte už stažená cache data, žádná síť).

**Centrální katalog** (`data/candidate_catalog.json`): **1985
kandidátů** po globální deduplikaci (327 Beskydy + 734 Krkonoše + 924
Vsetínsko, 0 hraničních duplicit - bbox hrany se sice dotýkají na
18.3°E, ale žádný konkrétní bod na ní přesně neleží). Podle typu:
1528 peaks, 428 viewpoints, 20 towers, 9 hills.

**Nový live pipeline** (`POST /api/find_sunset` + `GET
/api/find_sunset/progress`) - NAHRAZUJE (ne odstraňuje - `/api/search`
zůstal beze změny, vlastní testy dál platí) statické
"precomputed-only" hledání skutečnou terrain analýzou NAD CELÝM
katalogem za requestu:
1. `POST /api/find_sunset` (rychlé, synchronní): haversine filtr přes
   VŠECH ~2000 kandidátů -> `found_count` (skutečné číslo, ne
   omezené na malý vzorek), levný prefilter (řadí čistě podle
   vzdálenosti - ŽÁDNÁ preference typu, zadání bod J), strop
   `MAX_TERRAIN_ANALYSIS_CANDIDATES=15`, spustí terrain analýzu na
   pozadí (`threading.Thread`), vrátí `job_id`.
2. Vlákno volá STÁVAJÍCÍ `analyze_candidates()` jeden kandidát po
   druhém (ne v batch) - `job.processed_count`/`succeeded_count` jde
   číst přes `GET /api/find_sunset/progress?job_id=` PRŮBĚŽNĚ, dokud
   analýza běží - žádné fake procento, jen skutečný stav.
3. `SEARCH_TIME_BUDGET_S=60.0` - wall-clock pojistka: pokud strop
   kandidátů nestihne doběhnout v čase, job se čestně ukončí se
   `stopped_early: true` a vrátí, co se stihlo (viz reálný benchmark
   níže - tohle se v praxi u studených oblastí spouští VELMI často).

**Frontend** (`static/app.js` `findMySunset()`): přepsáno na
geocode -> `POST /api/find_sunset` -> `pollFindSunsetJob()` (pollování
po 900 ms, text vždy "Analyzuji terén… X / Y" se SKUTEČNÝM číslem) ->
render. Vítěz se pak (stejně jako ve Fázi 11) ještě jednou dopočítá
přes `selectLocation()`/`/api/analyze` pro plný detail (panorama/polar
chart) - `find_sunset` job vrací jen odlehčené shrnutí, ne celou
`LocationAnalysis`. BUG NALEZENÝ PŘED NASAZENÍM: nové API vracelo pole
`elevation_m`, ale `renderCandidateMarkers`/`renderOtherSunsetsFromCandidates`
čekají `measured_elevation_m` (konvence ze starého per-region
datasetu) - byl by to `undefined.toFixed()` při hoveru na marker.
Opraveno před browser testem, ne po něm.

**Reálný benchmark** (ne odhad): "found vs. analyzed" teď dává
smysluplně ROZDÍLNÁ čísla - hledání 15 km kolem Lysé hory našlo
**268** kandidátů v katalogu, ale při 60s time-budgetu se stihlo
naživo analyzovat jen **8 z 15** prefiltrovaných (5 rychle, ~1-5s
každý - blízko už dřív dotčeným DEM dlaždicím -, pak 3 po ~25s každý -
nové, "studené" body). Hledání 30 km kolem Valašského Meziříčí (nová
oblast) našlo **809** kandidátů, ale za 60s se stihly jen **2**. Tohle
potvrzuje už dřív zdokumentovaný bottleneck (ČÚZK ArcGIS ImageServer
cold-fetch cena, 10-50s/kandidát) - NENÍ to nová regrese a NENÍ v
scope týhle fáze ho opravovat (zadání "neoptimalizuj bezhlavě celý
engine"). `MAX_TERRAIN_ANALYSIS_CANDIDATES`/`SEARCH_TIME_BUDGET_S`
jsou obě konfigurovatelné konstanty v `app/main.py`.

**Testy**: `tests/test_candidate_catalog.py` (7 - merge napříč
regiony, globální dedup na hranici, filtr mimo ČR, `region` tag,
load/cache/invalidate), `tests/test_web.py` (+12 pro
`/api/find_sunset`+`/progress` - found vs. to_analyze, prázdný
katalog, neplatné souřadnice, prefilter strop, prefilter řadí podle
vzdálenosti NE typu, úspěšný pipeline + tvar výsledku, řazení rating
DESC/distance ASC/name ASC, deterministické opakování, přeskočení
selhavšího kandidáta bez pádu celého jobu, neznámé job_id, job
error stav). **196/196 unit testů** (bylo 177).

**Browser test**: desktop 1440×1000 (15 km od Lysé hory) - živě
sledován SKUTEČNÝ postup "Analyzuji terén… 0/15" až "7/15" v reálném
čase (5-79s), honest "časový limit vypršel" hláška, 0 console chyb,
Best Sunset/panorama/mapa fungují. Mobil 390×844 - žádný horizontální
přetok, prázdné pole "Where" hlásí chybu, slider funguje, progress se
živě aktualizuje, 0 console chyb. Vedlejší nález (ne bug): Nominatim
pro holé "Radhošť" vrátil JINÉ stejnojmenné místo u Ústí nad Orlicí
místo hory v Beskydech - geocoding disambiguation je mimo scope týhle
fáze, zaznamenáno jako známé omezení.

**Vědomě NEimplementováno**: SQLite (při ~2000 kandidátech
zbytečné, JSON + lineární sken stačí, viz benchmark), cleanup starých
dokončených jobů v `_SEARCH_JOBS` (roste bez mezí při dlouhodobém
běhu procesu - v pořádku pro prototyp, řešit až při reálném nasazení),
weather/AI/účty/redesign (explicitně mimo zadání).

## 106. Milestone "Persistent Terrain Cache + Background Precomputation" (Fáze 13)

Audit potvrdil čistou hranici uvnitř `analyze_location()`: DEM
stažení + 360° horizon scan + sklon/expozice ZÁVISÍ JEN na (lat, lon,
analysis_range_m, azimuth_step_deg, horizon_step_m,
observer_eye_height_m, pixel_size_m, refraction_coefficient) - NIKDY
na `when`. Sluneční pozice/viditelnost/rating závisí na `when`, ale NE
na DEM. Přesně tahle hranice = co cachovat vs. co vždy přepočítat.

**Refaktor `app/analyze_location.py`** (ŽÁDNÁ změna algoritmu, jen
rozdělení existujícího kódu): nový `StaticTerrain` dataclass +
`compute_static_terrain()` (drahá část) + `compute_dynamic_sunset()`
(levná část) + `analyze_location()` teď jen skládá obě dohromady.
Ověřeno: **18/18 `test_analyze_location.py` testů beze změny**, žádný
test se nemusel upravovat - potvrzuje "SAME INPUT + SAME ENGINE =
SAME RESULT".

**Nový `app/terrain_cache.py`** - SQLite (`data/terrain_cache.sqlite3`,
WAL mode), `PRIMARY KEY (candidate_id, cache_key)`. Cache klíč
(`compute_cache_key()`) = sha256 přes VŠECHNY parametry
`compute_static_terrain()` + `TERRAIN_ENGINE_VERSION="1"` +
`TERRAIN_CACHE_SCHEMA_VERSION=1` (obojí RUČNĚ zvyšovat při změně
algoritmu - ověřeno testem, že bump kteréhokoliv mění klíč a stará
cache se stane neviditelnou, ne smazanou - nový klíč prostě nenajde
starý řádek). DEM provider/dataset NENÍ součástí klíče (je to VÝSLEDEK
`resolve_dem()`, ne vstupní parametr) - ukládá se do záznamu jen pro
transparentnost. Status: `missing` (žádný řádek) / `running` / `ready`
/ `failed`. `reset_stale_running()` běží při `init_db()` (= při startu
FastAPI): žádný `running` záznam nemůže reálně dobíhat na čerstvě
nastartovaném procesu - jinak by po pádu procesu zůstal navěky
"trčet" (zadání bod 17).

**Nový `app/terrain_worker.py`** - `TERRAIN_WORKER_CONCURRENCY=1`
(bezpečná výchozí hodnota, "nezahltit ČÚZK/Overpass"), jedna
`queue.Queue` + N vláken (démoni), in-memory `_enqueued` set pro
dedup (opakovaný `enqueue()` pro stejný (candidate_id, cache_key) je
no-op, dokud se nezpracuje). Pokračuje po jednotlivém selhání (`try/except`
kolem `_process()`, samo vlákno se nikdy neukončí).

**`/api/find_sunset` PŘEPSÁN** (z předchozího milestone) - z "naživo
analyzuj s time budgetem" na "cache-aware, VŽDY rychlé": haversine
filtr přes CELÝ katalog -> prefilter (nejbližší první) -> per-kandidát
cache lookup -> `ready`: dopočítej jen levnou dynamickou část (žádný
DEM), vrať; `missing`/`failed`: zařaď na pozadí a NEČEKEJ. Endpoint už
nikdy neblokuje na DEM/horizon výpočtu. `/api/find_sunset/progress`
(z minula) smazán jako mrtvý kód - nahrazen tím, že samotný
`/api/find_sunset` je teď synchronní a rychlý.

**Reálný benchmark** (ne odhad):
- **cold vs. cache hit, genuině netknutý bod** (Sněžka, Krkonoše):
  **68,04 s -> 0,0010 s = 69 355× rychlejší.**
- **cold vs. cache hit, dřív jen jednou dotčený bod** (Lysá hora):
  **0,75 s -> 0,0010 s = 779× rychlejší** (menší rozdíl, protože DEM
  dlaždice v okolí Beskydského jádra byly už z dřívějška částečně
  zahřáté i bez cache).
- **search benchmark**: search 10 km od Lysé hory (částečně
  precomputed oblast) - **found=110, ready=13, enqueued=2, celý
  request 3,19s** (dřív by cold search stejné velikosti trval
  minuty). Search 15 km od Vsetína (zatím netknutá oblast) -
  **found=301, ready=0, enqueued=15** - request se vrátí OKAMŽITĚ
  (žádné čekání na DEM), 15 kandidátů se počítá na pozadí, UI čestně
  řekne "teprve se připravuje", ne fake výsledek.

**Persistence přes restart** (zadání bod 17, skutečně ověřeno):
uvitorn proces zabit a znovu nastartován, cache (33 ready záznamů)
zůstala na disku, živé hledání okamžitě našlo "Lysá hora" jako ready
bez přepočtu.

**Frontend** (`static/app.js` `findMySunset()`): `/api/find_sunset`
je teď JEDNO synchronní volání (žádný poll na dlouhotrvající job).
Pokud `ready_count === 0`, ale něco se počítá na pozadí, až
**3 lehká automatická opakování** po 4s ("pokus 1/3", "2/3", "3/3" -
skutečná čísla, ne fake progress) - pak čestná zpráva "teprve se
počítá, zkus to za chvíli". Hero badge/note teď rozlišují "nalezeno"
vs. "má hotový terén" vs. "připravuje se na pozadí" jako TŘI různá
čísla.

**Nový `scripts/precompute_terrain.py`** - `--limit`, `--region`,
`--candidate-type`, `--force`, deterministická priorita (region, pak
elevace sestupně - ne subjektivní typová preference). Ověřeno: druhé
spuštění se stejnými parametry správně přeskočí už `ready` kandidáty
("Přeskočeno: N"), `--force` je přepočítá znovu.

**Testy**: `tests/test_terrain_cache.py` (21 - cache key determinism/
změny/schema bump, missing/running/ready/failed přechody, roundtrip
zachovává hodnoty vč. `HorizonProfile` interpolace, nezávislost
různých klíčů, persistence, count-by-status, reset stale running),
`tests/test_terrain_worker.py` (7 - enqueue/dedup, concurrency limit
skutečně změřen vlákny, continue-after-failure, is_pending), `tests/test_web.py`
find_sunset sekce PŘEPSÁNA (14 testů - cache hit/miss/running/failed,
duplicate enqueue přes dvě volání, dynamic-computation failure
izolovaná, řazení, determinismus). **226/226 unit testů** (bylo 224
před refaktorem, +21+7 nové -8 nahrazené staré + net drobné úpravy).

**Browser test**: 3 scénáře (A: převážně cached - 3,19s, B: zcela
uncached - honest "15 kandidátů se připravuje" po 3 pokusech, C:
mixed - stejné jako A) na desktopu i mobilu (390px), žádný horizontální
přetok, marker klik -> panorama funguje, **0 console chyb**.

**Vědomě NEimplementováno**: cleanup starých `ready`/`failed` záznamů
při dlouhodobém provozu (SQLite roste, ale při ~2000 kandidátech x
pár KB/záznam je to řádově MB, ne problém teď), weather layer
(architektura na něj podle zadání bodu 22 připravená - `StaticTerrain`
cache je nezávislá na datu, takže weather/preference vrstva půjde
přidat jako DALŠÍ krok v pipeline, ne přepis).

## 107. Milestone "Complete Product Build" (Fáze 14)

**Weather**: `app/weather_provider.py` (Open-Meteo, živě ověřeno bez
klíče/účtu - cloud_cover/low/mid/high, visibility, precipitation,
temp, wind, humidity, pressure, weather_code -> label), `app/weather_cache.py`
(SAMOSTATNÁ SQLite `data/weather_cache.sqlite3`, TTL 3h, souřadnice
zaokrouhlené na ~1,1 km, hodina zaokrouhlená - sdílení mezi blízkými
kandidáty). Weather fetch selhání se NIKDY nepromítne jako pád
requestu - `WeatherUnavailableError` se zachytí, `weather=None`.

**Scoring engine rozdělen přesně podle zadání bodu 30**:
`terrain_score.py` (jen obal nad NEZMĚNĚNÝM `compute_sunset_rating()`),
`weather_score.py` (+ `compute_atmosphere_score()` - "zvonová" křivka
pro mid/high oblačnost, ne lineární "víc mraků = hůř"),
`visibility_score.py`, `sun_position_score.py` (reálná fyzika -
rychlost klesání Slunce k horizontu z existujícího `app.solar`, ŽÁDNÝ
nový model), `photography_score.py` (transparentní kompozit, ne nová
veličina), `sunset_score.py` (agregátor s vahami ze zadání: Weather 30 %,
Terrain 25 %, Visibility 15 %, Atmosphere 10 %, Sun position 10 %,
Photography 10 % - `assert sum==1.0`), `personalization.py`.

**Missing-data handling** (zadání bod 32): chybějící komponenta (typicky
weather) se PŘEROZDĚLÍ mezi dostupné, ne fake default - ověřeno testem
`test_no_weather_data_reweights_instead_of_faking`.

**Sunset Types**: `classify_sunset_type()` v `sunset_score.py`,
prioritní pravidla nad SKUTEČNÝMI daty (mountain/cloudscape/dramatic/
clear/photography/atmospheric/mixed), bez počasí `key=None` (badge se
v UI schová, nikdy "unknown" zobrazené jako typ). Formulace VŽDY
"Conditions favor..." (zadání bod 12/28 - nikdy jistota).

**Personalization**: `UserPreferences`/`compute_user_match()` - POCTIVĚ
přiznáno, že Water/City/Forest nemají dnes datový signál (v UI
nabízeny, ale checkbox je `disabled` + tooltip vysvětlení, žádné tiché
nic-nedělání). Hiking effort je ODHAD z distance_km (žádné trail/routing
data) - vždy popsáno jako odhad. `user_match_pct` je SAMOSTATNÉ číslo
vedle `sunset_score` (zadání bod 15 - nikdy netajně neovlivňuje fyzikální
skóre) - ovlivňuje jen ŘAZENÍ/výběr "Best Sunset FOR YOU" (`0.5*score+0.5*match`).

**API integrace**: `/api/analyze` i `/api/find_sunset` sdílí
`_compute_full_score()` (zadání bod 43 - nepřidávej endpoint jen aby
byl). `rating`/`rating_label` zůstávají BEZE ZMĚNY (zpětná kompatibilita,
terrain-only, technický detail) - nové `sunset_score`/`sunset_type`/
`weather`/`user_match`/`hike_access_estimate` jsou PŘIDANÁ pole, nic
nepřepisují. Živě ověřeno: `/api/find_sunset` s 15 ready kandidáty a
reálným počasím pro každého (kandidáti do ~1,1 km sdílí weather cache
záznam) = **1,6 s celkem**, ne znatelně pomalejší než bez počasí.

**Frontend** (beze změny existujícího vizuálního jazyka - jen nové
prvky do stávajících komponent): hero dostal "What are you looking
for? (optional)" collapsible s preferencemi (scene/style/hiking),
Best Sunset kruh teď ukazuje `sunset_score` (ne jen terrain), přidán
sunset type badge, "Match for you: X%" badge, 2 nové stat řádky
(clouds/hike - nové `.icon-cloud`/`.icon-hike` mask ikony stejným
systémem jako existující), rozšířený Why-list (terrain + weather +
personalization důvody), collapsible Score breakdown (6 komponent +
váhy). Other Sunsets karty a mapové markery/tooltips teď taky ukazují
`sunset_score`/typ badge/match % (s fallbackem na starý `rating` pro
statický precomputed dataset, který nové pole nemá).

**Testy**: `tests/test_scoring.py` (25 - přesně scénáře ze zadání:
perfect terrain+good weather, blocked horizon+perfect weather,
excellent terrain+heavy rain, low clouds+excellent visibility, high
clouds+clear horizon, no weather data, deterministický/reprodukovatelný,
sun position rychlost klesání, photography reweighting, sunset type
klasifikace včetně "nikdy jistota", personalization - hiking/easy
access/photographer/dramatic/unsupported scene/mountain), `tests/test_weather.py`
(9 - provider s mockovanou sítí, cache roundtrip/TTL/sdílení blízkých
souřadnic). `tests/test_web.py` rozšířen o 6 testů ověřujících API
integraci (sunset_score v odpovědi, user_match, weather v
find_sunset výsledcích, graceful degradace bez počasí, reorder podle
preferencí) + NOVÁ autouse fixture `_no_real_weather_network_calls`
(objeven a opraven reálný problém - existující find_sunset testy bez
mockování weather dělaly SKUTEČNÉ síťové volání na Open-Meteo, fungovalo
jen díky graceful degradaci, ne správně izolované). **266/266 unit
testů** (bylo 260).

**Browser test**: 4 scénáře ze zadání (Lysá hora 15km, Valašské Meziříčí
25km, Photography+Mountain+Hiking preference, Easy access+Clear
preference) na desktopu i mobilu (390px) - RŮZNÉ preference dávají
RŮZNÉ "Best Sunset" (Kykulka pro mountain+photo+hike vs. Zimný pro
easy+clear) = preference SKUTEČNĚ fungují, ne dekorativní checkboxy.
**0 console chyb**, žádný horizontální přetok.

**Vědomě NEimplementováno** (přiznáno, ne zamlčeno): `/tonight`
`/explore` `/sunset/{slug}` routing/multi-page struktura (zadání bod
53) - zůstalo na jedné stránce s rozšířenými sekcemi, ne kvůli lenosti,
ale reálnému odhadu rizika/času u už tak obřího scope týhle session;
"Best days this week" (zadání bod 25 - vyžadovalo by iteraci weather
fetch přes 7 dní × N kandidátů, zatím neimplementováno); plný
cinematic vizuální redesign (zadání bod 35-38) - existující vizuální
jazyk je zachovaný a rozšířený o nové prvky, ne přepsaný od nuly,
konzistentně s opakovaným "NEDĚLEJ velký redesign" napříč celou touhle
session; Water/City/Forest personalization (žádná reálná data).
