"""Inline stylesheet and the small vanilla-JS enhancement.

Everything here is embedded in the page: no CDN, no external fonts, no build
step. The Javascript only powers the live feed (polling ``/api/feed``) and the
copy-to-clipboard button; every page is fully readable with JS disabled.
"""

from __future__ import annotations

from .theme import root_variables

# The stylesheet is a plain string (not an f-string) because CSS is full of
# braces. The palette is injected through the ``__ROOT__`` sentinel.
_CSS_TEMPLATE = """
:root {
__ROOT__  --mono: ui-monospace, "SFMono-Regular", "SF Mono", "JetBrains Mono",
          "Fira Code", "Cascadia Code", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
          sans-serif;
  --radius: 10px;
  --radius-sm: 6px;
  --maxw: 1200px;
}

* { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  min-height: 100vh;
  background-color: var(--bg);
  background-image:
    radial-gradient(1100px 520px at 12% -12%, rgba(122, 162, 247, 0.08), transparent 62%),
    radial-gradient(900px 460px at 108% 0%, rgba(122, 162, 247, 0.045), transparent 58%);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.5;
  letter-spacing: 0.01em;
}

/* A faint technical grid, drawn behind everything. */
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background-image:
    linear-gradient(to right, rgba(47, 42, 36, 0.5) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(47, 42, 36, 0.5) 1px, transparent 1px);
  background-size: 34px 34px;
  -webkit-mask-image: radial-gradient(ellipse 120% 90% at 50% 0%, #000 25%, transparent 80%);
  mask-image: radial-gradient(ellipse 120% 90% at 50% 0%, #000 25%, transparent 80%);
  opacity: 0.35;
}

body > * { position: relative; z-index: 1; }

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hi); text-decoration: underline; text-underline-offset: 3px; }

h1, h2, h3 { font-family: var(--mono); font-weight: 600; letter-spacing: 0.02em; }

code, .mono { font-family: var(--mono); font-size: 0.86em; }

.muted { color: var(--muted); }
.faint { color: var(--faint); }
.accent { color: var(--accent); }
.ok { color: var(--ok); }
.warn { color: var(--warn); }
.err { color: var(--err); }

.wrap { max-width: var(--maxw); margin: 0 auto; padding: 0 20px 64px; }

/* -- chrome -------------------------------------------------------------- */

.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}

.topbar-inner {
  max-width: var(--maxw);
  margin: 0 auto;
  padding: 0 20px;
  height: 54px;
  display: flex;
  align-items: center;
  gap: 18px;
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-family: var(--mono);
  font-size: 15px;
  color: var(--text);
  white-space: nowrap;
}
.brand:hover { text-decoration: none; }
.brand .mark { color: var(--accent); font-size: 17px; }
.brand .sub { color: var(--faint); font-size: 12px; letter-spacing: 0.06em; }

.nav { display: flex; gap: 4px; margin-left: auto; flex-wrap: wrap; }
.nav a {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.05em;
  color: var(--muted);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}
.nav a:hover { color: var(--text); background: var(--panel-hi); text-decoration: none; }
.nav a.active { color: var(--accent); border-color: var(--border-hi); background: var(--accent-dim); }

/* -- hero / headings ----------------------------------------------------- */

.crumbs {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--faint);
  padding: 20px 0 6px;
}
.crumbs a { color: var(--muted); }

.hero { padding: 10px 0 22px; }
.hero h1 {
  margin: 0 0 6px;
  font-size: clamp(21px, 3.2vw, 30px);
  color: var(--text);
}
.hero .lede { color: var(--muted); max-width: 72ch; margin: 0; }
.hero .lede strong { color: var(--accent); font-weight: 600; }

.cards-title { font-size: clamp(18px, 2.4vw, 23px); color: var(--accent); margin: 0; }

/* -- panels -------------------------------------------------------------- */

.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin: 0 0 18px;
  box-shadow: inset 0 1px 0 rgba(222, 216, 204, 0.03);
  animation: rise 0.45s cubic-bezier(0.22, 1, 0.36, 1) both;
  animation-delay: calc(var(--i, 0) * 55ms);
}

.panel-title {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--faint);
  margin: 0 0 14px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.panel-title::after {
  content: "";
  flex: 1;
  height: 1px;
  background: linear-gradient(to right, var(--border), transparent);
}

.grid { display: grid; gap: 18px; }
.grid.two { grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); }
.grid.halves { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }

/* -- meta / provenance --------------------------------------------------- */

.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px 22px;
  margin: 0;
}
.meta dt {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--faint);
  margin: 0 0 3px;
}
.meta dd { margin: 0; font-family: var(--mono); font-size: 13px; color: var(--text); word-break: break-word; }

.replay {
  display: flex;
  align-items: stretch;
  gap: 8px;
  margin-top: 14px;
}
.replay code {
  flex: 1;
  min-width: 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  color: var(--accent-hi);
  overflow-x: auto;
  white-space: nowrap;
}

button.copy {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.05em;
  color: var(--muted);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0 14px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
button.copy:hover { color: var(--accent-hi); border-color: var(--border-hi); }

/* -- verdict badges ------------------------------------------------------ */

.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid currentColor;
  background: color-mix(in srgb, currentColor 12%, transparent);
  white-space: nowrap;
}
.badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge.loops { color: var(--ok); }
.badge.no_loop, .badge.refuted { color: var(--warn); }
.badge.inconclusive { color: var(--muted); }
.badge.error { color: var(--err); }
.badge.known { color: var(--ok); }
.badge.contained { color: var(--accent); }
.badge.candidate { color: var(--warn); }
.badge.neutral { color: var(--muted); }

.pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 9px;
  white-space: nowrap;
}

/* -- the star: state graph ---------------------------------------------- */

.graph-scroll {
  overflow-x: auto;
  overflow-y: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background:
    linear-gradient(180deg, rgba(122, 162, 247, 0.035), transparent 40%),
    var(--surface);
  padding: 6px 4px 2px;
}
.graph-scroll svg { display: block; }

.node rect {
  fill: var(--panel);
  stroke: var(--border);
  stroke-width: 1.2;
  transition: fill 0.15s, stroke 0.15s;
}
.node:hover rect { fill: var(--panel-hi); stroke: var(--border-hi); }
.node.baseline rect { fill: var(--accent-dim); stroke: var(--accent); stroke-width: 1.6; }
.node text { font-family: var(--mono); }
.node .n-iter { fill: var(--accent); font-size: 13px; font-weight: 600; }
.node.baseline .n-iter { fill: var(--accent-hi); }
.node .n-meta { fill: var(--muted); font-size: 9.5px; }
.node .n-sig { fill: var(--faint); font-size: 9.5px; }

.edge line { stroke: var(--border); stroke-width: 1.4; }
.edge.edge-progress line { stroke: var(--accent-dim); stroke-width: 1.7; }
.edge.edge-idle line { stroke-dasharray: 3 4; opacity: 0.8; }
.edge .e-label { font-family: var(--mono); font-size: 9px; }
.edge.edge-progress .e-label { fill: var(--muted); }
.edge.edge-idle .e-label { fill: var(--faint); }

.cycle-back-edge path {
  fill: none;
  stroke-width: 1.8;
  stroke-dasharray: 1;
  animation: draw 0.9s ease-out both;
}
.cycle-back-edge.cycle-loops path { stroke: var(--ok); }
.cycle-back-edge.cycle-no_loop path,
.cycle-back-edge.cycle-refuted path { stroke: var(--warn); }
.cycle-back-edge.cycle-inconclusive path { stroke: var(--muted); }
.cycle-back-edge.cycle-error path { stroke: var(--err); }
.cycle-back-edge.cycle-loops { filter: drop-shadow(0 0 6px rgba(127, 176, 138, 0.35)); }
.cycle-back-edge .c-label { font-family: var(--mono); font-size: 10px; font-weight: 600; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; stroke-linejoin: round; }
.cycle-back-edge .c-grown { font-family: var(--mono); font-size: 9px; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; }
.cycle-back-edge.cycle-loops .c-label { fill: var(--ok); }
.cycle-back-edge.cycle-no_loop .c-label,
.cycle-back-edge.cycle-refuted .c-label { fill: var(--warn); }
.cycle-back-edge.cycle-inconclusive .c-label { fill: var(--muted); }
.cycle-back-edge.cycle-error .c-label { fill: var(--err); }
.cycle-back-edge .c-grown { fill: var(--muted); }
.cycle-back-edge .c-reason { fill: var(--warn); font-family: var(--mono); font-size: 9px; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; }

.legend { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; font-family: var(--mono); font-size: 11px; color: var(--faint); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 18px; height: 0; border-top: 2px solid var(--muted); display: inline-block; }
.legend i.ok { border-color: var(--ok); }
.legend i.warn { border-color: var(--warn); }
.legend i.idle { border-top-style: dashed; }

.cycle-note { margin: 12px 0 0; padding: 10px 14px; border-left: 3px solid var(--muted); background: var(--surface); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; color: var(--muted); font-size: 13.5px; }
.cycle-note.ok { border-color: var(--ok); }
.cycle-note.warn { border-color: var(--warn); }
.cycle-note.err { border-color: var(--err); }
.cycle-note strong { color: var(--text); }

/* -- tables -------------------------------------------------------------- */

.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  text-align: left;
  color: var(--faint);
  background: var(--surface);
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  position: sticky;
  top: 0;
}
tbody td { padding: 9px 12px; border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--panel-hi); }
td.num, th.num { text-align: right; font-family: var(--mono); }
td.mono, .mono-cell { font-family: var(--mono); font-size: 12px; }

tbody tr.flash td { animation: flash 1.1s ease-out; }

/* -- stats --------------------------------------------------------------- */

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 12px; }
.stat .k { font-family: var(--mono); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--faint); }
.stat .v { font-family: var(--mono); font-size: 19px; color: var(--text); margin-top: 2px; }
.stat .v.ok { color: var(--ok); }
.stat .v.warn { color: var(--warn); }

/* -- timeline ------------------------------------------------------------ */

.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li {
  display: grid;
  grid-template-columns: 54px 150px 1fr;
  gap: 12px;
  align-items: baseline;
  padding: 7px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
  font-size: 13px;
}
.timeline li:last-child { border-bottom: none; }
.timeline .t-id { font-family: var(--mono); color: var(--faint); font-size: 12px; }
.timeline .t-type { font-family: var(--mono); color: var(--accent); font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase; }
.timeline .t-answer { font-family: var(--mono); color: var(--muted); word-break: break-word; }

/* -- mechanism vs executed ---------------------------------------------- */

.plan-list { list-style: none; margin: 0; padding: 0; }
.plan-list li { padding: 8px 0; border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent); font-size: 13px; }
.plan-list li:last-child { border-bottom: none; }
.plan-list .link { font-family: var(--mono); color: var(--text); font-size: 12.5px; }
.plan-list .tags { margin-top: 3px; display: flex; gap: 6px; flex-wrap: wrap; }

.mech { color: var(--text); font-size: 13.5px; white-space: pre-wrap; margin: 0 0 12px; }

/* -- raw json ------------------------------------------------------------ */

details.raw { margin-top: 14px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); }
details.raw summary { cursor: pointer; padding: 9px 12px; font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
details.raw pre { margin: 0; padding: 0 14px 14px; overflow-x: auto; font-family: var(--mono); font-size: 12px; color: var(--muted); }

.empty { padding: 26px 4px; color: var(--faint); font-size: 13.5px; }

/* -- live dot ------------------------------------------------------------ */

.live { display: inline-flex; align-items: center; gap: 7px; font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 55%, transparent); animation: pulse 1.8s ease-out infinite; }
.live.stale .dot { background: var(--warn); animation: none; }
.live.stale { color: var(--warn); }

.count-tag { font-family: var(--mono); font-size: 12px; color: var(--faint); }

/* -- footer -------------------------------------------------------------- */

.foot { margin-top: 30px; padding-top: 16px; border-top: 1px solid var(--border); font-family: var(--mono); font-size: 11px; color: var(--faint); display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }

/* -- animation ----------------------------------------------------------- */

@keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 55%, transparent); }
  70% { box-shadow: 0 0 0 7px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
@keyframes draw { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
@keyframes flash { from { background: var(--accent-dim); } to { background: transparent; } }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; }
}

/* -- responsive ---------------------------------------------------------- */

@media (max-width: 900px) {
  .grid.two, .grid.halves { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 560px) {
  body { font-size: 14px; }
  .wrap, .topbar-inner { padding-left: 12px; padding-right: 12px; }
  .topbar-inner { height: auto; min-height: 52px; padding-top: 8px; padding-bottom: 8px; flex-wrap: wrap; }
  .brand .sub { display: none; }
  .panel { padding: 13px 13px; }
  .timeline li { grid-template-columns: 44px 1fr; }
  .timeline .t-answer { grid-column: 1 / -1; }
  .meta { grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }
}

@media (max-width: 380px) {
  .wrap, .topbar-inner { padding-left: 10px; padding-right: 10px; }
  .badge { font-size: 10px; padding: 2px 7px; }
}
""".replace("__ROOT__", root_variables())

CSS = _CSS_TEMPLATE

# -- javascript --------------------------------------------------------------

JS = """
(function () {
  "use strict";

  // -- feed polling -------------------------------------------------------
  var tbody = document.getElementById("feed-body");
  if (tbody && window.fetch && window.setInterval) {
    var wrap = document.getElementById("feed-live");
    var updated = document.getElementById("feed-updated");
    var countTag = document.getElementById("feed-count");
    var seen = {};
    var lastSignature = null;

    function esc(value) {
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function badge(verdict) {
      var labels = { loops: "loops", no_loop: "no loop", refuted: "refuted",
                     inconclusive: "inconclusive", error: "error" };
      var key = String(verdict || "inconclusive");
      return '<span class="badge ' + esc(key) + '">' + esc(labels[key] || key) + "</span>";
    }

    function row(result) {
      var cards = (result.cards || []).map(function (name) {
        return '<span class="pill">' + esc(name) + "</span>";
      }).join(" ");
      return '<tr data-id="' + esc(result.id) + '">' +
        '<td class="mono-cell">' + cards + "</td>" +
        "<td>" + badge(result.verdict) + "</td>" +
        '<td class="num">' + esc(result.iterations) + "</td>" +
        '<td class="num">' + esc(result.observation_count || 0) + "</td>" +
        '<td class="mono-cell"><a href="/run/' + esc(result.run_id) + '">run ' +
          esc(result.run_id) + '</a> <span class="faint">· result ' +
          esc(result.id) + "</span></td>" +
        '<td class="mono-cell faint">' + esc(result.created_at || "") + "</td>" +
        "</tr>";
    }

    function apply(payload) {
      var results = payload.results || [];
      var signature = results.map(function (r) { return r.id + ":" + r.verdict; }).join(",");
      if (signature === lastSignature) { return; }
      var fresh = results.filter(function (r) { return !seen[r.id]; });
      lastSignature = signature;
      tbody.innerHTML = results.map(row).join("");
      results.forEach(function (r) { seen[r.id] = true; });
      fresh.forEach(function (r) {
        var tr = tbody.querySelector('tr[data-id="' + r.id + '"]');
        if (tr) { tr.classList.add("flash"); }
      });
      if (countTag) { countTag.textContent = results.length + " results"; }
      if (updated) { updated.textContent = "updated " + new Date().toLocaleTimeString(); }
      if (wrap) { wrap.classList.remove("stale"); }
    }

    function tick() {
      fetch("/api/feed", { cache: "no-store" })
        .then(function (response) { if (!response.ok) { throw new Error("bad status"); } return response.json(); })
        .then(apply)
        .catch(function () { if (wrap) { wrap.classList.add("stale"); } });
    }

    tick();
    window.setInterval(tick, 1000);
  }

  // -- copy replay command ------------------------------------------------
  document.querySelectorAll("button.copy").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy"));
      if (!target) { return; }
      var text = target.textContent || "";
      function done() {
        var original = button.textContent;
        button.textContent = "copied";
        window.setTimeout(function () { button.textContent = original; }, 1200);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var area = document.createElement("textarea");
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand("copy"); } catch (error) {}
        document.body.removeChild(area);
        done();
      }
    });
  });
})();
"""

__all__ = ["CSS", "JS"]
