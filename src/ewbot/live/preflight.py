"""Startup checks (Plan 3 spec 3.4, 4.5 `시작 시 점검`, 7 `안전 검사`). Any failure refuses to run.

  fingerprint  the 25 strategy-fingerprinted files hash to the frozen baseline e0435223... (the value is
               pinned here: the image has no git history). An empty file set (a non-editable install,
               where src/ewbot is not below the project root) fails as well.
  lock         holdout.lock is the committed one: the sha256 of the FILE's bytes equals LOCK_FILE_SHA256
               (6530175541c1...; the file holds more than the digest's inputs, e.g. the recorded runs and
               the data fingerprint, and any edit changes it), its sha256 field is d1be04baa06c..., its
               params are a grid combo, its holdout start is HOLDOUT_START and its sha256 equals
               lock_digest(BotConfig(), params, random_runs, code_sha256): the configuration the runtime
               uses is the one the holdout ran.
  key          GET /key_permissions: can_view, can_trade for CANARY/FULL, and never can_transfer.
  product      the futures product is BIP: contract_size 0.01, price_increment 5, base_increment 1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, lock_digest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import BotConfig
from ewbot.live.exchange import Exchange, ExchangeError, KeyPermissions, ProductSpec
from ewbot.paper.fingerprint import fingerprint_of, tree_files

BASELINE_FINGERPRINT = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
LOCK_SHA256 = "d1be04baa06c58dd483c88e053841d6eda56c1a560fd3196746bab311ece3c45"
LOCK_FILE_SHA256 = "6530175541c177509dab874cb944273996fa4b5e8a9207d0c19e6222224d68bb"  # sha256 of holdout.lock's bytes
FINGERPRINT_FILES = 25
CONTRACT_SIZE = 0.01
PRICE_INCREMENT = 5.0
BASE_INCREMENT = 1.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


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


def check_lock(path: Path = DEFAULT_LOCK_PATH, expected: str = LOCK_SHA256,
               expected_file: str = LOCK_FILE_SHA256) -> tuple[Check, dict[str, Any] | None]:
    try:
        raw = Path(path).read_bytes()
        lock = json.loads(raw.decode("utf-8"))
        params = tuple(float(x) for x in lock["params"])
        random_runs = int(lock["random_runs"])
        code_sha = str(lock["code_sha256"])
        holdout_start = int(lock["holdout_start"])
    except (OSError, ValueError, KeyError, TypeError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return Check("lock", False, f"{path} unreadable: {type(exc).__name__}"), None
    problems = []
    file_sha = hashlib.sha256(raw).hexdigest()
    if file_sha != expected_file:
        problems.append(f"file sha256 {file_sha[:12]} != committed {expected_file[:12]}")
    if params not in grid_combos():
        problems.append(f"params {list(params)} not a grid combo")
    if holdout_start != HOLDOUT_START:
        problems.append(f"holdout_start {holdout_start}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != lock.get("sha256"):
        problems.append(f"lock_digest {digest[:12]} != lock sha256 {str(lock.get('sha256'))[:12]}")
    if lock.get("sha256") != expected:
        problems.append(f"lock sha256 {str(lock.get('sha256'))[:12]} != committed {expected[:12]}")
    detail = "; ".join(problems) or f"file sha256 {file_sha[:12]}, sha256 {expected[:12]}, params {list(params)}"
    return Check("lock", not problems, detail), (lock if not problems else None)


def check_key(perms: KeyPermissions, stage: str) -> Check:
    problems = []
    if perms.can_transfer:
        problems.append("the key can TRANSFER: create a view+trade key only")
    if not perms.can_view:
        problems.append("the key cannot view")
    if stage in ("CANARY", "FULL") and not perms.can_trade:
        problems.append(f"{stage} needs trade permission")
    detail = "; ".join(problems) or f"view={perms.can_view} trade={perms.can_trade} transfer=False"
    return Check("key", not problems, detail)


def check_product(spec: ProductSpec, expected_id: str) -> Check:
    problems = []
    if spec.product_id != expected_id:
        problems.append(f"product {spec.product_id} != {expected_id}")
    if spec.contract_size != CONTRACT_SIZE:
        problems.append(f"contract_size {spec.contract_size} != {CONTRACT_SIZE}")
    if spec.price_increment != PRICE_INCREMENT:
        problems.append(f"price_increment {spec.price_increment} != {PRICE_INCREMENT}")
    if spec.base_increment != BASE_INCREMENT:
        problems.append(f"base_increment {spec.base_increment} != {BASE_INCREMENT}")
    detail = "; ".join(problems) or f"{spec.product_id}: 0.01 BTC/contract, $5 tick, whole contracts"
    return Check("product", not problems, detail)


def run_preflight(exchange: Exchange | None, stage: str, lock_path: Path = DEFAULT_LOCK_PATH,
                  files: Mapping[str, bytes] | None = None, expected_lock: str = LOCK_SHA256,
                  expected_lock_file: str = LOCK_FILE_SHA256) -> tuple[list[Check], dict[str, Any] | None]:
    """All checks; `exchange=None` runs the offline ones only (fingerprint, lock)."""
    lock_check, lock = check_lock(lock_path, expected_lock, expected_lock_file)
    checks = [check_fingerprint(files), lock_check]
    if exchange is not None:
        try:
            checks.append(check_key(exchange.key_permissions(), stage))
        except ExchangeError as exc:
            checks.append(Check("key", False, f"key_permissions failed: {exc}"))
        try:
            checks.append(check_product(exchange.product(), exchange.futures_id))
        except ExchangeError as exc:
            checks.append(Check("product", False, f"product failed: {exc}"))
    return checks, lock


def require(checks: list[Check]) -> None:
    if any(not c.ok for c in checks):
        raise PreflightError(checks)
