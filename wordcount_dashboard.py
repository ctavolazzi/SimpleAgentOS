#!/usr/bin/env python3
"""
wordcount_dashboard.py — Self-contained HTML dashboard for vault word counts.

Renders one page with no external requests (no CDN, no fonts, no network) so it
works offline and inside the vault:

  · hero figure     — words written on the target day
  · stat tiles      — files, prose/code split, 7-day total, 30-day average
  · contribution heatmap — a year of daily output, sequential one-hue ramp
  · 30-day trend    — column chart, single series
  · word cloud      — spiral-packed, sized by frequency, today or last 30 days
  · top words       — the cloud's table-view twin
  · where words went — per-folder horizontal bars
  · per-file table  — the day's files with their bucket

Colors come from the validated data-viz reference palette (blue sequential
ramp, both light and dark surfaces selected — not an automatic flip). Every
chart carries a hover tooltip and a table view, so no value is reachable only
by color or only by hover.

Usage:
  python3 wordcount_dashboard.py                 # build for today + open
  python3 wordcount_dashboard.py --no-open
  python3 wordcount_dashboard.py --date 2026-07-20
  python3 wordcount_dashboard.py --out ~/wc.html
"""

import html
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import word_count

# One live dashboard, regenerated in place. The heatmap already carries the
# history, so per-date archives would just be sync bloat in the vault.
DEFAULT_OUT = (word_count.VAULT_DIR / "System" / "40-49_telemetry"
               / "wordcount-dashboard.html")

HISTORY_DAYS = 371       # 53 whole weeks, so the heatmap grid comes out square
AREA_DAYS = 30
CLOUD_WORDS = 90


# ── Data assembly ────────────────────────────────────────────────────────────

def build_payload(date: Optional[str] = None, max_depth: int = 2) -> dict:
    """Everything the page needs, in one pass over the vault."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    files = word_count.vault_files()
    index = word_count.build_index(files)
    cache = word_count._load_cache()

    scan = word_count.scan_day(date, index=index, files=files, cache=cache,
                               max_depth=max_depth, save_cache=False)
    recs = word_count.window_records(HISTORY_DAYS, end_date=date, files=files,
                                     cache=cache, save_cache=False)
    hist = word_count.rollup_history(recs, HISTORY_DAYS, end_date=date)

    recent_cut = hist[-AREA_DAYS:][0]["date"] if hist else date
    recent_recs = [r for r in recs if r["attributed"] >= recent_cut]
    areas = word_count.rollup_areas(recent_recs)

    today_files = [r for r in scan["files"] if r["fresh"]]
    cloud_today = word_count.word_frequencies(today_files, top=CLOUD_WORDS)
    cloud_recent = word_count.word_frequencies(recent_recs, top=CLOUD_WORDS)

    word_count._save_cache(cache)

    # Trailing windows, computed on the zero-filled history so "active day"
    # means a day with words, not merely a day that exists.
    last7 = hist[-7:]
    last30 = hist[-30:]
    active30 = [d["words"] for d in last30 if d["words"]]
    active_all = [d for d in hist if d["words"]]

    prev = next((d for d in reversed(hist[:-1]) if d["words"]), None)
    best = max(hist, key=lambda d: d["words"]) if hist else None

    # Current streak of consecutive days with words, ending at the target day.
    streak = 0
    for row in reversed(hist):
        if row["words"]:
            streak += 1
        else:
            break

    month_prefix = date[:7]
    year_prefix = date[:4]

    return {
        "date": date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scan": {k: v for k, v in scan.items() if k != "files"},
        "files": [
            {"rel": r["rel"], "name": r["name"], "total": r["total"],
             "prose": r["prose"], "code": r["code"], "bucket": r["bucket"],
             "linked": r["linked"], "depth": r["depth"]}
            for r in scan["files"]
        ],
        "history": hist,
        "areas": areas,
        "cloud": {"today": cloud_today, "recent": cloud_recent},
        "stats": {
            "week_words": sum(d["words"] for d in last7),
            "month_words": sum(d["words"] for d in hist
                               if d["date"].startswith(month_prefix)),
            "year_words": sum(d["words"] for d in hist
                              if d["date"].startswith(year_prefix)),
            "avg30": round(sum(active30) / len(active30)) if active30 else 0,
            "active_days_30": len(active30),
            "active_days_all": len(active_all),
            "streak": streak,
            "prev_date": prev["date"] if prev else None,
            "prev_words": prev["words"] if prev else 0,
            "best_date": best["date"] if best and best["words"] else None,
            "best_words": best["words"] if best else 0,
            "window_days": HISTORY_DAYS,
            "area_days": AREA_DAYS,
        },
    }


# ── Page ─────────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; }

.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --good: #006300;
  /* Sequential blue, light->dark. Level 0 is the empty-day cell: the
     gridline gray, so "no words" reads as absence, not as a low value. */
  --seq-0: #e1e0d9;
  --seq-1: #cde2fb;
  --seq-2: #9ec5f4;
  --seq-3: #5598e7;
  --seq-4: #2a78d6;
  --seq-5: #184f95;

  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--plane);
  color: var(--text-primary);
  min-height: 100vh;
  padding: 40px 24px 64px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --good: #0ca30c;
    --seq-0: #2c2c2a;
    --seq-1: #184f95;
    --seq-2: #256abf;
    --seq-3: #3987e5;
    --seq-4: #6da7ec;
    --seq-5: #9ec5f4;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --good: #0ca30c;
  --seq-0: #2c2c2a;
  --seq-1: #184f95;
  --seq-2: #256abf;
  --seq-3: #3987e5;
  --seq-4: #6da7ec;
  --seq-5: #9ec5f4;
}

.wrap { max-width: 1120px; margin: 0 auto; }

.eyebrow {
  font-size: 12px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px;
}
h1 { font-size: 30px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--text-secondary); font-size: 14px; margin: 0; }

.hero {
  margin: 32px 0 24px; padding: 28px 32px;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px;
}
.hero-label { font-size: 13px; color: var(--text-secondary); margin-bottom: 4px; }
.hero-value { font-size: 64px; font-weight: 600; line-height: 1.05; letter-spacing: -0.02em; }
.hero-meta { margin-top: 10px; font-size: 13px; color: var(--text-secondary); }
.hero-meta .up { color: var(--good); font-weight: 600; }
.hero-meta .down { color: var(--text-secondary); font-weight: 600; }

.tiles {
  display: grid; gap: 12px; margin-bottom: 28px;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
}
.tile {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px;
}
.tile-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.tile-value { font-size: 24px; font-weight: 600; letter-spacing: -0.01em; }
.tile-note { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 14px; padding: 22px 24px; margin-bottom: 20px;
}
.card-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin-bottom: 4px;
}
.card h2 { font-size: 16px; font-weight: 600; margin: 0; }
.card-sub { font-size: 13px; color: var(--text-secondary); margin: 2px 0 18px; }

.controls { display: flex; gap: 6px; align-items: center; }
.btn {
  font: inherit; font-size: 12px; font-weight: 600;
  color: var(--text-secondary); background: transparent;
  border: 1px solid var(--border); border-radius: 7px;
  padding: 5px 11px; cursor: pointer;
}
.btn:hover { color: var(--text-primary); }
.btn[aria-pressed="true"] { background: var(--grid); color: var(--text-primary); }
.btn:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }

.scroll-x { overflow-x: auto; overflow-y: hidden; padding-bottom: 4px; }
svg { display: block; }
.tick { font-size: 11px; fill: var(--text-muted); font-variant-numeric: tabular-nums; }
.axis-label { font-size: 11px; fill: var(--text-muted); }
.direct-label { font-size: 11px; font-weight: 600; fill: var(--text-secondary);
                font-variant-numeric: tabular-nums; }
.gridline { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.baseline { stroke: var(--axis); stroke-width: 1; shape-rendering: crispEdges; }

.legend-scale { display: flex; align-items: center; gap: 7px;
                font-size: 12px; color: var(--text-muted); margin-top: 14px; }
.legend-scale i { width: 13px; height: 13px; border-radius: 3px; display: inline-block; }

.cloud { position: relative; width: 100%; height: 340px; }
.cloud span {
  position: absolute; white-space: nowrap; line-height: 1;
  cursor: default; transform-origin: center;
}
.cloud .w-hot { color: var(--series-1); font-weight: 700; }
.cloud .w-1 { color: var(--text-primary); font-weight: 600; }
.cloud .w-2 { color: var(--text-secondary); font-weight: 600; }
.cloud .w-3 { color: var(--text-muted); font-weight: 500; }

.two-col { display: grid; gap: 20px; grid-template-columns: 1fr 1fr; }
@media (max-width: 820px) { .two-col { grid-template-columns: 1fr; } }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
     color: var(--text-muted); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }
.table-wrap { max-height: 420px; overflow-y: auto; margin-top: 8px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }

.badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; font-weight: 600; color: var(--text-secondary);
  white-space: nowrap;
}
.badge i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.b-linked_fresh i   { background: var(--series-1); }
.b-unlinked_fresh i { background: var(--surface-1); box-shadow: inset 0 0 0 2px var(--series-1); }
.b-linked_carried i { background: var(--axis); }

.tooltip {
  position: fixed; pointer-events: none; z-index: 50; opacity: 0;
  transition: opacity .1s; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 11px; font-size: 12px; color: var(--text-primary);
  box-shadow: 0 6px 20px rgba(0,0,0,.14); max-width: 260px;
}
.tooltip b { font-weight: 600; }
.tooltip .t-sub { color: var(--text-secondary); display: block; margin-top: 2px; }

.hidden { display: none; }
footer { margin-top: 28px; font-size: 12px; color: var(--text-muted); line-height: 1.7; }
footer code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
"""


_JS = r"""
const D = window.__WC__;
const fmt = n => n.toLocaleString('en-US');
const NS = 'http://www.w3.org/2000/svg';
const el = (t, a = {}) => {
  const n = document.createElementNS(NS, t);
  for (const k in a) n.setAttribute(k, a[k]);
  return n;
};
const css = v => getComputedStyle(document.querySelector('.viz-root'))
  .getPropertyValue(v).trim();

// ── tooltip ────────────────────────────────────────────────────────────────
const tip = document.getElementById('tip');
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY - r.height - 12;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y < 8) y = evt.clientY + 18;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
const hideTip = () => { tip.style.opacity = 0; };
// Hover and keyboard focus surface the same content — a tooltip must never be
// the only way to reach a value.
function bindTip(node, html) {
  node.addEventListener('mousemove', e => showTip(e, html));
  node.addEventListener('mouseleave', hideTip);
  node.addEventListener('focus', e => {
    const b = node.getBoundingClientRect();
    showTip({clientX: b.left + b.width / 2, clientY: b.top}, html);
  });
  node.addEventListener('blur', hideTip);
}

// Bars grow from a single baseline: rounded 4px at the data end, square where
// they meet the axis.
function colPath(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} `
       + `L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}
function rowPath(x, y, w, h, r) {
  r = Math.min(r, h / 2, w);
  return `M${x},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} `
       + `L${x + w},${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} L${x},${y + h} Z`;
}
function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || mag * 10;
  // Round the top tick UP past the max. Stopping at the last tick <= max would
  // put the tallest column above the plot, clipping it and its label.
  const top = Math.ceil(max / step) * step;
  const out = [];
  for (let v = 0; v <= top + step * 0.001; v += step) out.push(Math.round(v));
  return out;
}

// ── heatmap ────────────────────────────────────────────────────────────────
// Sequential encoding: one hue, light->dark, fixed thresholds so the colors
// mean the same thing between runs. Six classes including "no words".
const BINS = [1, 1000, 2500, 5000, 10000];
const binOf = w => {
  if (!w) return 0;
  let b = 1;
  for (let i = 1; i < BINS.length; i++) if (w >= BINS[i]) b = i + 1;
  return b;
};
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const parseDay = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const longDate = s => parseDay(s).toLocaleDateString('en-US',
  { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });

function drawHeatmap() {
  const host = document.getElementById('heatmap');
  host.innerHTML = '';
  const rows = D.history;
  const CELL = 13, GAP = 3, STEP = CELL + GAP, LEFT = 30, TOP = 20;

  // Pad the front so the first column starts on a Sunday.
  const lead = parseDay(rows[0].date).getDay();
  const weeks = Math.ceil((lead + rows.length) / 7);
  const svg = el('svg', {
    width: LEFT + weeks * STEP + 4, height: TOP + 7 * STEP + 6,
    role: 'img', 'aria-label': 'Daily words written over the last year',
  });

  ['Mon', 'Wed', 'Fri'].forEach((lbl, i) => {
    const t = el('text', { x: 0, y: TOP + (i * 2 + 1) * STEP + 10, class: 'axis-label' });
    t.textContent = lbl;
    svg.appendChild(t);
  });

  let lastMonth = -1;
  rows.forEach((row, i) => {
    const idx = lead + i, wk = Math.floor(idx / 7), dow = idx % 7;
    const x = LEFT + wk * STEP, y = TOP + dow * STEP;
    const d = parseDay(row.date);

    if (d.getMonth() !== lastMonth && d.getDate() <= 7) {
      lastMonth = d.getMonth();
      const t = el('text', { x, y: 12, class: 'axis-label' });
      t.textContent = MONTHS[lastMonth];
      svg.appendChild(t);
    }

    const cell = el('rect', {
      x, y, width: CELL, height: CELL, rx: 3,
      fill: css('--seq-' + binOf(row.words)),
      tabindex: 0, role: 'listitem',
      'aria-label': `${row.date}: ${row.words} words`,
    });
    bindTip(cell, `<b>${fmt(row.words)} words</b>`
      + `<span class="t-sub">${longDate(row.date)}`
      + (row.files ? ` · ${row.files} file${row.files > 1 ? 's' : ''}` : '')
      + `</span>`);
    svg.appendChild(cell);
  });
  host.appendChild(svg);

  const legend = document.getElementById('heat-legend');
  legend.innerHTML = '<span>Less</span>'
    + [0, 1, 2, 3, 4, 5].map(b => `<i style="background:${css('--seq-' + b)}"></i>`).join('')
    + '<span>More</span>'
    + `<span style="margin-left:10px">0 · &lt;1k · 1k · 2.5k · 5k · 10k+ words</span>`;
}

// ── 30-day trend ───────────────────────────────────────────────────────────
function drawTrend() {
  const host = document.getElementById('trend');
  host.innerHTML = '';
  const rows = D.history.slice(-30);
  const W = Math.max(host.clientWidth || 900, 560);
  const PAD = { t: 22, r: 14, b: 34, l: 52 };
  const H = 230, plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
  const max = Math.max(...rows.map(r => r.words), 1);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1];
  const band = plotW / rows.length;
  const bw = Math.min(24, band - 6);
  const yOf = v => PAD.t + plotH - (v / top) * plotH;

  const svg = el('svg', { width: W, height: H, role: 'img',
    'aria-label': 'Words written per day over the last 30 days' });

  ticks.forEach(v => {
    svg.appendChild(el('line', { x1: PAD.l, x2: W - PAD.r, y1: yOf(v), y2: yOf(v),
      class: v === 0 ? 'baseline' : 'gridline' }));
    const t = el('text', { x: PAD.l - 8, y: yOf(v) + 4, class: 'tick',
      'text-anchor': 'end' });
    t.textContent = v >= 1000 ? (v / 1000) + 'k' : v;
    svg.appendChild(t);
  });

  const peak = rows.reduce((a, b) => (b.words > a.words ? b : a), rows[0]);
  rows.forEach((row, i) => {
    const x = PAD.l + i * band + (band - bw) / 2;
    const d = parseDay(row.date);
    if (row.words > 0) {
      const h = Math.max(2, plotH - (yOf(row.words) - PAD.t));
      const bar = el('path', { d: colPath(x, yOf(row.words), bw, h, 4),
        fill: css('--series-1'), tabindex: 0,
        'aria-label': `${row.date}: ${row.words} words` });
      bindTip(bar, `<b>${fmt(row.words)} words</b><span class="t-sub">`
        + `${longDate(row.date)} · ${row.files} file${row.files === 1 ? '' : 's'}`
        + `</span>`);
      svg.appendChild(bar);
      // Label the extreme only — a number on every column goes unread.
      if (row.date === peak.date) {
        const t = el('text', { x: x + bw / 2, y: yOf(row.words) - 7,
          class: 'direct-label', 'text-anchor': 'middle' });
        t.textContent = fmt(row.words);
        svg.appendChild(t);
      }
    }
    if (i % 5 === 0 || i === rows.length - 1) {
      const t = el('text', { x: x + bw / 2, y: H - 12, class: 'tick',
        'text-anchor': 'middle' });
      t.textContent = (d.getMonth() + 1) + '/' + d.getDate();
      svg.appendChild(t);
    }
  });
  host.appendChild(svg);
}

// ── where the words went ───────────────────────────────────────────────────
function drawAreas() {
  const host = document.getElementById('areas');
  host.innerHTML = '';
  const rows = D.areas;
  if (!rows.length) { host.innerHTML = '<p class="card-sub">No files in this window.</p>'; return; }
  const W = Math.max(host.clientWidth || 500, 320);
  const LABEL = Math.min(190, Math.round(W * 0.42)), VALUE = 62;
  const ROW = 26, BAR = 16;
  const max = Math.max(...rows.map(r => r.words), 1);
  const plotW = W - LABEL - VALUE - 8;
  const svg = el('svg', { width: W, height: rows.length * ROW + 6, role: 'img',
    'aria-label': 'Words by vault folder' });

  rows.forEach((row, i) => {
    const y = i * ROW + 4;
    const lbl = el('text', { x: 0, y: y + BAR - 3, class: 'axis-label' });
    lbl.textContent = row.area.length > 26 ? row.area.slice(0, 25) + '…' : row.area;
    svg.appendChild(lbl);

    const w = Math.max(2, (row.words / max) * plotW);
    const bar = el('path', { d: rowPath(LABEL, y, w, BAR, 4),
      fill: css('--series-1'), tabindex: 0,
      'aria-label': `${row.area}: ${row.words} words` });
    bindTip(bar, `<b>${fmt(row.words)} words</b><span class="t-sub">`
      + `${row.area} · ${row.files} file${row.files === 1 ? '' : 's'}</span>`);
    svg.appendChild(bar);

    const val = el('text', { x: LABEL + w + 8, y: y + BAR - 3, class: 'direct-label' });
    val.textContent = fmt(row.words);
    svg.appendChild(val);
  });
  host.appendChild(svg);
}

// ── word cloud ─────────────────────────────────────────────────────────────
// Archimedean-spiral packing with canvas text measurement. Size carries
// frequency; the top three take the accent so the eye has somewhere to land.
let cloudMode = 'today';
function drawCloud() {
  const host = document.getElementById('cloud');
  host.innerHTML = '';
  const words = (D.cloud[cloudMode] || []).slice(0, 90);
  if (!words.length) {
    host.innerHTML = '<p class="card-sub">Not enough prose to build a cloud yet.</p>';
    return;
  }
  const W = host.clientWidth || 900, H = host.clientHeight || 340;
  const ctx = document.createElement('canvas').getContext('2d');
  const font = getComputedStyle(host).fontFamily;
  const hi = words[0].count, lo = words[words.length - 1].count;
  const MIN = 13, MAX = Math.min(64, Math.round(W / 11));
  const size = c => hi === lo ? (MIN + MAX) / 2
    : MIN + (MAX - MIN) * Math.sqrt((c - lo) / (hi - lo));

  const placed = [];
  const hits = (a) => placed.some(b =>
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y);

  words.forEach((entry, i) => {
    const fs = size(entry.count);
    const weight = i < 3 ? 700 : (fs > 26 ? 600 : 500);
    ctx.font = `${weight} ${fs}px ${font}`;
    const w = ctx.measureText(entry.word).width + 10, h = fs * 1.22;
    let pos = null;
    // Ellipse-biased spiral: wider than tall, matching the container.
    for (let t = 0; t < 620; t += 0.22) {
      const r = 3.1 * t;
      const x = W / 2 + r * Math.cos(t) - w / 2;
      const y = H / 2 + r * Math.sin(t) * 0.52 - h / 2;
      if (x < 0 || y < 0 || x + w > W || y + h > H) continue;
      const box = { x, y, w, h };
      if (!hits(box)) { pos = box; break; }
    }
    if (!pos) return;   // no room left — drop the tail rather than overlap
    placed.push(pos);

    const span = document.createElement('span');
    span.textContent = entry.word;
    span.className = i < 3 ? 'w-hot' : (fs > 30 ? 'w-1' : fs > 20 ? 'w-2' : 'w-3');
    span.style.cssText = `left:${pos.x + 5}px;top:${pos.y}px;`
      + `font-size:${fs}px;font-weight:${weight};line-height:${h}px;`;
    span.tabIndex = 0;
    bindTip(span, `<b>${entry.word}</b><span class="t-sub">`
      + `${fmt(entry.count)} occurrence${entry.count === 1 ? '' : 's'}</span>`);
    host.appendChild(span);
  });
}

function drawTopWords() {
  const body = document.querySelector('#topwords tbody');
  body.innerHTML = '';
  (D.cloud[cloudMode] || []).slice(0, 18).forEach((entry, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="num" style="color:var(--text-muted)">${i + 1}</td>`
      + `<td>${entry.word}</td><td class="num">${fmt(entry.count)}</td>`;
    body.appendChild(tr);
  });
}

// ── toggles ────────────────────────────────────────────────────────────────
function wireToggle(groupId, onPick) {
  document.querySelectorAll(`#${groupId} .btn`).forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll(`#${groupId} .btn`)
        .forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
      onPick(btn.dataset.value);
    });
  });
}
function wireTableToggle(btnId, tableId, chartId) {
  const btn = document.getElementById(btnId);
  btn.addEventListener('click', () => {
    const table = document.getElementById(tableId);
    const shown = table.classList.toggle('hidden');
    document.getElementById(chartId).classList.toggle('hidden', !shown);
    btn.setAttribute('aria-pressed', String(!shown));
    btn.textContent = shown ? 'Table' : 'Chart';
  });
}

function renderAll() {
  drawHeatmap(); drawTrend(); drawAreas(); drawCloud(); drawTopWords();
}
renderAll();

wireToggle('cloud-toggle', v => { cloudMode = v; drawCloud(); drawTopWords(); });
wireTableToggle('heat-table-btn', 'heat-table', 'heatmap-wrap');
wireTableToggle('trend-table-btn', 'trend-table', 'trend');

let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderAll, 160);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', renderAll);
"""


def _delta_html(stats: dict, words: int) -> str:
    if not stats.get("prev_date"):
        return "First day with words in this window."
    prev = stats["prev_words"]
    diff = words - prev
    label = datetime.strptime(stats["prev_date"], "%Y-%m-%d").strftime("%b %-d")
    if diff == 0:
        return f"Level with {label} ({prev:,})."
    cls = "up" if diff > 0 else "down"
    return (f'<span class="{cls}">{diff:+,}</span> vs {label} '
            f'({prev:,} words)')


BUCKET_LABEL = {
    "linked_fresh": "wired",
    "unlinked_fresh": "unlinked",
    "linked_carried": "earlier",
}


def render_html(payload: dict) -> str:
    scan = payload["scan"]
    stats = payload["stats"]
    date = payload["date"]
    pretty = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    words = scan["words_written"]

    rows = "".join(
        '<tr><td class="mono">{rel}</td>'
        '<td><span class="badge b-{bucket}"><i></i>{label}</span></td>'
        '<td class="num">{prose:,}</td><td class="num">{code:,}</td>'
        '<td class="num"><b>{total:,}</b></td></tr>'.format(
            rel=html.escape(rec["rel"]), bucket=rec["bucket"],
            label=BUCKET_LABEL[rec["bucket"]], prose=rec["prose"],
            code=rec["code"], total=rec["total"])
        for rec in payload["files"]
    ) or '<tr><td colspan="5">No associated files found.</td></tr>'

    heat_rows = "".join(
        f'<tr><td class="mono">{r["date"]}</td>'
        f'<td class="num">{r["words"]:,}</td>'
        f'<td class="num">{r["files"]:,}</td></tr>'
        for r in reversed(payload["history"]) if r["words"]
    ) or '<tr><td colspan="3">No words recorded in this window.</td></tr>'

    trend_rows = "".join(
        f'<tr><td class="mono">{r["date"]}</td>'
        f'<td class="num">{r["prose"]:,}</td>'
        f'<td class="num">{r["code"]:,}</td>'
        f'<td class="num"><b>{r["words"]:,}</b></td></tr>'
        for r in reversed(payload["history"][-30:])
    )

    tiles = [
        ("Files written", f'{scan["files_written"]}',
         f'{scan["files_linked_fresh"]} wired · {scan["files_unlinked_fresh"]} unlinked'),
        ("Prose / code", f'{scan["prose_written"]:,}',
         f'plus {scan["code_written"]:,} words in code blocks'),
        ("Daily note itself", f'{scan["daily_note_words"]:,}',
         "words in the note, before its spokes"),
        ("Last 7 days", f'{stats["week_words"]:,}',
         f'{stats["streak"]}-day writing streak'),
        ("30-day average", f'{stats["avg30"]:,}',
         f'per active day · {stats["active_days_30"]} of 30 active'),
        ("Words in scope", f'{scan["words_in_scope"]:,}',
         f'{scan["files_in_scope"]} associated files incl. carried'),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="tile-label">{html.escape(label)}</div>'
        f'<div class="tile-value">{value}</div>'
        f'<div class="tile-note">{html.escape(note)}</div></div>'
        for label, value, note in tiles
    )

    best = ""
    if stats.get("best_date"):
        bd = datetime.strptime(stats["best_date"], "%Y-%m-%d").strftime("%b %-d")
        best = (f' · Best day in this window: <b>{stats["best_words"]:,}</b> on {bd}')

    data_json = json.dumps(payload, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Word Count — {html.escape(date)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="viz-root"><div class="wrap">

  <header>
    <div class="eyebrow">Vault word count</div>
    <h1>{html.escape(pretty)}</h1>
    <p class="sub">The daily note and every file wired to it ·
       generated {html.escape(payload["generated_at"])}</p>
  </header>

  <section class="hero">
    <div class="hero-label">Words written this day</div>
    <div class="hero-value">{words:,}</div>
    <div class="hero-meta">{_delta_html(stats, words)}{best}</div>
  </section>

  <section class="tiles">{tiles_html}</section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>A year of writing</h2>
        <p class="card-sub">Each cell is one day, shaded by words written.
          {stats["active_days_all"]:,} active days ·
          {stats["year_words"]:,} words in {html.escape(date[:4])}.</p>
      </div>
      <div class="controls">
        <button class="btn" id="heat-table-btn" aria-pressed="false">Table</button>
      </div>
    </div>
    <div class="scroll-x" id="heatmap-wrap"><div id="heatmap"></div></div>
    <div class="legend-scale" id="heat-legend"></div>
    <div class="table-wrap hidden" id="heat-table">
      <table><thead><tr><th>Date</th><th class="num">Words</th>
        <th class="num">Files</th></tr></thead><tbody>{heat_rows}</tbody></table>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>Last 30 days</h2>
        <p class="card-sub">Words written per day. Peak labelled; hover or focus
          any column for the rest.</p>
      </div>
      <div class="controls">
        <button class="btn" id="trend-table-btn" aria-pressed="false">Table</button>
      </div>
    </div>
    <div class="scroll-x"><div id="trend"></div></div>
    <div class="table-wrap hidden" id="trend-table">
      <table><thead><tr><th>Date</th><th class="num">Prose</th>
        <th class="num">Code</th><th class="num">Total</th></tr></thead>
        <tbody>{trend_rows}</tbody></table>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>What you wrote about</h2>
        <p class="card-sub">Word size is frequency, after stopwords. The three
          most frequent take the accent.</p>
      </div>
      <div class="controls" id="cloud-toggle">
        <button class="btn" data-value="today" aria-pressed="true">This day</button>
        <button class="btn" data-value="recent" aria-pressed="false">Last 30 days</button>
      </div>
    </div>
    <div class="cloud" id="cloud"></div>
  </section>

  <div class="two-col">
    <section class="card">
      <h2>Top words</h2>
      <p class="card-sub">The cloud's table view, same selection.</p>
      <div class="table-wrap"><table id="topwords">
        <thead><tr><th class="num">#</th><th>Word</th>
          <th class="num">Count</th></tr></thead><tbody></tbody>
      </table></div>
    </section>

    <section class="card">
      <h2>Where the words went</h2>
      <p class="card-sub">By vault folder, last {stats["area_days"]} days.</p>
      <div id="areas"></div>
    </section>
  </div>

  <section class="card">
    <h2>Files for {html.escape(date)}</h2>
    <p class="card-sub">
      <span class="badge b-linked_fresh"><i></i>wired</span> — reachable from the
      daily note and written this day ·
      <span class="badge b-unlinked_fresh"><i></i>unlinked</span> — written this
      day but not reachable from the note ·
      <span class="badge b-linked_carried"><i></i>earlier</span> — associated
      context, written another day (excluded from the headline).</p>
    <div class="table-wrap">
      <table><thead><tr><th>File</th><th>Bucket</th><th class="num">Prose</th>
        <th class="num">Code</th><th class="num">Total</th></tr></thead>
        <tbody>{rows}</tbody></table>
    </div>
  </section>

  <footer>
    A word is a whitespace-separated token starting with a letter or digit, so
    markdown syntax counts for nothing. YAML frontmatter, HTML comments and URLs
    are stripped; fenced code is counted separately from prose.<br>
    Days are attributed by the date in the filename where there is one
    (<code>2026-07-25_hub.md</code>), otherwise by modification time — so the
    morning rollover touching yesterday's note does not re-credit it to today.
    A long-lived file edited today still counts entirely toward today; that is
    the honest limit of mtime attribution, and the <b>earlier</b> bucket exists
    to keep such files visible without inflating the headline.<br>
    Generated by <code>wordcount_dashboard.py</code> ·
    regenerate with <code>/update-daily-note</code>.
  </footer>

</div></div>
<div class="tooltip" id="tip" role="status"></div>
<script>window.__WC__ = {data_json};</script>
<script>{_JS}</script>
</body>
</html>
"""


def build(date: Optional[str] = None, out: Optional[Path] = None,
          open_browser: bool = False, max_depth: int = 2) -> dict:
    """Build the dashboard, write it, optionally open it. Returns a summary."""
    payload = build_payload(date, max_depth=max_depth)
    out = Path(out) if out else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload), encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass
    return {
        "path": str(out),
        "date": payload["date"],
        "words_written": payload["scan"]["words_written"],
        "files_written": payload["scan"]["files_written"],
        "words_in_scope": payload["scan"]["words_in_scope"],
        "files_in_scope": payload["scan"]["files_in_scope"],
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Build the vault word-count dashboard.")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="target date (default today)")
    p.add_argument("--out", metavar="PATH", help=f"output file (default {DEFAULT_OUT})")
    p.add_argument("--no-open", action="store_true", help="write it, don't open it")
    p.add_argument("--depth", type=int, default=2,
                   help="wikilink hops to follow from the daily note (default 2)")
    args = p.parse_args()

    result = build(args.date, Path(args.out) if args.out else None,
                   open_browser=not args.no_open, max_depth=args.depth)
    print(f"📊 {result['words_written']:,} words written · "
          f"{result['files_written']} file(s)")
    print(f"   {result['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
