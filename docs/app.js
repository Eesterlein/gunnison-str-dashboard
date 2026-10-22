// Gunnison STR Monitor - static dashboard. Data is rebuilt weekly by the GitHub Action.
(() => {
  // ---- Demo sign-in -------------------------------------------------------
  // A placeholder gate, not real security: the data files are public. Replace with real auth later.
  const DEMO_HASH = "cec5ba709203495e974ceb31d69098a71b901da5ebaa09116e59b483bcec47e0"; // sha256("user:password")
  const SESSION_KEY = "strmon-session";
  const PROPERTY_URL = (acct) => `https://property.spatialest.com/co/gunnison#/property/${encodeURIComponent(acct)}`;
  const COLORS = { High: "#1f8a5b", Medium: "#d9932b", Low: "#c8553d", Unmatched: "#7c6fb0" };
  const CONF_ORDER = ["High", "Medium", "Low", "Unmatched"];

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => (n ?? 0).toLocaleString();
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
    del(k) { try { sessionStorage.removeItem(k); } catch {} },
  };

  async function sha256(text) {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const h = await sha256(`${$("#user").value.trim().toLowerCase()}:${$("#pass").value}`);
    if (h === DEMO_HASH) { store.set(SESSION_KEY, "1"); start(); }
    else $("#loginErr").hidden = false;
  });
  $("#logout").addEventListener("click", () => { store.del(SESSION_KEY); location.reload(); });
  if (store.get(SESSION_KEY) === "1") start();

  // ---- State -------------------------------------------------------------
  let rows = [], runs = [], lastRun = null, map, markers, parcelLayer, sortKey = "first_seen", sortDir = -1, selected = null;
  const filters = { q: "", status: "active", conf: "", area: "", plat: "", view: false };

  async function start() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    const [listings, kpis, parcels, towns, county] = await Promise.all(
      ["data/listings.json", "data/kpis.json", "data/parcels.geojson", "data/towns.geojson", "data/county.geojson"].map((u) => fetch(u, { cache: "no-cache" }).then((r) => r.json()))
    );
    rows = listings;
    runs = kpis;
    lastRun = runs.at(-1)?.date;
    await new Promise((r) => setTimeout(r, 0));
    initMap(parcels, towns, county);
    initFilters();
    renderAll();
  }

  // ---- Filtering ---------------------------------------------------------
  function statusOf(r) {
    if (!r.active) return "delisted";
    return r.first_seen === lastRun && runs.length > 1 ? "new" : "active";
  }

  function filtered() {
    const q = filters.q.toLowerCase();
    const bounds = filters.view && map ? map.getBounds() : null;
    return rows.filter((r) => {
      const st = statusOf(r);
      if (filters.status === "active" && st === "delisted") return false;
      if (filters.status === "new" && st !== "new") return false;
      if (filters.status === "delisted" && st !== "delisted") return false;
      if (filters.conf && r.confidence !== filters.conf) return false;
      if (filters.area && r.area !== filters.area) return false;
      if (filters.plat && r.platform !== filters.plat) return false;
      if (bounds && !bounds.contains([r.lat, r.lng])) return false;
      if (q) {
        const hay = [r.OWNERNAME, r.PROPERTYLO, r.ACCOUNTNO, r.ParcelNumb, r.name, r.ONELINEOWN, r.SUBDIVISIO, r.area,
          ...(r.candidates || []).flatMap((c) => [c.account, c.address, c.owner])].join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function initFilters() {
    const areas = [...new Set(rows.filter((r) => r.active).map((r) => r.area).filter(Boolean))].sort();
    $("#fArea").insertAdjacentHTML("beforeend", areas.map((a) => `<option>${esc(a)}</option>`).join(""));
    const plats = [...new Set(rows.map((r) => r.platform))].sort();
    $("#fPlat").insertAdjacentHTML("beforeend", plats.map((a) => `<option>${esc(a)}</option>`).join(""));
    const bind = (sel, key, ev = "change", get = (el) => el.value) =>
      $(sel).addEventListener(ev, (e) => { filters[key] = get(e.target); renderAll(); });
    bind("#q", "q", "input");
    bind("#fStatus", "status");
    bind("#fConf", "conf");
    bind("#fArea", "area");
    bind("#fPlat", "plat");
    bind("#fView", "view", "change", (el) => el.checked);
    map.on("moveend", () => filters.view && renderAll(false));
    $("#clear").addEventListener("click", () => setFilters({ q: "", status: "active", conf: "", area: "", plat: "", view: false }));
    $("#dl").addEventListener("click", downloadCsv);
    $("#closeDetail").addEventListener("click", () => { $("#detail").hidden = true; selected = null; renderTable(); });
    document.querySelectorAll("th[data-k]").forEach((th) => th.addEventListener("click", () => {
      sortDir = sortKey === th.dataset.k ? -sortDir : 1;
      sortKey = th.dataset.k;
      renderTable();
    }));
  }

  function setFilters(f) {
    Object.assign(filters, f);
    $("#q").value = filters.q; $("#fStatus").value = filters.status; $("#fConf").value = filters.conf;
    $("#fArea").value = filters.area; $("#fPlat").value = filters.plat; $("#fView").checked = filters.view;
    renderAll();
  }

  // ---- Rendering ---------------------------------------------------------
  function renderAll(fit = false) {
    const f = filtered();
    renderKpis();
    renderBars(f);
    renderMap(f, fit);
    renderTable(f);
    renderMeta();
  }

  function renderMeta() {
    const r = runs.at(-1) || {};
    $("#scanMeta").innerHTML = `Last scan <b>${esc(r.date || "n/a")}</b><br>${runs.length} weekly scan${runs.length === 1 ? "" : "s"} on record`;
    const st = r.platform_status || {};
    const ex = r.excluded || {};
    $("#sources").innerHTML = `<b>Sources this scan</b><ul>${Object.entries(st).map(([k, v]) =>
      `<li>${esc(k[0].toUpperCase() + k.slice(1))}: ${v.ok ? `${fmt(v.count)} listings found county-wide` : `<span style="color:var(--low)">${v.count ? "partial" : "failed"}</span>`}</li>`).join("")}</ul>
      <p style="margin:8px 0 0">Excluded: ${Object.entries(ex).map(([k, v]) => `${esc(k)} (${fmt(v)})`).join(", ") || "none"}.
      Parcels: county GIS. Listings are matched to the nearest improved parcel; Airbnb offsets pins, so check <i>Low</i> matches.</p>`;
  }

  function renderKpis() {
    const cur = runs.at(-1) || {}, prev = runs.at(-2);
    const delta = (k) => {
      if (!prev) return `<div class="d flat">first scan</div>`;
      const d = (cur[k] ?? 0) - (prev[k] ?? 0);
      return `<div class="d ${d > 0 ? "up" : d < 0 ? "down" : "flat"}">${d > 0 ? "+" : ""}${fmt(d)} vs last week</div>`;
    };
    const card = (v, l, extra, click, wide) => `<div class="kpi${wide ? " wide" : ""}" data-click='${JSON.stringify(click)}'><div class="v">${fmt(v)}</div><div class="l">${l}</div>${extra}</div>`;
    $("#kpis").innerHTML =
      card(cur.active_listings, "Active STR listings", delta("active_listings"), { status: "active", conf: "" }, true) +
      card(cur.likely_accounts, "Likely STR accounts", `<div class="d flat">High + Medium matches</div>`, { status: "active", conf: "" }) +
      card(cur.new_listings, "New this week", `<div class="d flat">first seen ${esc(cur.date || "")}</div>`, { status: runs.length > 1 ? "new" : "active" }) +
      card(cur.delisted, "Delisted this week", `<div class="d flat">no longer found</div>`, { status: "delisted" }) +
      card(cur.unmatched, "Unmatched listings", `<div class="d flat">no parcel nearby</div>`, { status: "active", conf: "Unmatched" });
    document.querySelectorAll(".kpi").forEach((el) => el.addEventListener("click", () => setFilters({ ...JSON.parse(el.dataset.click), area: "", plat: "" })));
    renderTrend();
  }

  function renderTrend() {
    const pts = runs.map((r) => r.active_listings || 0);
    if (pts.length < 2) { $("#trend").innerHTML = `<p class="note">The trend line starts after the second weekly scan. Current: <b>${fmt(pts[0])}</b> active listings.</p>`; return; }
    const W = 300, H = 70, pad = 6, max = Math.max(...pts), min = Math.min(...pts);
    const x = (i) => pad + (i * (W - 2 * pad)) / (pts.length - 1);
    const y = (v) => H - pad - ((v - min) / (max - min || 1)) * (H - 2 * pad);
    const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    $("#trend").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="var(--brand)" stroke-width="2"/>
      ${pts.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="var(--brand)"><title>${esc(runs[i].date)}: ${v}</title></circle>`).join("")}</svg>
      <div class="sources" style="display:flex;justify-content:space-between"><span>${esc(runs[0].date)}</span><span>${esc(runs.at(-1).date)}</span></div>`;
  }

  function bars(el, counts, order, colorFn, key) {
    const entries = order ? order.filter((k) => counts[k]).map((k) => [k, counts[k]]) : Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map((e) => e[1]));
    el.innerHTML = entries.map(([k, v]) => `<div class="bar${filters[key] === k ? " on" : ""}" data-v="${esc(k)}"><span>${esc(k)}</span><span class="n">${fmt(v)}</span>
      <div class="track"><div class="fill" style="width:${(100 * v) / max}%;background:${colorFn(k)}"></div></div></div>`).join("") || `<p class="note">No listings match.</p>`;
    el.querySelectorAll(".bar").forEach((b) => b.addEventListener("click", () => setFilters({ [key]: filters[key] === b.dataset.v ? "" : b.dataset.v })));
  }

  function renderBars(f) {
    const count = (k) => f.reduce((m, r) => ((m[r[k] || "(unmatched)"] = (m[r[k] || "(unmatched)"] || 0) + 1), m), {});
    bars($("#confBars"), count("confidence"), CONF_ORDER, (k) => COLORS[k], "conf");
    const areas = count("area");
    const top = Object.fromEntries(Object.entries(areas).sort((a, b) => b[1] - a[1]).slice(0, 15));
    bars($("#areaBars"), top, null, () => "var(--brand)", "area");
    bars($("#platBars"), count("platform"), null, () => "var(--brand)", "plat");
  }

  // ---- Map ---------------------------------------------------------------
  function initMap(parcels, towns, county) {
    map = L.map("map", { preferCanvas: true }).setView([38.75, -106.9], 9);
    const esri = (svc, label) => L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`,
      { maxZoom: 19, maxNativeZoom: 18, attribution: `Esri ${label}` });
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const streets = dark ? esri("Canvas/World_Dark_Gray_Base", "Dark Gray") : esri("World_Topo_Map", "World Topo");
    const sat = esri("World_Imagery", "World Imagery");
    sat.addTo(map);
    const countyLayer = L.geoJSON(county, { interactive: false, style: { color: "#2f5d4e", weight: 3, fill: false } }).addTo(map);
    L.geoJSON(towns, { interactive: false, style: { color: "#8a9096", weight: 1, fillColor: "#8a9096", fillOpacity: 0.18, dashArray: "4 3" } }).addTo(map);
    parcelLayer = L.geoJSON(parcels, {
      style: (f) => ({ color: COLORS[f.properties.confidence] || "#555", weight: 1.5, fillOpacity: 0.15 }),
      onEachFeature: (f, l) => l.on("click", () => {
        const r = rows.find((x) => x.ACCOUNTNO === f.properties.ACCOUNTNO && x.active);
        if (r) select(r);
      }),
    }).addTo(map);
    markers = L.layerGroup().addTo(map);
    L.control.layers({ Satellite: sat, [dark ? "Dark" : "Topo"]: streets }, { "Matched parcels": parcelLayer, Listings: markers }).addTo(map);
    const legend = L.control({ position: "bottomright" });
    legend.onAdd = () => {
      const d = L.DomUtil.create("div", "legend");
      d.innerHTML = CONF_ORDER.map((k) => `<i style="background:${COLORS[k]}"></i>${k}`).join("<br>") +
        `<br><i style="background:#fff;border:2px solid var(--new)"></i>New this week<br><i class="town"></i>Town (excluded)<br><i class="county"></i>Gunnison County`;
      return d;
    };
    legend.addTo(map);
    // Fit after layout settles; animating a zoom on a just-shown container can freeze mid-zoom.
    setTimeout(() => {
      map.invalidateSize();
      map.fitBounds(countyLayer.getBounds(), { padding: [10, 10], animate: false });
    }, 50);
  }

  function renderMap(f) {
    markers.clearLayers();
    for (const r of f) {
      const st = statusOf(r);
      const m = L.circleMarker([r.lat, r.lng], {
        radius: st === "new" ? 8 : 6,
        color: st === "new" ? "#2563eb" : "#1d2420",
        weight: st === "new" ? 3 : 1,
        fillColor: COLORS[r.confidence],
        fillOpacity: st === "delisted" ? 0.35 : 0.9,
      });
      m.bindTooltip(`${esc(r.name)}<br><small>${esc(r.PROPERTYLO || "No parcel match")}</small>`);
      m.on("click", () => select(r));
      markers.addLayer(m);
    }
  }

  // ---- Table -------------------------------------------------------------
  function renderTable(f = filtered()) {
    const sorted = [...f].sort((a, b) => {
      const va = a[sortKey] ?? "", vb = b[sortKey] ?? "";
      if (sortKey === "confidence") return (CONF_ORDER.indexOf(va) - CONF_ORDER.indexOf(vb)) * sortDir;
      return String(va).localeCompare(String(vb), undefined, { numeric: true }) * sortDir;
    });
    const shown = sorted.slice(0, 1000);
    $("#count").textContent = `${fmt(f.length)} listing${f.length === 1 ? "" : "s"}${f.length > 1000 ? " (showing first 1,000; CSV has all)" : ""}`;
    $("#tbl tbody").innerHTML = shown.map((r) => {
      const st = statusOf(r);
      return `<tr data-k="${esc(r.listing_key)}" class="${selected === r.listing_key ? "sel" : ""}">
        <td><span class="pill ${r.confidence}">${r.confidence}</span>${st === "new" ? '<span class="tag new">NEW</span>' : st === "delisted" ? '<span class="tag gone">GONE</span>' : ""}</td>
        <td title="${esc(r.OWNERNAME)}">${esc(r.OWNERNAME || "n/a")}</td>
        <td title="${esc(r.PROPERTYLO)}">${esc(r.PROPERTYLO || r.name)}</td>
        <td title="${esc(r.ONELINEOWN)}">${esc(r.ONELINEOWN || "n/a")}</td>
        <td>${r.ACCOUNTNO ? `<a href="${PROPERTY_URL(r.ACCOUNTNO)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(r.ACCOUNTNO)}</a>` : "n/a"}</td>
        <td>${esc(r.area || "n/a")}</td><td>${esc(r.first_seen)}</td></tr>`;
    }).join("");
    $("#tbl tbody").querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", () => {
      const r = rows.find((x) => x.listing_key === tr.dataset.k);
      select(r, true);
    }));
  }

  // ---- Detail ------------------------------------------------------------
  function select(r, pan) {
    selected = r.listing_key;
    if (pan) map.setView([r.lat, r.lng], Math.max(map.getZoom(), 16), { animate: false });
    const st = statusOf(r);
    const dl = (pairs) => `<dl>${pairs.filter(([, v]) => v !== undefined).map(([k, v]) => `<dt>${k}</dt><dd>${v ?? "n/a"}</dd>`).join("")}</dl>`;
    const confText = {
      High: "Only one improved parcel near the pin. Very likely this account.",
      Medium: "Two or three improved parcels near the pin. Check the candidates below.",
      Low: "Dense area with many parcels near the pin. Use listing photos to pick the right unit from the candidates.",
      Unmatched: "No improved parcel within 800 ft of the pin. Could be new construction, or the pin is far off.",
    }[r.confidence];
    $("#detailBody").innerHTML = `
      <h2>${esc(r.PROPERTYLO || r.name)}</h2>
      <div class="sub"><span class="pill ${r.confidence}">${r.confidence} match</span> ${st === "new" ? '<span class="tag new">NEW</span>' : st === "delisted" ? `<span class="tag gone">DELISTED ${esc(r.delisted_on)}</span>` : ""}</div>
      <div class="actions">
        ${r.ACCOUNTNO ? `<a class="primary" style="padding:8px 12px;border-radius:8px;color:var(--brand-ink);background:var(--brand)" href="${PROPERTY_URL(r.ACCOUNTNO)}" target="_blank" rel="noopener">Property record</a>` : ""}
        <a class="ghost" style="padding:7px 12px;border:1px solid var(--line);border-radius:8px" href="${esc(r.url)}" target="_blank" rel="noopener">View on ${esc(r.platform)}</a>
      </div>
      <h4>Property (county GIS)</h4>
      ${dl([["Owner", esc(r.OWNERNAME)], ["Mailing address", esc(r.ONELINEOWN)], ["Property address", esc(r.PROPERTYLO)],
        ["Account", esc(r.ACCOUNTNO)], ["Parcel", esc(r.ParcelNumb)], ["Area", esc(r.area)], ["Subdivision", esc(r.SUBDIVISIO)],
        ["Tax district", esc(r.TAXDISTRIC)], ["Account type", esc(r.ACCOUNTTYP)], ["Actual value", r.TOTALACTUA ? "$" + fmt(r.TOTALACTUA) : null]])}
      <h4>Listing</h4>
      ${dl([["Name", esc(r.name)], ["Description", esc(r.description)], ["Beds/baths", esc(r.beds_baths)], ["Rating", esc(r.rating)],
        ["Platform", esc(r.platform)], ["Pin (lat, lng)", `${r.lat?.toFixed(5)}, ${r.lng?.toFixed(5)}`], ["First seen", esc(r.first_seen)],
        ["Last seen", esc(r.last_seen)], ["Pin to parcel", r.dist_ft != null ? `${fmt(r.dist_ft)} ft` : null]])}
      <p class="note">${confText}</p>
      ${(r.candidates || []).length > 1 ? `<h4>Candidate parcels within 300 ft</h4><table class="cands">${r.candidates.map((c) =>
        `<tr><td><a href="${PROPERTY_URL(c.account)}" target="_blank" rel="noopener">${esc(c.account)}</a></td><td>${esc(c.address)}<br><small>${esc(c.owner)}</small></td><td>${c.ft} ft</td></tr>`).join("")}</table>` : ""}`;
    $("#detail").hidden = false;
    renderTable();
  }

  // ---- CSV ---------------------------------------------------------------
  function downloadCsv() {
    const f = filtered();
    const cols = [
      ["account", "ACCOUNTNO"], ["parcel_number", "ParcelNumb"], ["owner_name", "OWNERNAME"], ["mailing_street", "OWNERSTREE"], ["mailing_city", "OWNERCITY"],
      ["mailing_state", "OWNERSTATE"], ["mailing_zip", "OWNERZIP"], ["mailing_address_full", "ONELINEOWN"],
      ["mailing_address_missing", (r) => (r.ACCOUNTNO && !r.OWNERSTREE ? "YES" : "")],
      ["property_address", "PROPERTYLO"], ["area", "area"], ["subdivision", "SUBDIVISIO"], ["tax_district", "TAXDISTRIC"],
      ["match_confidence", "confidence"], ["status", (r) => statusOf(r)], ["platform", "platform"], ["listing_name", "name"],
      ["listing_url", "url"], ["latitude", "lat"], ["longitude", "lng"], ["first_seen", "first_seen"], ["last_seen", "last_seen"],
      ["property_record_url", (r) => (r.ACCOUNTNO ? PROPERTY_URL(r.ACCOUNTNO) : "")],
      ["candidate_accounts", (r) => (r.candidates || []).map((c) => c.account).join(" ")],
    ];
    const cell = (v) => {
      const s = String(v ?? "");
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [cols.map((c) => c[0]).join(",")].concat(
      f.map((r) => cols.map(([, k]) => cell(typeof k === "function" ? k(r) : r[k])).join(","))
    );
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `gunnison-unincorporated-STR-${lastRun}${filters.area ? "-" + filters.area.replace(/\W+/g, "_") : ""}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }
})();
