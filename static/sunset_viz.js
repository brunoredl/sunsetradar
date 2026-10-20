// Sdílené čistě prezentační pomůcky pro NOVÉ stránky place.js/explore.js
// (milestone "Complete Product Build", pokračování - place pages +
// Explore page). Stejná geometrie/logika jako v static/app.js (Tonight
// page) - Tonight page samotná zůstává BEZE ZMĚNY (zadání "nepřepisuj
// fungující engine", riziko regrese na už ověřeném flow), tenhle soubor
// jen zabraňuje duplikaci mezi DVĚMA novými strankami.

const COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
function azimuthToCompass(deg) {
  const index = Math.round((((deg % 360) + 360) % 360) / 45) % 8;
  return COMPASS_POINTS[index];
}

// Milník 12 (zadání bod 1 - "obloha má odpovídat předpovídaným
// podmínkám") - stejný princip jako static/app.js applySkyMood()
// (Tonight hero), zobecněný na libovolný element - place.js ho používá
// pro `#place-hero` podle `tonight.color_potential.key`, žádná nová
// grafika, jen `.sky-mood-*` třída (viz style.css).
function applySkyMoodClass(el, colorKey) {
  if (!el) return;
  el.classList.remove("sky-mood-vivid", "sky-mood-warm", "sky-mood-muted");
  if (colorKey === "vivid" || colorKey === "warm" || colorKey === "muted") {
    el.classList.add(`sky-mood-${colorKey}`);
  }
}

// Milník 12 ("Corona Bloom") - sluneční ukazatel skóre, náhrada za staré
// ploché kolečko/prstenec. Hranice kategorií jsou SCHVÁLNĚ STEJNÉ jako
// existující `_composite_label()` v app/main.py (85/70/50/30 - Excellent/
// Good/Fair/Poor+Unfavorable) - žádné nové, nekonzistentní hranice. "Poor"
// barevně kryje i "Unfavorable" (<30) - appka má pro Corona Bloom jen 4
// barevné kategorie (zadání), ne pět jako textové popisky.
const CORONA_BLOOM_TIERS = {
  // >= 85 "Excellent conditions"
  excellent: {
    rings: [
      { color: "#9a4fa0", opacity: 0.17 }, // outer - fialová (soumrak nad obzorem)
      { color: "#ff6f8a", opacity: 0.27 }, // mid - růžová
      { color: "#ff8f4d", opacity: 0.43 }, // inner - oranžová
    ],
    core: { fill: "#171b28", stroke: "#ffecc7" },
  },
  // 70-84 "Good conditions"
  good: {
    rings: [
      { color: "#ff8f4d", opacity: 0.19 },
      { color: "#ffb066", opacity: 0.33 },
    ],
    core: { fill: "#171b28", stroke: "#ffb066" },
  },
  // 50-69 "Fair conditions"
  fair: {
    rings: [
      { color: "#f2a559", opacity: 0.16 },
      { color: "#f2a559", opacity: 0.29 },
    ],
    core: { fill: "#2e2316", stroke: "#f2a559" },
  },
  // < 50 "Poor conditions" / "Unfavorable conditions" - vybledlá, záměrně
  // NEláká (zadání "nesmí vypadat jako by západ vypadal hezky").
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

// Poloměr jednoho prstence podle jeho pozice OD JÁDRA SMĚREM VEN
// (`fromInside` 0 = nejblíž jádru) a skutečného skóre (0-1) - víc
// skóre = víc "rozsvíceného" slunce, stejná kontinuální škála pro
// všechny kategorie (jen jiné barvy/počet prstenců).
function coronaBloomRingRadius(fromInside, scoreFrac) {
  if (fromInside === 0) return 30 + scoreFrac * 17;
  if (fromInside === 1) return 36 + scoreFrac * 20;
  return 40 + scoreFrac * 18;
}

// Vykreslí Corona Bloom SVG jako řetězec. `score` - skutečné 0-100,
// nebo null/undefined/NaN pro "zatím nevyhodnoceno" (zadání "nezobrazuj
// skóre u míst, která ho ještě nemají vypočtené" - stejná poctivost
// jako `.sunset-card.is-waiting`/mapové waiting-tečky jinde v appce,
// ŽÁDNÉ vymyšlené číslo). `sizePx` - vykreslená velikost v px (viewBox
// je VŽDY "0 0 120 120", jen se škáluje - prstence i číslo se tak
// přirozeně zmenší/zvětší spolu, žádná druhá sada souřadnic navíc).
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

// Zadání bod 3 - mapové markery jsou příliš malé na plnohodnotný
// vícevrstvý Corona Bloom meter ("pokud by tím mapa ztratila
// čitelnost... použij přiměřenou variantu stejného vizuálního
// systému") - tahle funkce vrací JEDNU reprezentativní barvu z PŘÍMO
// `CORONA_BLOOM_TIERS` (žádná nová paleta navíc) pro malé plochy
// (markery, tenké accent proužky karet), kde malé opacity-vrstvy
// Corona Bloom nejsou čitelné.
function coronaBloomAccentColor(score) {
  const numeric = score === null || score === undefined ? NaN : Number(score);
  if (Number.isNaN(numeric)) return CORONA_BLOOM_TIERS.poor.core.stroke;
  const clamped = Math.max(0, Math.min(100, numeric));
  const tier = coronaBloomTier(clamped);
  // outermost ring barva každé kategorie = nejvíc "charakteristická"
  // barva tý kategorie (fialová/oranžová/jantarová/šedá, viz zadání).
  return CORONA_BLOOM_TIERS[tier].rings[0].color;
}

const HIKE_ACCESS_LABELS = { easy: "Easy access", moderate: "Moderate hike", hike: "Hike", adventure: "Adventure" };

function normalizeAzimuth(deg) {
  return ((deg % 360) + 360) % 360;
}

// Standardní "spherical law" destinační bod - stejný vzorec jako
// app.js::destinationPoint, čistě geometrická projekce ZNÁMÉHO azimutu.
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

function circlePolygon(lat, lon, radiusKm, steps = 64) {
  const coords = [];
  for (let i = 0; i <= steps; i++) {
    const bearing = (360 * i) / steps;
    coords.push(destinationPoint(lat, lon, bearing, radiusKm * 1000));
  }
  return { type: "Polygon", coordinates: [coords] };
}

// Vykreslí 360° terénní horizont do canvasu - STEJNÁ vizualizace jako
// Tonight page panorama (app.js::drawPanoramaFrame), jen bez závislosti
// na globálním `panoramaResult`/DOM ID - parametry se předávají přímo,
// aby ji šlo použít z place.js nad datem z /api/place/{slug}.
//
// Zpětná vazba (živé použití) - "z toho moc nevyčtu, jen že západ je na
// západě, to ví každý" - AUDIT NÁLEZ (skutečný render): reálné rozdíly
// výškového úhlu terénu kolem azimutu západu jsou typicky jen 1-3°
// (tahle appka necílí na útesy, ale na běžné české kopce/vyhlídky), při
// starém měřítku `alt * 6` px/stupeň to na 380px plátně vyšlo na pár
// pixelů - vizuálně nerozeznatelné od šumu, takže jediná SKUTEČNÁ
// informace, co šla přečíst, byla textový popisek "sunset XXX°". Tenhle
// přepočet NEMĚNÍ, jak se čte "co je kde" (furt stejná data, stejné
// stupně), jen je čitelně PŘEVÝŠÍ (běžná praxe i u skutečných panorama
// aplikací typu PeakFinder) + přidává referenční linku "plochého
// horizontu" (0°), takže jde VIZUÁLNĚ porovnat "terén je NAD/POD touhle
// čarou", ne jen věřit barvě tenké čárkované linky.
const PANORAMA_FOV_DEG = 100;
const PANORAMA_ALT_PX_PER_DEG = 16; // dřív 6 - viz komentář výše
function drawPanoramaFrame(canvas, { azimuths, altitudes, sunsetAzimuthDeg, obstructed, viewCenterDeg }) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  const horizonY = H * 0.78;

  const azToX = (az) => {
    let diff = normalizeAzimuth(az) - normalizeAzimuth(viewCenterDeg);
    if (diff > 180) diff -= 360;
    if (diff < -180) diff += 360;
    return W / 2 + (diff / PANORAMA_FOV_DEG) * W;
  };
  // Clamp - ať opravdu strmý reálný terén (desítky stupňů jinde na
  // obzoru) nevyjede z plátna úplně nahoru, ne proto, že by data byla
  // nepřesná (jsou - clamp je čistě o tom, aby zůstal vidět i vršek).
  const altToY = (alt) => Math.max(H * 0.04, horizonY - (alt === null || alt === undefined ? 0 : alt) * PANORAMA_ALT_PX_PER_DEG);

  const sky = ctx.createLinearGradient(0, 0, 0, horizonY);
  sky.addColorStop(0, "#0d0f1c");
  sky.addColorStop(0.55, "#241c35");
  sky.addColorStop(1, "#4a2f3a");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, W, H);

  const sunX = azToX(sunsetAzimuthDeg);
  if (sunX > -200 && sunX < W + 200) {
    const glow = ctx.createRadialGradient(sunX, horizonY, 0, sunX, horizonY, W * 0.35);
    glow.addColorStop(0, "rgba(255, 176, 102, 0.55)");
    glow.addColorStop(1, "rgba(255, 176, 102, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H);
  }

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
  // terénní siluetě (NE před ní - reálný terén je skoro vždy aspoň
  // trochu nad 0°, takže dřívější pořadí by čáru prakticky vždy úplně
  // zakrylo pod výplní, a nedala by se s ničím porovnat). Vykreslená
  // navrch, vždy viditelná, aby šlo na první pohled srovnat "terén je
  // nad/pod tím, co by viděl pozorovatel na moři" - stejný trik jako
  // skutečné panorama/skyline aplikace.
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
    ctx.fillText(`sunset ${sunsetAzimuthDeg.toFixed(0)}°`, sunX, 52);
  }
}

// Zpětná vazba - panorama samo o sobě (tvar + barva čárkované linky)
// nikomu nic neřekla bez znalosti, co barva znamená. Jedna věta pod
// grafem to řekne přímo slovy, ze STEJNÝCH už ověřených/zobrazených
// čísel jako jinde na stránce (`visibility.terrain_obstruction`/
// `delay_minutes` - skutečná simulace dráhy slunce přes terén, ne
// nový odhad jen z úhlu v jednom azimutu) - žádná nová metrika, jen
// srozumitelná věta navíc k už existujícím datům.
function panoramaCaptionText({ obstructed, delayMinutes, sunsetAzimuthDeg }) {
  const compass = azimuthToCompass(sunsetAzimuthDeg);
  if (!obstructed) {
    return `Horizon is open toward ${compass} (${sunsetAzimuthDeg.toFixed(0)}°) - the terrain profile dips below the dotted "flat horizon" line there, so the sun reaches the true horizon with no delay.`;
  }
  const minutes = Math.round(delayMinutes);
  return `Terrain rises above the dotted "flat horizon" line toward ${compass} (${sunsetAzimuthDeg.toFixed(0)}°) - it hides the sun about ${minutes} minute${minutes === 1 ? "" : "s"} before it would set behind a flat horizon.`;
}
