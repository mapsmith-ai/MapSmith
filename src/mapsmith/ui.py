"""The in-chat map panel (MCP Apps, SEP-1865).

Design constraint that shapes everything here: the extension's DEFAULT content
security policy allows no network at all (connect-src 'none'), and the one
open host bug tracks CSP grants being ignored. So the panel is 100%
self-contained: the JSON-RPC postMessage handshake is hand-rolled to the
2026-01-26 spec (the official JS SDK would need a CDN), rendering is a small
canvas engine (no MapLibre, no WebGL, no workers), rasters arrive as data URIs
(img-src data: is allowed by the default CSP). A tile-based MapLibre upgrade
behind declared CSP domains is a follow-up once tested on real hosts.

The panel receives the preview payload produced by mapsmith.preview via the
ui/notifications/tool-result notification (structuredContent).
"""

from __future__ import annotations

MAP_UI_URI = "ui://mapsmith/map-panel.html"

MAP_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>MapSmith map panel</title>
<style>
  :root { --bg:#ffffff; --ink:#1a1d21; --muted:#6a7178; --line:#e3e6e9;
          --accent:#2563eb; --ok:#15803d; --bad:#b91c1c; --panel:#f6f7f8; }
  [data-theme="dark"] { --bg:#15181b; --ink:#e8eaed; --muted:#9aa2ab;
          --line:#31363c; --accent:#60a5fa; --ok:#4ade80; --bad:#f87171;
          --panel:#1d2125; }
  * { box-sizing:border-box; margin:0; }
  html,body { height:100%; }
  body { background:var(--bg); color:var(--ink);
         font:13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #app { display:flex; height:100%; }
  #map-wrap { position:relative; flex:1 1 auto; min-width:0; }
  #map { width:100%; height:100%; display:block; cursor:grab; }
  #status { position:absolute; left:10px; top:10px; padding:4px 10px;
            background:var(--panel); border:1px solid var(--line);
            border-radius:6px; color:var(--muted); }
  #side { flex:0 0 240px; overflow-y:auto; border-left:1px solid var(--line);
          background:var(--panel); padding:12px; }
  #side h1 { font-size:13px; letter-spacing:.4px; margin-bottom:10px; }
  .layer { border:1px solid var(--line); border-radius:8px; padding:8px;
           margin-bottom:8px; background:var(--bg); }
  .layer-head { display:flex; align-items:center; gap:7px; }
  .chip { width:11px; height:11px; border-radius:3px; flex:0 0 auto; }
  .layer-name { font-weight:600; overflow:hidden; text-overflow:ellipsis;
                white-space:nowrap; flex:1 1 auto; }
  .meta, .prov { color:var(--muted); font-size:12px; margin-top:4px; }
  .ok { color:var(--ok); font-weight:600; }
  .bad { color:var(--bad); font-weight:600; }
  .hidden-layer { opacity:.45; }
  footer { color:var(--muted); font-size:11px; margin-top:10px; }
</style>
</head>
<body>
<div id="app">
  <div id="map-wrap"><canvas id="map"></canvas><div id="status">connecting to host…</div></div>
  <aside id="side"><h1>MapSmith</h1><div id="layers"></div>
    <footer>preview in EPSG:4326 · datasets and manifests stay on disk</footer>
  </aside>
</div>
<script>
"use strict";
/* ---- JSON-RPC 2.0 over postMessage, per MCP Apps spec 2026-01-26 ---- */
var nextId = 1, pending = {};
function request(method, params) {
  return new Promise(function (resolve, reject) {
    var id = nextId++;
    pending[id] = { resolve: resolve, reject: reject };
    parent.postMessage({ jsonrpc: "2.0", id: id, method: method, params: params }, "*");
  });
}
function notify(method, params) {
  parent.postMessage({ jsonrpc: "2.0", method: method, params: params || {} }, "*");
}
window.addEventListener("message", function (ev) {
  if (ev.source !== window.parent) return;   // only the host may talk to us
  var m = ev.data;
  if (!m || m.jsonrpc !== "2.0") return;
  if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
    var p = pending[m.id];
    if (p) { delete pending[m.id]; if (m.error) p.reject(m.error); else p.resolve(m.result); }
    return;
  }
  if (m.id !== undefined && m.method) {          // host request: answer, never hang it
    if (m.method === "ui/resource-teardown") {
      parent.postMessage({ jsonrpc: "2.0", id: m.id, result: {} }, "*");
    } else {
      parent.postMessage({ jsonrpc: "2.0", id: m.id,
        error: { code: -32601, message: "method not found" } }, "*");
    }
    return;
  }
  if (m.method === "ui/notifications/tool-result") { onToolResult(m.params || {}); }
  if (m.method === "ui/notifications/host-context-changed") { applyTheme(m.params); }
});
function applyTheme(ctx) {
  var theme = ctx && (ctx.theme || (ctx.hostContext && ctx.hostContext.theme));
  if (theme) document.documentElement.setAttribute("data-theme", theme);
}
function setStatus(text) {
  var el = document.getElementById("status");
  el.textContent = text; el.style.display = text ? "block" : "none";
}
(function init() {
  request("ui/initialize", {
    protocolVersion: "2026-01-26",
    clientInfo: { name: "MapSmith map panel", version: "1.0.0" },
    capabilities: {},
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] }
  }).then(function (res) {
    applyTheme(res && res.hostContext ? res.hostContext : res);
    notify("ui/notifications/initialized", {});
    setStatus("waiting for map data…");
  }).catch(function () { setStatus("host handshake failed"); });
})();

/* ---- payload intake ---- */
var PAYLOAD = null;
function onToolResult(result) {
  var data = result.structuredContent;
  if (!data && result.content) {           // fallback: parse the text content
    for (var i = 0; i < result.content.length; i++) {
      if (result.content[i].type === "text") {
        try { data = JSON.parse(result.content[i].text); break; } catch (e) {}
      }
    }
  }
  if (!data || !data.layers) { setStatus("no map data in tool result"); return; }
  PAYLOAD = data;
  PAYLOAD.layers.forEach(function (l) { l._visible = true; });
  preloadRasters(); buildSidebar(); fit(PAYLOAD.bounds); setStatus(""); draw();
}
function preloadRasters() {
  PAYLOAD.layers.forEach(function (l) {
    if (l.kind === "raster" && l.png_data_uri) {
      l._img = new Image();
      l._img.onload = draw;
      l._img.src = l.png_data_uri;   // data: URI, allowed by the default CSP
    }
  });
}

/* ---- canvas map: equirectangular with latitude correction ---- */
var canvas = document.getElementById("map"), ctx2d = canvas.getContext("2d");
var view = { cx: 0, cy: 0, scale: 1, cosLat: 1 };   // world center + px per degree
var userInteracted = false;
var PALETTE = ["#2563eb", "#d97706", "#059669", "#dc2626", "#7c3aed", "#0891b2"];
function resize() {
  var r = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.max(1, r.width * devicePixelRatio);
  canvas.height = Math.max(1, r.height * devicePixelRatio);
  // data may arrive before the first ResizeObserver tick: refit until the user pans
  if (PAYLOAD && !userInteracted) fit(PAYLOAD.bounds);
  draw();
}
new ResizeObserver(resize).observe(canvas.parentElement);
function fit(b) {
  var midLat = (b[1] + b[3]) / 2;
  view.cosLat = Math.max(0.05, Math.cos(midLat * Math.PI / 180));
  view.cx = (b[0] + b[2]) / 2; view.cy = midLat;
  var spanX = Math.max(1e-6, (b[2] - b[0]) * view.cosLat);
  var spanY = Math.max(1e-6, b[3] - b[1]);
  view.scale = Math.min(canvas.width / spanX, canvas.height / spanY) / 1.15;
}
function px(lon, lat) {
  return [canvas.width / 2 + (lon - view.cx) * view.cosLat * view.scale,
          canvas.height / 2 - (lat - view.cy) * view.scale];
}
function drawRing(ring) {
  for (var i = 0; i < ring.length; i++) {
    var p = px(ring[i][0], ring[i][1]);
    if (i === 0) ctx2d.moveTo(p[0], p[1]); else ctx2d.lineTo(p[0], p[1]);
  }
}
function drawGeom(geom, color) {
  var t = geom.type, c = geom.coordinates;
  if (t === "Point") { drawPoint(c, color); }
  else if (t === "MultiPoint") { c.forEach(function (p) { drawPoint(p, color); }); }
  else if (t === "LineString") { strokePath([c], color); }
  else if (t === "MultiLineString") { strokePath(c, color); }
  else if (t === "Polygon") { fillPolygons([c], color); }
  else if (t === "MultiPolygon") { fillPolygons(c, color); }
  else if (t === "GeometryCollection") {
    (geom.geometries || []).forEach(function (g) { drawGeom(g, color); });
  }
}
function drawPoint(c, color) {
  var p = px(c[0], c[1]);
  ctx2d.beginPath(); ctx2d.arc(p[0], p[1], 4 * devicePixelRatio, 0, 7);
  ctx2d.fillStyle = color; ctx2d.fill();
  ctx2d.strokeStyle = "#ffffff"; ctx2d.lineWidth = devicePixelRatio; ctx2d.stroke();
}
function strokePath(lines, color) {
  ctx2d.beginPath(); lines.forEach(drawRing);
  ctx2d.strokeStyle = color; ctx2d.lineWidth = 2 * devicePixelRatio; ctx2d.stroke();
}
function fillPolygons(polygons, color) {
  // each polygon is an array of rings (outer + holes); evenodd handles the holes
  ctx2d.beginPath();
  polygons.forEach(function (rings) {
    rings.forEach(function (ring) { drawRing(ring); ctx2d.closePath(); });
  });
  ctx2d.fillStyle = color + "33"; ctx2d.fill("evenodd");
  ctx2d.strokeStyle = color; ctx2d.lineWidth = 1.5 * devicePixelRatio; ctx2d.stroke();
}
function draw() {
  if (!ctx2d) return;
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  if (!PAYLOAD) return;
  PAYLOAD.layers.forEach(function (l, i) {
    if (!l._visible) return;
    if (l.kind === "raster" && l._img && l._img.complete) {
      var a = px(l.bounds[0], l.bounds[3]), b = px(l.bounds[2], l.bounds[1]);
      ctx2d.imageSmoothingEnabled = false;
      ctx2d.drawImage(l._img, a[0], a[1], b[0] - a[0], b[1] - a[1]);
    } else if (l.geojson) {
      var color = PALETTE[i % PALETTE.length];
      (l.geojson.features || []).forEach(function (f) {
        if (f.geometry) drawGeom(f.geometry, color);
      });
    }
  });
}

/* ---- interactions: drag pan, wheel zoom, dblclick refit ---- */
var dragging = null;
canvas.addEventListener("mousedown", function (e) {
  userInteracted = true;
  dragging = { x: e.clientX, y: e.clientY }; canvas.style.cursor = "grabbing";
});
window.addEventListener("mousemove", function (e) {
  if (!dragging) return;
  view.cx -= (e.clientX - dragging.x) * devicePixelRatio / (view.scale * view.cosLat);
  view.cy += (e.clientY - dragging.y) * devicePixelRatio / view.scale;
  dragging = { x: e.clientX, y: e.clientY }; draw();
});
window.addEventListener("mouseup", function () { dragging = null; canvas.style.cursor = "grab"; });
canvas.addEventListener("wheel", function (e) {
  e.preventDefault();
  userInteracted = true;
  view.scale *= (e.deltaY < 0 ? 1.25 : 0.8);
  draw();
}, { passive: false });
canvas.addEventListener("dblclick", function () {
  userInteracted = false;
  if (PAYLOAD) { fit(PAYLOAD.bounds); draw(); }
});

/* ---- sidebar: layers + provenance cards ---- */
function buildSidebar() {
  var box = document.getElementById("layers");
  box.replaceChildren();
  PAYLOAD.layers.forEach(function (l, i) {
    var div = document.createElement("div");
    div.className = "layer";
    var head = document.createElement("div"); head.className = "layer-head";
    var chip = document.createElement("span"); chip.className = "chip";
    chip.style.background = l.kind === "raster" ? "#666" : PALETTE[i % PALETTE.length];
    var name = document.createElement("span"); name.className = "layer-name";
    name.textContent = l.name; name.title = l.path;
    head.appendChild(chip); head.appendChild(name);
    div.appendChild(head);
    var meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = l.kind === "raster"
      ? "raster · " + l.width + "×" + l.height + " px · " + l.crs_original
      : l.feature_count + " features" + (l.truncated ? " (showing " +
        l.geojson.features.length + ")" : "") + " · " + l.crs_original;
    div.appendChild(meta);
    var prov = document.createElement("div"); prov.className = "prov";
    if (l.provenance) {
      // textContent only: manifest strings come from disk, never trust them as HTML
      prov.appendChild(document.createTextNode(
        (l.provenance.operation || "?") + " · " + (l.provenance.engine || "?") + " · "));
      var badge = document.createElement("span");
      badge.className = l.provenance.verified ? "ok" : "bad";
      badge.textContent = l.provenance.verified ? "verified ✓" : "not verified";
      prov.appendChild(badge);
      if (l.provenance.crs_reason) prov.title = l.provenance.crs_reason;
    } else {
      prov.textContent = "no provenance manifest";
    }
    div.appendChild(prov);
    div.addEventListener("click", function () {
      l._visible = !l._visible;
      div.classList.toggle("hidden-layer", !l._visible);
      draw();
    });
    box.appendChild(div);
  });
}
</script>
</body>
</html>
"""
