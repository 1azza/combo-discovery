"""Inline stylesheet and the small vanilla-JS enhancement.

Everything here is embedded in the page: no CDN, no external fonts, no build
step. The Javascript only powers the Gallery board (polling ``/api/gallery``)
and the copy-to-clipboard button; every page is fully readable with JS disabled.
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
  /* A brighter warm grey for secondary text that must stay readable on the
     near-black ground (the TUI's --faint is too dim for body copy). */
  --soft: #b6ad9c;
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

.nav { display: flex; gap: 8px; margin-left: auto; align-items: stretch; flex-wrap: wrap; }
.nav-item {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 1px;
  padding: 5px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  text-decoration: none;
  line-height: 1.2;
}
.nav-item .nav-title { font-family: var(--mono); font-size: 13px; letter-spacing: 0.04em; color: var(--muted); }
.nav-item .nav-sub { font-size: 10.5px; color: var(--faint); }
.nav-item:hover { background: var(--panel-hi); text-decoration: none; }
.nav-item:hover .nav-title { color: var(--text); }
.nav-item.active { border-color: var(--border-hi); background: var(--accent-dim); }
.nav-item.active .nav-title { color: var(--accent-hi); }
.nav-item.active .nav-sub { color: var(--accent); }
.nav-disabled { cursor: not-allowed; opacity: 0.75; }
.nav-disabled .nav-title { color: var(--faint); }
.nav-soon {
  align-self: flex-start;
  margin-top: 1px;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--warn);
  border: 1px solid color-mix(in srgb, var(--warn) 55%, transparent);
  border-radius: 999px;
  padding: 0 6px;
}

.skip {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 200;
  padding: 10px 14px;
  background: var(--accent-dim);
  color: var(--accent-hi);
  border-radius: 0 0 var(--radius-sm) 0;
}
.skip:focus { left: 0; }

:focus-visible { outline: 2px solid var(--accent-hi); outline-offset: 2px; }

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
.hero .lede { color: var(--soft); max-width: 72ch; margin: 0; }
.hero .lede strong { color: var(--accent); font-weight: 600; }

.cards-title { font-size: clamp(18px, 2.4vw, 23px); color: var(--accent); margin: 0; }

/* -- verdict hero (run page) --------------------------------------------- */

.verdict-hero { padding-bottom: 18px; }
.verdict-title {
  font-size: clamp(30px, 6vw, 46px);
  line-height: 1.04;
  letter-spacing: -0.01em;
  margin: 0 0 10px;
}
.verdict-hero.loops .verdict-title { color: var(--ok); }
.verdict-hero.no_loop .verdict-title,
.verdict-hero.refuted .verdict-title { color: var(--warn); }
.verdict-hero.inconclusive .verdict-title { color: var(--muted); }
.verdict-hero.error .verdict-title { color: var(--err); }
.verdict-cards { font-family: var(--mono); font-size: clamp(17px, 3vw, 23px); margin: 0 0 10px; color: var(--text); }
.verdict-sentence { font-size: 16.5px; color: var(--text); max-width: 74ch; margin: 0 0 12px; }
.verdict-meta { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin: 0; }

.card-link { color: var(--accent-hi); font-weight: 600; }
.card-link:hover { color: var(--accent); }
.cards-cell { white-space: nowrap; }
.cards-cell .card-link { font-family: var(--mono); font-size: 15px; }
.result-link { display: inline-block; }
.result-link:hover { text-decoration: none; }

.graph-caption { color: var(--muted); font-size: 13.5px; margin: 0 0 12px; max-width: 80ch; }

/* -- gallery: filters ---------------------------------------------------- */

.filters { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; margin: 4px 0 16px; }
.filter { display: flex; flex-direction: column; gap: 4px; }
.filter-label {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--faint);
}
.filter select {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 7px 10px;
  font-family: var(--mono);
  font-size: 12px;
}
.filter select:hover { border-color: var(--border-hi); }

.board-status { display: flex; align-items: center; gap: 12px; margin: 0 0 10px; }

/* -- gallery: the board -------------------------------------------------- */

.board-wrap { overflow-x: auto; padding-bottom: 8px; }
.board {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}
.board-col {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.col-head {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  border-top: 3px solid var(--border);
}
.col-head-top { display: flex; align-items: center; gap: 8px; }
.col-title {
  margin: 0;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  display: flex;
  align-items: center;
}
.col-title::before {
  content: "";
  width: 8px;
  height: 8px;
  margin-right: 8px;
  border-radius: 50%;
  background: var(--muted);
  flex: none;
}
.col-count {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--soft);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 9px;
}
.col-count.bump { animation: bump 0.5s ease-out; }
/* The pager lives in the header, so it is visible without scrolling. It must
   stay on one line at board width, so it never wraps (see _pager_inner). */
.col-head-page {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  flex-wrap: nowrap;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--soft);
  white-space: nowrap;
}
.board-body { display: flex; flex-direction: column; gap: 12px; padding: 12px; }
.col-empty { margin: 0; padding: 8px 2px; color: var(--muted); font-size: 12.5px; }

/* Status is encoded by shape *and* colour, never colour alone. */
.col-loops .col-head { border-top-color: var(--ok); }
.col-loops .col-title::before { background: var(--ok); }
.col-refuted .col-head { border-top-color: var(--err); }
.col-refuted .col-title::before { background: var(--err); border-radius: 1px; }
.col-indecided .col-head { border-top-color: var(--muted); }
.col-indecided .col-title::before { background: transparent; border: 1.5px solid var(--muted); }
.col-queued .col-head { border-top-color: var(--muted); }
.col-queued .col-title::before { background: transparent; border: 1.5px dashed var(--muted); }
.col-playing .col-head { border-top-color: var(--accent); }
.col-playing .col-title::before { background: var(--accent); box-shadow: 0 0 0 0 var(--accent-dim); animation: pulse 1.6s ease-out infinite; }

/* -- gallery: status tabs (narrow widths only) --------------------------- */

.status-tabs { display: none; }
.tab-mark { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.status-tab {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--soft);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 6px 11px;
  white-space: nowrap;
}
.status-tab:hover { color: var(--text); border-color: var(--border-hi); text-decoration: none; }
/* Active is unmistakable without the inactive ones looking active: it alone
   gets the raised fill, the brighter border and the strongest text colour. */
.status-tab.active {
  color: var(--text);
  border-color: var(--border-hi);
  background: var(--panel-hi);
  box-shadow: inset 0 0 0 1px var(--border-hi);
}
.tab-count {
  font-size: 10px;
  color: var(--soft);
  background: var(--panel);
  border-radius: 999px;
  padding: 0 6px;
}
.tab-playing.active { color: var(--accent); }
.tab-playing .tab-mark { background: var(--accent); animation: pulse 1.6s ease-out infinite; }
.tab-loops.active { color: var(--ok); }
.tab-loops .tab-mark { background: var(--ok); }
.tab-refuted.active { color: var(--err); }
.tab-refuted .tab-mark { background: var(--err); border-radius: 1px; }
.tab-indecided .tab-mark { background: transparent; border: 1.5px solid var(--muted); }
.tab-queued .tab-mark { background: transparent; border: 1.5px dashed var(--muted); }

/* -- gallery: tiles ------------------------------------------------------ */

.tile {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 9px;
  animation: rise 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
}
.tile:hover { border-color: var(--border-hi); }
.tile.flash { animation: flash 0.9s ease-out; }
.tile-art { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.thumb {
  padding: 0;
  border: none;
  background: none;
  cursor: zoom-in;
  border-radius: 7px;
  overflow: hidden;
  aspect-ratio: 122 / 170;
  display: block;
  width: 100%;
}
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  border-radius: 7px;
  transition: transform 0.2s ease;
}
.tile:hover .thumb img { transform: scale(1.03); }
.thumb-fallback {
  cursor: default;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 3px;
  padding: 8px;
  text-align: left;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: linear-gradient(160deg, var(--accent-dim), var(--panel));
  aspect-ratio: 122 / 170;
}
.fallback-name { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--text); }
.fallback-type { font-size: 9.5px; color: var(--muted); }
.fallback-cost { font-family: var(--mono); font-size: 10px; color: var(--faint); }
.pips { display: flex; gap: 3px; margin-top: 2px; }
.pip { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.pip-W { background: #d9d2b8; }
.pip-U { background: #8fb8d8; }
.pip-B { background: #a99bb5; }
.pip-R { background: #d98a6a; }
.pip-G { background: #8fae82; }
.pip-C { background: #9a9384; }

.tile-cards { margin: 0; font-family: var(--mono); font-size: 12px; line-height: 1.35; }
.tile-card-link { color: var(--text); font-weight: 600; }
.tile-card-link:hover { color: var(--accent-hi); }
.tile-idea { margin: 4px 0 0; font-size: 12.5px; color: var(--muted); }
.tile-more { margin-top: 6px; }
.tile-more summary {
  cursor: pointer;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
}
.tile-more p { margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.tile-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tile-when { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-left: auto; }
.tile-open { font-family: var(--mono); font-size: 11px; color: var(--accent); white-space: nowrap; }
.tile-note {
  margin: 8px 0 0;
  padding-left: 9px;
  border-left: 2px solid var(--warn);
  font-size: 11.5px;
  color: var(--soft);
}

/* Attempts: one pairing, several tests. */
.tile-tries { margin-top: 6px; }
.tile-tries summary {
  cursor: pointer;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
.tile-tries summary::marker { color: var(--accent); }
.tries { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.try { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-family: var(--mono); font-size: 10.5px; }
.try-meta { color: var(--muted); }
.try-link { color: var(--accent); margin-left: auto; white-space: nowrap; }

/* One chip system: every status shares weight (border + tint + shape). */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid currentColor;
  background: color-mix(in srgb, currentColor 14%, transparent);
  color: var(--muted);
  white-space: nowrap;
}
.chip::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; }
.chip-loops { color: var(--ok); }
.chip-refuted { color: var(--err); }
.chip-refuted::before { border-radius: 1px; }
.chip-indecided { color: var(--muted); }
.chip-indecided::before { background: transparent; border: 1.5px solid currentColor; }
.chip-queued { color: var(--muted); }
.chip-queued::before { background: transparent; border: 1.5px dashed currentColor; }
.chip-playing { color: var(--accent); }

/* -- gallery: pager (lives in the column header) ------------------------- */

.col-page-nav { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.col-showing { white-space: nowrap; }
.col-page-num { color: var(--soft); white-space: nowrap; font-variant-numeric: tabular-nums; }
.page-link { color: var(--accent); white-space: nowrap; }
.page-link.disabled { color: var(--muted); opacity: 0.75; }
.page-link.disabled:hover { text-decoration: none; }

.board-note { color: var(--muted); font-size: 12px; margin: 10px 2px 0; }

/* -- lightbox ------------------------------------------------------------ */

.lightbox {
  position: fixed;
  inset: 0;
  z-index: 150;
  background: rgba(8, 7, 6, 0.88);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.lightbox[hidden] { display: none; }
.lb-img { max-width: min(92vw, 640px); max-height: 88vh; border-radius: 12px; box-shadow: 0 24px 70px rgba(0, 0, 0, 0.65); }
.lb-close {
  position: absolute;
  top: 16px;
  right: 18px;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 14px;
  cursor: pointer;
}
.lb-close:hover { color: var(--accent-hi); border-color: var(--border-hi); }

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
.node .n-phase { fill: var(--faint); font-size: 9.5px; }

.edge line { stroke: var(--border); stroke-width: 1.4; }
.edge.edge-progress line { stroke: var(--accent-dim); stroke-width: 1.7; }
.edge.edge-idle line { stroke-dasharray: 3 4; opacity: 0.8; }
.edge .e-label { font-family: var(--mono); font-size: 8px; }
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

/* -- technical details (collapsed) --------------------------------------- */

details.tech {
  margin: 0 0 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel);
  overflow: hidden;
}
details.tech > summary {
  cursor: pointer;
  padding: 15px 18px;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--muted);
  list-style: none;
  display: flex;
  align-items: center;
  gap: 10px;
}
details.tech > summary::before { content: "▸"; color: var(--accent); font-size: 11px; }
details.tech[open] > summary::before { content: "▾"; }
details.tech > summary::-webkit-details-marker { display: none; }
details.tech > summary:hover { color: var(--text); }
.tech-body { padding: 6px 18px 18px; border-top: 1px solid var(--border); }

.sig-list { list-style: none; margin: 0; padding: 0; }
.sig-list li {
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 5px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
}
.sig-list li:last-child { border-bottom: none; }
.sig-list .sig-step {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
  min-width: 54px;
}
.sig-list code { font-size: 11.5px; color: var(--muted); word-break: break-all; }

.empty { padding: 26px 4px; color: var(--faint); font-size: 13.5px; }

/* -- live dot ------------------------------------------------------------ */

.live { display: inline-flex; align-items: center; gap: 7px; font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 55%, transparent); animation: pulse 1.8s ease-out infinite; }
.live.stale .dot { background: var(--warn); animation: none; }
.live.stale { color: var(--warn); }

.count-tag { font-family: var(--mono); font-size: 12px; color: var(--soft); }

/* -- goldfish: watch a run ---------------------------------------------- */

.gf-head { padding: 6px 0 14px; display: flex; flex-direction: column; gap: 8px; }
.gf-pair { font-size: clamp(20px, 3.4vw, 30px); margin: 0; color: var(--text); }
.gf-status { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; }
.gf-links { font-family: var(--mono); font-size: 12px; }
.live.ended .dot { background: var(--muted); animation: none; }
.live.ended { color: var(--muted); }

.gf-verdict {
  border-radius: var(--radius);
  border: 1px solid var(--border);
  border-left: 3px solid var(--muted);
  background: var(--panel);
  padding: 14px 18px;
  margin: 0 0 18px;
  animation: rise 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
}
.gf-verdict.loops { border-left-color: var(--ok); }
.gf-verdict.no_loop, .gf-verdict.refuted { border-left-color: var(--warn); }
.gf-verdict.error { border-left-color: var(--err); }
.gf-verdict.live { border-left-color: var(--accent); }
.gf-verdict-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.gf-verdict-title { margin: 0; font-size: clamp(20px, 3vw, 26px); }
.gf-verdict.loops .gf-verdict-title { color: var(--ok); }
.gf-verdict.no_loop .gf-verdict-title, .gf-verdict.refuted .gf-verdict-title { color: var(--warn); }
.gf-verdict.inconclusive .gf-verdict-title { color: var(--muted); }
.gf-verdict.error .gf-verdict-title { color: var(--err); }
.gf-verdict-sentence { margin: 8px 0 0; color: var(--text); max-width: 84ch; }

.loop-hero { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.pass-badge {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-width: 124px;
  padding: 10px 18px;
  border-radius: var(--radius);
  background: linear-gradient(160deg, var(--accent-dim), var(--panel));
  border: 1px solid var(--border-hi);
}
.pass-num { font-family: var(--mono); font-size: clamp(38px, 7vw, 58px); line-height: 1; color: var(--accent-hi); font-weight: 600; font-variant-numeric: tabular-nums; }
.pass-word { font-family: var(--mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent); }
.loop-copy { flex: 1; min-width: 240px; }
.loop-sentence { font-size: 17px; color: var(--text); margin: 0 0 6px; }
.loop-sub { margin: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
.loop-pass-static { font-family: var(--mono); color: var(--soft); }
.loop-waiting { color: var(--muted); }

.counters { display: grid; grid-template-columns: repeat(auto-fit, minmax(104px, 1fr)); gap: 10px; margin-top: 16px; }
.counter { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 9px 12px; }
.counter .k { font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
.counter .v { font-family: var(--mono); font-size: 20px; color: var(--text); margin-top: 2px; font-variant-numeric: tabular-nums; }
.counter .v.bump { animation: bump 0.5s ease-out; }

.gf-grid { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 18px; }
.gf-grid .panel { margin: 0 0 18px; }

.board-zone { margin-bottom: 14px; }
.zone-title { font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--faint); margin-bottom: 8px; }
.zone-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 10px; }
.perm { position: relative; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--panel-hi); }
.perm.is-tapped { opacity: 0.62; }
.perm.is-tapped .perm-thumb img { transform: rotate(90deg) scale(0.72); }
.perm-thumb { aspect-ratio: 122 / 170; width: 100%; }
.perm-fallback { aspect-ratio: 122 / 170; }
.perm-count {
  position: absolute; top: 5px; right: 5px; z-index: 2;
  font-family: var(--mono); font-size: 11px; color: var(--text);
  background: color-mix(in srgb, var(--bg) 78%, transparent);
  border: 1px solid var(--border); border-radius: 999px; padding: 0 7px;
}
.perm-name { font-family: var(--mono); font-size: 10.5px; padding: 5px 6px 2px; }
.perm-chips { display: flex; gap: 4px; flex-wrap: wrap; padding: 0 6px 6px; }
.chip-token { color: var(--accent); }
.chip-copy { color: var(--accent-hi); }
.chip-tapped { color: var(--warn); }
.chip-counter { color: var(--ok); }
.zone-graveyard .zone-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.zone-list li { display: flex; justify-content: space-between; gap: 10px; font-family: var(--mono); font-size: 12px; border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent); padding: 3px 0; }
.zone-count { color: var(--muted); }
.zone-card-link { color: var(--text); font-weight: 600; }

.loopgraph-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(122, 162, 247, 0.05), transparent 42%), var(--surface);
}
.loopgraph { display: block; width: 100%; height: auto; }
.lg-edge path { fill: none; stroke: var(--accent-dim); stroke-width: 2; }
.loopgraph.is-live .lg-edge.lg-flow path { stroke: var(--accent); stroke-dasharray: 7 7; animation: flow 1.1s linear infinite; }
.lg-label { font-family: var(--mono); font-size: 10px; fill: var(--soft); paint-order: stroke; stroke: var(--surface); stroke-width: 3px; stroke-linejoin: round; }
.lg-node rect { fill: var(--panel); stroke: var(--border-hi); stroke-width: 1.2; }
.lg-node.lg-hub rect { fill: var(--accent-dim); stroke: var(--accent); stroke-width: 1.6; }
.loopgraph.is-live .lg-node.lg-hub rect { animation: halo 1.8s ease-out infinite; }
.lg-name { font-family: var(--mono); font-size: 11px; fill: var(--text); }
.lg-badge rect { fill: var(--ok); }
.lg-badge text { font-family: var(--mono); font-size: 11px; font-weight: 600; fill: var(--bg); }
.loop-caption { margin-top: 12px; }

.pb { list-style: none; margin: 0; padding: 0; max-height: 540px; overflow-y: auto; }
.pb-row {
  display: grid; grid-template-columns: 74px 1fr; gap: 12px; align-items: baseline;
  padding: 6px 4px; font-size: 14px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  animation: rise 0.3s cubic-bezier(0.22, 1, 0.36, 1) both;
}
.pb-row:last-child { border-bottom: none; }
.pb-kind { font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
.pb-text { color: var(--text); }
.pb-copy .pb-kind, .pb-cast .pb-kind { color: var(--accent-hi); }
.pb-trigger .pb-kind, .pb-token .pb-kind { color: var(--ok); }
.pb-untap .pb-kind, .pb-activate .pb-kind, .pb-play_land .pb-kind { color: var(--accent); }
.pb-verdict .pb-kind { color: var(--warn); }
.pb-turnbreak { margin: 10px 0 4px; padding-top: 8px; border-top: 1px solid var(--border); font-family: var(--mono); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); }
.pb-pass { display: flex; align-items: center; gap: 10px; margin: 10px 0 2px; }
.pb-pass-num { font-family: var(--mono); font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--accent); border: 1px solid var(--accent-dim); border-radius: 999px; padding: 1px 9px; }
.pb-pass-line { flex: 1; height: 1px; background: linear-gradient(to right, var(--accent-dim), transparent); }

.run-list { list-style: none; margin: 0; padding: 0; }
.run-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; padding: 10px 0; border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent); }
.run-row:last-child { border-bottom: none; }
.run-cards { font-family: var(--mono); font-size: 14px; }
.run-meta { display: flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 11px; color: var(--muted); flex-wrap: wrap; }
.run-meta time { color: var(--faint); }
.run-open { font-family: var(--mono); font-size: 12px; white-space: nowrap; }

@keyframes flow { to { stroke-dashoffset: -28; } }
@keyframes halo {
  0%, 100% { filter: drop-shadow(0 0 0 rgba(122, 162, 247, 0)); }
  50% { filter: drop-shadow(0 0 10px rgba(122, 162, 247, 0.6)); }
}

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
@keyframes bump {
  0% { transform: scale(1); }
  40% { transform: scale(1.3); color: var(--accent-hi); }
  100% { transform: scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; }
}

/* -- responsive ---------------------------------------------------------- */

@media (max-width: 900px) {
  .grid.two, .grid.halves { grid-template-columns: minmax(0, 1fr); }
  .gf-grid { grid-template-columns: minmax(0, 1fr); }
  /* One status at a time, reachable from the tab strip. */
  .status-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 0 0 12px;
  }
  .board { grid-template-columns: minmax(0, 1fr); }
  .board-wrap { overflow-x: visible; }
  .board-col { display: none; }
  .board[data-active="queued"] .col-queued,
  .board[data-active="playing"] .col-playing,
  .board[data-active="loops"] .col-loops,
  .board[data-active="refuted"] .col-refuted,
  .board[data-active="indecided"] .col-indecided { display: flex; }
  .board-body { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
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
  .verdict-sentence { font-size: 15px; }
  .cards-cell .card-link { font-size: 14px; }
  .nav-item { padding: 5px 9px; }
  .nav-item:not(.nav-disabled) .nav-sub { display: none; }
  .nav-soon { font-size: 8.5px; }
  .pb-row { grid-template-columns: 58px 1fr; }
  .pass-badge { min-width: 96px; padding: 8px 12px; }
  .run-row { grid-template-columns: minmax(0, 1fr) auto; }
  .run-meta { grid-column: 1 / -1; }
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

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // One number formatter everywhere counts appear (matches the server's f"{:,}").
  function num(value) {
    return String(value == null ? 0 : value).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
  }

  var board = document.getElementById("gallery-board");

  // -- gallery: live board -------------------------------------------------
  if (board && window.fetch && window.setInterval) {
    var live = document.getElementById("gallery-live");
    var updated = document.getElementById("gallery-updated");
    var prevCounts = {};

    function positions() {
      var box = {};
      board.querySelectorAll("article.tile[data-key]").forEach(function (tile) {
        box[tile.getAttribute("data-key")] = tile.getBoundingClientRect();
      });
      return box;
    }

    function bump(counter) {
      counter.classList.remove("bump");
      void counter.offsetWidth;
      counter.classList.add("bump");
    }

    function flip(before) {
      // Tiles that changed column visibly slide to their new home.
      board.querySelectorAll("article.tile[data-key]").forEach(function (tile) {
        var previous = before[tile.getAttribute("data-key")];
        if (!previous) {
          tile.classList.add("flash");
          window.setTimeout(function () { tile.classList.remove("flash"); }, 900);
          return;
        }
        var now = tile.getBoundingClientRect();
        var dx = previous.left - now.left;
        var dy = previous.top - now.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) { return; }
        tile.style.transition = "none";
        tile.style.transform = "translate(" + dx + "px," + dy + "px)";
        window.requestAnimationFrame(function () {
          tile.style.transition = "transform 0.5s cubic-bezier(0.22, 1, 0.36, 1)";
          tile.style.transform = "";
        });
      });
    }

    function apply(payload) {
      var columns = payload.columns || [];
      var before = positions();

      columns.forEach(function (column) {
        var section = board.querySelector('section[data-col="' + column.key + '"]');
        if (!section) { return; }
        var signature = column.sig || (column.count + ":" + (column.html || "").length);
        if (section.getAttribute("data-sig") === signature) { return; }
        var counter = section.querySelector("[data-count]");
        var changed = prevCounts[column.key] !== undefined
          && prevCounts[column.key] !== column.count;
        section.innerHTML = column.html
          || '<header class="col-head"><h2 class="col-title"></h2></header>'
             + '<div class="board-body"><p class="col-empty">' + esc(column.empty) + "</p></div>";
        section.setAttribute("data-sig", signature);
        prevCounts[column.key] = column.count;
        if (counter && changed) {
          var next = section.querySelector("[data-count]");
          if (next) { bump(next); }
        }
      });

      // Tab counts tick with the board (the tab strip lives outside it).
      (payload.tabs || []).forEach(function (tab) {
        var counter = document.querySelector('[data-tab-count="' + tab.key + '"]');
        var formatted = num(tab.count);
        if (counter && counter.textContent !== formatted) {
          counter.textContent = formatted;
        }
      });

      flip(before);
      if (updated) { updated.textContent = "updated " + new Date().toLocaleTimeString(); }
      if (live) { live.classList.remove("stale"); }
    }

    function tick() {
      if (document.hidden) { return; }
      // location.search carries filters, tab and page, so a poll never resets a
      // reader who has paged or switched tabs.
      fetch("/api/gallery" + window.location.search, { cache: "no-store" })
        .then(function (response) {
          if (!response.ok) { throw new Error("bad status"); }
          return response.json();
        })
        .then(apply)
        .catch(function () { if (live) { live.classList.add("stale"); } });
    }

    tick();
    window.setInterval(tick, 2500);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { tick(); }
    });
  }

  // -- gallery: filters submit themselves ---------------------------------
  var filters = document.getElementById("gallery-filters");
  if (filters) {
    filters.querySelectorAll("select").forEach(function (select) {
      select.addEventListener("change", function () { filters.submit(); });
    });
  }

  // -- lightbox ------------------------------------------------------------
  var lightbox = document.getElementById("lightbox");
  if (lightbox) {
    var lightboxImg = document.getElementById("lightbox-img");
    var lightboxClose = document.getElementById("lightbox-close");
    var lastFocus = null;

    function openLightbox(src, alt) {
      lightboxImg.src = src;
      lightboxImg.alt = alt || "";
      lightbox.hidden = false;
      lastFocus = document.activeElement;
      lightboxClose.focus();
    }
    function closeLightbox() {
      lightbox.hidden = true;
      lightboxImg.removeAttribute("src");
      if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
    }

    // Delegated on the document so card art enlarges on any page that has a
    // lightbox (The Gallery and The Goldfish both render thumbs).
    document.addEventListener("click", function (event) {
      var thumb = event.target.closest(".thumb[data-large]");
      if (!thumb) { return; }
      var src = thumb.getAttribute("data-large");
      if (!src) { return; }
      event.preventDefault();
      openLightbox(src, thumb.getAttribute("aria-label"));
    });
    if (lightboxClose) { lightboxClose.addEventListener("click", closeLightbox); }
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox) { closeLightbox(); }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !lightbox.hidden) { closeLightbox(); }
    });
  }

  // -- goldfish: watch a run live -----------------------------------------
  var gf = document.getElementById("goldfish");
  if (gf && window.fetch && window.setInterval) {
    var gfRun = gf.getAttribute("data-run");
    var gfSeq = parseInt(gf.getAttribute("data-seq") || "0", 10) || 0;
    var gfObs = parseInt(gf.getAttribute("data-obs") || "0", 10) || 0;
    var gfLive = gf.getAttribute("data-live") === "true";
    var gfList = document.getElementById("pb-list");
    var gfCount = document.getElementById("gf-pb-count");
    var gfStatus = document.getElementById("gf-status");
    var gfSummaryWrap = document.getElementById("gf-summary-wrap");
    var gfSigs = {};
    var gfTimer = null;

    function setPiece(id, key, sig, html) {
      if (html == null || gfSigs[key] === sig) { return; }
      var node = document.getElementById(id);
      if (node) { node.innerHTML = html; }
      gfSigs[key] = sig;
    }

    function nearBottom() {
      if (!gfList) { return true; }
      return gfList.scrollHeight - gfList.scrollTop - gfList.clientHeight < 120;
    }

    function appendRows(html) {
      if (!gfList || !html) { return; }
      var stick = nearBottom();
      gfList.insertAdjacentHTML("beforeend", html);
      while (gfList.children.length > 420) { gfList.removeChild(gfList.firstChild); }
      if (stick) { gfList.scrollTop = gfList.scrollHeight; }
    }

    function gfTick() {
      if (document.hidden || !gfLive) { return; }
      // Cursor-based: only narration rows the page has not seen come back.
      fetch("/api/run/" + gfRun + "/events?after=" + gfSeq, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          appendRows(data.rows_html || "");
          if (typeof data.cursor === "number") { gfSeq = data.cursor; }
          if (gfCount && typeof data.total === "number") {
            gfCount.textContent = num(data.total) + " moves";
          }
        })
        .catch(function () {});

      // The derived views are recomputed server-side, but the game is not
      // re-downloaded; ``sig`` gates each fragment so an unchanged board does
      // not repaint (and the graph animation keeps running).
      fetch("/api/run/" + gfRun + "/board?events_after=" + gfSeq +
            "&obs_after=" + gfObs, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          var h = data.html || {};
          var s = data.sig || {};
          setPiece("gf-board", "board", s.board, h.board);
          setPiece("gf-graph", "graph", s.graph, h.graph);
          setPiece("gf-loop", "loop", s.loop, h.loop);
          setPiece("gf-counters", "counters", s.counters, h.counters);
          setPiece("gf-verdict", "verdict", s.verdict, h.verdict);
          if (gfStatus && h.status) { gfStatus.innerHTML = h.status; }
          if (gfSummaryWrap && h.summary) {
            var summary = document.getElementById("gf-summary");
            if (summary) { summary.innerHTML = h.summary; }
            gfSummaryWrap.hidden = false;
          }
          if (typeof data.obs_cursor === "number") { gfObs = data.obs_cursor; }
          if (!data.live && gfTimer) {
            gfLive = false;
            window.clearInterval(gfTimer);
            gf.setAttribute("data-live", "false");
          }
        })
        .catch(function () {});
    }

    if (gfLive) { gfTimer = window.setInterval(gfTick, 1000); }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && gfLive) { gfTick(); }
    });
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
