// Sunset Radar - place page (`/sunset/{slug}/`, milestone "Complete
// Product Build", pokračování - zadání bod 2, MANDATORY). Čistě
// prezentační: veškerá data pochází z GET /api/place/{slug}
// (app/main.py::api_place) - terrain/solar/weather/score se tu nic
// nepočítá znovu, jen zobrazuje.

const body = document.body;
const SLUG = body.dataset.slug;
const CANDIDATE_LAT = parseFloat(body.dataset.lat);
const CANDIDATE_LON = parseFloat(body.dataset.lon);
const TIMEZONE = body.dataset.timezone;

// `distance_km` v URL query (proklik z Tonight/Explore výsledku, kde je
// skutečná vzdálenost od hledaného středu známá) - bez něj server vrátí
// "hike_access_estimate": null (poctivě "neznámé", ne vymyšlené číslo).
const urlParams = new URLSearchParams(window.location.search);
const distanceKmParam = urlParams.get("distance_km");

const PENDING_MAX_RETRIES = 10;
const PENDING_RETRY_DELAY_MS = 6000;

async function fetchPlace() {
  const qs = new URLSearchParams({ timezone: TIMEZONE });
  if (distanceKmParam) qs.set("distance_km", distanceKmParam);
  const res = await fetch(`/api/place/${encodeURIComponent(SLUG)}?${qs.toString()}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Nepodařilo se načíst místo.");
  return data;
}

function setLoadingText(text) {
  document.getElementById("place-loading-text").textContent = text;
}

async function loadPlace() {
  let data;
  try {
    data = await fetchPlace();
  } catch (err) {
    showPlaceError("Nepodařilo se spojit se serverem: " + err.message);
    return;
  }

  let attempt = 0;
  const start = Date.now();
  while (data.status === "pending" && attempt < PENDING_MAX_RETRIES) {
    attempt += 1;
    const elapsedS = Math.round((Date.now() - start) / 1000);
    setLoadingText(
      `Terén se poprvé počítá na pozadí (čekám ${elapsedS} s, pokus ${attempt}/${PENDING_MAX_RETRIES}) - první analýza nové ` +
        `oblasti může trvat desítky sekund kvůli stažení výškových dat…`,
    );
    await new Promise((resolve) => setTimeout(resolve, PENDING_RETRY_DELAY_MS));
    try {
      data = await fetchPlace();
    } catch (err) {
      showPlaceError("Nepodařilo se spojit se serverem: " + err.message);
      return;
    }
  }

  if (data.status === "pending") {
    showPlaceError(
      "Terén pro tohle místo se stále počítá na pozadí a nestihl se připravit ani po delším čekání. " +
        "Zkus prosím stránku načíst znovu za minutu nebo dvě.",
    );
    return;
  }

  renderPlace(data);
}

function showPlaceError(message) {
  document.getElementById("place-loading").hidden = true;
  const el = document.getElementById("place-error");
  el.textContent = message;
  el.hidden = false;
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("cs-CZ", { timeZone: TIMEZONE, hour: "2-digit", minute: "2-digit" });
}
function fmtDateShort(iso) {
  return new Date(iso).toLocaleDateString("cs-CZ", { timeZone: TIMEZONE, weekday: "short", day: "numeric", month: "numeric" });
}

function renderPlace(data) {
  document.getElementById("place-loading").hidden = true;
  const tonight = data.tonight;

  renderHero(tonight);
  renderWhy(tonight);
  renderScoreBreakdown(tonight.score_components);
  renderTerrain(tonight);
  renderAccess(tonight);
  renderBestDays(data.best_days);
  renderPanorama(tonight);
  renderMap();

  document.getElementById("place-hero-grid").hidden = false;
  document.getElementById("place-why").hidden = false;
  document.getElementById("place-score-breakdown").hidden = false;
  document.getElementById("place-terrain").hidden = false;
  document.getElementById("place-best-days").hidden = false;
  document.getElementById("place-panorama").hidden = false;
  document.getElementById("place-access").hidden = false;
  document.getElementById("place-map-section").hidden = false;
}

function renderHero(tonight) {
  const score = tonight.sunset_score;

  // Milník "Vizuální sjednocení" - AUDIT NÁLEZ (skutečný browser test):
  // `#place-sub` je SSR-vykreslené jen z katalogového `candidate.elevation_m`
  // (často chybí -> "elevation unknown"), i když `tonight.measured_elevation_m`
  // (skutečná změřená hodnota z terénní analýzy) je o pár řádků níž na
  // stránce vidět správně. Jakmile se `tonight` načte, přepíše se na
  // skutečně změřenou hodnotu - nikdy horší/nejistější číslo, než jaké
  // už beztak stránka zobrazuje jinde.
  document.getElementById("place-sub").textContent = `${tonight.measured_elevation_m.toFixed(0)} m elevation`;

  // Milník 12 ("Corona Bloom") - stejná sdílená funkce jako zbytek
  // appky (viz static/sunset_viz.js), žádná vlastní implementace tady.
  document.getElementById("corona-bloom-place-hero").innerHTML = coronaBloomSvg(score, 160);
  document.getElementById("place-rating-desc").textContent = tonight.sunset_score_label;

  // Milník 12 (zadání bod 1) - sky-panel gradient reaguje na SKUTEČNÝ
  // barevný potenciál TOHOTO místa (stejný princip jako Tonight hero
  // applySkyMood(), jen přes sdílenou static/sunset_viz.js funkci).
  applySkyMoodClass(document.getElementById("place-hero"), tonight.color_potential && tonight.color_potential.key);

  const badgesEl = document.getElementById("place-badges");
  badgesEl.innerHTML = "";
  if (tonight.sunset_type && tonight.sunset_type.key) {
    const badge = document.createElement("span");
    badge.className = "sunset-type-badge";
    badge.textContent = tonight.sunset_type.label;
    badge.title = tonight.sunset_type.confidence_text || "";
    badgesEl.appendChild(badge);
  }
  // Milník 12 (zadání bod 2/3) - barevný potenciál a potenciál inverze/
  // mlhy PRO TOHLE KONKRÉTNÍ místo (na rozdíl od hero "obecné" verze na
  // Tonight stránce). Inverze se zobrazuje jen při skutečném signálu
  // (favorable/possible/uncertain) - "unlikely" by jen zabíralo místo
  // bez užitečné informace (stejný vzor jako hero `applySkyMood`
  // badge). Celý zdůvodňující text zůstává v `title` (hover) - badge
  // samotný nese jen krátký label, žádná vymyšlená přesnost navíc.
  if (tonight.color_potential && tonight.color_potential.key) {
    const badge = document.createElement("span");
    badge.className = "sunset-type-badge";
    badge.textContent = tonight.color_potential.label;
    badge.title = tonight.color_potential.detail || "";
    badgesEl.appendChild(badge);
  }
  if (tonight.inversion_potential && tonight.inversion_potential.key && tonight.inversion_potential.key !== "unlikely") {
    const badge = document.createElement("span");
    const INVERSION_BADGE_CLASS = { favorable: "is-open", possible: "is-delayed", uncertain: "" };
    badge.className = `sunset-type-badge ${INVERSION_BADGE_CLASS[tonight.inversion_potential.key] || ""}`;
    badge.textContent = tonight.inversion_potential.label;
    badge.title = tonight.inversion_potential.detail || "";
    badgesEl.appendChild(badge);
  }
  for (const [flag, label] of [["near_water", "Water nearby"], ["near_forest", "Forest nearby"], ["near_city", "City nearby"]]) {
    if (tonight[flag]) {
      const badge = document.createElement("span");
      badge.className = "sunset-type-badge env-badge";
      badge.textContent = label;
      badgesEl.appendChild(badge);
    }
  }

  // Milestone "Trustworthy Results with Partial Cache" - zadání bod 4:
  // soft, srozumitelná poznámka, jen když `reduced_dem_quality` (server,
  // stejný práh jako jinde, viz app/main.py _dem_quality_fields()).
  const demNoteEl = document.getElementById("place-dem-quality-note");
  if (tonight.reduced_dem_quality) {
    demNoteEl.textContent = "ℹ️ Výškový model terénu pro tohle místo má omezenou přesnost - hodnocení terénu může být méně přesné.";
    demNoteEl.hidden = false;
  } else {
    demNoteEl.hidden = true;
  }

  document.getElementById("place-stat-sunset").textContent = `${fmtTime(tonight.sunset_time)} sunset`;
  document.getElementById("place-stat-horizon").textContent = `${azimuthToCompass(tonight.sunset_azimuth_deg)} (${tonight.sunset_azimuth_deg.toFixed(0)}°)`;
  document.getElementById("place-stat-visible").textContent = tonight.terrain_obstruction
    ? `${tonight.delay_minutes.toFixed(0)} min delay`
    : "Open horizon";
  document.getElementById("place-stat-elevation").textContent = `${tonight.measured_elevation_m.toFixed(0)} m`;

  const cloudsRow = document.getElementById("place-stat-clouds-row");
  if (tonight.weather) {
    document.getElementById("place-stat-clouds").textContent = `${Math.round(tonight.weather.cloud_cover_pct)}% clouds`;
    cloudsRow.hidden = false;
  } else {
    cloudsRow.hidden = true;
  }
  const precipRow = document.getElementById("place-stat-precip-row");
  if (tonight.weather && tonight.weather.precipitation_probability_pct !== null && tonight.weather.precipitation_probability_pct !== undefined) {
    document.getElementById("place-stat-precip").textContent = `${Math.round(tonight.weather.precipitation_probability_pct)}% precipitation`;
    precipRow.hidden = false;
  } else {
    precipRow.hidden = true;
  }
}

// Stejná logika jako static/app.js::renderWhyList (Tonight page), nad
// polem, které place page skutečně dostává (`rating_detail.components` +
// `weather` - žádný hardcoded marketing text, jen skutečné komponenty).
const WHY_THRESHOLD = 70;
const WHY_ELEVATION_THRESHOLD_M = 800;

function renderWhy(tonight) {
  const list = document.getElementById("place-why-list");
  list.innerHTML = "";
  const reasons = [];
  const c = tonight.rating_detail.components;
  const elevationM = tonight.measured_elevation_m;
  if (c.horizon_at_azimuth >= WHY_THRESHOLD) reasons.push("Open horizon toward the sunset.");
  if (c.sector_openness >= WHY_THRESHOLD) reasons.push("Wide open sector around the sunset direction.");
  if (c.duration >= WHY_THRESHOLD) reasons.push("Long sunset visibility.");
  if (c.delay >= WHY_THRESHOLD) reasons.push("Minimal delay from terrain.");
  if (elevationM >= WHY_ELEVATION_THRESHOLD_M) reasons.push(`High elevation (${elevationM.toFixed(0)} m) with a wide panorama.`);

  const weather = tonight.weather;
  if (weather) {
    if (weather.cloud_cover_low_pct < 15) reasons.push("Low cloud cover should leave the horizon clear.");
    const midHigh = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2;
    if (midHigh >= 20 && midHigh <= 65 && weather.cloud_cover_low_pct < 20) reasons.push("Mid/high clouds may create stronger sunset colors.");
    if (weather.visibility_m && weather.visibility_m >= 25000) reasons.push("Excellent visibility today.");
    if (weather.precipitation_probability_pct != null && weather.precipitation_probability_pct >= 50) reasons.push("Chance of precipitation may reduce visibility.");
  }
  if (tonight.near_water) reasons.push("A body of water is nearby - potential for reflections.");
  if (tonight.near_forest) reasons.push("Forest surroundings nearby.");
  if (tonight.near_city) reasons.push("A city/town is nearby - urban backdrop possible.");

  const isNegative = reasons.length === 0;
  if (isNegative) reasons.push("Terén v okolí tohoto místa výhled na západ výrazně omezuje.");

  for (const reason of reasons) {
    const li = document.createElement("li");
    if (isNegative) li.className = "why-negative";
    li.textContent = reason;
    list.appendChild(li);
  }
}

function renderScoreBreakdown(components) {
  const list = document.getElementById("place-score-breakdown-list");
  list.innerHTML = "";
  for (const c of components) {
    const dt = document.createElement("dt");
    dt.textContent = `${c.label} (${Math.round(c.weight * 100)}%)`;
    const dd = document.createElement("dd");
    if (c.value_0_100 === null || c.value_0_100 === undefined) {
      dd.textContent = "Unavailable";
      dd.className = "is-unavailable";
    } else {
      dd.textContent = `${Math.round(c.value_0_100)} / 100 — ${c.detail}`;
    }
    list.appendChild(dt);
    list.appendChild(dd);
  }
}

function renderTerrain(tonight) {
  const rows = [
    ["Sunset direction", `${azimuthToCompass(tonight.sunset_azimuth_deg)} (${tonight.sunset_azimuth_deg.toFixed(1)}°)`],
    ["Terrain obstruction", tonight.terrain_obstruction ? `ano (${tonight.delay_minutes.toFixed(0)} min delay)` : "ne - open horizon"],
    ["Horizon at sunset azimuth (rating)", `${tonight.rating_detail.components.horizon_at_azimuth.toFixed(0)} / 100`],
    ["Sector openness (rating)", `${tonight.rating_detail.components.sector_openness.toFixed(0)} / 100`],
    ["Visibility duration (rating)", `${tonight.rating_detail.components.duration.toFixed(0)} / 100`],
    ["Elevation", `${tonight.measured_elevation_m.toFixed(0)} m`],
    ["Slope / aspect", tonight.terrain.slope_deg !== null && tonight.terrain.slope_deg !== undefined
      ? `${tonight.terrain.slope_deg.toFixed(1)}° / ${tonight.terrain.aspect_deg.toFixed(0)}°` : "nedostupné"],
  ];
  const dl = document.getElementById("place-terrain-list");
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

function renderAccess(tonight) {
  const el = document.getElementById("place-access-text");
  if (tonight.hike_access_estimate && tonight.distance_km !== null && tonight.distance_km !== undefined) {
    const label = HIKE_ACCESS_LABELS[tonight.hike_access_estimate] || tonight.hike_access_estimate;
    el.textContent = `Estimated effort: ${label} (~${tonight.distance_km} km straight-line from your search origin, ` +
      `${tonight.measured_elevation_m.toFixed(0)} m elevation). This is a distance+elevation estimate, NOT precise trail/routing data.`;
  } else {
    el.textContent = "Estimated effort depends on where you're starting from - open this place from a Tonight/Explore " +
      "search result to see a distance-based access estimate. (No trail/routing data is used - only straight-line distance and elevation.)";
  }
}

// Milník 12 (zadání bod 6) - "nevymýšlej přesnost, kterou model nemá".
// Žádná nová meteorologická metrika - jen poctivé UI přiznání, že
// předpověď je čím dál míň jistá, čím dál dopředu se dívá (obecně platná
// vlastnost předpovědi počasí, ne něco spočítaného z dat, co nemáme).
const FURTHER_OUT_DAY_INDEX = 3; // 0 = dnes, 1-2 = "zítra/pozítří" (běžně spolehlivé), 3+ = "further out"

function renderBestDays(bestDays) {
  const grid = document.getElementById("place-best-days-grid");
  grid.innerHTML = "";
  bestDays.forEach((day, index) => {
    const score = day.score.overall_0_100;
    const card = document.createElement("div");
    card.className = "best-day-card tap-tile";
    const typeBit = day.score.sunset_type && day.score.sunset_type.key ? day.score.sunset_type.label : "—";
    const cloudsBit = day.score.weather ? `${Math.round(day.score.weather.cloud_cover_pct)}% clouds` : "weather unavailable";
    const uncertaintyBit = index >= FURTHER_OUT_DAY_INDEX
      ? `<div class="best-day-uncertainty" title="Forecasts this far out are less reliable than today's">further out</div>` : "";
    card.innerHTML = `
      <div class="best-day-date">${fmtDateShort(day.date)}</div>
      <div class="best-day-score">${coronaBloomSvg(score, 46)}</div>
      <div class="best-day-time">${fmtTime(day.sunset_time)} sunset</div>
      <div class="best-day-type">${typeBit}</div>
      <div class="best-day-clouds">${cloudsBit}</div>
      ${uncertaintyBit}
    `;
    grid.appendChild(card);
  });
}

let panoramaViewCenter = 0;
let panoramaTonight = null;

function renderPanorama(tonight) {
  panoramaTonight = tonight;
  panoramaViewCenter = tonight.sunset_azimuth_deg;
  document.getElementById("place-panorama-caption").textContent = panoramaCaptionText({
    obstructed: tonight.terrain_obstruction,
    delayMinutes: tonight.delay_minutes,
    sunsetAzimuthDeg: tonight.sunset_azimuth_deg,
  });
  drawPlacePanorama();
}

function drawPlacePanorama() {
  if (!panoramaTonight) return;
  const canvas = document.getElementById("place-panorama-canvas");
  drawPanoramaFrame(canvas, {
    azimuths: panoramaTonight.terrain.azimuths_deg,
    altitudes: panoramaTonight.terrain.horizon_altitudes_deg,
    sunsetAzimuthDeg: panoramaTonight.sunset_azimuth_deg,
    obstructed: panoramaTonight.terrain_obstruction,
    viewCenterDeg: panoramaViewCenter,
  });
}

function initPanoramaInteraction() {
  const wrap = document.querySelector("#place-panorama .panorama-wrap");
  let dragging = false;
  let dragStartX = 0;
  let dragStartCenter = 0;
  const canvas = document.getElementById("place-panorama-canvas");

  wrap.addEventListener("pointerdown", (e) => {
    if (e.target.closest("#place-panorama-recenter")) return;
    dragging = true;
    dragStartX = e.clientX;
    dragStartCenter = panoramaViewCenter;
    wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const deltaX = e.clientX - dragStartX;
    const deltaDeg = (deltaX / canvas.clientWidth) * 100;
    panoramaViewCenter = normalizeAzimuth(dragStartCenter - deltaDeg);
    drawPlacePanorama();
  });
  wrap.addEventListener("pointerup", () => { dragging = false; });
  wrap.addEventListener("pointercancel", () => { dragging = false; });

  document.getElementById("place-panorama-recenter").addEventListener("click", () => {
    if (!panoramaTonight) return;
    panoramaViewCenter = panoramaTonight.sunset_azimuth_deg;
    drawPlacePanorama();
  });
}
initPanoramaInteraction();

function renderMap() {
  // Milestone "Sunset-first UI and Minimalist Map Redesign" - stejný
  // styl jako Tonight/Explore (viz static/app.js pro plné zdůvodnění
  // OpenFreeMap "dark" volby - bez klíče/účtu, monochromatický).
  const map = new maplibregl.Map({
    container: "place-map",
    style: "https://tiles.openfreemap.org/styles/dark",
    center: [CANDIDATE_LON, CANDIDATE_LAT],
    zoom: 12.5,
    attributionControl: true,
  });
  map.addControl(new maplibregl.NavigationControl(), "top-right");
  map.on("load", () => {
    new maplibregl.Marker({ color: "#f4a742" }).setLngLat([CANDIDATE_LON, CANDIDATE_LAT]).addTo(map);
    map.resize();
  });
}

// Fáze 5 - GPS souřadnice + kopírování. Souřadnice jsou SSR-vykreslené
// přímo z `candidate.lat`/`candidate.lon` (viz templates/place.html -
// STEJNÁ hodnota jako `data-lat`/`data-lon` na <body>, skutečná poloha
// bodu z katalogu, nikdy odhad ze jména/obce) - JS tu jen obsluhuje
// kopírování do schránky a zpětnou vazbu. "Open in Maps" je čistý
// server-rendered odkaz (žádné JS potřeba).
const copyCoordsBtn = document.getElementById("place-copy-coords-btn");
if (copyCoordsBtn) {
  copyCoordsBtn.addEventListener("click", async () => {
    const text = document.getElementById("place-coords-text").textContent;
    const feedback = document.getElementById("place-coords-feedback");
    let copied = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (copied) {
      feedback.textContent = "✓ Coordinates copied";
    } else {
      // Fallback (Clipboard API nedostupné/zamítnuté - zadání "umožni
      // uživateli souřadnice snadno ručně označit a zkopírovat"): text
      // je `user-select: all` (viz style.css), takže stačí ho programově
      // označit a uživatel hned může Ctrl+C/Cmd+C sám.
      const textEl = document.getElementById("place-coords-text");
      const range = document.createRange();
      range.selectNodeContents(textEl);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      feedback.textContent = "Selected — press Ctrl+C / Cmd+C to copy";
    }
    feedback.hidden = false;
    clearTimeout(copyCoordsBtn._feedbackTimer);
    copyCoordsBtn._feedbackTimer = setTimeout(() => { feedback.hidden = true; }, 3000);
  });
}

loadPlace();
