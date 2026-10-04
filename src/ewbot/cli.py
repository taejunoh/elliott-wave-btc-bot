"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper | live.

Holdout isolation: every research command (backtest, compare, walkforward) cuts the candle
frame to ts < HOLDOUT_START (2024-01-01) right after loading it, before any engine run, benchmark or
report candle sees it, and an explicit --start at/after HOLDOUT_START or --end after it is refused
(exit 2). runner.run_backtest and runner.compare refuse the holdout on their own as well (defence in
depth). `holdout` is the one-shot evaluation. `paper run` (Plan 2) works on the period AFTER the
holdout: it reads the params from the committed holdout.lock only and has no strategy or config
options (ewbot.paper.runner). `live run | preflight | status | stage | backup` (Plan 3) runs and inspects
the live-trading runtime (ewbot.live); like `paper` it has no strategy or config options."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, NoReturn, Sequence

import pandas as pd

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, HoldoutError, run_holdout
from ewbot.backtest.metrics import compute_metrics, reliability_by_q
from ewbot.backtest.report import BENCHMARKS, to_jsonable, write_report
from ewbot.backtest.runner import breakdowns, compare, equity_pairs, run_backtest, utc_ts
from ewbot.backtest.walkforward import GRID, HOLDOUT_START, MIN_TRAIN_TRADES, WF_END, walk_forward
from ewbot.config import AGGRESSIVE, CONSERVATIVE, BotConfig, load_config, with_overrides
from ewbot.data.candles import aggregate_4h, check_integrity, fill_gaps, load_1h
from ewbot.data.coinbase_history import update_cache
from ewbot.live import app as live_app
from ewbot.live import runtime as live_runtime
from ewbot.live.exchange import Exchange, ExchangeError
from ewbot.live.journal import Journal
from ewbot.live.preflight import LOCK_FILE_SHA256, LOCK_SHA256, run_preflight
from ewbot.live.settings import LiveSettings, SettingsError, load_settings
from ewbot.live.stage import (
    STAGES,
    Gate,
    StageError,
    canary_entry_gates,
    canary_gates,
    full_entry_gates,
    gate_lines,
    restart_stint,
    shadow_gates,
    transition,
)
from ewbot.paper.criteria import golive_status, status_lines
from ewbot.paper.ledger import LedgerError, current_epoch, load_ledger
from ewbot.paper.notify import deliver, failure_text, make_event, position_line, sender_from_env
from ewbot.paper.runner import PaperEnv, PaperRunError, run_paper
from ewbot.types import H1

DEFAULT_CONFIG = Path("config/default.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ewbot", description="Elliott Wave BTC bot - research tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="YAML config (default: config/default.yaml if present)")
    common.add_argument("--profile", choices=("conservative", "aggressive"), default=None, help="risk profile override")
    common.add_argument("--mode", choices=("A", "C", "HYBRID"), default=None, help="strategy for `backtest` (compare runs all three)")
    common.add_argument("--start", default=None, help="UTC date YYYY-MM-DD (default: first bar)")
    common.add_argument(
        "--end", default=None, help=f"UTC date YYYY-MM-DD, exclusive, at most {WF_END} (default: after the last bar before it)"
    )
    common.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parallel processes")
    common.add_argument("--out", type=Path, default=None, help="output HTML path (default: reports/<command>.html)")
    common.add_argument("--product", default="BTC-USD")
    common.add_argument("--cache-dir", type=Path, default=Path("data/cache"))

    sub.add_parser("fetch", parents=[common], help="download/update the Coinbase 1h candle cache")
    sub.add_parser("backtest", parents=[common], help="one backtest of --mode over --start/--end")
    p_cmp = sub.add_parser("compare", parents=[common], help="A, C, HYBRID vs buy&hold, EMA 50/200 and random entries")
    p_cmp.add_argument("--random-runs", type=int, default=100)
    sub.add_parser("walkforward", parents=[common], help="60-combo walk-forward on 2017-01..2023-12")
    p_ho = sub.add_parser(
        "holdout", parents=[common], help="ONE-SHOT evaluation on 2024-01-01..end (lock: <project root>/holdout.lock)"
    )
    p_ho.add_argument("--random-runs", type=int, default=100)
    p_ho.add_argument("--params", default=None, help="K4,Q_MIN,S_MIN e.g. 3.0,0.6,0.25")
    p_ho.add_argument("--params-from", type=Path, default=None, help="reports/walkforward.json (uses chosen_final)")
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")

    p_paper = sub.add_parser("paper", help="paper-trading runtime (Plan 2): hourly recompute, ledger, Telegram")
    paper_sub = p_paper.add_subparsers(dest="paper_cmd", required=True)
    p_run = paper_sub.add_parser("run", help="one paper run; params come from holdout.lock only")
    p_run.add_argument("--ledger-dir", type=Path, required=True, help="paper branch worktree (ledger.json, report.html, runs.log)")
    p_run.add_argument("--cache-dir", type=Path, default=Path("data/cache"), help="candle cache directory")
    p_status = paper_sub.add_parser("status", help="print the ledger's go-live status (read-only, no network)")
    p_status.add_argument("--ledger-dir", type=Path, required=True)

    p_live = sub.add_parser("live", help="live-trading runtime (Plan 3): Coinbase CFM BIP, SHADOW -> CANARY -> FULL")
    live_sub = p_live.add_subparsers(dest="live_cmd", required=True)
    live_sub.add_parser("run", help="run the live runtime (settings and secrets from the environment)")
    p_pre = live_sub.add_parser("preflight", help="startup checks: fingerprint, holdout.lock, alerts, key permissions, "
                                "product")
    p_pre.add_argument("--offline", action="store_true", help="fingerprint, lock and alerts only (no Coinbase request)")
    p_ls = live_sub.add_parser("status", help="journal status and the current stage's gates (read-only)")
    p_ls.add_argument("--json", action="store_true", help="machine-readable (deploy/live/deploy.sh)")
    p_ls.add_argument("--max-age", type=float, default=None, help="exit 1 when the loop heartbeat is older (seconds)")
    p_ls.add_argument("--exchange", action="store_true", help="also read the Coinbase position and open orders")
    p_ls.add_argument("--require-flat", action="store_true",
                      help="exit 3 unless no trade is open and (with --exchange) the account is flat (deploy.sh)")
    p_stage = live_sub.add_parser("stage", help="change the operating stage (promotions need every gate met)")
    p_stage.add_argument("to", choices=("shadow", "canary", "full"))
    p_stage.add_argument("--confirm", action="store_true", help="required: the user's explicit approval")
    p_stage.add_argument("--restart", action="store_true",
                         help="shadow or canary, the current stage: start a new stint of it (flat book)")
    p_bk = live_sub.add_parser("backup", help="consistent copy of the journal (SQLite online backup)")
    p_bk.add_argument("--out", type=Path, required=True)
    return parser


# ----------------------------------------------------------------------------- helpers


def _load_cfg(args: argparse.Namespace) -> BotConfig:
    path = args.config if args.config is not None else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    cfg = load_config(path)
    if args.profile:
        cfg = dataclasses.replace(cfg, risk=AGGRESSIVE if args.profile == "aggressive" else CONSERVATIVE)
    if args.mode:
        cfg = with_overrides(cfg, **{"strategy.mode": args.mode})
    return cfg


def _refuse(msg: str) -> NoReturn:
    print(f"refusing: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _load_df(args: argparse.Namespace, *, holdout: bool = False) -> pd.DataFrame:
    """The gap-filled 1h frame. Unless holdout=True (only `ewbot holdout`), every bar at/after
    HOLDOUT_START is dropped HERE, before anything else can see it."""
    path = Path(args.cache_dir) / f"{args.product}_1h.parquet"
    if not path.exists():
        raise SystemExit(f"candle cache not found: {path} - run `ewbot fetch` first")
    raw = load_1h(path)
    note = ""
    if not holdout:
        n_all = len(raw)
        raw = raw[raw["ts"] < HOLDOUT_START].reset_index(drop=True)
        if raw.empty:
            raise SystemExit(f"no bars before {WF_END} in {path}")
        note = f", {n_all - len(raw)} bars at/after {WF_END} excluded (holdout)"
    rep = check_integrity(raw)
    print(
        f"[data] {rep.n} bars, {len(rep.missing_ts)} missing, {len(rep.duplicate_ts)} duplicate{note}",
        file=sys.stderr,
    )
    return fill_gaps(raw)


def _requested_window(args: argparse.Namespace) -> tuple[int | None, int | None]:
    """Explicit --start/--end as epoch seconds (None when not given). Research commands never reach
    into the holdout: --start at/after HOLDOUT_START or --end after it is refused with exit code 2."""
    start = utc_ts(args.start) if args.start else None
    end = utc_ts(args.end) if args.end else None
    if (start is not None and start >= HOLDOUT_START) or (end is not None and end > HOLDOUT_START):
        _refuse(
            f"--start {args.start} / --end {args.end} reach into the holdout ({WF_END} onwards). Research "
            f"windows must end by {WF_END}; the holdout is evaluated once, with `ewbot holdout`."
        )
    return start, end


def _range(args: argparse.Namespace, df: pd.DataFrame) -> tuple[int, int]:
    """[start, end): explicit --start/--end (checked by _requested_window), else the first bar and
    one hour after the last bar of the (already cut) frame."""
    req_start, req_end = _requested_window(args)
    start = req_start if req_start is not None else int(df["ts"].iloc[0])
    end = req_end if req_end is not None else int(df["ts"].iloc[-1]) + H1
    if end <= start:
        raise SystemExit("--end must be after --start")
    return start, end


def _out(args: argparse.Namespace) -> Path:
    return args.out if args.out is not None else Path("reports") / f"{args.cmd}.html"


def _candles4h(df: pd.DataFrame) -> dict[str, list]:
    d4 = aggregate_4h(df)
    return {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}


def _print_comparison(comp: dict[str, dict[str, Any]]) -> None:
    """Console table; BH / EMA show "-" for win_rate and avg_r (benchmark R is not comparable)."""
    cols = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")
    print(f"{'':8}" + "".join(f"{c:>13}" for c in cols))
    for name, m in comp.items():
        cells = []
        for c in cols:
            if c == "n_trades":
                cells.append(f"{m[c]:>13d}")
            elif name in BENCHMARKS and c in ("win_rate", "avg_r"):
                cells.append(f"{'-':>13}")
            else:
                cells.append(f"{m[c]:>13.3f}")
        print(f"{name:8}" + "".join(cells))


def _compare_sections(cmp: dict[str, Any], df: pd.DataFrame, notes: list[str]) -> dict[str, Any]:
    bd = cmp["breakdown"]
    elliott_trades = {k: v for k, v in cmp["trades"].items() if k in bd}
    return {
        "notes": notes,
        "equity": cmp["equity"],
        "comparison": cmp["comparison"],
        "random": cmp["random"],
        "ci": cmp["ci"],
        "per_setup": {k: v["per_setup"] for k, v in bd.items()},
        "long_short": {k: v["long_short"] for k, v in bd.items()},
        "per_year": {k: v["per_year"] for k, v in bd.items()},
        "reliability": reliability_by_q(cmp["trades"].get("A", [])),
        "trades": elliott_trades,
        "candles4h": _candles4h(df),
    }


def _notes(cfg: BotConfig, start: int, end: int) -> list[str]:
    c = cfg.costs
    return [
        f"window {time.strftime('%Y-%m-%d', time.gmtime(start))} .. {time.strftime('%Y-%m-%d', time.gmtime(end))} (UTC, end exclusive)",
        f"risk profile {cfg.risk.name}; fee {c.fee_rate:.4%}, slippage {c.slippage:.4%}, funding {c.funding_apr_long:.0%} APR (long pays)",
        f"K4={cfg.wave.K4} Q_MIN={cfg.strategy.Q_MIN} S_MIN={cfg.strategy.S_MIN}",
    ]


# ----------------------------------------------------------------------------- commands


def cmd_fetch(args: argparse.Namespace) -> int:
    path = update_cache(args.product, Path(args.cache_dir))
    df = load_1h(path)
    rep = check_integrity(df)
    print(f"cache: {path}\nbars: {rep.n}  first_ts: {int(df['ts'].iloc[0])}  last_ts: {int(df['ts'].iloc[-1])}")
    print(f"missing: {len(rep.missing_ts)}  duplicate: {len(rep.duplicate_ts)}  nonpositive: {len(rep.nonpositive_ts)}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    _requested_window(args)  # refuse holdout dates before touching any data
    cfg, df = _load_cfg(args), _load_df(args)
    start, end = _range(args, df)
    t0 = time.perf_counter()
    res = run_backtest(cfg, df, start, end)
    dt = time.perf_counter() - t0
    name = cfg.strategy.mode
    m = compute_metrics(res.equity, res.trades, start, end)
    bd = breakdowns(res, start, end)
    _print_comparison({name: dataclasses.asdict(m)})
    print(f"backtest {name}: {len(res.trades)} trades in {dt:.1f}s")
    sections = {
        "notes": _notes(cfg, start, end) + [f"runtime {dt:.1f}s"],
        "equity": {name: equity_pairs(res.equity)},
        "comparison": {name: m},
        "per_setup": {name: bd["per_setup"]},
        "long_short": {name: bd["long_short"]},
        "per_year": {name: bd["per_year"]},
        "reliability": reliability_by_q(res.trades),
        "trades": {name: list(res.trades)},
        "candles4h": _candles4h(df),
    }
    print(f"report: {write_report(_out(args), f'Backtest {name}', sections)}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    _requested_window(args)  # refuse holdout dates before touching any data
    cfg, df = _load_cfg(args), _load_df(args)
    start, end = _range(args, df)
    t0 = time.perf_counter()
    cmp = compare(cfg, df, start, end, random_runs=args.random_runs, workers=args.workers)
    dt = time.perf_counter() - t0
    _print_comparison(cmp["comparison"])
    print(f"random control: {cmp['random']['runs']} runs, {cmp['random']['n_signals']} entries each; compare took {dt:.1f}s")
    sections = _compare_sections(cmp, df, _notes(cfg, start, end) + [f"runtime {dt:.1f}s"])
    print(f"report: {write_report(_out(args), 'Strategy comparison', sections)}")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    cfg, df = _load_cfg(args), _load_df(args)
    t0 = time.perf_counter()
    wf = walk_forward(cfg, df, args.workers)
    dt = time.perf_counter() - t0
    out = _out(args)
    rows = []
    for f in wf["folds"]:
        k4, q, s = f["chosen"] if f["chosen"] else (None, None, None)
        oos = f["oos"] or {}
        rows.append(
            {
                "test_start": time.strftime("%Y-%m-%d", time.gmtime(f["test"][0])),
                "K4": k4, "Q_MIN": q, "S_MIN": s,
                "train_calmar": None if f["train_calmar"] is None else round(f["train_calmar"], 2),
                "oos_return": oos.get("total_return"), "oos_max_dd": oos.get("max_dd"), "oos_trades": oos.get("n_trades"),
            }
        )
    jpath = out.with_suffix(".json")
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json.dumps(to_jsonable({k: v for k, v in wf.items() if k != "oos_trades"}), indent=1), encoding="utf-8")
    print(f"walk-forward: {len(wf['folds'])} folds in {dt:.0f}s; chosen_final = {wf['chosen_final']}; json: {jpath}")
    warn = []
    if wf["chosen_final_fallback"]:  # the last fold had no selectable combo: do not pass this on silently
        warn = [
            f"WARNING: chosen_final {wf['chosen_final']} comes from fold {wf['chosen_final_from_fold']}, not the last "
            "fold (no selectable combo there); `holdout --params-from` refuses it"
        ]
        print(warn[0], file=sys.stderr)
    _print_comparison({"WF-OOS": wf["stitched_metrics"]})
    sections = {
        "notes": _notes(cfg, 0, 0)[1:]
        + [
            f"runtime {dt:.0f}s",
            f"grid {dict(GRID)}",
            f"circuit breakers disabled in grid runs; a combo needs >= {MIN_TRAIN_TRADES} trades per training window",
        ]
        + warn,
        "equity": {"WF-OOS (stitched)": wf["stitched_equity"]},
        "comparison": {"WF-OOS": wf["stitched_metrics"]},
        "trades": {"WF-OOS": wf["oos_trades"]},
        "candles4h": _candles4h(df),
        "folds": rows,
        "combo_table": wf["combo_table"],
    }
    print(f"report: {write_report(out, 'Walk-forward (OOS stitched)', sections)}")
    return 0


def _parse_params(args: argparse.Namespace) -> tuple[tuple[float, ...], str]:
    """(K4, Q_MIN, S_MIN) and their provenance: "explicit" or "walkforward:<path>#chosen_final"."""
    if args.params:
        vals = tuple(float(x) for x in args.params.split(","))
        source = "explicit"
    elif args.params_from:
        wf = json.loads(Path(args.params_from).read_text(encoding="utf-8"))
        chosen = wf.get("chosen_final")
        if not chosen:
            raise SystemExit(f"{args.params_from} has no chosen_final")
        if wf.get("chosen_final_fallback"):
            raise SystemExit(
                f"{args.params_from}: chosen_final {chosen} comes from fold {wf.get('chosen_final_from_fold')}, not the "
                "last fold (no selectable combo there); pass --params explicitly if you accept it"
            )
        vals = tuple(float(x) for x in chosen)
        source = f"walkforward:{args.params_from}#chosen_final"
    else:
        raise SystemExit("holdout needs --params K4,Q_MIN,S_MIN or --params-from reports/walkforward.json")
    if len(vals) != len(GRID):
        raise SystemExit(f"expected {len(GRID)} params for {list(GRID)}, got {len(vals)}")
    return vals, source


def cmd_holdout(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the holdout is single-use. Re-run with --confirm once you are done tuning.", file=sys.stderr)
        return 2
    params, source = _parse_params(args)
    cfg, df = _load_cfg(args), _load_df(args, holdout=True)
    try:  # the lock is always DEFAULT_LOCK_PATH (<project root>/holdout.lock): not a CLI choice
        out = run_holdout(
            cfg, df, params, confirm=True, random_runs=args.random_runs, workers=args.workers, params_source=source
        )
    except HoldoutError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    cmp = out["compare"]
    _print_comparison(cmp["comparison"])
    print(json.dumps(to_jsonable(out["criteria"]), indent=1))
    for line in out["criteria"]["trade_count"]:  # spec 9.6 criterion 3: out-of-sample trades N/50
        print(line)
    start, end = out["window"]
    notes = _notes(with_overrides(cfg, **dict(zip(GRID, params))), start, end) + [f"lock {out['lock_path']} sha256 {out['sha256'][:12]}"]
    sections = _compare_sections(cmp, df, out["criteria"]["trade_count"] + notes)
    sections["criteria"] = out["criteria"]
    print(f"report: {write_report(_out(args), 'Holdout 2024-01-01..end (one-shot)', sections)}")
    lock = out["lock_path"]
    print(
        f"\nholdout lock written: {lock} (sha256 {out['sha256'][:12]}).\n"
        "Commit it NOW - it is the permanent record that the holdout has been used (not done automatically):\n"
        f'  git add "{lock}"\n'
        f'  git commit -m "chore(holdout): record holdout run {out["sha256"][:12]}"'
    )
    return 0


RUN_URL_ENV = "EWBOT_RUN_URL"  # set by .github/workflows/paper.yml: link to the Actions run log
FAILURE_MARKER_ENV = "EWBOT_FAILURE_MARKER"  # file the workflow checks before its own fallback alert


def _paper_env(args: argparse.Namespace) -> PaperEnv:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    return PaperEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _alert_failure(env: PaperEnv, stage: str, message: str) -> None:
    """Failure alert with the stage and the run-log link; on success touch the marker file so the
    workflow's fallback step does not send a second one."""
    if env.sender is None:
        return
    event = make_event("FAILURE", env.clock(), stage, failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([event], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _paper_run(args: argparse.Namespace) -> int:
    env = _paper_env(args)
    try:
        out = run_paper(env)
    except PaperRunError as exc:
        print(f"paper run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    count = out.status["count"]
    print(
        f"paper run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
        f"epoch {out.epoch_id}, out-of-sample {count['total']}/{count['required']}, {out.new_decisions} new "
        f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending"
    )
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _paper_status(args: argparse.Namespace) -> int:
    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    st = golive_status(led, int(time.time()))
    for line in status_lines(st):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(
        f"epoch {cur['id']} of {len(led.epochs)}: fingerprint {cur['fingerprint'][:12]} "
        f"(baseline {led.baseline['fingerprint'][:12]} @ {led.baseline['commit'][:7]})"
    )
    if led.runs:
        r = led.runs[-1]
        print(
            f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
            f"data {r['data_sha256'][:12]}, holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH'}, "
            f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['A']})"
        )
    unexplained = sum(1 for d in led.discrepancies if d["cause"] == "UNEXPLAINED")
    environment = sum(1 for d in led.discrepancies if d["cause"] == "ENVIRONMENT_CHANGE")
    print(
        f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}, "
        f"environment change {environment}), pending events {len(led.pending_events)}"
    )
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    return _paper_status(args) if args.paper_cmd == "status" else _paper_run(args)


LIVE_REFUSAL_WAIT_S = 600.0  # a refused start waits before exiting: Docker restarts it, alerts stay sparse


def _live_settings() -> LiveSettings:
    """Production settings from the environment and the secrets files (tests replace this function)."""
    return load_settings()


def _live_lock() -> tuple[Path, str, str]:
    """The committed holdout lock, its pinned sha256 field and the pinned sha256 of the file's bytes (tests replace
    this function)."""
    return DEFAULT_LOCK_PATH, LOCK_SHA256, LOCK_FILE_SHA256


def _print_checks(checks: list) -> bool:
    for c in checks:
        print(f"preflight {c.name}: {'ok' if c.ok else 'FAILED'} - {c.detail}")
    return all(c.ok for c in checks)


def _live_run(s: LiveSettings) -> int:
    journal = Journal(s.journal_path)
    stage, _ = journal.stage()
    notify = live_app.notifier(s)
    try:
        real = live_app.real_exchange(s)
    except SettingsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        notify("PREFLIGHT", f"[시작 거부] {exc}")
        time.sleep(LIVE_REFUSAL_WAIT_S)
        return 2
    path, sha, file_sha = _live_lock()
    checks, lock = run_preflight(real, stage, path, expected_lock=sha, expected_lock_file=file_sha, settings=s)
    if not _print_checks(checks) or lock is None:
        notify("PREFLIGHT", "[시작 거부] 시작 점검 실패: " + "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok))
        time.sleep(LIVE_REFUSAL_WAIT_S)
        return 2
    wired = live_app.build(s, lock, real, journal, notify)
    try:
        return wired.runtime.run_forever()
    except live_runtime.StartupRefused as exc:  # Coinbase unreadable for minutes at start: refused, no /fail
        print(f"refusing: {exc}", file=sys.stderr)
        notify("PREFLIGHT", f"[시작 거부] {exc}")
        time.sleep(LIVE_REFUSAL_WAIT_S)
        return 2
    except BaseException as exc:
        wired.heartbeat.fail(f"[LIVE] 중단: {type(exc).__name__}")  # a third party: the type name only, never the text
        notify("FATAL", f"[중단] {type(exc).__name__}: {live_app.redact(str(exc), s)[:300]}")
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt, SystemExit
        print(live_app.fatal_report(exc, s), file=sys.stderr, end="")  # re-raised, Python would print it unredacted
        return 1
    finally:
        wired.close()  # stops the Telegram poller and the feed, lets the queued messages go out


def _live_preflight(s: LiveSettings, args: argparse.Namespace) -> int:
    stage = "SHADOW"
    if s.journal_path.exists():
        journal = Journal(s.journal_path)
        stage = journal.stage()[0]
        journal.close()
    real = None
    if not args.offline:
        try:
            real = live_app.real_exchange(s)
        except SettingsError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
    path, sha, file_sha = _live_lock()
    checks, _ = run_preflight(real, stage, path, expected_lock=sha, expected_lock_file=file_sha, settings=s)
    return 0 if _print_checks(checks) else 2


def _live_gates(journal: Journal, stage: str, now: float) -> list[Gate]:
    audit = live_runtime.audit_from_journal(journal)  # the runtime's last audit, a failed one included
    if stage == "SHADOW":
        return shadow_gates(journal, now, audit)
    if stage == "CANARY":
        return canary_gates(journal, now, audit)
    return []


def _live_status(s: LiveSettings, args: argparse.Namespace) -> int:
    """Journal status (no journal yet: --json still answers, stage null). Exit 1 when --max-age is exceeded
    (Docker HEALTHCHECK), 3 when --require-flat finds an open trade, position or resting order."""
    if not s.journal_path.exists() and not args.json:
        print(f"no journal at {s.journal_path}", file=sys.stderr)
        return 1
    journal = Journal(s.journal_path) if s.journal_path.exists() else None
    try:
        now = time.time()
        stage, since = journal.stage() if journal is not None else (None, None)
        hb = journal.get("heartbeat") if journal is not None else None
        age = None if hb is None else now - float(hb)
        trade = journal.get("trade") if journal is not None else None
        out: dict[str, Any] = {"stage": stage, "since": since, "heartbeat_age": age,
                               "paused": journal.get("paused") if journal is not None else None,
                               "trade": trade if trade and trade.get("status") == "OPEN" else None}
        if args.exchange:
            real = live_app.real_exchange(s)
            pos = real.position()
            out["exchange_position"] = None if pos is None else {"side": pos.side, "contracts": pos.contracts}
            out["exchange_open_orders"] = len(real.open_orders())
        if args.json:
            print(json.dumps(out, sort_keys=True))
        elif journal is not None:
            assert stage is not None
            for line in live_runtime.status_lines(journal, now) + gate_lines(_live_gates(journal, stage, now)):
                print(line)
            if args.exchange:
                print(f"exchange position: {out['exchange_position']}, open orders: {out['exchange_open_orders']}")
        if args.require_flat and (out["trade"] is not None or out.get("exchange_position") is not None
                                  or out.get("exchange_open_orders", 0) > 0):
            print("not flat: an open trade, position or resting order (flatten first: /flatten)", file=sys.stderr)
            return 3
        return 1 if args.max_age is not None and (age is None or age > args.max_age) else 0
    finally:
        if journal is not None:
            journal.close()


def _flat_book(s: LiveSettings, journal: Journal) -> tuple[bool, Exchange]:
    """(no open trade in the journal and no position or resting order on Coinbase, the real exchange)."""
    trade = journal.get("trade")
    real = live_app.real_exchange(s)
    flat = not (trade and trade.get("status") == "OPEN") and real.position() is None and not real.open_orders()
    return flat, real


def _live_stage(s: LiveSettings, args: argparse.Namespace) -> int:
    """A promotion (one stage up, every gate met) and a restart of the current SHADOW or CANARY stint (--restart)
    need --confirm and a flat book (journal and Coinbase) before anything is written. A demotion needs only --confirm: it is written first - a running
    runtime sizes new entries for it at once - and then the command reports whether the book is flat (the runtime
    keeps protecting and managing an open position and restarts into the new stage once flat)."""
    journal = Journal(s.journal_path)
    try:
        now = time.time()
        to = args.to.upper()
        cur, _ = journal.stage()
        if not args.restart and STAGES.index(to) < STAGES.index(cur):
            try:
                transition(journal, to, now, [], confirm=args.confirm, flat=False)
            except StageError as exc:
                print(f"refusing: {exc}", file=sys.stderr)
                return 2
            print(f"stage {cur} -> {to} recorded: the running runtime sizes new entries for {to} at once")
            try:
                flat, _ = _flat_book(s, journal)
            except (SettingsError, ExchangeError) as exc:
                print(f"book: the account could not be read ({exc}) - check it with ewbot live status --exchange")
                return 0
            print("book flat: the runtime restarts into the new stage within a minute" if flat else
                  "book not flat: the runtime keeps protecting and managing the open position and restarts into the "
                  "new stage once flat")
            return 0
        try:  # the real account must be flat too
            flat, real = _flat_book(s, journal)
        except (SettingsError, ExchangeError) as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        if args.restart:
            try:
                restart_stint(journal, to, now, confirm=args.confirm, flat=flat)
            except StageError as exc:
                print(f"refusing: {exc}", file=sys.stderr)
                return 2
            print(f"{to} stint restarted: every {to} gate counts from now; the running runtime restarts into it "
                  "within a minute (a pause stays until /resume)")
            return 0
        try:  # the trade key is a CANARY / FULL gate
            perms = real.key_permissions()
        except ExchangeError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        gates: list[Gate] = []
        audit = live_runtime.audit_from_journal(journal)
        if (cur, to) == ("SHADOW", "CANARY"):
            gates = canary_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        elif (cur, to) == ("CANARY", "FULL"):
            gates = full_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        for line in gate_lines(gates):
            print(line)
        try:
            transition(journal, to, now, gates, confirm=args.confirm, flat=flat)
        except StageError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        print(f"stage {cur} -> {to} recorded; the running runtime restarts into it once flat")
        return 0
    finally:
        journal.close()


def _live_backup(s: LiveSettings, args: argparse.Namespace) -> int:
    journal = Journal(s.journal_path)
    try:
        print(f"journal backup: {journal.backup(args.out)}")
    finally:
        journal.close()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    try:
        s = _live_settings()
    except SettingsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        if args.live_cmd == "run":
            time.sleep(LIVE_REFUSAL_WAIT_S)  # no Telegram without settings: the wait keeps Docker's restarts sparse
        return 2
    if args.live_cmd == "run":
        return _live_run(s)
    handlers = {"preflight": _live_preflight, "status": _live_status, "stage": _live_stage, "backup": _live_backup}
    return handlers[args.live_cmd](s, args)


_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
    "paper": cmd_paper,
    "live": cmd_live,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
