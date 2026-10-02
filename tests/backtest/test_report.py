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
                   "<details", "renderSnap(0)", "show table", "costs: 0.05% fee", "<h2>folds</h2>", "2019H1"):
        assert needle in html, needle
    assert "Test &lt;report&gt;" in html and "<report>" not in html
    # only the trade with a snapshot gets a chart; payload is valid JSON with labelled polyline points
    assert "renderSnap(1)" not in html
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
