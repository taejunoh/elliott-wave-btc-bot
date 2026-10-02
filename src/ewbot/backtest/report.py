"""Self-contained HTML report (plotly via CDN): equity, drawdown, tables, per-trade charts."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ewbot.engine.loop import Trade

PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

_METRIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "Return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("max_dd", "Max DD", "pct"),
    ("sharpe", "Sharpe", "num"),
    ("sortino", "Sortino", "num"),
    ("calmar", "Calmar", "num"),
    ("win_rate", "Win rate", "pct"),
    ("avg_r", "Avg R", "num"),
    ("n_trades", "Trades", "int"),
    ("exposure", "Exposure", "pct"),
)
_STAT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("n", "Trades", "int"),
    ("win_rate", "Win rate", "pct"),
    ("avg_r", "Avg R", "num"),
    ("pnl", "PnL", "money"),
)
_KNOWN_KEYS = {
    "notes", "equity", "comparison", "random", "ci", "per_setup", "long_short", "per_year",
    "reliability", "trades", "candles4h", "snapshot_window_bars",
}


class _Raw(str):
    """HTML fragment that must not be escaped by _table."""


def to_jsonable(o: Any) -> Any:
    """Recursively convert dataclasses / numpy / tuples / NaN into strict-JSON-safe values."""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return to_jsonable(dataclasses.asdict(o))
    if isinstance(o, Mapping):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, np.generic):
        return to_jsonable(o.item())
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    return str(o)


def _script_json(o: Any) -> str:
    return json.dumps(to_jsonable(o), separators=(",", ":")).replace("</", "<\\/")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(value: Any, kind: str) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"
    if kind == "int":
        return f"{int(value):,}"
    if kind == "money":
        return f"{float(value):,.0f}"
    return f"{float(value):.2f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], cls: str = "") -> str:
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{c if isinstance(c, _Raw) else escape(str(c))}</td>" for c in row)
        body.append(f"<tr>{cells}</tr>")
    return f'<div class="tw"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


# ----------------------------------------------------------------------------- equity data


def _pt(p: Any) -> tuple[int, float]:
    return (int(p.ts), float(p.equity)) if hasattr(p, "ts") else (int(p[0]), float(p[1]))


def _equity_payload(equity: Mapping[str, Sequence[Any]], max_points: int = 4000) -> dict[str, dict[str, list]]:
    out: dict[str, dict[str, list]] = {}
    for name, pts in equity.items():
        if not pts:
            continue
        pairs = [_pt(p) for p in pts]
        ts = np.array([p[0] for p in pairs], dtype=np.int64)
        ev = np.array([p[1] for p in pairs], dtype=np.float64)
        peak = np.maximum.accumulate(ev)
        dd = np.where(peak > 0, ev / np.where(peak > 0, peak, 1.0) - 1.0, 0.0)
        k = max(1, math.ceil(len(ts) / max_points))
        idx = np.arange(0, len(ts), k)
        if idx[-1] != len(ts) - 1:
            idx = np.append(idx, len(ts) - 1)
        worst = [int(i + np.argmin(dd[i : i + k])) for i in range(0, len(ts), k)]  # keep each block's trough
        out[name] = {
            "x": [_iso(ts[i]) for i in idx],
            "y": [round(float(ev[i]), 2) for i in idx],
            "dd_x": [_iso(ts[i]) for i in worst],
            "dd": [round(float(dd[i]) * 100, 3) for i in worst],
        }
    return out


# ----------------------------------------------------------------------------- snapshots


def _labels(pattern: Any, n: int) -> list[str]:
    base = ["X", "A", "B"] if pattern == "ABC" else ["S", "1", "2", "3", "4"]
    return base[:n]


def _snapshot_view(t: Trade) -> dict[str, Any]:
    """Normalise Trade.snapshot for the browser. The counts list is looked up under
    'counts' | 'top5' | 'top_counts'; each item: {pattern, d, phase, q, points: [(ts, price)]}."""
    snap = t.snapshot or {}
    raw: Sequence[Any] = []
    for key in ("counts", "top5", "top_counts"):
        if isinstance(snap.get(key), (list, tuple)):
            raw = snap[key]
            break
    counts = []
    for c in raw:
        if not isinstance(c, Mapping):
            continue
        pts = [[int(p[0]), float(p[1])] for p in (c.get("points") or [])]
        counts.append(
            {
                "pattern": c.get("pattern"), "d": c.get("d"), "phase": c.get("phase"),
                "q": c.get("q"), "pts": pts, "labels": _labels(c.get("pattern"), len(pts)),
            }
        )
    return {
        "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "dir": t.direction, "setup": t.setup,
        "entry": t.entry_price, "stop": t.stop0 or None, "tp1": t.tp1 or None, "tp2": t.tp2,
        "counts": counts,
        "meta": {k: snap.get(k) for k in ("q_top", "s_rev", "E_live", "reason")},
    }


# ----------------------------------------------------------------------------- html parts


def _comparison_html(comp: Mapping[str, Mapping[str, Any]], rnd: Mapping[str, Any] | None, ci: Mapping[str, Any] | None) -> str:
    headers = ["Strategy"] + [h for _, h, _ in _METRIC_COLUMNS] + ["Avg R 95% CI"]
    rows: list[list[Any]] = []
    for name, m in comp.items():
        interval = (ci or {}).get(name, {}).get("avg_r")
        ci_txt = f"[{interval[0]:.2f}, {interval[1]:.2f}]" if interval else "-"
        rows.append([name] + [_fmt(m.get(k), kind) for k, _, kind in _METRIC_COLUMNS] + [ci_txt])
    dist = (rnd or {}).get("dist") or {}
    if dist:
        cells: list[Any] = [f"RANDOM x{rnd.get('runs', '?')} (mean [p5, p95])"]
        for k, _, kind in _METRIC_COLUMNS:
            d = dist.get(k)
            cells.append("-" if not d else f"{_fmt(d['mean'], kind)} [{_fmt(d['p05'], kind)}, {_fmt(d['p95'], kind)}]")
        cells.append("-")
        rows.append(cells)
    out = _table(headers, rows, "cmp")
    beaten = (rnd or {}).get("beaten_by") or {}
    if beaten:
        items = "; ".join(
            f"{n}: Sharpe {v['sharpe'] * 100:.0f}%, Calmar {v['calmar'] * 100:.0f}%" for n, v in beaten.items()
        )
        out += f'<p class="muted">Share of random runs beaten by each strategy - {escape(items)}</p>'
    return out


def _stats_tables(title: str, data: Mapping[str, Mapping[str, Mapping[str, Any]]], first_col: str) -> str:
    parts = [f"<h2>{escape(title)}</h2>"]
    for strat, groups in data.items():
        rows = []
        for key, st in groups.items():
            ci = f"[{st['ci_lo']:.2f}, {st['ci_hi']:.2f}]" if "ci_lo" in st else "-"
            rows.append([key] + [_fmt(st.get(k), kind) for k, _, kind in _STAT_COLUMNS] + [ci])
        parts.append(f"<h3>{escape(strat)}</h3>" + _table([first_col] + [h for _, h, _ in _STAT_COLUMNS] + ["Avg R 95% CI"], rows))
    return "".join(parts)


def _per_year_html(data: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> str:
    parts = ["<h2>Per year</h2>"]
    cols = (("total_return", "Return", "pct"), ("max_dd", "Max DD", "pct"), ("sharpe", "Sharpe", "num"), ("n_trades", "Trades", "int"))
    for strat, years in data.items():
        rows = [[y] + [_fmt(m.get(k), kind) for k, _, kind in cols] for y, m in years.items()]
        parts.append(f"<h3>{escape(strat)}</h3>" + _table(["Year"] + [h for _, h, _ in cols], rows))
    return "".join(parts)


def _extra_html(key: str, value: Any) -> str:
    v = to_jsonable(value)
    if isinstance(v, list) and v and all(isinstance(r, dict) and all(not isinstance(x, (dict, list)) for x in r.values()) for r in v):
        cols = list(v[0].keys())
        return f"<h2>{escape(key)}</h2>" + _table(cols, [[r.get(c, "") for c in cols] for r in v])
    return f"<h2>{escape(key)}</h2><pre>{escape(json.dumps(v, indent=2), quote=False)}</pre>"


# ----------------------------------------------------------------------------- page

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="__CDN__"></script>
<style>
:root{--bg:#fff;--fg:#1b1f27;--muted:#5d6675;--line:#d9dde5;--card:#f5f6f8;--accent:#2b6cb0}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--fg:#e7eaef;--muted:#9aa3b0;--line:#2d333d;--card:#1b1f26;--accent:#63a4ff}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:16px;max-width:1200px;margin-inline:auto}
h1{font-size:22px;margin:4px 0 12px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}h3{font-size:14px;margin:14px 0 6px;color:var(--muted)}
.muted{color:var(--muted);font-size:12px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
thead th{background:var(--card);position:sticky;top:0}
.chart{width:100%;height:380px}.snap{width:100%;height:340px}
details{margin:6px 0}summary{cursor:pointer;color:var(--accent)}
pre{background:var(--card);padding:10px;overflow:auto;border-radius:6px}
ul.notes{margin:0 0 8px;padding-left:18px;color:var(--muted)}
</style></head><body>
__BODY__
<script>
__DATA__
const FG = getComputedStyle(document.documentElement).getPropertyValue('--fg').trim() || '#222';
const GRID = getComputedStyle(document.documentElement).getPropertyValue('--line').trim() || '#ccc';
function baseLayout(extra){
  return Object.assign({margin:{t:20,r:16,b:36,l:56},paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
    font:{color:FG},legend:{orientation:'h'},xaxis:{gridcolor:GRID},yaxis:{gridcolor:GRID}}, extra||{});
}
function drawMain(){
  const names = Object.keys(EQ);
  if(!names.length || typeof Plotly === 'undefined'){return;}
  Plotly.newPlot('eq-chart', names.map(n=>({x:EQ[n].x,y:EQ[n].y,name:n,mode:'lines'})),
    baseLayout({yaxis:{type: LOGY ? 'log' : 'linear', title:'Equity', gridcolor:GRID}}), {responsive:true});
  Plotly.newPlot('dd-chart', names.map(n=>({x:EQ[n].dd_x,y:EQ[n].dd,name:n,mode:'lines'})),
    baseLayout({yaxis:{title:'Drawdown (%)', gridcolor:GRID}}), {responsive:true});
}
function iso(s){return new Date(s*1000).toISOString();}
function renderSnap(i){
  const div = document.getElementById('snap-'+i);
  if(!div || div.dataset.done){return;}
  if(typeof Plotly === 'undefined'){div.textContent = 'Plotly not loaded (offline?)'; return;}
  div.dataset.done = '1';
  const t = TRADES[i], W = WINDOW_BARS * 14400;
  const top = t.counts.length ? t.counts[0] : null;
  let lo = t.entry_ts - W; const hi = t.exit_ts + W;
  if(top && top.pts.length){lo = Math.min(lo, top.pts[0][0] - 2 * 14400);}
  const xs = [], ys = [];
  for(let k = 0; k < C4.ts.length; k++){const s = C4.ts[k]; if(s >= lo && s <= hi){xs.push(iso(s)); ys.push(C4.close[k]);}}
  const traces = [{x:xs,y:ys,mode:'lines',name:'4h close',line:{width:1.5}}];
  if(top){
    traces.push({x:top.pts.map(p=>iso(p[0])),y:top.pts.map(p=>p[1]),mode:'lines+markers+text',text:top.labels,
      textposition:'top center',line:{dash:'dot'},
      name:top.pattern+' '+top.phase+' q='+(top.q==null?'?':Number(top.q).toFixed(2))});
  }
  const ex = iso(t.entry_ts), xe = iso(t.exit_ts);
  traces.push({x:[ex],y:[t.entry],mode:'markers',name:'entry',marker:{size:12,symbol:t.dir>0?'triangle-up':'triangle-down'}});
  const shapes = [];
  [t.stop,t.tp1,t.tp2].forEach(v=>{if(v!=null){shapes.push({type:'line',x0:ex,x1:xe,y0:v,y1:v,line:{dash:'dash',width:1,color:'#888'}});}});
  Plotly.newPlot(div, traces, baseLayout({shapes:shapes,title:{text:t.setup+' '+(t.dir>0?'long':'short')+' - '+JSON.stringify(t.meta),font:{size:11}}}), {responsive:true});
}
window.addEventListener('load', drawMain);
</script></body></html>
"""


def write_report(path: Path, title: str, sections: dict[str, Any]) -> Path:
    """Write a self-contained HTML report (plotly loaded from a CDN <script> tag).

    Recognised `sections` keys (all optional; unknown keys are dumped as tables / JSON):
      notes: list[str]
      equity: {name: [(ts, equity) | EquityPoint]}         -> equity + drawdown charts
      comparison: {name: Metrics dict}, random: runner "random" dict, ci: {name: {"avg_r": (lo, hi)}}
      per_setup / long_short: {name: {group: trade_stats}}; per_year: {name: {year: metrics dict}}
      reliability: list[dict] (metrics.reliability_by_q)
      trades: {name: [Trade]}; candles4h: {"ts": [...], "close": [...]}; snapshot_window_bars: int (60)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sections = dict(sections)
    for key in ("comparison", "random", "ci", "per_setup", "long_short", "per_year", "reliability"):
        if key in sections:
            sections[key] = to_jsonable(sections[key])  # Metrics dataclasses, tuples, NaN -> plain JSON types
    body: list[str] = [f"<h1>{escape(title)}</h1>"]
    if sections.get("notes"):
        body.append('<ul class="notes">' + "".join(f"<li>{escape(str(n))}</li>" for n in sections["notes"]) + "</ul>")

    eq = _equity_payload(sections.get("equity") or {})
    log_y = bool(eq) and all(min(v["y"]) > 0 for v in eq.values())
    if eq:
        body.append('<h2>Equity</h2><div id="eq-chart" class="chart"></div>')
        body.append('<h2>Drawdown</h2><div id="dd-chart" class="chart"></div>')
    if sections.get("comparison"):
        body.append("<h2>Comparison</h2>" + _comparison_html(sections["comparison"], sections.get("random"), sections.get("ci")))
    if sections.get("per_setup"):
        body.append(_stats_tables("Per setup", sections["per_setup"], "Setup"))
    if sections.get("long_short"):
        body.append(_stats_tables("Long / short", sections["long_short"], "Side"))
    if sections.get("per_year"):
        body.append(_per_year_html(sections["per_year"]))
    if sections.get("reliability"):
        rows = [
            [f"{r['q_lo']:.2f} - {r['q_hi']:.2f}", r["n"], _fmt(r["hit_rate"], "pct"), _fmt(r["avg_r"], "num")]
            for r in sections["reliability"]
        ]
        body.append("<h2>Reliability by q</h2>" + _table(["q bin", "Trades", "Hit rate", "Avg R"], rows))

    views: list[dict[str, Any]] = []
    for name, trades in (sections.get("trades") or {}).items():
        rows = []
        for t in trades:
            gid = len(views)
            view = _snapshot_view(t)
            views.append(view)
            chart = _Raw(
                f'<details ontoggle="renderSnap({gid})"><summary>chart</summary><div class="snap" id="snap-{gid}"></div></details>'
                if view["counts"]
                else ""
            )
            rows.append(
                [
                    _iso(t.entry_ts), _iso(t.exit_ts), t.setup, "L" if t.direction > 0 else "S", t.contracts,
                    f"{t.entry_price:,.2f}", f"{t.exit_price_avg:,.2f}", f"{t.pnl:,.2f}", f"{t.r_multiple:.2f}",
                    t.exit_reason, chart,
                ]
            )
        headers = ["Entry (UTC)", "Exit (UTC)", "Setup", "Dir", "Ctr", "Entry px", "Exit px", "PnL", "R", "Exit", "Snapshot"]
        body.append(f"<h2>Trades - {escape(name)} ({len(trades)})</h2><details><summary>show table</summary>{_table(headers, rows)}</details>")

    for key, value in sections.items():
        if key not in _KNOWN_KEYS:
            body.append(_extra_html(key, value))

    c4 = sections.get("candles4h") or {"ts": [], "close": []}
    data_js = (
        f"const EQ = {_script_json(eq)};\nconst LOGY = {'true' if log_y else 'false'};\n"
        f"const C4 = {_script_json({'ts': list(c4['ts']), 'close': list(c4['close'])})};\n"
        f"const TRADES = {_script_json(views)};\n"
        f"const WINDOW_BARS = {int(sections.get('snapshot_window_bars', 60))};"
    )
    page = (
        _PAGE.replace("__TITLE__", escape(title))
        .replace("__CDN__", PLOTLY_CDN)
        .replace("__DATA__", data_js)
        .replace("__BODY__", "\n".join(body))
    )
    path.write_text(page, encoding="utf-8")
    return path
