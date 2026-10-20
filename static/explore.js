// Sunset Radar - Explore page (`/explore`, milestone "Complete Product
// Build", pokračování - zadání bod 8, MANDATORY: map-first browsing,
// candidate markers, filters, marker -> preview -> place page). Čistě
// prezentační nad `/api/geocode` + `/api/find_sunset` (STEJNÉ endpointy
// jako Tonight page) - žádný nový engine, jen jiné rozvržení/vizuál
// zaměřené na mapu jako primární plochu.

const TIMEZONE = document.body.dataset.timezone;
const whereInput = document.getElementById("explore-where");
const radiusInput = document.getElementById("explore-radius");
const radiusValueEl = document.getElementById("explore-radius-value");
const searchBtn = document.getElementById("explore-search-btn");
const statusEl = document.getElementById("explore-status");
const errorEl = document.getElementById("explore-error");
const resultsListEl = document.getElementById("explore-results-list");
const coverageNoteEl = document.getElementById("explore-coverage-note");
const previewEl = document.getElementById("explore-preview");
const previewBodyEl = document.getElementById("explore-preview-body");

// Milestone "Sunset-first UI and Minimalist Map Redesign" - stejný
// styl jako Tonight page (static/app.js), stejné zdůvodnění (OpenFreeMap
// "dark" - bez klíče/účtu, monochromatický, žádné turistické barvy -
// viz komentář tam). Explore stránka zůstává map-first layoutem (to je
// její deklarovaný účel jako SEKUNDÁRNÍ, explicitně mapová obrazovka v
// navigaci - zadání bod 1 se týká hlavní/Tonight stránky), jen sdílí
// stejný vizuální jazyk mapy.
const map = new maplibregl.Map({
  container: "explore-map-canvas",
  style: "https://tiles.openfreemap.org/styles/dark",
  center: [15.4, 49.8], // střed ČR - výchozí pohled, dokud uživatel nehledá
  zoom: 7,
  attributionControl: true,
});
map.addControl(new maplibregl.NavigationControl(), "top-right");

let mapReady = false;
map.on("load", () => {
  mapReady = true;
  map.resize();
});

let candidateMarkers = [];
let activeFilter = "all";
let lastCandidates = [];
let lastBestId = null;
let selectedId = null;

radiusInput.addEventListener("input", () => {
  radiusValueEl.textContent = `${radiusInput.value} km`;
});

function setStatus(text) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
}
function setError(text) {
  if (text) setStatus(null);
  errorEl.hidden = !text;
  errorEl.textContent = text || "";
}

function displayScore(c) {
  if (c.sunset_score !== undefined && c.sunset_score !== null) return { score: c.sunset_score, label: c.sunset_score_label };
  return { score: c.rating, label: c.rating_label };
}

// Milník "Postupné vylepšení" (Fáze 4 - ikony typů vyhlídek) - stejná
// ikonová sada jako static/app.js (plné zdůvodnění tam) - STEJNÁ
// geometrie jako `.candidate-marker.type-*` tvary na mapě.
const TYPE_ICON_CLASS = { peak: "icon-peak", viewpoint: "icon-viewpoint", tower: "icon-tower", castle: "icon-castle", hill: "icon-hill" };
function typeIconHtml(type) {
  return `<span class="icon ${TYPE_ICON_CLASS[type] || "icon-pin"}" aria-hidden="true"></span>`;
}

async function fetchFindSunset(lat, lon, radiusKm) {
  const res = await fetch("/api/find_sunset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lon, radius_km: radiusKm, timezone: TIMEZONE }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Hledání se nezdařilo.");
  return data;
}

async function runSearch() {
  const query = whereInput.value.trim();
  setError(null);
  if (!query) {
    setError("Zvol místo, kde chceš hledat.");
    return;
  }
  searchBtn.disabled = true;
  try {
    setStatus("Hledám lokalitu…");
    let geo;
    try {
      const geoRes = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`);
      geo = await geoRes.json();
      if (!geoRes.ok) {
        setError(geo.error || "Místo se nepodařilo najít.");
        clearStaleResults();
        return;
      }
    } catch (err) {
      setError("Nepodařilo se spojit se serverem: " + err.message);
      clearStaleResults();
      return;
    }
    await searchFromCenter(geo);
  } finally {
    searchBtn.disabled = false;
  }
}

// Milestone "Sunset-first UI and Minimalist Map Redesign" - zadání bod
// 1A: "nezobrazuj falešného vítěze". AUDIT NÁLEZ: `found_count===0`/
// `ready_count===0` pro NOVÉ hledání dřív jen ukázaly chybu, ale
// markery/seznam/preview z PŘEDCHOZÍHO úspěšného hledání zůstaly na
// obrazovce - mohly působit jako odpověď na nový dotaz.
function clearStaleResults() {
  stopFindSunsetPolling();
  findSunsetSearchToken++; // zahoď i případný už běžící poll z předchozího hledání
  lastCandidates = [];
  lastBestId = null;
  selectedId = null;
  renderMarkers([], null);
  renderResultsList([], null);
  previewEl.hidden = true;
  coverageNoteEl.hidden = true;
  document.getElementById("explore-filter-chips").hidden = true;
  const refreshBtn = document.getElementById("explore-refresh-results-btn");
  if (refreshBtn) refreshBtn.hidden = true;
  const showMoreBtn = document.getElementById("explore-show-more-candidates-btn");
  if (showMoreBtn) showMoreBtn.hidden = true;
}

// Milestone "Immediate Full Results" - AUDIT NÁLEZ (reprodukováno živě,
// hledání "Brno" @ 5 km): `found_count` 64, strop analýzy 15
// (`to_analyze_count`, ZÁMĚRNÝ, zachovaný). PŘEDCHOZÍ oprava ("Visible
// Waiting Candidates") přidala `other_candidates` do API, ale frontend
// PŘED prvním vykreslením pořád blokoval na retry smyčce (až 40s) - viz
// stejný nález a stejná oprava jako static/app.js (plné zdůvodnění tam).
// Nová architektura: 1) jeden fetch, 2) OKAMŽITÉ vykreslení celé
// nalezené množiny, 3) lehké opakované dotazování na pozadí (žádná nová
// DEM zátěž), dokud něco čeká/se vyhodnocuje.
let lastSearchGeo = null;
let lastSearchRadiusKm = null;
let findSunsetSearchToken = 0;
let findSunsetPollTimer = null;
let findSunsetHeroId = null; // vítěz se po ustálení nepřehazuje

const FIND_SUNSET_POLL_INTERVAL_MS = 10000;
const FIND_SUNSET_MAX_POLL_ATTEMPTS = 60; // ~10 minut na pozadí, pak jen manuální "Zkontrolovat nové výsledky"

function stopFindSunsetPolling() {
  if (findSunsetPollTimer) {
    clearTimeout(findSunsetPollTimer);
    findSunsetPollTimer = null;
  }
}

async function searchFromCenter(geo) {
  const radiusKm = parseFloat(radiusInput.value);
  map.flyTo({ center: [geo.lon, geo.lat], zoom: 10.5 });

  stopFindSunsetPolling();
  const myToken = ++findSunsetSearchToken;
  findSunsetHeroId = null;
  selectedId = null;
  lastSearchGeo = geo;
  lastSearchRadiusKm = radiusKm;

  coverageNoteEl.hidden = true; // stará poznámka nesmí přežít nové hledání, i kdyby tohle selhalo/nic nenašlo
  setStatus("Hledám viewpointy… Kontroluji terén… Kontroluji počasí… Řadím západy slunce…");
  let data;
  try {
    data = await fetchFindSunset(geo.lat, geo.lon, radiusKm);
  } catch (err) {
    setError("Nepodařilo se spojit se serverem: " + err.message);
    clearStaleResults();
    return;
  }
  if (myToken !== findSunsetSearchToken) return; // mezitím začalo novější hledání

  if (data.found_count === 0) {
    setError(`V okruhu ${radiusKm} km od „${geo.display_name}“ nebyly nalezeny žádné vyhlídky v katalogu.`);
    clearStaleResults();
    return;
  }

  // Fáze 2 - OKAMŽITÉ zobrazení celé nalezené množiny, bez čekání na worker.
  renderFindSunsetOutcome(data, geo, radiusKm);
  scheduleFindSunsetPoll(myToken, geo, radiusKm, 0);
}

// Fáze 3 - postupné doplňování na pozadí (stejný vzor/zdůvodnění jako
// static/app.js `scheduleFindSunsetPoll()`): bezpečné, ohraničené
// opakované dotazování, žádná nová DEM zátěž (worker dedup i tak
// zablokuje duplicitní práci - tohle jen čte stav).
function scheduleFindSunsetPoll(myToken, geo, radiusKm, attempt) {
  if (myToken !== findSunsetSearchToken) return;
  findSunsetPollTimer = setTimeout(async () => {
    if (myToken !== findSunsetSearchToken) return;
    let data;
    try {
      data = await fetchFindSunset(geo.lat, geo.lon, radiusKm);
    } catch {
      return; // tichý neúspěch jednoho pokusu na pozadí - příští pokus to zkusí znovu
    }
    if (myToken !== findSunsetSearchToken) return;
    renderFindSunsetOutcome(data, geo, radiusKm, { isBackgroundUpdate: true });
    const stillPending = data.enqueued_count + data.pending_count > 0;
    if (stillPending && attempt + 1 < FIND_SUNSET_MAX_POLL_ATTEMPTS) {
      scheduleFindSunsetPoll(myToken, geo, radiusKm, attempt + 1);
    }
  }, FIND_SUNSET_POLL_INTERVAL_MS);
}

function renderFindSunsetOutcome(data, geo, radiusKm, { isBackgroundUpdate = false } = {}) {
  const pendingTotal = data.enqueued_count + data.pending_count;
  const refreshBtn = document.getElementById("explore-refresh-results-btn");
  if (refreshBtn) refreshBtn.hidden = pendingTotal === 0;

  if (data.results.length > 0 && findSunsetHeroId === null) {
    findSunsetHeroId = data.results[0].id;
  }
  const best = data.results.find((r) => r.id === findSunsetHeroId) || data.results[0] || null;

  if (!best) {
    // Zpětná vazba (terénní kvalitní práh, MIN_DISPLAYED_TERRAIN_RATING
    // v app/main.py) - "nulové výsledky" už NEMUSÍ znamenat "ještě se
    // počítá", viz stejný komentář ve static/app.js.
    const stillPending = data.enqueued_count + data.pending_count > 0;
    const allBelowFloor = !stillPending && (data.below_quality_floor_count || 0) > 0;
    if (!isBackgroundUpdate) {
      setStatus(null);
      setError(
        allBelowFloor
          ? `${data.found_count} vyhlídek nalezeno - žádná z nich ale nemá dost otevřený výhled na západ (zkus větší okruh).`
          : `${data.found_count} vyhlídek nalezeno. Zatím žádná nemá hotové hodnocení (vyhodnocují se na pozadí, viz seznam).`,
      );
    }
    lastCandidates = [];
    lastBestId = null;
    renderMarkers([], null);
    document.getElementById("explore-filter-chips").hidden = true;
    if (!isBackgroundUpdate || selectedId === null) previewEl.hidden = true;
    // I bez vítěze zůstávají VŠICHNI nalezení kandidáti viditelní jako
    // vyhodnocuje se/čeká/nevyhodnoceno, ne jen chybová hláška beze seznamu.
    renderResultsList([], null);
    renderWaitingList(data.other_candidates);
    applyFilter(activeFilter); // waiting karty se přidaly PO renderMarkers() - musí dostat stejný filtr
    updateShowMoreButton(data, geo, radiusKm);
    return;
  }

  lastCandidates = data.results;
  lastBestId = best.id;
  document.getElementById("explore-filter-chips").hidden = false;
  renderMarkers(lastCandidates, lastBestId);
  renderResultsList(lastCandidates, lastBestId);
  renderWaitingList(data.other_candidates); // zbylí nalezení kandidáti (vyhodnocují se/čekají/selhali/nad stropem)
  applyFilter(activeFilter); // waiting karty se přidaly PO renderMarkers() (ten uvnitř volá applyFilter) - zopakuj nad úplným seznamem
  updateShowMoreButton(data, geo, radiusKm);
  // Milestone "Immediate Full Results" - zadání bod "bez ztráty výběru":
  // pokud uživatel měl něco vybrané (preview panel otevřený), aktualizace
  // na pozadí ho nesmí tiše zavřít/přepsat - výběr se zachová, pokud
  // vybraný kandidát v aktuální množině stále existuje.
  if (selectedId && lastCandidates.some((c) => c.id === selectedId)) {
    selectCandidate(selectedId, { flyTo: false });
  } else if (!isBackgroundUpdate) {
    selectCandidate(lastBestId, { flyTo: false });
  }
  renderCoverageNote(data, radiusKm);
  setStatus(null);
}

// Milestone "Immediate Full Results" - manuální záloha nad automatickým
// pollingem (užitečná zejména po vypršení `FIND_SUNSET_MAX_POLL_ATTEMPTS`).
document.getElementById("explore-refresh-results-btn")?.addEventListener("click", async () => {
  if (!lastSearchGeo) return;
  const btn = document.getElementById("explore-refresh-results-btn");
  btn.disabled = true;
  try {
    setStatus("Kontroluji nové výsledky…");
    let data;
    try {
      data = await fetchFindSunset(lastSearchGeo.lat, lastSearchGeo.lon, lastSearchRadiusKm);
    } catch (err) {
      setError("Nepodařilo se spojit se serverem: " + err.message);
      return;
    }
    renderFindSunsetOutcome(data, lastSearchGeo, lastSearchRadiusKm);
  } finally {
    btn.disabled = false;
  }
});

// Milestone "Immediate Full Results" - kandidáti BEZE skóre (vyhodnocují
// se/čekají/selhali/jsou nad stropem analýzy) - žádné vymyšlené číslo,
// jen pravdivý stav; neklikatelné (nemá smysl otevírat detail místa,
// které ještě nemá žádná data).
const WAITING_STATUS_ICON = { evaluating: "◐", waiting: "⏳", failed: "⚠️", not_analyzed: "…" };
const WAITING_STATUS_LABEL = {
  evaluating: "Vyhodnocuje se",
  waiting: "Čeká na vyhodnocení",
  failed: "Analýza se nezdařila (zkouší se znovu)",
  not_analyzed: "Zatím nevyhodnoceno (nad stropem analýzy)",
};

function buildWaitingResultCard(c) {
  const card = document.createElement("div");
  card.className = "explore-result-card tap-tile is-waiting";
  card.dataset.type = c.type;
  card.innerHTML = `
    <div class="explore-result-wait-status" title="${WAITING_STATUS_LABEL[c.status] || c.status}">${WAITING_STATUS_ICON[c.status] || "?"}</div>
    <div class="explore-result-info">
      <strong>${typeIconHtml(c.type)} ${c.name}</strong>
      <div class="explore-result-meta">${WAITING_STATUS_LABEL[c.status] || c.status} · ${c.distance_km} km</div>
    </div>
  `;
  return card;
}

function renderWaitingList(otherCandidates) {
  if (!otherCandidates || otherCandidates.length === 0) return;
  for (const c of otherCandidates) {
    resultsListEl.appendChild(buildWaitingResultCard(c));
  }
}

// Milestone "Immediate Full Results" - zadání bod "pokud se seznam musí
// stránkovat... musí existovat možnost zobrazit další, nesmí se tvrdit,
// že je zobrazena celá množina, když není" - stejný vzor jako
// static/app.js `updateShowMoreButton()` (plné zdůvodnění tam).
let lastOtherCandidatesNextOffset = 0;

function updateShowMoreButton(data, geo, radiusKm) {
  const btn = document.getElementById("explore-show-more-candidates-btn");
  if (!btn) return;
  lastOtherCandidatesNextOffset = data.other_candidates_next_offset || 0;
  if (data.other_candidates_truncated) {
    btn.textContent = `Zobrazit dalších ${data.other_candidates_remaining} míst`;
    btn.hidden = false;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await fetch("/api/find_sunset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lat: geo.lat, lon: geo.lon, radius_km: radiusKm, timezone: TIMEZONE, other_candidates_offset: lastOtherCandidatesNextOffset }),
        });
        const more = await res.json();
        for (const c of more.other_candidates.filter((c) => c.status === "not_analyzed")) {
          resultsListEl.appendChild(buildWaitingResultCard(c));
        }
        applyFilter(activeFilter);
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

// Milestone "Trustworthy Results with Partial Cache" - dřív explore
// stránka po dokončení hledání nenesla ŽÁDNOU trvalou informaci o tom,
// že `found_count` (celý radius) a `ready_count`/`data.results.length`
// (skutečně vyhodnoceno PRÁVĚ TEĎ) jsou různá čísla - zpráva o
// "čekajících" místech zmizela spolu s přechodným `setStatus`. Navíc
// "BEST" značka na mapě/kartě implicitně tvrdila "nejlepší v okolí",
// i když šlo jen o nejbližších `to_analyze_count` kandidátů (a z nich
// jen těch, co byly zrovna hotové) - `data.fully_covered` (z API,
// stejná čísla, žádný nový vymyšlený práh) rozhoduje mezi upřímným
// "nejlepší z vyhodnocených míst" a skutečným "nejlepší v okolí".
function renderCoverageNote(data, radiusKm) {
  if (!coverageNoteEl) return;
  const pendingTotal = data.enqueued_count + data.pending_count;
  const parts = [];
  if (data.fully_covered) {
    parts.push(`Vyhodnoceno všech ${data.found_count} vyhlídek v okruhu ${radiusKm} km - "BEST" je skutečně nejlepší místo v okolí.`);
  } else {
    parts.push(
      `Zobrazeno ${data.results.length} vyhodnocených vyhlídek z ${data.found_count} nalezených v okruhu ${radiusKm} km - ` +
        `"BEST" je nejlepší z vyhodnocených míst, ne nutně nejlepší v celém okolí.`,
    );
  }
  if (pendingTotal > 0) parts.push(`${pendingTotal} dalších se připravuje na pozadí (přibudou při dalším hledání).`);
  if (data.previously_failed_count > 0) parts.push(`U ${data.previously_failed_count} míst se poslední pokus o terénní data nezdařil (zkouší se znovu).`);
  coverageNoteEl.textContent = parts.join(" ");
  coverageNoteEl.hidden = false;
}

const exploreUseMyLocationBtn = document.getElementById("explore-use-my-location-btn");
exploreUseMyLocationBtn.addEventListener("click", () => {
  setError(null);
  if (!navigator.geolocation) {
    setError("Tenhle prohlížeč nepodporuje zjištění polohy - zadej prosím místo ručně.");
    clearStaleResults();
    return;
  }
  exploreUseMyLocationBtn.disabled = true;
  setStatus("Zjišťuji tvoji polohu…");
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      try {
        await searchFromCenter({ lat: pos.coords.latitude, lon: pos.coords.longitude, display_name: "tvoje poloha" });
      } finally {
        exploreUseMyLocationBtn.disabled = false;
      }
    },
    (err) => {
      exploreUseMyLocationBtn.disabled = false;
      setError("Nepodařilo se získat tvoji polohu (" + err.message + "). Zkus prosím zadat místo ručně.");
      clearStaleResults();
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
  );
});

searchBtn.addEventListener("click", runSearch);
whereInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

function candidateTypeClass(type) {
  return { peak: "type-peak", viewpoint: "type-viewpoint", tower: "type-tower", castle: "type-castle" }[type] || "";
}

function renderMarkers(candidates, bestId) {
  candidateMarkers.forEach((m) => m.marker.remove());
  candidateMarkers = [];
  for (const c of candidates) {
    const { score } = displayScore(c);
    const wrap = document.createElement("div");
    wrap.className = "candidate-marker-wrap";
    wrap.dataset.type = c.type;
    wrap.dataset.best = c.id === bestId ? "1" : "0";

    const el = document.createElement("div");
    // Milník 12 ("Corona Bloom") - stejná "přiměřená varianta" jako
    // static/app.js renderCandidateMarkers() (plné zdůvodnění tam) -
    // jedna reprezentativní barva z CORONA_BLOOM_TIERS, ne plnohodnotný
    // meter (marker je na to příliš malý).
    el.className = `candidate-marker ${candidateTypeClass(c.type)}` + (c.id === bestId ? " is-best" : "");
    el.style.background = coronaBloomAccentColor(score);
    el.innerHTML = `<span>${score}</span>`;
    wrap.appendChild(el);

    wrap.addEventListener("click", (e) => {
      e.stopPropagation();
      selectCandidate(c.id, { flyTo: true });
    });

    const marker = new maplibregl.Marker({ element: wrap }).setLngLat([c.lon, c.lat]).addTo(map);
    candidateMarkers.push({ id: c.id, marker, wrap });
  }
  applyFilter(activeFilter);
}

function applyFilter(filter) {
  activeFilter = filter;
  for (const { wrap } of candidateMarkers) {
    const matches = filter === "all" || (filter === "best" && wrap.dataset.best === "1") || wrap.dataset.type === filter;
    wrap.style.display = matches ? "" : "none";
  }
  for (const card of resultsListEl.querySelectorAll(".explore-result-card")) {
    const matches = filter === "all" || (filter === "best" && card.dataset.best === "1") || card.dataset.type === filter;
    card.hidden = !matches;
  }
}

document.getElementById("explore-filter-chips").addEventListener("click", (e) => {
  const btn = e.target.closest(".filter-chip");
  if (!btn) return;
  document.querySelectorAll("#explore-filter-chips .filter-chip").forEach((c) => c.classList.remove("is-active"));
  btn.classList.add("is-active");
  applyFilter(btn.dataset.filter);
});

function renderResultsList(candidates, bestId) {
  resultsListEl.innerHTML = "";
  for (const c of candidates) {
    const { score, label } = displayScore(c);
    const card = document.createElement("div");
    card.className = "explore-result-card tap-tile";
    card.dataset.id = c.id;
    card.dataset.type = c.type;
    card.dataset.best = c.id === bestId ? "1" : "0";
    card.style.setProperty("--card-accent", coronaBloomAccentColor(score));
    const bestBit = c.id === bestId ? '<span class="explore-result-best">BEST</span>' : "";
    const typeBit = c.sunset_type && c.sunset_type.key ? ` · ${c.sunset_type.label}` : "";
    card.innerHTML = `
      <div class="explore-result-score">${coronaBloomSvg(score, 44)}</div>
      <div class="explore-result-info">
        <strong>${typeIconHtml(c.type)} ${c.name}</strong> ${bestBit}
        <div class="explore-result-meta">${label}${typeBit} · ${c.distance_km} km</div>
      </div>
    `;
    card.addEventListener("click", () => selectCandidate(c.id, { flyTo: true }));
    resultsListEl.appendChild(card);
  }
}

function selectCandidate(id, { flyTo }) {
  selectedId = id;
  const c = lastCandidates.find((cand) => cand.id === id);
  if (!c) return;

  for (const { id: mid, wrap } of candidateMarkers) wrap.classList.toggle("is-selected", mid === id);
  for (const card of resultsListEl.querySelectorAll(".explore-result-card")) {
    card.classList.toggle("is-selected", card.dataset.id === id);
  }
  if (flyTo) map.flyTo({ center: [c.lon, c.lat], zoom: Math.max(map.getZoom(), 12.5) });

  renderPreview(c);
}

function renderPreview(c) {
  const { score, label } = displayScore(c);
  const typeBit = c.sunset_type && c.sunset_type.key ? `<span class="sunset-type-badge">${c.sunset_type.label}</span>` : "";
  // Milník 12 (zadání bod 2/3) - lehké surfacování barevného/inverzního
  // potenciálu i na mapové preview kartě (zadání bod 6 "konzistentní
  // chování napříč appkou"), ÚMYSLNĚ jen jako malý doplňující badge
  // (title = plné zdůvodnění), ne celý preferences panel - Explore
  // zůstává map-first (viz komentáře výše u typeIconHtml).
  const colorBit = c.color_potential && c.color_potential.key
    ? `<span class="sunset-type-badge" title="${c.color_potential.detail || ""}">${c.color_potential.label}</span>` : "";
  const inversionBit = c.inversion_potential && c.inversion_potential.key && c.inversion_potential.key !== "unlikely"
    ? `<span class="sunset-type-badge ${c.inversion_potential.key === "favorable" ? "is-open" : "is-delayed"}" title="${c.inversion_potential.detail || ""}">${c.inversion_potential.label}</span>` : "";
  const cloudsBit = c.weather ? `${Math.round(c.weather.cloud_cover_pct)}% clouds · ` : "";
  const placeUrl = c.slug ? `/sunset/${c.slug}/?distance_km=${c.distance_km}` : null;
  // Milestone "Trustworthy Results with Partial Cache" - zadání bod 4:
  // soft, srozumitelná poznámka (ne syrové "36.1m/px"), jen když to má
  // skutečně význam pro důvěru v hodnocení (`reduced_dem_quality`).
  const demNoteBit = c.reduced_dem_quality
    ? `<p class="explore-preview-dem-note">ℹ️ Výškový model terénu pro tohle místo má omezenou přesnost.</p>` : "";
  previewBodyEl.innerHTML = `
    <p class="section-eyebrow">${typeIconHtml(c.type)} ${(c.type || "").toUpperCase()}</p>
    <h3>${c.name}</h3>
    ${typeBit} ${colorBit} ${inversionBit}
    <div class="explore-preview-score-row">
      ${coronaBloomSvg(score, 64)}
      <span class="explore-preview-score-label">${label}</span>
    </div>
    <p class="explore-preview-meta">${cloudsBit}${c.distance_km} km · ${c.measured_elevation_m.toFixed(0)} m elevation</p>
    ${demNoteBit}
    ${placeUrl ? `<a href="${placeUrl}" class="btn-primary explore-preview-cta">Open place page →</a>` : ""}
  `;
  previewEl.hidden = false;
}

document.getElementById("explore-preview-close").addEventListener("click", () => { previewEl.hidden = true; });

map.on("click", () => { previewEl.hidden = true; });
