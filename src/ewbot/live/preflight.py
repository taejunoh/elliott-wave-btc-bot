"""Startup checks (Plan 3 spec 3.4, 4.5 `시작 시 점검`, 7 `안전 검사`). Any failure refuses to run.

  fingerprint  the 25 strategy-fingerprinted files hash to the frozen baseline e0435223... (the value is
               pinned here: the image has no git history). An empty file set (a non-editable install,
               where src/ewbot is not below the project root) fails as well.
  lock         holdout.lock is the committed one: the sha256 of the FILE's bytes equals LOCK_FILE_SHA256
               (6530175541c1...; the file holds more than the digest's inputs, e.g. the recorded runs and
               the data fingerprint, and any edit changes it) - checked first, so an edited, re-encoded or
               re-serialised copy is refused as such -, then its sha256 field is d1be04baa06c..., its
               params are a grid combo, its holdout start is HOLDOUT_START and its sha256 equals
               lock_digest(BotConfig(), params, random_runs, code_sha256): the configuration the runtime
               uses is the one the holdout ran. A malformed lock is a failing check, never an exception.
  alerts       (with the settings) CANARY and FULL need TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and
               EWBOT_HEALTHCHECK_URL: alerts, /pause and /flatten, the heartbeat; SHADOW passes with a warning.
  key          GET /key_permissions: can_view, can_trade for CANARY/FULL, and never can_transfer; a stage
               that is not one of SHADOW, CANARY, FULL is refused.
  product      the exchange is set up for FUTURES_PRODUCT (BIP-20DEC30-CDE) and SPOT_PRODUCT (BTC-USD), both
               fixed, and the futures product is BIP: contract_size 0.01, price_increment 5, base_increment 1,
               intraday and overnight margin rates (long and short) that are finite numbers above 0 (a rate the
               reply did not give reads 0, which would turn the 70% entry limit and the 90% reduction off),
               with a contract expiry (contract_expiry) no earlier than PRODUCT_EXPIRY, the date in its id, and
               more than EXPIRY_MARGIN_DAYS = 90 days away (the earlier of the two). The venue reported
               2089-12-30 for it on 2026-10-03 (contract_expiry_type EXPIRING): neither the venue's date nor the
               type can be required to equal the id's; the runbook has the manual check."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, lock_digest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import BotConfig
from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT, Exchange, ExchangeError, KeyPermissions, ProductSpec
from ewbot.live.settings import LiveSettings
from ewbot.live.stage import STAGES
from ewbot.paper.fingerprint import fingerprint_of, tree_files

BASELINE_FINGERPRINT = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
LOCK_SHA256 = "d1be04baa06c58dd483c88e053841d6eda56c1a560fd3196746bab311ece3c45"
LOCK_FILE_SHA256 = "6530175541c177509dab874cb944273996fa4b5e8a9207d0c19e6222224d68bb"  # sha256 of holdout.lock's bytes
FINGERPRINT_FILES = 25
CONTRACT_SIZE = 0.01
PRICE_INCREMENT = 5.0
BASE_INCREMENT = 1.0
PRODUCT_EXPIRY = "2030-12-20"  # the date in FUTURES_PRODUCT (BIP-20DEC30-CDE)
EXPIRY_MARGIN_DAYS = 90
ALERT_SETTINGS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EWBOT_HEALTHCHECK_URL")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    retry: bool = False  # failed only because Coinbase could not be read just now (429, 5xx, timeout): an outage


class PreflightError(RuntimeError):
    def __init__(self, checks: list[Check]) -> None:
        bad = [c for c in checks if not c.ok]
        super().__init__("preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in bad))
        self.checks = checks


def check_fingerprint(files: Mapping[str, bytes] | None = None, expected: str = BASELINE_FINGERPRINT) -> Check:
    files = tree_files() if files is None else files
    fp = fingerprint_of(files)
    ok = len(files) == FINGERPRINT_FILES and fp == expected
    return Check("fingerprint", ok, f"{fp[:12]} over {len(files)} files (baseline {expected[:12]}, {FINGERPRINT_FILES} files)")


def _number(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise TypeError("not a finite number")
    return float(x)


def _integer(x: Any) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        raise TypeError("not an integer")
    return x


def _parse_lock(raw: bytes) -> tuple[dict[str, Any], tuple[float, ...], int, str, int, str]:
    lock = json.loads(raw.decode("utf-8"))
    if not isinstance(lock, dict) or not isinstance(lock["params"], list):
        raise TypeError("not a lock object")
    params = tuple(_number(x) for x in lock["params"])
    code_sha, sha = lock["code_sha256"], lock["sha256"]
    if not (isinstance(code_sha, str) and isinstance(sha, str)):
        raise TypeError("not a string")
    return lock, params, _integer(lock["random_runs"]), code_sha, _integer(lock["holdout_start"]), sha


def check_lock(path: Path = DEFAULT_LOCK_PATH, expected: str = LOCK_SHA256,
               expected_file: str = LOCK_FILE_SHA256) -> tuple[Check, dict[str, Any] | None]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return Check("lock", False, f"{path} unreadable: {type(exc).__name__}"), None
    file_sha = hashlib.sha256(raw).hexdigest()
    if file_sha != expected_file:  # first: any other file (edited, CRLF, a BOM, re-serialised) is refused as such
        return Check("lock", False, f"file sha256 {file_sha[:12]} != committed {expected_file[:12]}"), None
    try:
        lock, params, random_runs, code_sha, holdout_start, sha = _parse_lock(raw)
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:  # incl. JSONDecodeError
        return Check("lock", False, f"{path} malformed: {type(exc).__name__}"), None
    problems = []
    if params not in grid_combos():
        problems.append(f"params {list(params)} not a grid combo")
    if holdout_start != HOLDOUT_START:
        problems.append(f"holdout_start {holdout_start} != {HOLDOUT_START}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != sha:
        problems.append(f"lock_digest {digest[:12]} != lock sha256 {sha[:12]}")
    if sha != expected:
        problems.append(f"lock sha256 {sha[:12]} != committed {expected[:12]}")
    detail = "; ".join(problems) or f"file sha256 {file_sha[:12]}, sha256 {expected[:12]}, params {list(params)}"
    return Check("lock", not problems, detail), (lock if not problems else None)


def check_key(perms: KeyPermissions, stage: str) -> Check:
    problems = []
    if stage not in STAGES:  # never read as SHADOW (view only is enough) nor trusted as a real-money stage
        problems.append(f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
    if perms.can_transfer:
        problems.append("the key can TRANSFER: create a view+trade key only")
    if not perms.can_view:
        problems.append("the key cannot view")
    if stage != "SHADOW" and not perms.can_trade:
        problems.append(f"{stage} needs trade permission")
    detail = "; ".join(problems) or f"view={perms.can_view} trade={perms.can_trade} transfer=False"
    return Check("key", not problems, detail)


def check_alerts(settings: LiveSettings, stage: str) -> Check:
    """CANARY and FULL trade real money: they need the Telegram alerts and commands (/pause, /flatten) and the
    external heartbeat. SHADOW may run without them; the check then passes with a warning."""
    if stage not in STAGES:
        return Check("alerts", False, f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
    missing = [name for name, value in zip(ALERT_SETTINGS, (settings.telegram_token, settings.telegram_chat,
                                                              settings.healthcheck_url)) if not value]
    if not missing:
        return Check("alerts", True, "Telegram alerts and commands, heartbeat")
    if stage == "SHADOW":
        return Check("alerts", True, f"WARNING: {', '.join(missing)} not set - SHADOW runs without alerts, Telegram "
                                     "commands or heartbeat; CANARY and FULL refuse to start")
    return Check("alerts", False, f"{stage} needs {', '.join(missing)} in the secrets file (alerts, /pause and "
                                  "/flatten, the heartbeat)")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def check_product(spec: ProductSpec, expected_id: str = FUTURES_PRODUCT, now: float | None = None) -> Check:
    now = time.time() if now is None else now
    problems = []
    if spec.product_id != expected_id:
        problems.append(f"product {spec.product_id} != {expected_id}")
    if spec.contract_size != CONTRACT_SIZE:
        problems.append(f"contract_size {spec.contract_size} != {CONTRACT_SIZE}")
    if spec.price_increment != PRICE_INCREMENT:
        problems.append(f"price_increment {spec.price_increment} != {PRICE_INCREMENT}")
    if spec.base_increment != BASE_INCREMENT:
        problems.append(f"base_increment {spec.base_increment} != {BASE_INCREMENT}")
    for name, rates in (("intraday", spec.intraday_margin), ("overnight", spec.overnight_margin)):
        if not all(isinstance(r, (int, float)) and math.isfinite(r) and r > 0 for r in rates):
            problems.append(f"{name} margin rates {list(rates)} are not finite numbers above 0 (long, short)")
    named = datetime.fromisoformat(PRODUCT_EXPIRY).replace(tzinfo=UTC).timestamp()
    if spec.expiry is None:
        problems.append("no contract expiry in the product reply (contract_expiry)")
    elif spec.expiry < named:
        problems.append(f"contract expiry {_day(spec.expiry)} before {PRODUCT_EXPIRY} (the date in {FUTURES_PRODUCT})")
    ends = named if spec.expiry is None else min(spec.expiry, named)
    if ends - now < EXPIRY_MARGIN_DAYS * 86_400:
        problems.append(f"the contract expires {_day(ends)}: less than {EXPIRY_MARGIN_DAYS} days away "
                        "(a new contract needs a plan change)")
    detail = "; ".join(problems) or (f"{spec.product_id}: 0.01 BTC/contract, $5 tick, whole contracts, "
                                     f"expires {_day(ends)} at the earliest")
    return Check("product", not problems, detail)


def run_preflight(exchange: Exchange | None, stage: str, lock_path: Path = DEFAULT_LOCK_PATH,
                  files: Mapping[str, bytes] | None = None, expected_lock: str = LOCK_SHA256,
                  expected_lock_file: str = LOCK_FILE_SHA256, *, settings: LiveSettings | None,
                  now: float | None = None) -> tuple[list[Check], dict[str, Any] | None]:
    """All checks in this order: fingerprint, lock, alerts, key and product (only with an `exchange`; `exchange=None`
    runs the offline ones). `settings` must be given - every start passes its own, so the alerts check runs; None
    (unit checks of the other ones only) skips it. A key or product read that failed for an outage (a retryable error)
    is a failing check marked `retry`."""
    lock_check, lock = check_lock(lock_path, expected_lock, expected_lock_file)
    checks = [check_fingerprint(files), lock_check]
    if settings is not None:
        checks.append(check_alerts(settings, stage))
    if exchange is not None:
        try:
            checks.append(check_key(exchange.key_permissions(), stage))
        except ExchangeError as exc:
            checks.append(Check("key", False, f"key_permissions failed: {exc}", retry=exc.retryable))
        if (exchange.futures_id, exchange.spot_id) != (FUTURES_PRODUCT, SPOT_PRODUCT):
            checks.append(Check("product", False, f"the exchange is set up for {exchange.futures_id} and "
                                                  f"{exchange.spot_id}, not {FUTURES_PRODUCT} and {SPOT_PRODUCT}: "
                                                  "both are fixed"))
        else:
            try:
                checks.append(check_product(exchange.product(), FUTURES_PRODUCT, now))
            except ExchangeError as exc:
                checks.append(Check("product", False, f"product failed: {exc}", retry=exc.retryable))
    return checks, lock


def require(checks: list[Check]) -> None:
    if any(not c.ok for c in checks):
        raise PreflightError(checks)
