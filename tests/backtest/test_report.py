from __future__ import annotations

import json
import re

from ewbot.backtest.metrics import compute_metrics, trade_stats
from ewbot.backtest.report import to_jsonable, write_report

T0 = 1_577_836_800
H = 3600
H4 = 14400


def _snapshot():
    return {
        "counts": [
            {"pattern": "IMP", "d": 1, "phase": "W2", "q": 0.71,
             "points": [(T0 - 40 * H4, 90.0), (T0 - 30 * H4, 120.0)]},
            {"pattern": "ABC", "d": -1, "phase": "C", "q": 0.4, "points": [(T0 - 50 * H4, 130.0)]},
        ],
        "q_top": 0.71, "s_rev": 0.3, "E_live": 105.0, "reason": "W3 long",
    }


def test_write_report_contains_sections(tmp_path, mk_trade, mk_equity):
    eq_a = mk_equity([100, 110, 99, 120], t0=T0)
    eq_b = mk_equity([100, 101, 102, 103], t0=T0)
    t1 = mk_trade(entry_ts=T0 + H, exit_ts=T0 + 3 * H, pnl=12.5, r=1.5, setup="W3", snapshot=_snapshot())
    t2 = mk_trade(entry_ts=T0 + 2 * H, exit_ts=T0 + 3 * H, pnl=-3.0, r=-1.0, setup="C_END", direction=-1)
    m = compute_metrics(eq_a, [t1, t2])
    sections = {
        "notes": ["costs: 0.05% fee"],
        "equity": {"A": [(p.ts, p.equity) for p in eq_a], "BH": eq_b},
        "comparison": {"A": m, "BH": compute_metrics(eq_b, [])},
        "random": {"runs": 3, "dist": {"sharpe": {"mean": 0.1, "p05": -0.2, "p50": 0.1, "p95": 0.4}}, "beaten_by": {"A": {"sharpe": 0.67, "calmar": 0.5, "total_return": 0.5}}},
        "ci": {"A": {"avg_r": (0.1, 1.2)}},
        "per_setup": {"A": {"W3": trade_stats([t1]), "C_END": trade_stats([t2])}},
        "long_short": {"A": {"long": trade_stats([t1]), "short": trade_stats([t2])}},
        "per_year": {"A": {"2020": {"total_return": 0.2, "max_dd": 0.1, "sharpe": 1.0, "n_trades": 2}}},
        "reliability": [{"q_lo": 0.6, "q_hi": 0.7, "n": 2, "hit_rate": 0.5, "avg_r": 0.25}],
        "trades": {"A": [t1, t2]},
        "candles4h": {"ts": [T0 - 60 * H4 + i * H4 for i in range(80)], "close": [100.0 + i for i in range(80)]},
        "folds": [{"test": "2019H1", "K4": 3.0, "oos_return": 0.12}],
    }
    out = write_report(tmp_path / "out" / "r.html", "Test <report>", sections)
    assert out.exists() and out.name == "r.html"
    html = out.read_text(encoding="utf-8")
    assert "cdn.plot.ly" in html and "Plotly.newPlot" in html
    for needle in ("Equity", "Drawdown", "Comparison", "Per setup", "Long / short", "Per year",
                   "Reliability by q", "Trades - A (2)", "RANDOM x3", "C_END", "W3", "2020-01-01 01:00".replace(" ", "T") + ":00Z",
                   "<details", "toggleSnap(0)", "show table", "costs: 0.05% fee", "<h2>folds</h2>", "2019H1"):
        assert needle in html, needle
    assert "Test &lt;report&gt;" in html and "<report>" not in html
    # only the trade with a snapshot gets a chart; payload is valid JSON with labelled polyline points
    assert "toggleSnap(1)" not in html and "snaprow-1" not in html
    payload = re.search(r"const TRADES = (\[.*?\]);\n", html, re.S).group(1)
    trades = json.loads(payload)
    assert len(trades) == 2 and trades[0]["counts"][0]["labels"] == ["S", "1"]
    assert trades[0]["counts"][1]["labels"] == ["X"] and trades[0]["meta"]["reason"] == "W3 long"
    assert trades[1]["counts"] == []
    assert html.count("</script>") == 2  # the plotly tag + the single inline script


def test_write_report_minimal_and_script_injection_safe(tmp_path):
    out = write_report(tmp_path / "m.html", "Empty", {"notes": ["</script><b>x</b>"], "weird": {"k": float("nan")}})
    html = out.read_text(encoding="utf-8")
    assert "&lt;/script&gt;" in html and "</script><b>" not in html
    assert '"k": null' in html
    assert "<h2>Equity</h2>" not in html and "const EQ = {};" in html


def test_to_jsonable_handles_dataclasses_numpy_nan(mk_trade):
    import numpy as np

    d = to_jsonable({"t": mk_trade(), "n": np.int64(3), "x": float("inf"), "tup": (1, 2)})
    assert d["n"] == 3 and d["x"] is None and d["tup"] == [1, 2] and d["t"]["setup"] == "W3"
    json.dumps(d, allow_nan=False)


def _cells(html: str, first: str) -> list[str]:
    row = re.search(rf"<tr><td>{re.escape(first)}</td>(.*?)</tr>", html, re.S).group(1)
    return [first] + re.findall(r"<td>(.*?)</td>", row, re.S)


def test_snapshot_chart_gets_its_own_full_width_row(tmp_path, mk_trade):
    """The chart is not squeezed into the last cell: a hidden row right below the trade row spans
    every column, and the chart is resized once it becomes visible."""
    t0 = mk_trade(entry_ts=T0 + H, exit_ts=T0 + 3 * H, snapshot=_snapshot())
    t1 = mk_trade(entry_ts=T0 + 4 * H, exit_ts=T0 + 5 * H)  # no snapshot: no chart row
    t2 = mk_trade(entry_ts=T0 + 6 * H, exit_ts=T0 + 7 * H, snapshot=_snapshot())
    html = write_report(tmp_path / "t.html", "T", {"trades": {"A": [t0, t1, t2]}}).read_text(encoding="utf-8")
    table = re.search(r'<table class="trades">(.*?)</table>', html, re.S).group(1)
    n_cols = len(re.findall(r"<th>", table))
    assert n_cols == 11
    rows = re.findall(r"<tr[^>]*>.*?</tr>", table.split("<tbody>")[1], re.S)
    assert len(rows) == 5  # trade 0, its chart row, trade 1, trade 2, its chart row
    for k, gid in ((1, 0), (4, 2)):
        assert rows[k] == (
            f'<tr class="snaprow" id="snaprow-{gid}" hidden><td colspan="{n_cols}">'
            f'<div class="snap" id="snap-{gid}"></div></td></tr>'
        )
        assert f"toggleSnap({gid})" in rows[k - 1]  # the button sits in the trade row just above
    assert "toggleSnap(1)" not in html and "snaprow-1" not in html
    script = html.split("<script>")[-1]
    assert "function toggleSnap" in script and "Plotly.Plots.resize" in script


def test_benchmarks_show_no_trade_r_statistics(tmp_path, mk_trade, mk_equity):
    """BH and EMA are always-in-the-market benchmarks: their R multiples are not comparable with the
    strategies', so win rate, avg R and its CI are shown as '-' (trades and exposure still are)."""
    eq = mk_equity([100, 110, 120, 130], t0=T0)
    tr = [mk_trade(entry_ts=T0, exit_ts=T0 + 3 * H, pnl=30.0, r=4.0)]
    m = compute_metrics(eq, tr)
    ci = {n: {"avg_r": (1.0, 2.0)} for n in ("A", "BH", "EMA")}
    html = write_report(
        tmp_path / "c.html", "C", {"comparison": {"A": m, "BH": m, "EMA": m}, "ci": ci}
    ).read_text(encoding="utf-8")
    head = re.findall(r"<th>(.*?)</th>", re.search(r'<table class="cmp">(.*?)</thead>', html, re.S).group(1))
    win, avg, trades, ci_col = (head.index(h) for h in ("Win rate", "Avg R", "Trades", "Avg R 95% CI"))
    a = _cells(html, "A")
    assert a[win] == "100.0%" and a[avg] == "4.00" and a[ci_col] == "[1.00, 2.00]"
    for name in ("BH", "EMA"):
        c = _cells(html, name)
        assert (c[win], c[avg], c[ci_col]) == ("-", "-", "-"), name
        assert c[trades] == "1" and c[1] == a[1]  # trade count and return are still shown
