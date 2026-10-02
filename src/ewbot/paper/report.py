"""Paper-trading HTML report (Plan 2 spec 6.2), built on backtest.report.write_report.

Top of the page (notes): the go-live status block, epochs, holdout data integrity, the open position
and the current top count. Then the out-of-sample equity / drawdown (bars after the holdout's last
bar), its metrics, the trade tables with per-trade snapshot charts and the discrepancy list."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.report import write_report
from ewbot.data.candles import aggregate_4h
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.paper.criteria import status_lines
from ewbot.paper.ledger import Ledger, current_epoch, trade_from_record
from ewbot.paper.notify import position_line
from ewbot.types import H1

CANDLE_LOOKBACK_S = 45 * 86400  # 4h context before the paper window for the snapshot charts


def _utc(ts: int | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _top_note(top: dict[str, Any] | None) -> str:
    if top is None:
        return "현재 1등 카운트: 없음"
    return (
        f"현재 1등 카운트 ({_utc(top['ts'])}): {top['pattern']} d={top['d']} {top['phase']} · q {top['q']:.3f} · "
        f"q_top {top['q_top']:.3f} · S_rev {top['s_rev']:.3f} · E_live {top['E_live']:,.2f}"
    )


def write_paper_report(
    path: Path,
    ledger: Ledger,
    status: dict[str, Any],
    equity: Sequence[EquityPoint],
    paper_closed: Sequence[Trade],
    df1h: pd.DataFrame,
    top_count: dict[str, Any] | None,
    now_ts: int,
) -> Path:
    """Write report.html atomically (temp file + os.replace). `equity` / `paper_closed` come from
    this run's recompute; the window starts right after the holdout's last bar."""
    path = Path(path)
    boundary = int(ledger.holdout["last_bar_ts"])
    start = boundary + H1
    cur = current_epoch(ledger)
    last_run = ledger.runs[-1] if ledger.runs else None
    notes = status_lines(status) + [
        f"생성 {_utc(now_ts)} · 마지막 봉 {_utc(last_run['last_bar_ts']) if last_run else '-'}",
        f"현재 구간 {cur['id']} (지문 {cur['fingerprint'][:12]}, 시작 {_utc(cur['started_ts'])}) · 기준 지문 "
        f"{ledger.baseline['fingerprint'][:12]} @ {ledger.baseline['commit'][:7]}",
        "홀드아웃 데이터 해시: "
        + ("일치" if last_run is None or last_run.get("holdout_data_ok", True) else "불일치 (재현성 손상 기록)"),
        position_line(ledger.open_position),
        _top_note(top_count),
        f"표본 외 구간: {_utc(start)} 이후 (홀드아웃 마지막 봉 {_utc(boundary)})",
    ]
    window_eq = [p for p in equity if p.ts > boundary]
    sections: dict[str, Any] = {"notes": notes}
    if len(window_eq) >= 2:
        sections["equity"] = {"A paper": window_eq}
        sections["comparison"] = {"A paper": compute_metrics(equity, paper_closed, start, None)}
    trades: dict[str, list[Trade]] = {}
    for ep in reversed(ledger.epochs):
        rows = [trade_from_record(t) for t in ledger.trades if t["epoch_id"] == ep["id"]]
        label = "current" if ep["id"] == cur["id"] else "frozen"
        trades[f"epoch {ep['id']} ({label})"] = rows
    sections["trades"] = trades
    lo = boundary - CANDLE_LOOKBACK_S
    d4 = aggregate_4h(df1h[df1h["ts"] >= lo])
    sections["candles4h"] = {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}
    crit = status.get("criteria_1_2")
    if crit:
        rows = [{"strategy": n, **v} for n, v in crit["headline"].items()]
        rows.append({"strategy": f"RANDOM x{crit['random']['runs']} mean", "sharpe": crit["random"]["sharpe_mean"],
                     "calmar": crit["random"]["calmar_mean"]})
        sections["criteria 1-2 window " + f"{_utc(crit['window'][0])} .. {_utc(crit['window'][1])}"] = rows
    sections["discrepancies"] = [
        {"id": d["id"], "type": d["type"], "cause": d["cause"], "epoch": d["epoch_id"],
         "detected": _utc(d["detected_ts"]), "run": d["detected_run"]}
        for d in ledger.discrepancies
    ] or "none"
    sections["epochs"] = [
        {"id": e["id"], "fingerprint": e["fingerprint"][:12], "started": _utc(e["started_ts"]),
         "through": _utc(e["through_ts"]), "reason": e["reason"]}
        for e in ledger.epochs
    ]
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write_report(tmp, f"ewbot paper - {_utc(now_ts)}", sections)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
