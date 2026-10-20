// Sunset Radar - Fáze 7 frontend ("Najdi svůj dokonalý západ slunce").
// ČISTĚ prezentační vrstva: veškerá terén/solar/visibility/rating data
// pochází z /api/analyze (= serializovaný výsledek analyze_location() +
// compute_sunset_rating()). Frontend tady NIC znovu nepočítá - jediné
// výjimky jsou čistě geometrické promítnutí známého azimutu do bodu na
// mapě/panoramatu (destinationPoint) a generování "Proč toto místo?"
// vět z už hotových rating komponent (buildWhyList) - obojí je
// prezentace, ne přepočet terrain/solar/rating geometrie.

const loadingEl = document.getElementById("loading");
const errorEl = document.getElementById("error");

const latInput = document.getElementById("lat");
const lonInput = document.getElementById("lon");
const dateInput = document.getElementById("date");
const tzInput = document.getElementById("timezone");
const testLocationSelect = document.getElementById("test-location");
const manualAnalyzeBtn = document.getElementById("manual-analyze-btn");

// --- Fáze 11 ("Where? / Search within / Find my sunset") ---
const searchWhereInput = document.getElementById("search-where");
const searchRadiusInput = document.getElementById("search-radius");
const searchRadiusValueEl = document.getElementById("search-radius-value");
const findSunsetBtn = document.getElementById("find-sunset-btn");
const searchStatusEl = document.getElementById("search-status");
const searchErrorEl = document.getElementById("search-error");

// --- Settings drawer ("☰") ---
const panelToggle = document.getElementById("panel-toggle");
const panelClose = document.getElementById("panel-close");
const controlPanel = document.getElementById("control-panel");
panelToggle.addEventListener("click", () => { controlPanel.hidden = false; });
panelClose.addEventListener("click", () => { controlPanel.hidden = true; });

let currentLat = parseFloat(latInput.value);
let currentLon = parseFloat(lonInput.value);
let currentTz = tzInput.value;

// --- Mapa (Fáze 6.5/6.6/7) - MapLibre GL JS, žádný API klíč.
//
// Fáze 7 mění ROLI mapy (zadání bod 9): dřív byla mapa fullscreen hlavní
// plocha, teď je to jedna sekce stránky ("Explore the map") - technicky
// beze změny (stejný styl/terén/vrstvy), jen `#map` už není `position:
// fixed` přes celý viewport, ale žije uvnitř `#map-frame` (viz CSS).
//
// Milestone "Sunset-first UI and Minimalist Map Redesign" - mapa je
// teď VÝSLOVNĚ sekundární orientační nástroj ("kde to je a co je
// poblíž"), ne hlavní cesta k objevení produktu - viz zmenšený
// `#map-frame` níže a hierarchie sekcí v templates/index.html (hlavní
// doporučení -> další místa -> mapa). Cílový vizuál se proto obrátil:
// dřív (Fáze 8) jsme chtěli HODNĚ detailu (turistické trasy,
// vrstevnice, lesy) a kvůli tomu přešli z vektorového OpenFreeMap
// "liberty" na barevný rastrový OpenTopoMap. Teď chceme PŘESNÝ OPAK -
// "monochromatický, čistý, minimální vizuální šum, žádné dominantní
// barevné turistické dlaždice" (zadání bod 2) - OpenTopoMap jde touhle
// cestou zhoršit jen agresivním CSS filtrem na rastru (zadání
// explicitně varuje, že rastr "nelze přebarvit do kvalitního vlastního
// stylu"), ne skutečně vyčistit.
//
// Řešení: OpenFreeMap "dark" (https://tiles.openfreemap.org/styles/dark)
// - kompletní, hotový MapLibre vektorový styl nad stejnými OpenStreetMap
// daty, ověřeno živě (`curl`) před nasazením: žádný API klíč, žádný
// účet, žádný limit požadavků (viz openfreemap.org/quick_start - "no
// registration, no API keys"), a sám o sobě je to přesně ta "tmavě
// šedá/neutrální" paleta se zdrženlivým rozlišením vody/lesa/zástavby,
// co zadání chce - žádné vlastní přebarvování vrstev. Vrstevnice/reliéf
// (`ne2_shaded` hillshade zdroj) tenhle konkrétní styl nevyužívá (jen
// ho má definovaný, beze čtení) - OpenMapTiles schéma obecně
// podrobnější terénní stínování pro ČR nenabízí, takže se transparentně
// NEDOPLŇUJE (zadání bod 2 - "pokud je lze získat" - tady to není
// bezpečně dostupné bez vlastní dlaždicové infrastruktury).
// Atribuce: `openmaptiles`/`planet` zdroj má v TileJSON vlastní pole
// `attribution` (OpenFreeMap + OpenMapTiles + OpenStreetMap), MapLibre
// ho automaticky převezme do `attributionControl` - nic se nemusí psát
// ručně (ověřeno v `openmaptiles` zdroji stylu).
const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/dark",
  center: [currentLon, currentLat],
  zoom: 11.5,
  pitch: 0,
  bearing: 0,
  attributionControl: true,
});
map.addControl(new maplibregl.NavigationControl(), "top-right");

// Milestone "Sunset-first UI and Minimalist Map Redesign" - mapa je
// VÝCHOZÍ kompaktní (viz #map-frame v style.css), rozšiřitelná na
// vyžádání ("Upřednostni menší mapový panel... kterou lze rozšířit",
// zadání bod 1C). `map.resize()` po přepnutí třídy je nutný - MapLibre
// si velikost canvasu nepřepočítá samo při změně CSS rozměru kontejneru.
const mapFrameEl = document.getElementById("map-frame");
const mapExpandBtn = document.getElementById("map-expand-btn");
const mapExpandBtnLabel = document.getElementById("map-expand-btn-label");
mapExpandBtn.addEventListener("click", () => {
  const expanding = !mapFrameEl.classList.contains("is-expanded");
  mapFrameEl.classList.toggle("is-expanded", expanding);
  mapExpandBtnLabel.textContent = expanding ? "← Zpět na doporučení" : "Rozšířit mapu";
  requestAnimationFrame(() => map.resize());
});

let marker = null;
let sunMarker = null;
let candidateMarkers = [];

function emptyFeatureCollection() {
  return { type: "FeatureCollection", features: [] };
}

// AUDIT NÁLEZ (Fáze 8, zjištěno skutečným browser testem - občas 12
// candidate markerů, občas 0, nedeterministicky): `map.loaded()` hlásí
// false ještě chvíli PO tom, co jednorázová událost "load" už proběhla
// (loaded() čeká i na dokreslení dlaždic, ne jen na inicializaci stylu).
// Kód `if (map.loaded()) cb(); else map.on("load", cb);` proto měl
// race condition - když "load" mezitím už proběhlo, `.on("load", cb)`
// čekalo na událost, která se znovu nikdy nespustí. Nahrazeno vlastním
// flagem nastaveným uvnitř JEDINÉHO "load" handleru.
let mapReady = false;
let pendingMapCallbacks = [];
function whenMapReady(cb) {
  if (mapReady) cb();
  else pendingMapCallbacks.push(cb);
}

map.on("load", () => {
  map.addSource("sunset-glow", { type: "geojson", data: emptyFeatureCollection() });
  map.addLayer({
    id: "sunset-glow-layer",
    type: "fill",
    source: "sunset-glow",
    paint: { "fill-color": "#ffb066", "fill-opacity": 0.16 },
  });

  map.addSource("sunset-line", { type: "geojson", data: emptyFeatureCollection() });
  map.addLayer({
    id: "sunset-line-layer",
    type: "line",
    source: "sunset-line",
    layout: { "line-cap": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 3, "line-dasharray": [1, 1] },
  });

  // Fáze 11 (zadání bod E) - vizuální kruh "search area" kolem
  // hledaného středu. ČISTĚ vizuální (žádné API volání při každém
  // posunu slideru) - jen `setData()` na už existující zdroj, viz
  // `drawSearchCircle()`. Barva úmyslně jiná (studená modrá) než
  // teplá "sunset direction" výseč výše, ať jde vizuálně odlišit.
  map.addSource("search-area", { type: "geojson", data: emptyFeatureCollection() });
  map.addLayer({
    id: "search-area-fill",
    type: "fill",
    source: "search-area",
    paint: { "fill-color": "#5aa9e6", "fill-opacity": 0.14 },
  });
  map.addLayer({
    id: "search-area-outline",
    type: "line",
    source: "search-area",
    paint: { "line-color": "#5aa9e6", "line-width": 2.5, "line-dasharray": [3, 2] },
  });

  marker = new maplibregl.Marker({ color: "#f4a742" }).setLngLat([currentLon, currentLat]).addTo(map);
  // Kontejner mapy dostal svůj konečný rozměr z CSS layoutu stránky
  // (Fáze 7 - mapa už není fullscreen) - jeden `resize()` navíc pro
  // jistotu, kdyby se layout ustálil až po prvním renderu.
  map.resize();

  mapReady = true;
  pendingMapCallbacks.forEach((cb) => cb());
  pendingMapCallbacks = [];
});

// Klik na mapu = jediná akce, která spouští analýzu. Pan/zoom/rotate/tilt
// (mapová interakce) žádnou síťovou analýzu nikdy nevyvolá - MapLibre
// vyvolává "click" jen na skutečné kliknutí/tap, ne během gest.
map.on("click", (e) => {
  const lat = Math.round(e.lngLat.lat * 10000) / 10000;
  const lon = Math.round(e.lngLat.lng * 10000) / 10000;
  selectLocation(lat, lon, { flyTo: false, resetDropdown: true });
});

testLocationSelect.addEventListener("change", () => {
  if (!testLocationSelect.value) return;
  const [lat, lon] = testLocationSelect.value.split(",").map(Number);
  selectLocation(lat, lon, { flyTo: true, resetDropdown: false });
  controlPanel.hidden = true;
});

manualAnalyzeBtn.addEventListener("click", () => {
  const lat = parseFloat(latInput.value);
  const lon = parseFloat(lonInput.value);
  if (Number.isNaN(lat) || Number.isNaN(lon)) return;
  selectLocation(lat, lon, { flyTo: true, resetDropdown: true });
  controlPanel.hidden = true;
});

// AUDIT NÁLEZ (skutečné použití v prohlížeči, ne jen čtení kódu): změna
// data/časové zóny v "☰" panelu neměla ŽÁDNÝ viditelný efekt, dokud
// uživatel nerozkliknul skrytou "Manuální souřadnice" a nestiskl tlačítko
// "Analyze coordinates" (které navíc svým názvem vůbec nenaznačuje, že
// řeší datum). Reálný uživatel by tohle nikdy nenašel. Oprava: změna
// data NEBO časové zóny vždy okamžitě přeanalyzuje AKTUÁLNĚ vybrané
// místo, bez nutnosti cokoliv dalšího hledat/klikat.
dateInput.addEventListener("change", () => runAnalysis(currentLat, currentLon));
tzInput.addEventListener("change", () => runAnalysis(currentLat, currentLon));

// `slug` (milestone "Complete Product Build", pokračování) - jen
// výsledky z /api/find_sunset skutečně znají candidate slug (výchozí
// TEST_LOCATIONS/manuální klik na mapu ne) - "Open place page" tlačítko
// se proto zobrazí jen tehdy, když ho máme opravdu ke komu odkázat.
let currentBestSlug = null;
function updateOpenPlacePageBtn() {
  const btn = document.getElementById("open-place-page-btn");
  if (currentBestSlug) {
    btn.href = `/sunset/${currentBestSlug}/`;
    btn.hidden = false;
  } else {
    btn.hidden = true;
  }
}

function selectLocation(lat, lon, { flyTo, resetDropdown, slug = null }) {
  currentLat = lat;
  currentLon = lon;
  latInput.value = lat;
  lonInput.value = lon;
  currentBestSlug = slug;
  updateOpenPlacePageBtn();
  if (resetDropdown) testLocationSelect.value = "";
  if (marker) marker.setLngLat([lon, lat]);
  if (flyTo) map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 13) });
  return runAnalysis(lat, lon); // vráceno (Fáze 11) - findMySunset() na dokončení počká, aby mohl navázat vlastním renderem výsledků
}

document.getElementById("show-on-map-btn").addEventListener("click", () => {
  document.getElementById("explore-map").scrollIntoView({ behavior: "smooth", block: "start" });
  map.flyTo({ center: [currentLon, currentLat], zoom: Math.max(map.getZoom(), 13) });
});
document.getElementById("explore-panorama-btn").addEventListener("click", () => {
  document.getElementById("panorama-section").scrollIntoView({ behavior: "smooth", block: "start" });
});

// AUDIT NÁLEZ (zadání bod 12/18 - "zabraň race conditions"): pokud
// uživatel klikne na nové místo dřív, než dorazí odpověď na předchozí
// požadavek, obě "běží" souběžně - a bez ochrany by mohla POMALEJŠÍ
// (starší) odpověď dorazit POSLEDNÍ a přepsat už vykreslený NOVĚJŠÍ
// výsledek zastaralými daty (mapa by ukazovala jeden bod, zbytek stránky
// jiný). Řešení: monotónní token požadavku - vykreslí/zobrazí chybu jen
// odpověď na SKUTEČNĚ POSLEDNÍ vyslaný požadavek, starší se tiše zahodí.
let latestRequestId = 0;

// Milestone "Complete Product Build" (zadání bod 13/24) - uživatelské
// preference se čtou přímo z hero UI a posílají na server PŘI KAŽDÉ
// analýze (ne jen při search), aby "Best Sunset FOR YOU" fungovalo i
// při kliku na mapu/kartu, ne jen v /api/find_sunset. `null`, pokud
// uživatel nic nevybral (ne prázdný objekt) - server pak `user_match`
// vůbec nepočítá (zadání bod 15 - "no preference" != "0% match").
function collectPreferences() {
  const sceneTypes = Array.from(document.querySelectorAll(".pref-scene:checked")).map((el) => el.value);
  const photography = document.getElementById("pref-photography").checked;
  const dramatic = document.getElementById("pref-dramatic").checked;
  const clear = document.getElementById("pref-clear").checked;
  const hikingEffort = document.getElementById("pref-hiking-effort").value || null;
  // Milník 12 (zadání bod 4) - čtyři nové preference, stejný vzor jako
  // výše (prostý checkbox stav -> `app/personalization.py UserPreferences`
  // pole se stejným jménem - viz `PreferencesModel` v app/main.py).
  // Vivid/Inversion/Sun-on-horizon žijí v rozkliknutém panelu (skupina
  // "Tonight" - jsou o dnešním počasí, ne o typu místa, viz komentář u
  // `.pref-quick-row` v templates/index.html).
  const vividColors = document.getElementById("pref-vivid").checked;
  const inversion = document.getElementById("pref-inversion").checked;
  const sunOnHorizon = document.getElementById("pref-horizon").checked;
  const panorama = document.getElementById("pref-panorama").checked;
  if (
    sceneTypes.length === 0 && !photography && !dramatic && !clear && !hikingEffort
    && !vividColors && !inversion && !sunOnHorizon && !panorama
  ) return null;
  return {
    scene_types: sceneTypes, photography, dramatic, clear, hiking_effort: hikingEffort,
    vivid_colors: vividColors, inversion, sun_on_horizon: sunOnHorizon, panorama,
  };
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

document.getElementById("preferences-toggle-btn").addEventListener("click", () => {
  const el = document.getElementById("hero-preferences");
  el.hidden = !el.hidden;
});

// Milník 12 (zadání bod 1 - minimalistický výběr lokality) - "Jiná
// lokalita" odkryje vyhledávací kartu (Where?/radius/preferences/Find
// my sunset), která zůstává skrytá, dokud o ni uživatel výslovně
// nepožádá - žádná nová funkce, jen pozdější zobrazení už existující
// `.hero-search` karty.
document.getElementById("other-location-link").addEventListener("click", (e) => {
  e.preventDefault();
  const search = document.getElementById("hero-search");
  search.hidden = !search.hidden;
  if (!search.hidden) searchWhereInput.focus();
});

// Milník "Postupné vylepšení" (Fáze 3, rozšířeno Milníkem 12 po zpětné
// vazbě "preference v rychlém řádku ať jsou o typu okolí - přírodní/
// město/voda") - rychlé chipy jen ZAŠKRTNOU existující `.pref-scene`
// checkboxy (STEJNÝ zdroj pravdy jako `collectPreferences()` výše,
// STEJNÝ `app/personalization.py` systém, beze změny výpočtu) - žádná
// paralelní/duplicitní logika preferencí. "Bez preference" = checkbox
// odškrtnutý (výchozí chování zůstává, zadání bod "zachovej současné
// výchozí chování, pokud si uživatel žádnou možnost nezvolí").
function wireQuickSceneToggle(quickId, sceneValues) {
  const quickEl = document.getElementById(quickId);
  if (!quickEl) return;
  quickEl.addEventListener("change", () => {
    for (const value of sceneValues) {
      const box = document.querySelector(`.pref-scene[value="${value}"]`);
      if (box) box.checked = quickEl.checked;
    }
  });
}
wireQuickSceneToggle("pref-natural-quick", ["mountain", "forest"]);
wireQuickSceneToggle("pref-city-quick", ["city"]);
wireQuickSceneToggle("pref-water-quick", ["water"]);

async function runAnalysis(lat, lon) {
  const requestId = ++latestRequestId;
  errorEl.hidden = true;
  loadingEl.hidden = false;

  const preferences = collectPreferences();
  // `searchCenter` (Fáze 11) je jediný skutečný "odkud hledáme" kontext,
  // který máme - bez něj by "distance_km"/hike odhad byl vymyšlený.
  const distanceKm = searchCenter ? haversineKm(searchCenter.lat, searchCenter.lon, lat, lon) : null;
  const payload = { lat, lon, date: dateInput.value, timezone: tzInput.value, preferences, distance_km: distanceKm };

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (requestId !== latestRequestId) return; // mezitím vznikl novější požadavek - tohle zahodit

    if (!response.ok) {
      showError(formatApiError(data));
      return;
    }
    currentTz = payload.timezone;
    renderAll(data, currentTz, lat, lon);
  } catch (err) {
    if (requestId !== latestRequestId) return;
    showError("Nepodařilo se spojit se serverem: " + err.message);
  } finally {
    if (requestId === latestRequestId) loadingEl.hidden = true;
  }
}

function formatApiError(data) {
  if (data && data.error) return data.error;
  if (data && data.detail) {
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("\n");
    }
    return String(data.detail);
  }
  return "Neznámá chyba (" + JSON.stringify(data) + ")";
}

function showError(message) {
  // AUDIT NÁLEZ (skutečné použití v prohlížeči): po neúspěšné analýze
  // (např. oceán bez DEM pokrytí) zůstávala "Best Sunset"/panorama
  // sekce zobrazená se STARÝM, teď už neplatným výsledkem přímo pod
  // chybovou hláškou - vypadalo to jako protichůdný stav ("chyba, ale
  // tady je pořád platný rating 92?"). Chyba teď schová i staré výsledky,
  // aby stránka neukazovala zastaralá data jako aktuální.
  document.getElementById("best-sunset").hidden = true;
  document.getElementById("panorama-section").hidden = true;
  errorEl.textContent = message;
  errorEl.hidden = false;
}

// --- Destinační bod na kouli podle azimutu (pro sunset-line/glow na mapě
// i pro panorama) - čistě geometrická projekce ZNÁMÉHO azimutu, ne
// přepočet solar/terrain geometrie. Standardní "spherical law" vzorec. ---
function destinationPoint(lat, lon, bearingDeg, distanceM) {
  const R = 6371000;
  const delta = distanceM / R;
  const theta = (bearingDeg * Math.PI) / 180;
  const phi1 = (lat * Math.PI) / 180;
  const lambda1 = (lon * Math.PI) / 180;
  const phi2 = Math.asin(Math.sin(phi1) * Math.cos(delta) + Math.cos(phi1) * Math.sin(delta) * Math.cos(theta));
  const lambda2 =
    lambda1 + Math.atan2(Math.sin(theta) * Math.sin(delta) * Math.cos(phi1), Math.cos(delta) - Math.sin(phi1) * Math.sin(phi2));
  return [(lambda2 * 180) / Math.PI, (phi2 * 180) / Math.PI];
}

// Fáze 11 (zadání bod E) - kruh o `radiusKm` kolem [lat, lon] jako
// GeoJSON polygon (64 bodů po obvodu, geodetický výpočet přes stejnou
// `destinationPoint()` funkci jako sunset direction výše - žádná nová
// knihovna). Volá se jen z JS na klientovi, ŽÁDNÝ síťový request.
function circlePolygon(lat, lon, radiusKm, steps = 64) {
  const coords = [];
  for (let i = 0; i <= steps; i++) {
    const bearing = (360 * i) / steps;
    coords.push(destinationPoint(lat, lon, bearing, radiusKm * 1000));
  }
  return { type: "Polygon", coordinates: [coords] };
}

function drawSearchCircle(lat, lon, radiusKm) {
  const src = map.getSource("search-area");
  if (!src) return;
  src.setData({
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: {}, geometry: circlePolygon(lat, lon, radiusKm) }],
  });
}

function applySunsetVisuals(result, lat, lon) {
  const sunsetAz = result.solar.sunset_azimuth_deg;
  const obstructed = result.visibility.terrain_obstruction;
  const color = obstructed ? "#e5484d" : "#3ecf6e";

  const zoom = map.getZoom();
  const distanceM = Math.max(400, Math.min(20000, 6000 * Math.pow(2, 10 - zoom)));
  const [destLon, destLat] = destinationPoint(lat, lon, sunsetAz, distanceM);

  const lineSrc = map.getSource("sunset-line");
  if (lineSrc) {
    lineSrc.setData({
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: { color }, geometry: { type: "LineString", coordinates: [[lon, lat], [destLon, destLat]] } }],
    });
  }

  const glowSrc = map.getSource("sunset-glow");
  if (glowSrc) {
    const halfAngle = 10;
    const [leftLon, leftLat] = destinationPoint(lat, lon, sunsetAz - halfAngle, distanceM);
    const [rightLon, rightLat] = destinationPoint(lat, lon, sunsetAz + halfAngle, distanceM);
    glowSrc.setData({
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[lon, lat], [leftLon, leftLat], [rightLon, rightLat], [lon, lat]]] } }],
    });
  }

  if (!sunMarker) {
    const el = document.createElement("div");
    el.className = "sun-glyph";
    el.textContent = "🌅";
    sunMarker = new maplibregl.Marker({ element: el }).setLngLat([destLon, destLat]).addTo(map);
  } else {
    sunMarker.setLngLat([destLon, destLat]);
  }
}

// --- Vykreslení výsledku do VŠECH sekcí stránky (hero context, Best
// Sunset, panorama, mapa, technické detaily) - jeden zdroj pravdy. ---
function renderAll(result, tz, lat, lon) {
  const fmtTime = (iso) => new Date(iso).toLocaleTimeString("cs-CZ", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
  const fmtDate = (iso) => new Date(iso).toLocaleDateString("cs-CZ", { timeZone: tz, day: "numeric", month: "long" });

  const locationLabel = currentLocationLabel(lat, lon);
  const sunsetTimeStr = fmtTime(result.solar.astronomical_sunset);

  document.getElementById("hero-location").textContent = locationLabel;
  document.getElementById("hero-context").textContent = `Dnes · ${fmtDate(result.solar.astronomical_sunset)} · západ slunce ${sunsetTimeStr}`;

  document.getElementById("best-title").textContent = locationLabel;
  document.getElementById("tz-note").textContent = `Časy v časovém pásmu: ${tz}`;

  const obstructed = result.visibility.terrain_obstruction;
  document.getElementById("stat-sunset").textContent = `${sunsetTimeStr} sunset`;
  // AUDIT NÁLEZ (skutečné použití v prohlížeči): syrová hodnota terénního
  // horizontu ve stupních ("-0.1° horizon") nic neřekla běžnému uživateli
  // outdoor/travel produktu - působilo to jako zbytek GIS terminologie.
  // Nahrazeno směrem, odkud Slunce zapadá (kompasový bod + azimut) - reálná
  // data (sunset_azimuth_deg), ale prakticky užitečná informace ("kam se
  // dívat"), ne technický parametr.
  document.getElementById("stat-horizon").textContent = `${azimuthToCompass(result.solar.sunset_azimuth_deg)} (${result.solar.sunset_azimuth_deg.toFixed(0)}°)`;
  const dur = result.visibility.visibility_duration;
  const suffix = result.visibility.visibility_duration_is_lower_bound ? "+" : "";
  document.getElementById("stat-visible").textContent = !obstructed
    ? "Open horizon"
    : dur
      ? `${dur.minutes.toFixed(0)}${suffix} min visible`
      : "—";
  document.getElementById("stat-elevation").textContent = `${result.location.elevation_m.toFixed(0)} m`;

  // Milestone "Complete Product Build" - hlavní kroužek/popisek teď
  // ukazuje kompozitní `sunset_score` (terrain+weather+visibility+
  // atmosphere+sun_position+photography), ne jen terrain-only `rating`
  // (ten zůstává beze změny, jen jako technický detail/Why-list vstup).
  renderTodayConditions(result);
  renderRatingRing(result.sunset_score);
  renderSunsetTypeBadge(result.sunset_score.sunset_type);
  renderWeatherAndHikeStats(result.sunset_score.weather, result.hike_access_estimate);
  renderScoreBreakdown(result.sunset_score.components);
  renderUserMatchBadge(result.user_match);
  renderWhyList(result);
  renderDetails(result, tz);
  renderWarnings(result.diagnostics.warnings);
  renderDemQualityNote(result.dem);
  drawPolarChart(result);
  applySunsetVisuals(result, lat, lon);
  setPanoramaResult(result);
  document.getElementById("raw-json").textContent = JSON.stringify(result, null, 2);

  document.getElementById("best-sunset").hidden = false;
  document.getElementById("panorama-section").hidden = false;
}

// Milník 12 (zadání bod 1A/1B) - "jedna dominantní informace, několik
// doplňujících, krátké vysvětlení", NEZÁVISLÉ na výběru konkrétní
// vyhlídky. Dominantní = `general_color_potential` (app/sunset_score.py
// `classify_color_potential()`) - specifičtější a vizuálně výraznější
// otázka ("jak barevná bude obloha") než dřívější obecný "typ" západu,
// který tu dřív byl (zadání "barevný západ není totéž co jasná obloha").
// Vysvětlení (`.tonight-explain`) je `color_potential.detail` - UŽ
// hotová, zdůvodněná věta přímo z klasifikační funkce, ne nová
// marketingová fráze navíc. Doplňující badge = přímý výhled na slunce
// (`general_horizon_view`, terén-nezávislé) + inverze/mlha
// (`general_inversion_potential`, NOVÉ, zadání bod 3) + poctivá výhrada
// bez počasí (JEDINÝ reálný signál nejistoty, který appka má).
// `applySkyMood()` navíc převezme `general_color_potential.key` a
// přetónuje dekorativní hero siluetu (zadání bod 1B - "vizuál má
// odpovídat předpovídaným podmínkám").
function renderTodayConditions(result) {
  const headline = document.getElementById("tonight-headline");
  const explain = document.getElementById("tonight-explain");
  const badgesWrap = document.getElementById("today-conditions");
  const horizonBadge = document.getElementById("today-horizon-badge");
  const inversionBadge = document.getElementById("today-inversion-badge");
  const confidenceBadge = document.getElementById("today-confidence-badge");

  const color = result.general_color_potential;
  if (color && color.label) {
    headline.textContent = color.label;
    headline.hidden = false;
    explain.textContent = color.detail || "";
    explain.hidden = !color.detail;
  } else {
    headline.hidden = true;
    explain.hidden = true;
  }
  applySkyMood(color && color.key);

  const horizonView = result.general_horizon_view;
  if (horizonView && horizonView.key) {
    const HORIZON_CLASS = { likely: "is-open", partial: "is-delayed", unlikely: "is-delayed" };
    horizonBadge.className = "today-badge " + (HORIZON_CLASS[horizonView.key] || "");
    horizonBadge.textContent = horizonView.label;
    horizonBadge.hidden = false;
  } else {
    horizonBadge.hidden = true;
  }

  const inversion = result.general_inversion_potential;
  if (inversion && inversion.key) {
    const INVERSION_CLASS = { favorable: "is-open", possible: "is-delayed", uncertain: "is-delayed", unlikely: "" };
    inversionBadge.className = "today-badge " + (INVERSION_CLASS[inversion.key] || "");
    inversionBadge.textContent = inversion.label;
    inversionBadge.title = inversion.detail || "";
    inversionBadge.hidden = inversion.key === "unlikely"; // bez signálu mlhy/inverze badge jen zbytečně plní řádek
  } else {
    inversionBadge.hidden = true;
  }

  const hasWeather = result.sunset_score.weather !== null && result.sunset_score.weather !== undefined;
  confidenceBadge.hidden = hasWeather;
  if (!hasWeather) confidenceBadge.textContent = "Weather unavailable · no sky/visibility estimate right now";

  badgesWrap.hidden = false;
}

// Milník 12 (zadání bod 1B) - "vlastní vizuální jazyk... má odpovídat
// předpovídaným podmínkám, ale nesmí předstírat přesnou simulaci".
// Přetónuje STEJNOU dekorativní SVG siluetu/glow (žádná nová grafika,
// jen jiné CSS proměnné, viz style.css `.sky-mood-*`) podle SKUTEČNÉHO
// `color_potential.key` - "vivid"/"warm"/"muted"/bez dat necháva
// výchozí (bezpečný, nic netvrdící) teplý tón.
function applySkyMood(colorKey) {
  const hero = document.getElementById("hero");
  if (!hero) return;
  hero.classList.remove("sky-mood-vivid", "sky-mood-warm", "sky-mood-muted");
  if (colorKey === "vivid" || colorKey === "warm" || colorKey === "muted") {
    hero.classList.add(`sky-mood-${colorKey}`);
  }
}

const COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
function azimuthToCompass(deg) {
  const index = Math.round(((deg % 360) + 360) % 360 / 45) % 8;
  return COMPASS_POINTS[index];
}

// BUG NALEZENÝ (Fáze 11f, reálný browser test s "Find my sunset"):
// tahle funkce dřív znala jména jen ze statického výchozího Beskydy
// datasetu (`beskydyCandidates`) - výsledek z `/api/search` (klidně z
// úplně jiného regionu, např. Radhošť z Vsetínska) se pak v hero/Best
// Sunset nadpisu zobrazil jako holé "49.4916, 18.2231" místo jména.
// Řešení: sdílená mapa "lat,lon -> jméno" naplňovaná KAŽDÝM zdrojem
// kandidátů (initApp i findMySunset), ne jen jedním konkrétním datasetem.
const knownLocationNames = new Map();
function registerLocationNames(candidates) {
  for (const c of candidates) knownLocationNames.set(`${c.lat},${c.lon}`, c.name);
}

function currentLocationLabel(lat, lon) {
  for (const opt of testLocationSelect.options) {
    if (opt.value === `${lat},${lon}`) return opt.textContent;
  }
  const known = knownLocationNames.get(`${lat},${lon}`);
  if (known) return known;
  return `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
}

// --- Sunset Rating (Fáze 6.6, prezentace přepracovaná ve Fázi 7, meter
// nahrazen Corona Bloom v Milníku 12) - `result.rating` přichází hotové
// z /api/analyze (app/sunset_rating.py), výpočet se tu NEMĚNIL - frontend
// jen zobrazuje. ---

// Milník 12 ("Corona Bloom") - sluneční ukazatel skóre, náhrada za staré
// ploché kolečko/prstenec (a za starou open/delayed/blocked trojbarevnou
// škálu, kterou `coronaBloomAccentColor()` níž nahrazuje). Stejná logika
// jako static/sunset_viz.js (plné zdůvodnění tam, včetně proč 85/70/50/30
// - viz app/main.py::_composite_label()) - Tonight page má vlastní
// zdvojenou kopii (stejný důvod jako u panoramaCaptionText výše -
// stránka zůstává nezávislá na novém sdíleném souboru).
const CORONA_BLOOM_TIERS = {
  excellent: {
    rings: [
      { color: "#9a4fa0", opacity: 0.17 },
      { color: "#ff6f8a", opacity: 0.27 },
      { color: "#ff8f4d", opacity: 0.43 },
    ],
    core: { fill: "#171b28", stroke: "#ffecc7" },
  },
  good: {
    rings: [
      { color: "#ff8f4d", opacity: 0.19 },
      { color: "#ffb066", opacity: 0.33 },
    ],
    core: { fill: "#171b28", stroke: "#ffb066" },
  },
  fair: {
    rings: [
      { color: "#f2a559", opacity: 0.16 },
      { color: "#f2a559", opacity: 0.29 },
    ],
    core: { fill: "#2e2316", stroke: "#f2a559" },
  },
  poor: {
    rings: [
      { color: "#5b5f6b", opacity: 0.09 },
      { color: "#5b5f6b", opacity: 0.15 },
    ],
    core: { fill: "#20222a", stroke: "#6b7280" },
  },
};

function coronaBloomTier(score) {
  if (score >= 85) return "excellent";
  if (score >= 70) return "good";
  if (score >= 50) return "fair";
  return "poor";
}

function coronaBloomRingRadius(fromInside, scoreFrac) {
  if (fromInside === 0) return 30 + scoreFrac * 17;
  if (fromInside === 1) return 36 + scoreFrac * 20;
  return 40 + scoreFrac * 18;
}

function coronaBloomSvg(score, sizePx, ariaLabel) {
  const size = sizePx || 52;
  const numeric = score === null || score === undefined ? NaN : Number(score);
  if (Number.isNaN(numeric)) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 120 120" role="img" aria-label="${ariaLabel || "Score not available yet"}">
      <circle cx="60" cy="60" r="44" fill="#171b28" stroke="rgba(255,255,255,0.14)" stroke-width="1.5"/>
      <circle cx="60" cy="60" r="7" fill="rgba(255,255,255,0.3)"/>
    </svg>`;
  }
  const clamped = Math.max(0, Math.min(100, numeric));
  const scoreFrac = clamped / 100;
  const rounded = Math.round(clamped);
  const tier = coronaBloomTier(clamped);
  const t = CORONA_BLOOM_TIERS[tier];
  const total = t.rings.length;
  const ringsSvg = t.rings
    .map((ring, indexFromOutside) => {
      const fromInside = total - 1 - indexFromOutside;
      const r = coronaBloomRingRadius(fromInside, scoreFrac).toFixed(2);
      return `<circle cx="60" cy="60" r="${r}" fill="${ring.color}" opacity="${ring.opacity}"/>`;
    })
    .join("");
  const fontSize = size <= 40 ? 30 : 32;
  const label = ariaLabel || `Sunset score ${rounded} out of 100`;
  return `<svg width="${size}" height="${size}" viewBox="0 0 120 120" role="img" aria-label="${label}">
    ${ringsSvg}
    <circle cx="60" cy="60" r="28" fill="${t.core.fill}" stroke="${t.core.stroke}" stroke-width="1.5"/>
    <text x="60" y="69" text-anchor="middle" font-family="Fraunces, serif" font-size="${fontSize}" font-weight="700" fill="#fff" style="filter:drop-shadow(0 1px 3px rgba(0,0,0,0.6))">${rounded}</text>
  </svg>`;
}

// Stejná logika jako static/sunset_viz.js coronaBloomAccentColor - mapové
// markery a tenké accent proužky karet jsou příliš malé na plnohodnotný
// vícevrstvý meter, tahle funkce vrací jen JEDNU reprezentativní barvu
// přímo z CORONA_BLOOM_TIERS (žádná nová paleta navíc).
function coronaBloomAccentColor(score) {
  const numeric = score === null || score === undefined ? NaN : Number(score);
  if (Number.isNaN(numeric)) return CORONA_BLOOM_TIERS.poor.core.stroke;
  const clamped = Math.max(0, Math.min(100, numeric));
  const tier = coronaBloomTier(clamped);
  return CORONA_BLOOM_TIERS[tier].rings[0].color;
}

// Milník 12 ("Corona Bloom") - hlavní hero meter teď vykresluje STEJNÁ
// sdílená funkce jako zbytek appky (žádná vlastní/odlišná implementace
// jen pro Tonight stránku), jen ve větší vykreslené velikosti.
function renderRatingRing(rating) {
  const score = rating.overall_0_100;
  const descEl = document.getElementById("rating-desc");
  document.getElementById("corona-bloom-hero").innerHTML = coronaBloomSvg(score, 160);
  descEl.textContent = rating.label;
}

// Prahy pro "Proč toto místo?" - čistě popisné rozdělení nad už hotovými
// rating komponentami (žádný nový scoring algoritmus, zadání bod 6).
const WHY_THRESHOLD = 70;
const WHY_ELEVATION_THRESHOLD_M = 800;

function renderWhyList(result) {
  // Milestone "Complete Product Build" (zadání bod 16) - rozšířeno z
  // čistě terénních důvodů i o počasí a uživatelské preference - VŽDY
  // ze SKUTEČNÝCH hodnot (rating.components/sunset_score.weather/
  // user_match.reasons), žádný hardcoded marketing text.
  const list = document.getElementById("why-list");
  list.innerHTML = "";
  const reasons = [];
  const c = result.rating.components;
  const elevationM = result.location.elevation_m;
  if (c.horizon_at_azimuth >= WHY_THRESHOLD) reasons.push("Open horizon toward the sunset.");
  if (c.sector_openness >= WHY_THRESHOLD) reasons.push("Wide open sector around the sunset direction.");
  if (c.duration >= WHY_THRESHOLD) reasons.push("Long sunset visibility.");
  if (c.delay >= WHY_THRESHOLD) reasons.push("Minimal delay from terrain.");
  if (elevationM >= WHY_ELEVATION_THRESHOLD_M) reasons.push(`High elevation (${elevationM.toFixed(0)} m) with a wide panorama.`);

  const weather = result.sunset_score && result.sunset_score.weather;
  if (weather) {
    if (weather.cloud_cover_low_pct < 15) reasons.push("Low cloud cover should leave the horizon clear.");
    const midHigh = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2;
    if (midHigh >= 20 && midHigh <= 65 && weather.cloud_cover_low_pct < 20) reasons.push("Mid/high clouds may create stronger sunset colors.");
    if (weather.visibility_m && weather.visibility_m >= 25000) reasons.push("Excellent visibility today.");
    if (weather.precipitation_probability_pct != null && weather.precipitation_probability_pct >= 50) reasons.push("Chance of precipitation may reduce visibility.");
  }

  if (result.user_match && result.user_match.reasons) {
    for (const r of result.user_match.reasons) reasons.push(r);
  }

  const isNegative = reasons.length === 0;
  if (isNegative) reasons.push("Terén v okolí tohoto místa výhled na západ výrazně omezuje.");

  for (const reason of reasons) {
    const li = document.createElement("li");
    if (isNegative) li.className = "why-negative";
    li.textContent = reason;
    list.appendChild(li);
  }
}

function renderSunsetTypeBadge(sunsetType) {
  const badge = document.getElementById("sunset-type-badge");
  if (!sunsetType || !sunsetType.key) {
    badge.hidden = true;
    return;
  }
  badge.textContent = sunsetType.label;
  badge.title = sunsetType.confidence_text || "";
  badge.hidden = false;
}

const HIKE_ACCESS_LABELS = { easy: "Easy access", moderate: "Moderate hike", hike: "Hike", adventure: "Adventure" };

function renderWeatherAndHikeStats(weather, hikeAccessEstimate) {
  const cloudsRow = document.getElementById("stat-clouds-row");
  if (weather) {
    document.getElementById("stat-clouds").textContent = `${Math.round(weather.cloud_cover_pct)}% clouds`;
    cloudsRow.hidden = false;
  } else {
    cloudsRow.hidden = true;
  }

  const hikeRow = document.getElementById("stat-hike-row");
  if (hikeAccessEstimate) {
    document.getElementById("stat-hike").textContent = HIKE_ACCESS_LABELS[hikeAccessEstimate] || hikeAccessEstimate;
    hikeRow.hidden = false;
  } else {
    hikeRow.hidden = true;
  }
}

function renderScoreBreakdown(components) {
  const list = document.getElementById("score-breakdown-list");
  list.innerHTML = "";
  for (const c of components) {
    const dt = document.createElement("dt");
    dt.textContent = `${c.label} (${Math.round(c.weight * 100)}%)`;
    const dd = document.createElement("dd");
    if (c.value_0_100 === null || c.value_0_100 === undefined) {
      dd.textContent = "Unavailable";
      dd.className = "is-unavailable";
    } else {
      dd.textContent = `${Math.round(c.value_0_100)} / 100`;
    }
    list.appendChild(dt);
    list.appendChild(dd);
  }
}

function renderUserMatchBadge(userMatch) {
  const badge = document.getElementById("user-match-badge");
  if (!userMatch || userMatch.match_pct === null || userMatch.match_pct === undefined) {
    badge.hidden = true;
    return;
  }
  badge.textContent = `Match for you: ${userMatch.match_pct}%`;
  badge.hidden = false;
}

function renderDetails(result, tz) {
  const rows = [
    ["Observer elevation", `${result.location.elevation_m.toFixed(1)} m`],
    ["DEM provider", result.dem.provider],
    ["DEM dataset", result.dem.dataset],
    ["DEM resolution", `${result.dem.resolution_m.toFixed(1)} m`],
    ["Analysis range", `${(result.dem.analysis_range_m / 1000).toFixed(0)} km`],
    ["DEM fallback used", result.dem.fallback_used ? `ano (${result.dem.fallback_reason || ""})` : "ne"],
    ["Solar azimuth (sunset)", `${result.solar.sunset_azimuth_deg.toFixed(1)}°`],
    ["Terrain horizon at sunset azimuth", `${result.solar.sunset_terrain_horizon_deg.toFixed(2)}°`],
    ["Terrain delay", `${result.visibility.delay_minutes.toFixed(1)} min`],
    ["Slope / aspect", formatSlopeAspect(result.terrain)],
    ["Sunrise (astronomical)", result.solar.astronomical_sunrise ? fmtDateTime(result.solar.astronomical_sunrise, tz) : "—"],
    ["Rating: horizon at azimuth", `${result.rating.components.horizon_at_azimuth.toFixed(1)} / 100`],
    ["Rating: delay", `${result.rating.components.delay.toFixed(1)} / 100`],
    ["Rating: sector openness", `${result.rating.components.sector_openness.toFixed(1)} / 100`],
    ["Rating: visibility duration", `${result.rating.components.duration.toFixed(1)} / 100`],
  ];

  const dl = document.getElementById("details-list");
  dl.innerHTML = "";
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
}

function fmtDateTime(iso, tz) {
  const d = new Date(iso);
  const datePart = d.toLocaleDateString("sv-SE", { timeZone: tz });
  const timePart = d.toLocaleTimeString("cs-CZ", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
  return `${datePart} ${timePart}`;
}

function formatSlopeAspect(terrain) {
  if (terrain.slope_deg === null || terrain.slope_deg === undefined) return "nedostupné";
  return `${terrain.slope_deg.toFixed(1)}° / ${terrain.aspect_deg.toFixed(0)}°`;
}

function renderWarnings(warnings) {
  const section = document.getElementById("warnings-section");
  const list = document.getElementById("warnings-list");
  list.innerHTML = "";
  if (!warnings || warnings.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  for (const w of warnings) {
    const li = document.createElement("li");
    li.textContent = w;
    list.appendChild(li);
  }
}

// =====================================================================
// SUNSET PANORAMA (Fáze 7, zadání bod 7/8) - interaktivní horizontální
// vizualizace terénního horizontu, postavená nad STEJNÝMI 360° daty jako
// polar-chart (`terrain.azimuths_deg`/`horizon_altitudes_deg`) - žádný
// nový 3D engine, žádné vymyšlené hory. Uživatel může tažením myší/prstem
// "rozhlížet" po azimutech; tlačítko Recenter vrátí pohled na sunset
// azimuth. Sky gradient a warm glow jsou VIZUÁLNÍ EFEKT (dekorace), silueta
// terénu a poloha sunset markeru jsou ANALYTICKÁ DATA z /api/analyze.
// =====================================================================
const PANORAMA_FOV_DEG = 100;
let panoramaResult = null;
let panoramaViewCenter = 0;
let panoramaDragging = false;
let panoramaDragStartX = 0;
let panoramaDragStartCenter = 0;

function setPanoramaResult(result) {
  panoramaResult = result;
  panoramaViewCenter = result.solar.sunset_azimuth_deg;
  document.getElementById("panorama-caption").textContent = panoramaCaptionText({
    obstructed: result.visibility.terrain_obstruction,
    delayMinutes: result.visibility.delay_minutes,
    sunsetAzimuthDeg: result.solar.sunset_azimuth_deg,
  });
  drawPanoramaFrame();
}

function normalizeAzimuth(deg) {
  return ((deg % 360) + 360) % 360;
}

// Zpětná vazba (živé použití) - "z toho moc nevyčtu, jen že západ je na
// západě, to ví každý" - stejná oprava jako static/sunset_viz.js
// (plné zdůvodnění tam, Tonight page má vlastní zdvojenou kopii kresby
// panoramatu, viz komentář nahoře u souboru), jen duplikovaná sem,
// stejný vzor jako `azimuthToCompass`/`ratingBand` výše.
function panoramaCaptionText({ obstructed, delayMinutes, sunsetAzimuthDeg }) {
  const compass = azimuthToCompass(sunsetAzimuthDeg);
  if (!obstructed) {
    return `Horizon is open toward ${compass} (${sunsetAzimuthDeg.toFixed(0)}°) - the terrain profile dips below the dotted "flat horizon" line there, so the sun reaches the true horizon with no delay.`;
  }
  const minutes = Math.round(delayMinutes);
  return `Terrain rises above the dotted "flat horizon" line toward ${compass} (${sunsetAzimuthDeg.toFixed(0)}°) - it hides the sun about ${minutes} minute${minutes === 1 ? "" : "s"} before it would set behind a flat horizon.`;
}

const PANORAMA_ALT_PX_PER_DEG = 16; // dřív 6 - viz komentář ve static/sunset_viz.js

function drawPanoramaFrame() {
  if (!panoramaResult) return;
  const canvas = document.getElementById("panorama-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  const horizonY = H * 0.78;

  const azimuths = panoramaResult.terrain.azimuths_deg;
  const altitudes = panoramaResult.terrain.horizon_altitudes_deg;
  const sunsetAz = panoramaResult.solar.sunset_azimuth_deg;
  const obstructed = panoramaResult.visibility.terrain_obstruction;

  const azToX = (az) => {
    let diff = normalizeAzimuth(az) - normalizeAzimuth(panoramaViewCenter);
    if (diff > 180) diff -= 360;
    if (diff < -180) diff += 360;
    return W / 2 + (diff / PANORAMA_FOV_DEG) * W;
  };
  const altToY = (alt) => Math.max(H * 0.04, horizonY - (alt === null || alt === undefined ? 0 : alt) * PANORAMA_ALT_PX_PER_DEG);

  // --- Obloha: tmavě modrá/fialová nahoře -> teplá zlatá u horizontu
  // (dekorace, ne fyzikální nebe) + jemný warm glow kolem pozice slunce. ---
  const sky = ctx.createLinearGradient(0, 0, 0, horizonY);
  sky.addColorStop(0, "#0d0f1c");
  sky.addColorStop(0.55, "#241c35");
  sky.addColorStop(1, "#4a2f3a");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, W, H);

  const sunX = azToX(sunsetAz);
  if (sunX > -200 && sunX < W + 200) {
    const glow = ctx.createRadialGradient(sunX, horizonY, 0, sunX, horizonY, W * 0.35);
    glow.addColorStop(0, "rgba(255, 176, 102, 0.55)");
    glow.addColorStop(1, "rgba(255, 176, 102, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H);
  }

  // --- Terénní silueta - PŘÍMO ze skutečných 360° dat, žádná interpolace
  // do vymyšlených tvarů mimo vzorkované body.
  //
  // AUDIT NÁLEZ (skutečné použití v prohlížeči): u opravdu otevřeného
  // horizontu (např. výchozí Milešovka, výškové úhly skoro 0°) splyne
  // vyplněná silueta s pozadím oblohy - panorama pak vypadá jako prázdný/
  // nenačtený gradient, ne jako důkaz kvality místa. Oprava: profil se
  // navíc obtáhne zřetelnou linkou (bez ohledu na to, jak plochý/členitý
  // terén skutečně je) - u otevřeného místa je pak jasně vidět čistá
  // rovná linka horizontu, ne "nic". ---
  const profilePoints = [];
  for (let i = 0; i < azimuths.length; i++) {
    const x = azToX(azimuths[i]);
    if (x < -20 || x > W + 20) continue;
    profilePoints.push([x, altToY(altitudes[i])]);
  }

  ctx.beginPath();
  ctx.moveTo(0, H);
  for (const [x, y] of profilePoints) ctx.lineTo(x, y);
  ctx.lineTo(W, H);
  ctx.closePath();
  const terrainGradient = ctx.createLinearGradient(0, 0, 0, H);
  terrainGradient.addColorStop(0, "#2a2138");
  terrainGradient.addColorStop(1, "#120f18");
  ctx.fillStyle = terrainGradient;
  ctx.fill();

  if (profilePoints.length > 1) {
    ctx.beginPath();
    profilePoints.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    ctx.strokeStyle = "rgba(255, 214, 170, 0.55)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // Zpětná vazba - referenční "plochý horizont" (0°), vykreslená AŽ PO
  // terénní siluetě (stejné zdůvodnění jako static/sunset_viz.js -
  // reálný terén je skoro vždy aspoň trochu nad 0°, takže dřívější
  // pořadí by čáru prakticky vždy úplně zakrylo). Vykreslená navrch,
  // vždy viditelná, aby šlo na první pohled srovnat "terén je nad/pod
  // tím, co by viděl pozorovatel na moři".
  const flatY = altToY(0);
  ctx.save();
  ctx.setLineDash([4, 5]);
  ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, flatY);
  ctx.lineTo(W, flatY);
  ctx.stroke();
  ctx.restore();
  ctx.font = "10px sans-serif";
  ctx.fillStyle = "rgba(255,255,255,0.6)";
  ctx.textAlign = "left";
  ctx.fillText("flat horizon", 8, flatY - 5);

  // --- Kompasové značky ---
  ctx.font = "11px sans-serif";
  ctx.fillStyle = "rgba(255,255,255,0.55)";
  ctx.textAlign = "center";
  for (let az = 0; az < 360; az += 10) {
    const x = azToX(az);
    if (x < 0 || x > W) continue;
    const isCardinal = az % 90 === 0;
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.beginPath();
    ctx.moveTo(x, H - 1);
    ctx.lineTo(x, H - (isCardinal ? 16 : 8));
    ctx.stroke();
    if (isCardinal) {
      const label = { 0: "N", 90: "E", 180: "S", 270: "W" }[az];
      ctx.fillText(label, x, H - 20);
    }
  }

  // --- Sunset direction marker (dashed vertikální čára + popisek) -
  // ANALYTICKÁ hodnota (sunsetAz), barva nese stejnou sémantiku jako na
  // mapě/polar-chartu (červená = terén blokuje). ---
  if (sunX > -20 && sunX < W + 20) {
    ctx.strokeStyle = obstructed ? "#ef5b60" : "#4ecf8a";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(sunX, 0);
    ctx.lineTo(sunX, H);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.font = "20px sans-serif";
    ctx.fillStyle = "#ffe1b3";
    ctx.textAlign = "center";
    ctx.fillText("🌅", sunX, 34);
    ctx.font = "12px sans-serif";
    ctx.fillStyle = "#e8edf2";
    ctx.fillText(`sunset ${sunsetAz.toFixed(0)}°`, sunX, 52);
  }
}

function initPanoramaInteraction() {
  const wrap = document.querySelector(".panorama-wrap");
  const canvas = document.getElementById("panorama-canvas");

  const onDown = (clientX) => {
    panoramaDragging = true;
    panoramaDragStartX = clientX;
    panoramaDragStartCenter = panoramaViewCenter;
  };
  const onMove = (clientX) => {
    if (!panoramaDragging) return;
    const deltaX = clientX - panoramaDragStartX;
    const deltaDeg = (deltaX / canvas.clientWidth) * PANORAMA_FOV_DEG;
    panoramaViewCenter = normalizeAzimuth(panoramaDragStartCenter - deltaDeg);
    drawPanoramaFrame();
  };
  const onUp = () => { panoramaDragging = false; };

  wrap.addEventListener("pointerdown", (e) => {
    // AUDIT NÁLEZ (Fáze 7 validace): `setPointerCapture` na `wrap` při
    // stisknutí i uvnitř vnořeného tlačítka Recenter potlačovalo jeho
    // nativní "click" - tlačítko pak vypadalo mrtvé. Táhnout jde jen ze
    // samotného canvasu, ne z ovládacích prvků nad ním.
    if (e.target.closest("#panorama-recenter")) return;
    onDown(e.clientX);
    wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener("pointermove", (e) => onMove(e.clientX));
  wrap.addEventListener("pointerup", onUp);
  wrap.addEventListener("pointercancel", onUp);

  document.getElementById("panorama-recenter").addEventListener("click", () => {
    if (!panoramaResult) return;
    panoramaViewCenter = panoramaResult.solar.sunset_azimuth_deg;
    drawPanoramaFrame();
  });
}
initPanoramaInteraction();

// =====================================================================
// OTHER SUNSETS (Fáze 7, zadání bod 11/20) - ukázkové karty z existujících
// TEST_LOCATIONS (app/main.py) - VÝSLOVNĚ development/test data, ne
// výsledek automatického vyhledávání okolních míst (to je Fáze budoucí
// "candidate search", tady NEIMPLEMENTOVÁNO). Každá karta volá STEJNÉ
// /api/analyze jako zbytek aplikace - žádná duplicitní logika.
// =====================================================================
async function loadOtherSunsets() {
  const grid = document.getElementById("other-sunsets-grid");
  const others = [];
  for (const opt of testLocationSelect.options) {
    if (!opt.value) continue;
    const [lat, lon] = opt.value.split(",").map(Number);
    if (lat === currentLat && lon === currentLon) continue;
    others.push({ lat, lon, name: opt.textContent });
  }

  grid.innerHTML = "";
  const cardEls = others.map((loc) => {
    const card = document.createElement("div");
    card.className = "sunset-card tap-tile is-loading";
    card.innerHTML = `
      <div class="card-icon">${typeIconHtml(loc.type)}</div>
      <h3>${loc.name}</h3>
      <div class="card-score">${coronaBloomSvg(null, 64)}</div>
      <div class="card-label">Načítám…</div>
      <div class="card-meta"></div>
      <button type="button">Prozkoumat</button>
    `;
    grid.appendChild(card);
    const btn = card.querySelector("button");
    btn.addEventListener("click", () => {
      selectLocation(loc.lat, loc.lon, { flyTo: false, resetDropdown: false });
      document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return card;
  });

  // AUDIT NÁLEZ (Fáze 7 validace, nalezeno skutečným browser testem):
  // fetch všech "Other Sunsets" karet NAJEDNOU (Promise.all styl) posílal
  // až 4 souběžné /api/analyze požadavky při KAŽDÉM načtení stránky -
  // terénní výpočet je CPU-náročný (horizon profil se pro konkrétní bod
  // nekešuje, jen podkladová DEM dlaždice), takže souběžné požadavky si
  // navzájem konkurovaly o CPU a jeden běžný klik uživatele na mapu pak
  // čekal frontou desítky sekund za nimi. Řešení: karty se donačítají
  // POSTUPNĚ (jedna po druhé), ne najednou - nezvyšuje to zbytečně
  // souběžné zatížení backendu vedle uživatelovy vlastní interakce.
  for (let i = 0; i < others.length; i++) {
    const loc = others[i];
    const card = cardEls[i];
    try {
      const r = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: loc.lat, lon: loc.lon, date: dateInput.value, timezone: tzInput.value }),
      });
      const data = await r.json();
      card.classList.remove("is-loading");
      if (!r.ok) {
        card.querySelector(".card-label").textContent = "Data nedostupná";
        continue;
      }
      const time = new Date(data.solar.astronomical_sunset).toLocaleTimeString("cs-CZ", {
        timeZone: tzInput.value, hour: "2-digit", minute: "2-digit",
      });
      card.querySelector(".card-score").innerHTML = coronaBloomSvg(data.rating.overall_0_100, 64);
      card.style.setProperty("--card-accent", coronaBloomAccentColor(data.rating.overall_0_100));
      card.querySelector(".card-label").textContent = data.rating.label;
      card.querySelector(".card-meta").textContent = `${time} · ${data.location.elevation_m.toFixed(0)} m`;
    } catch {
      card.classList.remove("is-loading");
      card.querySelector(".card-label").textContent = "Chyba spojení";
    }
  }
}

// --- Polární graf 360° terénního horizontu (Fáze 5, beze změny logiky) -
// technický analytický graf, teď schovaný v "Technical details" - hlavní
// interaktivní vizualizace pro běžného uživatele je panorama výše. ---
function drawPolarChart(result) {
  const canvas = document.getElementById("polar-chart");
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  const LEGEND_H = 84;
  const cx = W / 2;
  const cy = (H - LEGEND_H) / 2;
  const R = Math.min(W, H - LEGEND_H) / 2 - 40;

  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0d1218";
  ctx.fillRect(0, 0, W, H);

  const azimuths = result.terrain.azimuths_deg;
  const altitudes = result.terrain.horizon_altitudes_deg;

  const validAlts = altitudes.filter((a) => a !== null && a !== undefined);
  let dataMin = Math.min(0, ...validAlts);
  let dataMax = Math.max(0, ...validAlts);
  if (dataMax - dataMin < 20) {
    const mid = (dataMax + dataMin) / 2;
    dataMin = mid - 10;
    dataMax = mid + 10;
  }
  const pad = (dataMax - dataMin) * 0.12;
  const scaleMin = dataMin - pad;
  const scaleMax = dataMax + pad;

  const altToRadius = (altDeg) => {
    const alt = altDeg === null || altDeg === undefined ? 0 : altDeg;
    const frac = (alt - scaleMin) / (scaleMax - scaleMin);
    return R * (1 - Math.min(Math.max(frac, 0), 1));
  };
  const toXY = (azimuthDeg, radius) => {
    const rad = (azimuthDeg * Math.PI) / 180;
    return [cx + radius * Math.sin(rad), cy - radius * Math.cos(rad)];
  };

  const step = niceStep((scaleMax - scaleMin) / 4);
  const ringValues = new Set([0]);
  for (let v = Math.ceil(scaleMin / step) * step; v <= scaleMax; v += step) {
    ringValues.add(Math.round(v * 10) / 10);
  }
  ctx.font = "11px sans-serif";
  ctx.textAlign = "center";
  for (const altRing of ringValues) {
    const r = altToRadius(altRing);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    if (altRing === 0) {
      ctx.strokeStyle = "#4a5568";
      ctx.setLineDash([5, 3]);
      ctx.lineWidth = 1.5;
    } else {
      ctx.strokeStyle = "#232b34";
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
    }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = altRing === 0 ? "#c5cdd6" : "#8b98a5";
    ctx.fillText(`${altRing}°`, cx + 4, cy - r - 2);
  }
  for (const [label, az] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
    const [x, y] = toXY(az, R + 16);
    ctx.beginPath();
    const [x1, y1] = toXY(az, R);
    ctx.moveTo(cx, cy);
    ctx.lineTo(x1, y1);
    ctx.strokeStyle = "#20272f";
    ctx.stroke();
    ctx.fillStyle = "#e8edf2";
    ctx.fillText(label, x, y + 4);
  }

  ctx.beginPath();
  for (let i = 0; i < azimuths.length; i++) {
    const [x, y] = toXY(azimuths[i], altToRadius(altitudes[i]));
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fillStyle = "rgba(139, 90, 43, 0.35)";
  ctx.fill();
  ctx.strokeStyle = "#c98a3f";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  const sunsetAz = result.solar.sunset_azimuth_deg;
  const [sx, sy] = toXY(sunsetAz, R);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(sx, sy);
  ctx.strokeStyle = result.visibility.terrain_obstruction ? "#e5484d" : "#3ecf6e";
  ctx.lineWidth = 2.5;
  ctx.stroke();

  const [hx, hy] = toXY(sunsetAz, altToRadius(result.solar.sunset_terrain_horizon_deg));
  ctx.beginPath();
  ctx.arc(hx, hy, 5, 0, 2 * Math.PI);
  ctx.fillStyle = "#f4a742";
  ctx.fill();
  ctx.strokeStyle = "#1a1300";
  ctx.stroke();

  if (result.solar.sunrise_azimuth_deg !== null && result.solar.sunrise_azimuth_deg !== undefined) {
    const [rx, ry] = toXY(result.solar.sunrise_azimuth_deg, R);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(rx, ry);
    ctx.strokeStyle = "#f4d78244";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.textAlign = "left";
  ctx.font = "12px sans-serif";
  const legendY = H - LEGEND_H + 14;
  const rowH = 18;

  ctx.fillStyle = "#c98a3f";
  ctx.fillRect(8, legendY - 8, 14, 4);
  ctx.fillStyle = "#e8edf2";
  ctx.fillText("terénní horizont (celý 360° profil)", 28, legendY - 3);

  ctx.strokeStyle = result.visibility.terrain_obstruction ? "#e5484d" : "#3ecf6e";
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  ctx.moveTo(8, legendY + rowH - 4);
  ctx.lineTo(22, legendY + rowH - 4);
  ctx.stroke();
  ctx.fillStyle = "#e8edf2";
  ctx.fillText("azimut západu Slunce (červená = terén blokuje)", 28, legendY + rowH);

  ctx.strokeStyle = "#f4d782";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(8, legendY + 2 * rowH - 4);
  ctx.lineTo(22, legendY + 2 * rowH - 4);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#e8edf2";
  ctx.fillText("azimut východu Slunce", 28, legendY + 2 * rowH);

  ctx.strokeStyle = "#4a5568";
  ctx.setLineDash([5, 3]);
  ctx.beginPath();
  ctx.moveTo(8, legendY + 3 * rowH - 4);
  ctx.lineTo(22, legendY + 3 * rowH - 4);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#e8edf2";
  ctx.fillText("0° = astronomický horizont (bez terénu)", 28, legendY + 3 * rowH);
}

function niceStep(roughStep) {
  if (roughStep <= 0) return 10;
  const exp = Math.floor(Math.log10(roughStep));
  const base = Math.pow(10, exp);
  const frac = roughStep / base;
  let niceFrac;
  if (frac < 1.5) niceFrac = 1;
  else if (frac < 3) niceFrac = 2;
  else if (frac < 7) niceFrac = 5;
  else niceFrac = 10;
  return niceFrac * base;
}

// --- Fáze 7, zadání bod 3/4 - hero i "Best Sunset" musí mít reálný
// obsah HNED po načtení, ne až po prvním kliknutí. ---
//
// AUDIT NÁLEZ (Fáze 7 validace, nalezeno skutečným browser testem):
// i se sekvenčním donačítáním (viz loadOtherSunsets výše) běžely "Other
// Sunsets" fetche na pozadí HNED po načtení stránky, takže uživatel, který
// okamžitě klikl do mapy, soutěžil o CPU s ještě běžícím karta-fetchem -
// terénní výpočet je CPU-náročný a nekešuje se per-bod, takže souběžnost
// i jen 2 požadavků citelně natahovala odezvu. Řešení: karty se donačtou,
// až sekce "Other Sunsets" opravdu vstoupí do viewportu (lazy load).
//
// DRUHÝ NÁLEZ při implementaci tohohle fixu: `IntersectionObserver`
// nastavený HNED při načtení skriptu (dřív, než `renderAll()` poprvé
// odkryje `#best-sunset`/`#panorama-section`) vyhodnocuje polohu
// `#other-sunsets` vůči TEHDEJŠÍMU (skoro prázdnému, `hidden` sekce mají
// nulovou výšku) rozvržení stránky - v něm sekce vychází HNED pod hero,
// takže observer "lazy" fetch spustil úplně stejně okamžitě jako dřív.
// Observer se proto připojuje až PO prvním úspěšném vykreslení výsledku,
// kdy už stránka má svou skutečnou výšku.
function attachLazyOtherSunsets() {
  let otherSunsetsLoaded = false;
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting) && !otherSunsetsLoaded) {
        otherSunsetsLoaded = true;
        loadOtherSunsets();
        observer.disconnect();
      }
    },
    { rootMargin: "200px" },
  );
  observer.observe(document.getElementById("other-sunsets"));
}

// =====================================================================
// CANDIDATE DISCOVERY (Fáze 8, zadání bod 15-19) - "Find the best sunset
// in Czechia", zatím jen pro Beskydy. Frontend čte PŘEDPOČÍTANÝ ranked
// seznam z GET /api/candidates (app/candidate_batch.py +
// scripts/build_candidate_dataset.py) - žádná terrain/solar geometrie
// se tu nepočítá, jen zobrazení už hotového výsledku algoritmu.
// =====================================================================
let beskydyCandidates = null;

async function fetchCandidates(region) {
  try {
    const r = await fetch(`/api/candidates?region=${region}`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Fáze 9 (zadání bod 11/12) - vlastní vizuální jazyk podle typu místa:
// vrchol = špička, vyhlídka = diamant, jinak kruh (viz .candidate-marker
// CSS). `activeTypeFilter` řídí filtr chips (zadání bod 21).
let activeTypeFilter = "all";

// Milestone "Complete Product Build" - výsledky z `/api/find_sunset`
// mají nový kompozitní `sunset_score`/`sunset_score_label`; starší
// statický dataset (`/api/candidates`, výchozí demo před prvním
// hledáním) má jen terrain-only `rating`/`rating_label` - zobrazí se
// ten, žádná fabrikace chybějícího kompozitu.
function displayScore(c) {
  if (c.sunset_score !== undefined && c.sunset_score !== null) {
    return { score: c.sunset_score, label: c.sunset_score_label };
  }
  return { score: c.rating, label: c.rating_label };
}

// Milník "Postupné vylepšení" (Fáze 4 - ikony typů vyhlídek) - STEJNÁ
// množina ikon jako existující `.candidate-marker.type-*` tvary na
// mapě (peak/viewpoint/tower/castle), jen jako `<span class="icon">`
// pro karty/seznamy. `.icon-hill` je nová (viz style.css). Typ, který
// katalog skutečně nemá (ověřeno přímo v datech - jen těchto 5
// hodnot existuje), dostane neutrální `.icon-pin`, ne vymyšlený tvar.
const TYPE_ICON_CLASS = { peak: "icon-peak", viewpoint: "icon-viewpoint", tower: "icon-tower", castle: "icon-castle", hill: "icon-hill" };
function typeIconHtml(type) {
  return `<span class="icon ${TYPE_ICON_CLASS[type] || "icon-pin"}" aria-hidden="true"></span>`;
}

// Zpětná vazba (živé použití) - "ukázat víc na mapě" - mapa dřív
// vykreslovala jen `data.results` (max `MAX_TERRAIN_ANALYSIS_CANDIDATES`
// = 15 opravdu vyhodnocených míst), i když v okolí reálně bylo mnohem
// víc kandidátů (stejná data, co už jde vidět v "Other sunsets" gridu
// jako čekající karty) - mapa tak vypadala "chudší", než realita v
// okolí skutečně je. `waitingCandidates` (= `data.other_candidates`)
// se teď vykreslí jako tlumené tečky BEZE SKÓRE (žádné vymyšlené číslo -
// stejné pravidlo jako `buildWaitingSunsetCard`), klik na ně dělá
// STEJNOU věc jako tlačítko "Prozkoumat" na jejich kartě (živá analýza
// toho bodu), žádná nová logika navíc.
function renderCandidateMarkers(candidates, bestId, waitingCandidates = []) {
  candidateMarkers.forEach((m) => m.remove());
  candidateMarkers = [];
  for (const c of waitingCandidates) {
    const wrap = document.createElement("div");
    wrap.className = "candidate-marker-wrap";
    wrap.dataset.type = c.type;
    wrap.dataset.best = "0";
    wrap.dataset.waiting = "1";

    const dot = document.createElement("div");
    dot.className = "candidate-marker is-waiting";
    wrap.appendChild(dot);
    wrap.title = `${c.name} - ${WAITING_STATUS_LABELS[c.status] || c.status}`;
    wrap.addEventListener("click", (e) => {
      e.stopPropagation();
      selectLocation(c.lat, c.lon, { flyTo: true, resetDropdown: true, slug: c.slug || null });
      document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
    });

    const marker = new maplibregl.Marker({ element: wrap }).setLngLat([c.lon, c.lat]).addTo(map);
    candidateMarkers.push(marker);
  }
  for (const c of candidates) {
    const { score, label } = displayScore(c);
    // Fáze 10 (zadání bod 7/9) - vlastní hover tooltip (název + rating)
    // MÍSTO pomalého/nestylovaného nativního `title` - "wrap" nese hover
    // stav, `.candidate-marker` samo nese jen tvar/barvu/rotaci, aby se
    // tooltip u diamantových (`type-viewpoint`, rotate 45°) markerů
    // nezkosil spolu s markerem.
    const wrap = document.createElement("div");
    wrap.className = "candidate-marker-wrap";

    wrap.dataset.type = c.type;
    wrap.dataset.best = c.id === bestId ? "1" : "0";

    const typeClass = { peak: "type-peak", viewpoint: "type-viewpoint", tower: "type-tower", castle: "type-castle" }[c.type] || "";
    const marker_el = document.createElement("div");
    // Milník 12 ("Corona Bloom") - zadání bod 3: "u mapových markerů
    // nepřenášej plnohodnotný kruhový ukazatel... použij přiměřenou
    // variantu stejného vizuálního systému" - marker je příliš malý na
    // vícevrstvý meter, takže dostane jen JEDNU reprezentativní barvu
    // přímo z `CORONA_BLOOM_TIERS` (`coronaBloomAccentColor()`, stejný
    // zdroj pravdy jako velký meter), místo staré open/delayed/blocked
    // trojbarevné škály.
    marker_el.className = `candidate-marker ${typeClass}` + (c.id === bestId ? " is-best" : "");
    marker_el.style.background = coronaBloomAccentColor(score);
    marker_el.innerHTML = `<span>${score}</span>`;

    // Fáze 10 (druhé kolo, zadání bod 9) - "malý floating detail" místo
    // jen jméno+rating: typ, elevace, rating, skutečný sunset směr
    // (`sunset_azimuth_deg` z předpočítaného datasetu - reálná data,
    // stejná funkce `azimuthToCompass()` jako v hero statistikách) a
    // textová výzva k akci (klik na marker samotný = "Explore", žádné
    // vnořené tlačítko v malém popoveru).
    const typeLabel = { peak: "Peak", hill: "Hill", viewpoint: "Viewpoint", tower: "Tower", castle: "Castle" }[c.type] || "Místo";
    const sunsetTypeBit = c.sunset_type && c.sunset_type.key ? ` · ${c.sunset_type.label}` : "";
    const tooltip = document.createElement("div");
    tooltip.className = "marker-tooltip";
    tooltip.innerHTML = `
      <div class="marker-tooltip-type">${typeLabel}</div>
      <strong>${c.name}</strong>
      <span class="marker-tooltip-elevation">${c.measured_elevation_m.toFixed(0)} m</span>
      <div class="marker-tooltip-rating">${score}<span>/100</span></div>
      <span class="marker-tooltip-sunset">Sunset ${azimuthToCompass(c.sunset_azimuth_deg)} · ${label}${sunsetTypeBit}</span>
      <span class="marker-tooltip-cta">Explore →</span>
    `;

    wrap.appendChild(tooltip);
    wrap.appendChild(marker_el);
    wrap.addEventListener("click", (e) => {
      e.stopPropagation(); // ať to nepočítá jako klik na "prázdnou mapu"
      selectLocation(c.lat, c.lon, { flyTo: true, resetDropdown: true, slug: c.slug || null });
      document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
    });

    const marker = new maplibregl.Marker({ element: wrap }).setLngLat([c.lon, c.lat]).addTo(map);
    candidateMarkers.push(marker);
  }
  applyTypeFilter(activeTypeFilter);
}

function applyTypeFilter(filter) {
  activeTypeFilter = filter;
  for (const marker of candidateMarkers) {
    const el = marker.getElement(); // .candidate-marker-wrap
    const matches =
      filter === "all" ||
      (filter === "best" && el.dataset.best === "1") ||
      el.dataset.type === filter;
    el.style.display = matches ? "" : "none";
  }
}

// Milník 12 ("Corona Bloom") - dřív téměř identická duplikace
// `buildReadySunsetCard()` (vlastní kopie markupu/barev) - zadání bod 4
// "nevytvářej několik odlišných implementací stejného ukazatele", tak
// se teď jen volá STEJNÁ karta-builder funkce (žádný nový vizuál navíc,
// jen odstraněná duplikace).
function renderOtherSunsetsFromCandidates(candidates, bestId) {
  const grid = document.getElementById("other-sunsets-grid");
  grid.innerHTML = "";
  for (const c of candidates.filter((cand) => cand.id !== bestId)) {
    grid.appendChild(buildReadySunsetCard(c));
  }
}

const TONIGHT_PREVIEW_COUNT = 3;

// Milník 12 (zadání bod 1/3 - "náhled Tonight" dostává Corona Bloom) -
// malý náhled špičky skutečně analyzovaných míst přímo pod dnešní
// předpovědí. STEJNÁ data jako Best Places grid (`candidates`, žádné
// nové API volání/vymyšlené skóre) - jen prvních pár kandidátů, hero
// místo se vynechá (to je vidět celé v #best-sunset níž).
function renderTonightPreview(candidates, bestId) {
  const wrap = document.getElementById("tonight-preview");
  const row = document.getElementById("tonight-preview-row");
  const top = candidates.filter((c) => c.id !== bestId).slice(0, TONIGHT_PREVIEW_COUNT);
  if (top.length === 0) {
    wrap.hidden = true;
    return;
  }
  row.innerHTML = "";
  for (const c of top) {
    const { score } = displayScore(c);
    const tile = document.createElement("div");
    tile.className = "tonight-preview-tile tap-tile";
    tile.innerHTML = `
      ${coronaBloomSvg(score, 56)}
      <div class="tonight-preview-tile-name">${typeIconHtml(c.type)}<span>${c.name}</span></div>
    `;
    tile.addEventListener("click", () => {
      selectLocation(c.lat, c.lon, { flyTo: true, resetDropdown: true, slug: c.slug || null });
      document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    row.appendChild(tile);
  }
  wrap.hidden = false;
}

// ============================= FÁZE 11 - "Where? / Search within /
// Find my sunset" =============================
// Tok (zadání bod H): geocode "Where" text -> /api/search (filtruje
// PŘEDPOČÍTANÉ kandidáty ze VŠECH registrovaných regionů podle
// haversine vzdálenosti a řadí podle jejich předpočítaného ratingu,
// ŽÁDNÁ terrain analýza naživo, viz app/main.py) -> vítěz se
// dopočítá naživo přes STÁVAJÍCÍ `selectLocation()`/`runAnalysis()`
// (= vždy AKTUÁLNÍ datum, zadání bod P), zbytek výsledků a mapa se
// vykreslí ze search response. Žádný nový engine, jen orchestrace nad
// existujícím tokem (zadání "nepřepisuj fungující engine").
let searchCenter = { lat: currentLat, lon: currentLon };
let searchRadiusKm = parseFloat(searchRadiusInput.value);

function setSearchStatus(text) {
  if (!text) {
    searchStatusEl.hidden = true;
    searchStatusEl.textContent = "";
    return;
  }
  searchStatusEl.hidden = false;
  searchStatusEl.textContent = text;
}

function setSearchError(text) {
  if (text) setSearchStatus(null); // chyba a "právě hledám..." stav se nikdy nezobrazují zároveň
  if (!text) {
    searchErrorEl.hidden = true;
    searchErrorEl.textContent = "";
    return;
  }
  searchErrorEl.hidden = false;
  searchErrorEl.textContent = text;
}

searchRadiusInput.addEventListener("input", () => {
  searchRadiusKm = parseFloat(searchRadiusInput.value);
  searchRadiusValueEl.textContent = `${searchRadiusKm} km`;
  // Fáze 11 (zadání bod D/E) - "nesmí při každém pohybu spouštět terrain
  // analysis" - jen přepočet a překreslení GeoJSON kruhu na klientovi,
  // ŽÁDNÝ network request.
  whenMapReady(() => drawSearchCircle(searchCenter.lat, searchCenter.lon, searchRadiusKm));
});

// Milestone "Persistent Terrain Cache + Background Precomputation" -
// /api/find_sunset je teď SYNCHRONNÍ a rychlý (cache hit = jen levná
// dynamická dopočítávka, cache miss = zařadí se na pozadí a request na
// něj NEČEKÁ, viz app/main.py). Milestone "Immediate Full Results"
// (viz `runFindSunsetWithRetries()`/`scheduleFindSunsetPoll()` níže)
// nahradila dřívější BLOKUJÍCÍ retry smyčku před prvním vykreslením -
// celá nalezená množina se teď zobrazí hned po první odpovědi, další
// doplňování běží na pozadí.
async function fetchFindSunset(lat, lon, radiusKm) {
  const res = await fetch("/api/find_sunset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lon, radius_km: radiusKm, timezone: tzInput.value, preferences: collectPreferences() }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Hledání se nezdařilo.");
  return data;
}

async function searchFromCenter(geo) {
  searchCenter = { lat: geo.lat, lon: geo.lon };
  map.flyTo({ center: [geo.lon, geo.lat], zoom: 10.5 });
  whenMapReady(() => drawSearchCircle(searchCenter.lat, searchCenter.lon, searchRadiusKm));
  await runFindSunsetWithRetries(geo, searchRadiusKm);
}

async function findMySunset() {
  const query = searchWhereInput.value.trim();
  setSearchError(null);
  if (!query) {
    setSearchError("Zvol místo, kde chceš hledat.");
    return;
  }

  findSunsetBtn.disabled = true;
  try {
    setSearchStatus("Hledám lokalitu…");
    let geo;
    try {
      const geoRes = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`);
      geo = await geoRes.json();
      if (!geoRes.ok) {
        setSearchError(geo.error || "Místo se nepodařilo najít.");
        clearAllFindSunsetResults();
        return;
      }
    } catch (err) {
      setSearchError("Nepodařilo se spojit se serverem: " + err.message);
      clearAllFindSunsetResults();
      return;
    }

    await searchFromCenter(geo);
  } finally {
    findSunsetBtn.disabled = false;
  }
}

// Milestone "Complete Product Build" (pokračování, uživatelský feedback
// z živého použití) - "Use my current location" jako PRVNÍ volba před
// psaním názvu místa (prohlížečové Geolocation API, žádný nový server
// endpoint - poloha se nikam neposílá, jen se z ní rovnou spustí STEJNÉ
// `/api/find_sunset` hledání jako po geocodingu jména).
const useMyLocationBtn = document.getElementById("use-my-location-btn");
useMyLocationBtn.addEventListener("click", () => {
  setSearchError(null);
  if (!navigator.geolocation) {
    setSearchError("Tenhle prohlížeč nepodporuje zjištění polohy - zadej prosím místo ručně.");
    clearAllFindSunsetResults();
    return;
  }
  useMyLocationBtn.disabled = true;
  setSearchStatus("Zjišťuji tvoji polohu…");
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      try {
        await searchFromCenter({ lat: pos.coords.latitude, lon: pos.coords.longitude, display_name: "tvoje poloha" });
      } finally {
        useMyLocationBtn.disabled = false;
      }
    },
    (err) => {
      useMyLocationBtn.disabled = false;
      setSearchError("Nepodařilo se získat tvoji polohu (" + err.message + "). Zkus prosím zadat místo ručně.");
      clearAllFindSunsetResults();
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
  );
});

// Milestone "Sunset-first UI and Minimalist Map Redesign" - zadání bod
// 1A: "pokud žádné místo nemá platné hodnocení, nezobrazuj falešného
// vítěze". AUDIT NÁLEZ: dřív se při `found_count===0`/`ready_count===0`
// pro NOVÉ hledání jen zobrazila chybová hláška, ale PŘEDCHOZÍ výsledek
// (z initApp() demo lokality nebo z dřívějšího hledání) zůstal
// viditelný v #best-sunset/#panorama-section - uživatel mohl snadno
// nabyl dojem, že je to odpověď na JEHO dotaz. Teď se tyhle sekce pro
// aktuální (nevyhodnocené) hledání schovají, dokud nepřijde skutečný
// výsledek.
function hideStaleRecommendation() {
  document.getElementById("best-sunset").hidden = true;
  document.getElementById("panorama-section").hidden = true;
  document.getElementById("type-filter-chips").hidden = true;
  document.getElementById("hero-stat").hidden = true;
  document.getElementById("candidates-analyzed-note").hidden = true;
  document.getElementById("dem-quality-note").hidden = true;
}

// Milník "Ověření skutečného UI" - AUDIT NÁLEZ (reálný browser test,
// Playwright): hledání, které SELHALO dřív, než se vůbec stihlo zavolat
// `/api/find_sunset` (geocoding nenašel místo, nebo spojení se serverem
// selhalo úplně), vracelo jen chybovou hlášku - staré karty v `#other-
// sunsets-grid` i stará `#best-sunset` sekce z PŘEDCHOZÍHO úspěšného
// hledání zůstaly plně viditelné POD novou chybou. Jedna sdílená
// funkce pro VŠECHNA místa, kde hledání skončí bez výsledku.
function clearAllFindSunsetResults() {
  stopFindSunsetPolling();
  findSunsetSearchToken++; // zahoď i případný už běžící poll z předchozího hledání
  renderCandidateMarkers([], null);
  hideStaleRecommendation();
  document.getElementById("refresh-results-btn").hidden = true;
  document.getElementById("show-more-candidates-btn").hidden = true;
  document.getElementById("expand-other-sunsets-btn").hidden = true;
  document.getElementById("other-sunsets").hidden = true;
  document.getElementById("other-sunsets-grid").innerHTML = "";
}

// Milestone "Immediate Full Results" - AUDIT NÁLEZ (reprodukováno
// živě, hledání "Brno" @ 5 km): `found_count` 64, server zkusí
// nejbližších 15 (`to_analyze_count`, strop `MAX_TERRAIN_ANALYSIS_CANDIDATES`
// - ZÁMĚRNÝ, zachovaný), ale `TERRAIN_WORKER_CONCURRENCY=1` zpracovává
// frontu SEKVENČNĚ (desítky sekund/kandidát). PŘEDCHOZÍ oprava
// ("Visible Waiting Candidates") přidala `other_candidates` do API, ale
// frontend PŘED prvním vykreslením pořád blokoval na retry smyčce (až
// 40s), takže uživatel 40 sekund neviděl VŮBEC ŽÁDNÝ seznam, jen
// "hledám…" text. Nová architektura (zadání bod "odděl tři fáze"):
//   1. VYHLEDÁNÍ - jeden fetch, žádné podmiňování zobrazení hotovým skóre.
//   2. OKAMŽITÉ ZOBRAZENÍ - celá nalezená množina (results + other_candidates)
//      se vykreslí HNED po první odpovědi, žádné čekání na worker.
//   3. POSTUPNÉ DOPLŇOVÁNÍ - lehké opakované dotazování na pozadí (stejný
//      endpoint, žádná nová DEM zátěž - jen čtení stavu), dokud něco
//      čeká/se vyhodnocuje; aktualizuje JEN status/skóre karet, celý
//      seznam se nepřekresluje od nuly zbytečně. Zastaví se automaticky,
//      jakmile nic nečeká, nebo po rozumném počtu pokusů (pak zůstává
//      manuální "Zkontrolovat nové výsledky" jako záloha).
let lastSearchGeo = null;
let lastSearchRadiusKm = null;
let findSunsetSearchToken = 0; // zruší/ignoruje zpoždění z PŘEDCHOZÍHO hledání, pokud uživatel hledá znovu dřív
let findSunsetPollTimer = null;
let findSunsetHeroId = null; // vítěz se po prvním ustálení NEPŘEHAZUJE (ne rušivá změna "hlavní odpovědi" pod rukama uživatele)
let findSunsetHeroDetailLoaded = false; // `/api/analyze` detail pro hera se natáhne jen JEDNOU, ne při každé aktualizaci na pozadí

const FIND_SUNSET_POLL_INTERVAL_MS = 10000;
const FIND_SUNSET_MAX_POLL_ATTEMPTS = 60; // ~10 minut na pozadí, pak jen manuální "Zkontrolovat nové výsledky"

function stopFindSunsetPolling() {
  if (findSunsetPollTimer) {
    clearTimeout(findSunsetPollTimer);
    findSunsetPollTimer = null;
  }
}

async function runFindSunsetWithRetries(geo, radiusKm) {
  stopFindSunsetPolling();
  const myToken = ++findSunsetSearchToken;
  findSunsetHeroId = null;
  findSunsetHeroDetailLoaded = false;
  lastSearchGeo = geo;
  lastSearchRadiusKm = radiusKm;

  let data;
  try {
    setSearchStatus("Prohledávám katalog kandidátů…");
    data = await fetchFindSunset(geo.lat, geo.lon, radiusKm);
  } catch (err) {
    setSearchError("Nepodařilo se spojit se serverem: " + err.message);
    clearAllFindSunsetResults();
    return;
  }
  if (myToken !== findSunsetSearchToken) return; // mezitím začalo novější hledání

  if (data.found_count === 0) {
    setSearchError(`V okruhu ${radiusKm} km od "${geo.display_name}" nebyly nalezeny žádné vyhlídky v katalogu. Zkus větší radius nebo jiné místo.`);
    clearAllFindSunsetResults();
    return;
  }

  // Fáze 2 - OKAMŽITÉ zobrazení celé nalezené množiny, bez čekání na worker.
  await renderFindSunsetOutcome(data, geo, radiusKm);
  scheduleFindSunsetPoll(myToken, geo, radiusKm, 0);
}

// Fáze 3 - postupné doplňování na pozadí. Bezpečné opakované dotazování
// (ŽÁDNÝ nový enqueue navíc - `app/terrain_worker.py` dedup i tak
// zablokuje duplicitní práci, tohle jen ČTE aktuální stav) v rozumném
// intervalu (10s), ohraničené počtem pokusů - ne nekonečný polling, ne
// agresivní frekvence. `myToken` zajišťuje, že zpoždění z PŘEDCHOZÍHO
// hledání se nikdy nepromítne do aktuálně zobrazeného seznamu.
function scheduleFindSunsetPoll(myToken, geo, radiusKm, attempt) {
  if (myToken !== findSunsetSearchToken) return;
  findSunsetPollTimer = setTimeout(async () => {
    if (myToken !== findSunsetSearchToken) return;
    let data;
    try {
      data = await fetchFindSunset(geo.lat, geo.lon, radiusKm);
    } catch {
      return; // tichý neúspěch jednoho pokusu na pozadí - neukazuj chybu, příští pokus to zkusí znovu
    }
    if (myToken !== findSunsetSearchToken) return;
    await renderFindSunsetOutcome(data, geo, radiusKm, { isBackgroundUpdate: true });
    const stillPending = data.enqueued_count + data.pending_count > 0;
    if (stillPending && attempt + 1 < FIND_SUNSET_MAX_POLL_ATTEMPTS) {
      scheduleFindSunsetPoll(myToken, geo, radiusKm, attempt + 1);
    }
  }, FIND_SUNSET_POLL_INTERVAL_MS);
}

async function renderFindSunsetOutcome(data, geo, radiusKm, { isBackgroundUpdate = false } = {}) {
  const pendingTotal = data.enqueued_count + data.pending_count;
  const refreshBtn = document.getElementById("refresh-results-btn");
  refreshBtn.hidden = pendingTotal === 0;

  // Zadání bod "žádné místo se nesmí tvářit jako špatné jen proto, že
  // ještě nemá skóre" + "vítěz se po ustálení nepřehazuje" - jakmile
  // existuje `results[0]`, stane se herem a zůstává jím i přes další
  // aktualizace na pozadí (jen se mu případně zpřesní skóre, pokud by
  // se teoreticky změnilo - reálně se nemění, terén je cachovaný).
  if (data.results.length > 0 && findSunsetHeroId === null) {
    findSunsetHeroId = data.results[0].id;
  }
  const best = data.results.find((r) => r.id === findSunsetHeroId) || data.results[0] || null;

  if (!best) {
    // Zadání bod "pokud žádné místo nemá platné hodnocení, nezobrazuj
    // falešného vítěze... navrhni jasný stav bez výsledku". Celá
    // nalezená množina (other_candidates) se i tak zobrazí jako čekající.
    //
    // Zpětná vazba (terénní kvalitní práh, MIN_DISPLAYED_TERRAIN_RATING
    // v app/main.py) - "nulové výsledky" už NEMUSÍ znamenat "ještě se
    // počítá" - `below_quality_floor_count > 0` + nic nečeká na pozadí
    // znamená "už je hotovo, ale žádné místo nemělo dost dobrý terén".
    // Stará hláška by tohle tvrdila falešně ("vyhodnocují se na
    // pozadí"), i když appka už skončila.
    const stillPending = data.enqueued_count + data.pending_count > 0;
    const allBelowFloor = !stillPending && (data.below_quality_floor_count || 0) > 0;
    const message = allBelowFloor
      ? `${data.found_count} vyhlídek nalezeno v okruhu ${radiusKm} km - žádná z nich ale nemá dost otevřený výhled ` +
        `na západ (zkus prosím větší okruh).`
      : `${data.found_count} vyhlídek nalezeno v okruhu ${radiusKm} km - zatím žádná nemá hotové hodnocení ` +
        `(právě se vyhodnocují na pozadí, viz seznam níže).`;
    if (!isBackgroundUpdate) {
      setSearchStatus(null);
      setSearchError(message);
    }
    hideStaleRecommendation();
    document.getElementById("other-sunsets").hidden = false;
    document.getElementById("other-sunsets-hint").textContent = message;
    renderFindSunsetGrid([], data.other_candidates, null);
    updateShowMoreButton(data, geo, radiusKm);
    whenMapReady(() => renderCandidateMarkers([], null));
    return;
  }

  registerLocationNames(data.results);

  // Detail hera (`/api/analyze` přes `selectLocation()`) se natáhne jen
  // JEDNOU - buď hned (hero existoval od první odpovědi), nebo poprvé,
  // až se na pozadí objeví (žádný ready kandidát na začátku nebyl).
  // Další aktualizace na pozadí už jen přepočítají/přeskládají seznam
  // ostatních míst, hero samotný se znovu nenatahuje.
  if (!findSunsetHeroDetailLoaded) {
    if (!isBackgroundUpdate) setSearchStatus("Načítám detail nejlepšího vyhodnoceného místa…");
    await selectLocation(best.lat, best.lon, { flyTo: false, resetDropdown: true, slug: best.slug || null });
    findSunsetHeroDetailLoaded = true;
  }

  whenMapReady(() => renderCandidateMarkers(data.results, best.id, data.other_candidates));
  renderFindSunsetGrid(data.results, data.other_candidates, best.id);
  updateShowMoreButton(data, geo, radiusKm);
  document.getElementById("type-filter-chips").hidden = false;

  // Zadání bod 12 - "found" (celý katalog v radiusu), "ready"
  // (skutečně dostupný terén PRÁVĚ TEĎ) a "připravuje se na pozadí"
  // jsou TŘI různá, upřímná čísla - nikdy netvrdíme, že je hotovo víc,
  // než skutečně je.
  //
  // Milestone "Trustworthy Results with Partial Cache" - "tohle je
  // vítěz" byl dřív NEPODMÍNĚNÝ text, i když se analyzovalo jen
  // nejbližších `to_analyze_count` kandidátů (strop
  // MAX_TERRAIN_ANALYSIS_CANDIDATES v app/main.py) a i když část z nich
  // ještě čekala na dopočet - tvrdilo to "nejlepší v okolí", i když
  // reálně to bylo jen "nejlepší z toho mála, co bylo zrovna hotové".
  // `data.fully_covered` (nová hodnota z API, spočítaná ze stejných
  // čísel, žádný nový vymyšlený práh) teď rozhoduje mezi upřímným
  // "nejlepší z vyhodnocených míst" a skutečným "vítězem" jen tehdy,
  // když bylo opravdu vyhodnoceno úplně vše v radiusu.
  const pendingNote = pendingTotal > 0 ? ` · ${pendingTotal} dalších se vyhodnocuje/čeká na pozadí` : "";
  const failedNote = data.previously_failed_count > 0
    ? ` · u ${data.previously_failed_count} míst se poslední pokus o terénní data nezdařil (zkouší se znovu)`
    : "";
  const winnerPhrase = data.fully_covered ? "tohle je vítěz" : "nejlepší z vyhodnocených míst (zbytek radiusu ještě čeká na vyhodnocení)";
  const note = document.getElementById("candidates-analyzed-note");
  note.textContent = `🔎 ${data.found_count} vyhlídek nalezeno v okruhu ${radiusKm} km od „${geo.display_name.split(",")[0]}“ · ${data.ready_count} má hotový terén${pendingNote}${failedNote} — ${winnerPhrase}.`;
  note.hidden = false;
  document.querySelector("#best-sunset .dev-note").hidden = true;
  document.getElementById("other-sunsets").hidden = false;
  document.getElementById("other-sunsets-hint").textContent =
    `${data.found_count} vyhlídek nalezeno v okruhu ${radiusKm} km - ${data.ready_count} má hotový terén` +
    (pendingTotal > 0 ? `, ${pendingTotal} se ještě vyhodnocuje/čeká (viz stavy níže).` : ".");

  const heroStat = document.getElementById("hero-stat");
  heroStat.textContent = `🔎 ${data.found_count} nalezeno · ${data.ready_count} připraveno (do ${radiusKm} km)`;
  heroStat.hidden = false;

  if (!isBackgroundUpdate) {
    document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  setSearchStatus(null);
}

// Milestone "Immediate Full Results" - manuální záloha nad automatickým
// pollingem (zadání bod "existovat možnost zobrazit další"/"aktualizovat
// bez nutnosti hledat znovu") - užitečná zejména po vypršení
// `FIND_SUNSET_MAX_POLL_ATTEMPTS`.
document.getElementById("refresh-results-btn").addEventListener("click", async () => {
  if (!lastSearchGeo) return;
  const btn = document.getElementById("refresh-results-btn");
  btn.disabled = true;
  try {
    setSearchStatus("Kontroluji nové výsledky…");
    let data;
    try {
      data = await fetchFindSunset(lastSearchGeo.lat, lastSearchGeo.lon, lastSearchRadiusKm);
    } catch (err) {
      setSearchError("Nepodařilo se spojit se serverem: " + err.message);
      return;
    }
    await renderFindSunsetOutcome(data, lastSearchGeo, lastSearchRadiusKm);
  } finally {
    btn.disabled = false;
  }
});

// Milestone "Immediate Full Results" - JEDEN vykreslovací průchod pro
// CELOU nalezenou množinu (ready + vyhodnocuje se/čeká/selhalo/nad
// stropem), vždy úplný rebuild gridu (jednoduché, bezpečné proti
// duplicitám - žádné ruční DOM-diffování karet, "zbytečné" náklady na
// 15-200 malých DOM uzlů jsou v praxi nulové). `heroId` se z gridu
// vynechá (zobrazuje se v hlavní #best-sunset sekci samostatně).
const WAITING_STATUS_LABELS = {
  evaluating: "Vyhodnocuje se",
  waiting: "Čeká na vyhodnocení",
  failed: "Analýza se nezdařila (zkouší se znovu)",
  not_analyzed: "Zatím nevyhodnoceno (nad stropem analýzy tohoto hledání)",
};

// Milník "Ověření skutečného UI" - AUDIT NÁLEZ (reálný browser test):
// u hustě pokrytých oblastí (Brno @ 5 km -> 59 karet) sekce "Other
// sunsets" natáhla stránku na přes 7000 px - přesně "zahlcení hlavní
// části stránky", kterému měl zadání bod 2 zabránit. Karty se teď
// zobrazí jen do rozumného počtu najednou, zbytek za jedním kliknutím
// (čistě klientské - stejná už stažená data, žádný nový request).
// Zpětná vazba (živé použití) - "myslím jen 5 míst a zbytek bude
// schovaných" (dřív 15 - moje první čtení "dalších 5 tipů" bylo
// špatně, mělo to znamenat výchozí počet, ne velikost dávky).
const COLLAPSED_OTHER_SUNSETS_LIMIT = 5;
// Zpětná vazba (živé použití) - "dejme třeba dalších 5 tipů" - dřív
// "Zobrazit všechna ostatní místa" odkrylo VŠECHNO najednou (klidně
// desítky karet, zpátky stejný problém, který `COLLAPSED_OTHER_SUNSETS_LIMIT`
// měl řešit). Tlačítko teď odkrývá po menších dávkách.
const OTHER_SUNSETS_PAGE_SIZE = 5;

function renderFindSunsetGrid(readyResults, otherCandidates, heroId) {
  const grid = document.getElementById("other-sunsets-grid");
  grid.innerHTML = "";
  const cards = [];
  for (const c of readyResults) {
    if (c.id === heroId) continue;
    cards.push(buildReadySunsetCard(c));
  }
  for (const c of otherCandidates || []) {
    cards.push(buildWaitingSunsetCard(c));
  }

  const expandBtn = document.getElementById("expand-other-sunsets-btn");
  let visibleCount = Math.min(cards.length, COLLAPSED_OTHER_SUNSETS_LIMIT);
  for (let i = 0; i < visibleCount; i++) grid.appendChild(cards[i]);

  function updateExpandBtn() {
    if (visibleCount >= cards.length) {
      expandBtn.hidden = true;
      return;
    }
    const remaining = cards.length - visibleCount;
    const nextBatch = Math.min(OTHER_SUNSETS_PAGE_SIZE, remaining);
    expandBtn.textContent = `Zobrazit dalších ${nextBatch} míst (zbývá ${remaining})`;
    expandBtn.hidden = false;
  }

  expandBtn.onclick = () => {
    const upTo = Math.min(visibleCount + OTHER_SUNSETS_PAGE_SIZE, cards.length);
    for (let i = visibleCount; i < upTo; i++) grid.appendChild(cards[i]);
    visibleCount = upTo;
    updateExpandBtn();
  };
  updateExpandBtn();
}

// Milestone "Immediate Full Results" - zadání bod "pokud se seznam musí
// stránkovat... musí existovat možnost zobrazit další, nesmí se tvrdit,
// že je zobrazena celá množina, když není". `other_candidates` je
// ohraničené na `MAX_SEARCH_RESULTS` (app/main.py) - při hustě
// pokrytých oblastech (víc než ~185 kandidátů nad stropem analýzy) se
// zbytek dá dostránkovat přes `other_candidates_offset`.
let lastOtherCandidatesNextOffset = 0;

function updateShowMoreButton(data, geo, radiusKm) {
  const btn = document.getElementById("show-more-candidates-btn");
  lastOtherCandidatesNextOffset = data.other_candidates_next_offset || 0;
  if (data.other_candidates_truncated) {
    btn.textContent = `Zobrazit dalších ${data.other_candidates_remaining} míst`;
    btn.hidden = false;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const more = await fetchFindSunsetPage(geo, radiusKm, lastOtherCandidatesNextOffset);
        for (const c of more.other_candidates.filter((c) => c.status === "not_analyzed")) {
          document.getElementById("other-sunsets-grid").appendChild(buildWaitingSunsetCard(c));
        }
        lastOtherCandidatesNextOffset = more.other_candidates_next_offset || 0;
        btn.hidden = !more.other_candidates_truncated;
        btn.textContent = `Zobrazit dalších ${more.other_candidates_remaining} míst`;
      } finally {
        btn.disabled = false;
      }
    };
  } else {
    btn.hidden = true;
  }
}

async function fetchFindSunsetPage(geo, radiusKm, offset) {
  const res = await fetch("/api/find_sunset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      lat: geo.lat, lon: geo.lon, radius_km: radiusKm, timezone: tzInput.value,
      preferences: collectPreferences(), other_candidates_offset: offset,
    }),
  });
  return res.json();
}

function buildReadySunsetCard(c) {
  const { score, label } = displayScore(c);
  const card = document.createElement("div");
  // Milník 12 ("Corona Bloom") - `.tap-tile` (jemný okraj/skoro
  // neviditelné pozadí, viz style.css) místo ploché `--surface` karty -
  // dlaždice teď vypadá klikatelně, ale ne jako plochý šedý box.
  card.className = "sunset-card tap-tile";
  card.style.setProperty("--card-accent", coronaBloomAccentColor(score));
  const time = new Date(c.sunset_time).toLocaleTimeString("cs-CZ", { timeZone: tzInput.value, hour: "2-digit", minute: "2-digit" });
  const distancePart = c.distance_km !== undefined ? ` · ${c.distance_km} km` : "";
  const typeBadge = c.sunset_type && c.sunset_type.key ? `<div class="card-type-badge">${c.sunset_type.label}</div>` : "";
  const matchBadge = c.user_match && c.user_match.match_pct !== null && c.user_match.match_pct !== undefined
    ? `<div class="card-match-badge">${c.user_match.match_pct}% match</div>` : "";
  const placeLink = c.slug ? `<a href="/sunset/${c.slug}/" class="card-link">Place page →</a>` : "";
  card.innerHTML = `
    <div class="card-icon">${typeIconHtml(c.type)}</div>
    <h3>${c.name}</h3>
    <div class="card-score">${coronaBloomSvg(score, 64)}</div>
    <div class="card-label">${label}</div>
    ${typeBadge}
    ${matchBadge}
    <div class="card-meta">${time} · ${c.measured_elevation_m.toFixed(0)} m${distancePart}</div>
    <div class="card-actions">
      <button type="button">Prozkoumat</button>
      ${placeLink}
    </div>
  `;
  card.querySelector("button").addEventListener("click", () => {
    selectLocation(c.lat, c.lon, { flyTo: true, resetDropdown: true, slug: c.slug || null });
    document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  return card;
}

// Milestone "Immediate Full Results" - kandidáti BEZ platného
// hodnocení (vyhodnocují se, čekají, selhali, nebo jsou nad stropem
// analýzy `MAX_TERRAIN_ANALYSIS_CANDIDATES`). Tlumené karty BEZE SKÓRE
// (žádné vymyšlené/nulové číslo), jen pravdivý stav - "čeká na
// vyhodnocení" nikdy nevypadá jako "špatné místo".
function buildWaitingSunsetCard(c) {
  const card = document.createElement("div");
  card.className = `sunset-card is-waiting is-waiting-${c.status}`;
  const distancePart = c.distance_km !== undefined && c.distance_km !== null ? ` · ${c.distance_km} km` : "";
  const placeLink = c.slug ? `<a href="/sunset/${c.slug}/" class="card-link">Place page →</a>` : "";
  card.innerHTML = `
    <div class="card-icon">${typeIconHtml(c.type)}</div>
    <h3>${c.name}</h3>
    <div class="card-wait-status">${WAITING_STATUS_LABELS[c.status] || c.status}</div>
    <div class="card-meta">${(c.type || "").toUpperCase()}${distancePart}</div>
    <div class="card-actions">
      <button type="button">Prozkoumat</button>
      ${placeLink}
    </div>
  `;
  card.querySelector("button").addEventListener("click", () => {
    selectLocation(c.lat, c.lon, { flyTo: true, resetDropdown: true, slug: c.slug || null });
    document.getElementById("best-sunset").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  return card;
}

// Milestone "Trustworthy Results with Partial Cache" - zadání bod 4:
// "nezobrazuj interní technické detaily každému uživateli bez důvodu",
// ale "pokud má nižší rozlišení význam pro důvěru v hodnocení,
// poskytni srozumitelnou informaci". Jediná podmínka je
// `reduced_dem_quality` (serverem spočítané - stejný práh jako
// app/analyze_location.py/app/terrain_cache.py, žádné nové číslo) -
// žádné syrové "10.0m" ani "36.1m" se neukazuje běžnému uživateli.
function renderDemQualityNote(dem) {
  const el = document.getElementById("dem-quality-note");
  if (!el) return;
  if (dem && dem.reduced_dem_quality) {
    el.textContent = "ℹ️ Výškový model terénu pro tohle místo má omezenou přesnost - hodnocení terénu může být méně přesné než u jiných míst.";
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

findSunsetBtn.addEventListener("click", findMySunset);
searchWhereInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") findMySunset();
});

async function initApp() {
  const dataset = await fetchCandidates("beskydy");
  if (dataset && Array.isArray(dataset.candidates) && dataset.candidates.length > 0) {
    beskydyCandidates = dataset;
    registerLocationNames(dataset.candidates);
    const best = dataset.candidates[0];
    // Zadání bod 17 - "Best Sunset musí být skutečný": výchozí místo teď
    // NENÍ natvrdo Milešovka, ale vítěz skutečného rankingu nad reálně
    // objevenými/analyzovanými kandidáty (viz PLAN.md Fáze 8).
    currentLat = best.lat;
    currentLon = best.lon;
    latInput.value = best.lat;
    lonInput.value = best.lon;
    map.setCenter([best.lon, best.lat]);
  }

  searchCenter = { lat: currentLat, lon: currentLon };
  whenMapReady(() => drawSearchCircle(searchCenter.lat, searchCenter.lon, searchRadiusKm));

  await runAnalysis(currentLat, currentLon);

  if (beskydyCandidates) {
    const best = beskydyCandidates.candidates[0];

    const heroStat = document.getElementById("hero-stat");
    heroStat.textContent = `🔎 ${beskydyCandidates.candidate_count} vyhlídek analyzováno v Beskydech · nejlepší skóre ${best.rating}/100`;
    heroStat.hidden = false;

    const note = document.getElementById("candidates-analyzed-note");
    note.textContent = `🔎 ${beskydyCandidates.candidate_count} vyhlídek analyzováno v Beskydech (pro ${beskydyCandidates.analyzed_for_date}) — tohle je vítěz skutečného žebříčku.`;
    note.hidden = false;
    document.querySelector("#best-sunset .dev-note").hidden = true;
    document.getElementById("other-sunsets-hint").textContent =
      `Zbývajících ${beskydyCandidates.candidate_count - 1} skutečně analyzovaných vyhlídek v Beskydech, seřazeno podle Sunset Ratingu — zatím jen tahle oblast, další regiony ČR přibudou příště.`;

    whenMapReady(() => renderCandidateMarkers(beskydyCandidates.candidates, best.id));

    renderOtherSunsetsFromCandidates(beskydyCandidates.candidates, best.id);
    renderTonightPreview(beskydyCandidates.candidates, best.id);

    const chips = document.getElementById("type-filter-chips");
    chips.hidden = false;
    chips.addEventListener("click", (e) => {
      const btn = e.target.closest(".filter-chip");
      if (!btn) return;
      chips.querySelectorAll(".filter-chip").forEach((c) => c.classList.remove("is-active"));
      btn.classList.add("is-active");
      applyTypeFilter(btn.dataset.filter);
    });
  } else {
    // Fallback beze změny (Fáze 7) - pokud dataset zatím neexistuje
    // (region ještě nebyl zpracovaný `scripts/build_candidate_dataset.py`),
    // aplikace zůstává plně funkční nad výchozí Milešovkou + lazy-load
    // testovacích lokalit.
    attachLazyOtherSunsets();
  }
}

initApp();

// Fáze 10 (zadání bod 7) - velmi jemný parallax hero siluety podle
// polohy myši (různá rychlost na vrstvu = pocit hloubky). Čistě
// dekorativní, žádný vliv na analytická data. Vypnuto úplně při
// `prefers-reduced-motion`, ne jen zkráceno.
if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const heroEl = document.getElementById("hero");
  const layers = [
    { el: document.querySelector(".hero-silhouette .ridge-back"), amount: 10 },
    { el: document.querySelector(".hero-silhouette .ridge-mid"), amount: 6 },
    { el: document.querySelector(".hero-silhouette .ridge-front"), amount: 3 },
  ];
  heroEl.addEventListener("mousemove", (e) => {
    const rect = heroEl.getBoundingClientRect();
    const centerX = (e.clientX - rect.left) / rect.width - 0.5; // -0.5..0.5
    for (const layer of layers) {
      if (layer.el) layer.el.style.transform = `translateX(${(-centerX * layer.amount).toFixed(1)}px)`;
    }
  });
  heroEl.addEventListener("mouseleave", () => {
    for (const layer of layers) if (layer.el) layer.el.style.transform = "";
  });
}

// Fáze 9, zadání bod 5/27 - jemný scroll-reveal fade-in pro sekce (CSS
// dělá samotnou animaci, tohle jen přidává `.is-visible` při vstupu do
// viewportu). Sleduje VŠECHNY sekce průběžně (ne jen jednou při startu),
// protože `#best-sunset`/`#panorama-section`/... jsou na začátku
// `[hidden]` (nulová velikost) a do DOM "reálně" vstoupí až po první
// analýze - MutationObserver na `main` znovu přihlásí nově odkryté
// sekce k pozorování.
const revealObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      }
    }
  },
  { threshold: 0.02, rootMargin: "0px 0px 300px 0px" },
);
function observeRevealCandidates() {
  document.querySelectorAll("main > section:not(#hero):not(.is-visible)").forEach((el) => {
    if (el.hidden) return;
    el.classList.add("reveal-pending");
    revealObserver.observe(el);
    // AUDIT NÁLEZ (skutečný browser test - full-page screenshot ukázal
    // velké prázdné mezery): pokud z jakéhokoliv důvodu IntersectionObserver
    // pro danou sekci nespustí (nestandardní scroll/capture nástroj,
    // sekce vyšší než viewport apod.), obsah nesmí zůstat NATRVALO
    // neviditelný jen kvůli dekorativní animaci - bezpečnostní pojistka.
    setTimeout(() => el.classList.add("is-visible"), 2500);
  });
}
observeRevealCandidates();
new MutationObserver(observeRevealCandidates).observe(document.querySelector("main"), {
  attributes: true,
  attributeFilter: ["hidden"],
  subtree: true,
});
