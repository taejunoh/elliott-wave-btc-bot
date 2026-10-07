from __future__ import annotations

import pandas as pd
import pytest

from ewbot.paper import notify as a_notify
from ewbot.paper.decisions import verify_decisions
from ewbot.paper.ledger import Ledger, ensure_epoch, load_ledger, replace_epoch_trades, save_ledger
from ewbot.paperb.decisions import build_decisions_b, key_of, open_positions_b, split_result_b, trade_key_b
from ewbot.paperb.ledger import PAPER_B_REQUIRED, holdout_summary_b, new_ledger_b, paper_count_b
from ewbot.paperb.notify import (
    HEAD_B,
    b_head,
    close_text_b,
    daily_text_b,
    decision_events_b,
    entry_text_b,
    position_lines_b,
)
from ewbot.stratb.config import BConfig
from ewbot.stratb.engine import run_engine
from ewbot.stratb.signals import SignalB, build_frame

T0 = 1_700_006_400
M15 = 900
LOCK = {
    "sha256": "ab" * 32, "params": [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"], "holdout_start": 1_704_067_200,
    "replay_anchor": 1_696_118_400, "last_bar_ts": 1_717_027_200 - 900, "code_sha256": "cd" * 32, "git_head": "f00",
    "first_run": {"data": {"sha256": "ef" * 32}, "metrics": {"B": {"n_trades": 62}}, "criteria": {"passed": True}},
}


def _frame(rows):
    return build_frame(pd.DataFrame([(T0 + k * M15, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)],
                                    columns=["ts", "open", "high", "low", "close", "volume"]))


def _sig(f, d=1, scale=1.0):
    return SignalB(i=2, ts=int(f.ts[2]), direction=d, entry_ref=20_000.0 * scale, stop=(20_000.0 - d * 100.0) * scale,
                   tp1=(20_000.0 + d * 100.0) * scale, tp2=(20_000.0 + d * 300.0) * scale, reason="long div 1/2 break 3.00")


def _run(rows):
    f = _frame(rows)
    return f, run_engine({"BTC": f}, {"BTC": [_sig(f)]}, BConfig())


FLAT = [(20_000.0, 20_005.0, 19_995.0, 20_000.0)]
TP1_THEN_BE = FLAT * 4 + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_120.0, 19_850.0, 19_860.0)] + FLAT * 2


def test_the_ledger_is_strategy_a_schema_bound_to_the_b_lock(tmp_path):
    led = new_ledger_b(LOCK)
    assert led.baseline == {"fingerprint": "cd" * 32, "commit": "f00"}
    assert led.epochs == [{"id": 1, "fingerprint": "cd" * 32, "started_ts": LOCK["last_bar_ts"],
                           "reason": "baseline: holdout-b.lock", "through_ts": LOCK["last_bar_ts"]}]
    assert holdout_summary_b(LOCK)["n_trades"] == {"B": 62} and holdout_summary_b(LOCK)["passed"] is True
    save_ledger(tmp_path, led)
    assert isinstance(load_ledger(tmp_path), Ledger) and load_ledger(tmp_path).holdout == led.holdout


def test_the_paper_count_needs_30_and_resets_with_a_new_b_fingerprint():
    led = new_ledger_b(LOCK)
    _, res = _run(TP1_THEN_BE)
    trade = res.trades[0]
    replace_epoch_trades(led, 1, [trade])
    assert paper_count_b(led) == {"epoch_id": 1, "eligible": True, "paper": 1, "required": PAPER_B_REQUIRED}
    ensure_epoch(led, "99" * 32, LOCK["last_bar_ts"] + 3600)
    assert paper_count_b(led) == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}


def test_decisions_are_strategy_a_records_and_replay_cleanly():
    f, res = _run(TP1_THEN_BE)
    dec = build_decisions_b(res)
    assert [d["kind"] for d in dec] == ["SIGNAL", "ENTRY", "TP1", "BREAKEVEN", "STOP"]
    sig, entry, tp1, be, stop = dec
    assert (sig["ts"], sig["contracts"], sig["levels"], sig["trade_key"]) == (int(f.ts[2]), 41, [19_900.0, 20_100.0, 20_300.0], int(f.ts[2]))
    assert entry["id"] == f"ENTRY:{int(f.ts[3])}:{int(f.ts[3])}" and entry["reason"] == entry["setup"] == "B-BTC"
    assert (be["contracts"], be["levels"], be["price"]) == (21, [19_900.0], entry["price"])
    assert stop["reason"] == "STOP" and tp1["contracts"] == 20
    epoch = {"id": 1, "started_ts": 0, "through_ts": int(f.ts[-1])}
    rec = [{**d, "epoch_id": 1} for d in dec]
    assert verify_decisions(rec, dec, epoch, lambda d: False, lambda d: False) == []
    changed = [dict(d) for d in dec]
    changed[2]["price"] += 1.0
    (disc,) = verify_decisions(rec, changed, epoch, lambda d: False, lambda d: False)
    assert (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")


def test_positions_open_at_the_last_bar_are_split_off_and_described():
    f, res = _run(FLAT * 8)
    closed, still = split_result_b(res)
    assert closed == [] and [t.exit_reason for t in still] == ["END"]
    pos = open_positions_b(still, res, epoch_start=int(f.ts[0]))
    assert list(pos) == ["BTC"] and open_positions_b([], res, 0) is None
    assert (pos["BTC"]["remaining"], pos["BTC"]["stop"], pos["BTC"]["counted"], pos["BTC"]["tp1_done"]) == (41, 19_900.0, True, False)
    assert position_lines_b(pos)[0].startswith("포지션: B-BTC 롱 41/41계약") and position_lines_b(None) == ["포지션: 없음"]
    assert "ENTRY" in [d["kind"] for d in build_decisions_b(res)] and "EXIT" not in [d["kind"] for d in build_decisions_b(res)]


def test_two_assets_entering_on_the_same_bar_have_distinct_decisions_and_messages():
    f, g = _frame(FLAT * 8), _frame([tuple(x / 10 for x in FLAT[0])] * 8)
    res = run_engine({"BTC": f, "ETH": g}, {"BTC": [_sig(f)], "ETH": [_sig(g, d=-1, scale=0.1)]}, BConfig())
    t3 = int(f.ts[3])
    dec = build_decisions_b(res)
    assert [(d["id"], d["setup"], d["direction"]) for d in dec] == [
        (f"SIGNAL:{t3 - M15}:{t3 - M15}", "B-BTC", 1), (f"SIGNAL:{t3 - M15}:{t3 - M15 + 1}", "B-ETH", -1),
        (f"ENTRY:{t3}:{t3}", "B-BTC", 1), (f"ENTRY:{t3}:{t3 + 1}", "B-ETH", -1)]
    assert (trade_key_b("BTC", t3), trade_key_b("ETH", t3)) == (t3, t3 + 1)
    epoch = {"id": 1, "started_ts": 0, "through_ts": int(f.ts[-1])}
    assert verify_decisions([{**d, "epoch_id": 1} for d in dec], dec, epoch, lambda d: False, lambda d: False) == []
    closed, still = split_result_b(res)
    pos = open_positions_b(still, res, epoch_start=0)
    assert closed == [] and list(pos) == ["BTC", "ETH"]
    assert [x.split(" · ")[0] for x in position_lines_b(pos)] == ["포지션: B-BTC 롱 41/41계약", "포지션: B-ETH 숏 41/41계약"]
    events = decision_events_b(dec, {key_of(t): t for t in res.trades}, {key_of(t) for t in still}, 0, {"required": 30}, [])
    assert [e["id"] for e in events] == [f"ENTRY:{t3}:{t3}", f"ENTRY:{t3}:{t3 + 1}"]
    assert "B-BTC 롱" in events[0]["text"] and "B-ETH 숏" in events[1]["text"]


def test_messages_carry_the_paper_b_prefix():
    _, res = _run(TP1_THEN_BE)
    t = res.trades[0]
    assert entry_text_b(t).startswith(f"{HEAD_B} 신규 진입\nB-BTC 롱 · ") and "근거: long div 1/2 break 3.00" in entry_text_b(t)
    assert close_text_b(t, 3, 30, True).startswith(f"{HEAD_B} 청산 · STOP") and "3/30" in close_text_b(t, 3, 30, True)
    assert b_head(a_notify.failure_text("data", "x", None)) == f"{HEAD_B} 실패 · 단계 data\nx"
    assert daily_text_b("2024-06-01", ["a", "b"]) == f"{HEAD_B} 일일 요약 2024-06-01 (뉴욕)\na\nb"
    with pytest.raises(ValueError):
        b_head("[LIVE] x")


def test_decision_events_announce_entries_breakeven_moves_and_closes():
    f, res = _run(TP1_THEN_BE)
    dec = build_decisions_b(res)
    trades = {key_of(t): t for t in res.trades}
    events = decision_events_b(dec, trades, set(), epoch_start=0, count={"required": 30}, cur_closed=list(res.trades))
    assert [e["kind"] for e in events] == ["ENTRY", "BREAKEVEN", "CLOSE"]
    assert all(e["text"].startswith(HEAD_B) for e in events) and "1/30" in events[-1]["text"]
