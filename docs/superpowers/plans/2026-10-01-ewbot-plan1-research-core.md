# ewbot Plan 1 — Research Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a backtest-only research core that recognises Elliott Wave structure on BTC 4h candles, trades four setups long/short with 1h breakout entries (E-bar neighbourhood, spec 5.2) under fixed-fractional risk, and answers — via walk-forward and a one-shot holdout against baselines — whether Elliott counting adds value.

**Architecture:** One event-driven engine (`TradingEngine`) processes closed 1h bars: PaperBroker fills → WaveEngine (ATR ZigZag → count enumeration → hard rules → Fibonacci scoring → hysteresis/S_rev) → ChochDetector → position management → circuit breakers → strategy signal → sizing → queued market entry at next open. Backtest runner, walk-forward grid, holdout lock and HTML report sit on top. Live/paper runtime (Plan 2) and Telegram/dashboard (Plan 3) are out of scope and will reuse this engine unchanged.

**Tech Stack:** Python 3.12 (uv), numpy, pandas, pyarrow, pyyaml, requests, plotly; pytest + Hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md`

## Global Constraints

- Python 3.12 via `uv venv --python 3.12`; run tests with `uv run pytest`. Runtime deps only: numpy, pandas, pyarrow, pyyaml, requests, plotly. Dev deps: pytest, hypothesis.
- Package `ewbot`, src layout `src/ewbot/`, tests in `tests/` mirroring modules.
- Timestamps are int epoch seconds UTC = bar OPEN time. Directions are ints: +1 long/bull/up, −1 short/bear/down.
- Decisions only on CLOSED bars. A pivot may be used at 4h index t only if `confirmed_at <= t` (no lookahead) — enforced by T10 property tests.
- Defaults (spec §11): K4=3.0, ATR 14, trigger bars N=3, M=12, σ=0.12, w0=1, T=0.05, h=0.05, top-k=5, Q_MIN=0.6, S_MIN=0.25, R gate 1.5, TP split 50/50, time stop 1.5× leg clipped [12, 60] 4h bars, setup ③ risk ×0.5, stop buffers 0.25/0.5 ATR4h, wick clip 6×ATR1h, warmup 200 4h bars.
- Risk profiles: conservative 1% / 3x (default), aggressive 3% / 10x. Breakers: daily −3% (reset at America/New_York midnight), total −15% from peak (latched), 5 consecutive losses → 24h pause, max 1 position. Breakers are on in every backtest and in the holdout; only the walk-forward grid runs switch them off (`breakers.enabled=False`, T21).
- Costs (backtest): fee 0.05%/side of notional (no $0.15 minimum in backtests), slippage 0.05%, funding 10% APR long pays / short receives (hourly), contract 0.01 BTC, integer contracts.
- Coinbase futures weekly close Fri 17:00–18:00 America/New_York: pending fills wait for reopen; no new entries decided or filled Fri 16:00–18:00 ET.
- Fill simulation: market entry at next 1h open ± slippage; stop gap-through fills at open; stop beats TP on same bar; TP requires strict cross.
- Walk-forward: 2017-01-01 → 2024-01-01 only; 60-combo grid (K4 × Q_MIN × S_MIN); a combo needs ≥ 12 trades in a 24-month training window; plateau selection; holdout [2024-01-01, end) runs ONCE behind a lock file. Go-live needs ≥ 50 out-of-sample trades with frozen parameters (holdout + later paper); the holdout report states the count.
- Tests never hit the network (except explicitly marked manual steps).

## Plan Structure

| Part | Tasks | Area |
|---|---|---|
| A | 1–4 | Scaffold, config, Coinbase history, candle utils, indicators |
| B1 | 5–10 | Wave engine + synthetic / no-lookahead / mirror property tests |
| B2 | 11–14 | CHoCH, strategy base, Elliott setups, Fib baseline, hybrid |
| C1 | 15–17 | Costs, PaperBroker, sizing, breakers, position manager, engine loop |
| C2 | 18–23 | Metrics, benchmarks, runner/compare, walk-forward/holdout, report/CLI, real-data E2E |

Execute tasks in numeric order; each task's **Interfaces** block lists exactly what it consumes from earlier tasks.

---

## Part A — Scaffold & Data

Tasks T1–T4. Everything below runs from the **repository root** (the directory that contains `docs/`). Shell blocks assume `uv` 0.12 and network access (only T1 `uv sync` and the one manual live check in T2 need it; no automated test touches the network).

Conventions fixed here for the whole plan:

- Package `ewbot`, src layout (`src/ewbot/`), tests in `tests/` mirroring the modules (`tests/data/test_candles.py` tests `src/ewbot/data/candles.py`).
- pytest runs with `--import-mode=importlib` and `pythonpath = ["."]`, so test files in different folders may share a basename and tests may `import` helpers from `tests.<module>`. `__init__.py` files in `tests/` are optional under this import mode: T5 adds empty `tests/__init__.py` and `tests/waves/__init__.py`, the other test folders have none, and the suite passes either way.
- `pytest` and `hypothesis` live in the `dev` dependency group (installed by plain `uv sync`).
- The `ewbot` console script is declared now but `ewbot.cli` is created in T22; do not create `cli.py` in Part A.
- DataFrame schema everywhere: columns `ts:int64, open, high, low, close, volume:float64`, ascending `ts`, unique `ts`, `RangeIndex` (`synthetic: bool` is added by `fill_gaps`).

---

### Task 1: Scaffold, config and shared types

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `config/default.yaml`
- Create: `src/ewbot/__init__.py`, `src/ewbot/types.py`, `src/ewbot/config.py`
- Test: `tests/test_types.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ewbot.types.Bar`, `H1`, `H4`; `ewbot.config.{WaveParams, StrategyParams, RiskProfile, CONSERVATIVE, AGGRESSIVE, BreakerParams, CostParams, BotConfig, SETUPS, load_config, with_overrides}` as in the contract, with three amendments from the trigger study (2017–2023 mechanics only):
  - `WaveParams` has no `K1`: the 1h trigger (T11) uses no 1h ZigZag.
  - `StrategyParams.choch_bars: int = 3` (spec 5.2: the trigger level is the high/low of the 3 1h bars before the E bar). It replaces `choch_fallback_bars`.
  - `BreakerParams.enabled: bool = True`. Only the walk-forward grid (T21) sets it to `False`; `CircuitBreakers` (T16) is then a no-op.

  Behaviour details the contract leaves open:
  - `load_config` / `with_overrides` raise `KeyError` for any unknown section, field or risk-profile name.
  - `risk` may be `"conservative"` / `"aggressive"` or a mapping (`{risk_pct, max_leverage[, name]}` merged over CONSERVATIVE, `name` defaults to `"custom"`). In `with_overrides`, `risk="aggressive"`, `risk=RiskProfile(...)` and `**{"risk.risk_pct": 0.02}` are all accepted.
  - YAML ints are coerced to float where the default is a float; YAML lists to tuples where the default is a tuple.

- [ ] **Step 1: Create the scaffold and write the failing tests**

Initialise git (skip if the repo already exists), create the venv and the project files:

```bash
git rev-parse --git-dir >/dev/null 2>&1 || git init
mkdir -p src/ewbot config tests/data
uv venv --python 3.12
```

`pyproject.toml`:
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ewbot"
version = "0.1.0"
description = "Elliott Wave BTC bot - research core"
requires-python = ">=3.12"
dependencies = [
    "numpy>=1.26",
    "pandas>=2.2",
    "pyarrow>=15",
    "pyyaml>=6",
    "requests>=2.31",
    "plotly>=5.20",
]

[project.scripts]
ewbot = "ewbot.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "hypothesis>=6.100",
]

[tool.hatch.build.targets.wheel]
packages = ["src/ewbot"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "--import-mode=importlib"
```

`.gitignore`:
```gitignore
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.hypothesis/
data/cache/
reports/
holdout.lock
```

`src/ewbot/__init__.py`:
```python
"""Elliott Wave BTC bot - research core."""

__version__ = "0.1.0"
```

Install (creates `uv.lock`, installs the project editable plus the dev group):

```bash
uv sync
```

Now the tests.

`tests/test_types.py`:
```python
import dataclasses

import pytest

from ewbot.types import H1, H4, Bar


def test_constants():
    assert H1 == 3600
    assert H4 == 14400
    assert H4 == 4 * H1


def test_bar_defaults_and_frozen():
    b = Bar(ts=1_700_000_000, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    assert b.synthetic is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.close = 3.0  # type: ignore[misc]
    assert not hasattr(b, "__dict__")  # slots=True
```

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from ewbot.config import (
    AGGRESSIVE,
    CONSERVATIVE,
    SETUPS,
    BotConfig,
    load_config,
    with_overrides,
)

DEFAULT_YAML = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def test_defaults():
    cfg = load_config(None)
    assert cfg == BotConfig()
    assert cfg.wave.K4 == 3.0 and cfg.wave.atr_len == 14 and not hasattr(cfg.wave, "K1")
    assert cfg.wave.M == 12 and cfg.wave.warmup_4h == 200 and cfg.wave.top_k == 5
    assert cfg.strategy.mode == "A"
    assert cfg.strategy.setups_enabled == SETUPS == ("W3", "W5", "W5_REV", "C_END")
    assert cfg.strategy.Q_MIN == 0.6 and cfg.strategy.S_MIN == 0.25 and cfg.strategy.choch_bars == 3
    assert cfg.risk == CONSERVATIVE
    assert cfg.risk.risk_pct == 0.01 and cfg.risk.max_leverage == 3.0
    assert AGGRESSIVE.risk_pct == 0.03 and AGGRESSIVE.max_leverage == 10.0
    assert cfg.costs.apply_min_fee is False and cfg.costs.fee_rate == 0.0005
    assert cfg.breakers.daily_loss == 0.03 and cfg.breakers.max_consec_losses == 5
    assert cfg.breakers.enabled is True
    assert cfg.initial_equity == 10_000.0 and cfg.seed == 0


def test_default_yaml_mirrors_dataclass_defaults():
    assert load_config(DEFAULT_YAML) == BotConfig()


def test_yaml_override(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "wave:\n  K4: 2.5\n"
        "strategy:\n  mode: HYBRID\n  Q_MIN: 0.55\n  setups_enabled: [W3, W5]\n"
        "risk: aggressive\n"
        "costs:\n  apply_min_fee: true\n  slippage: 1\n"
        "initial_equity: 5000\n"
        "seed: 42\n"
    )
    cfg = load_config(p)
    assert cfg.wave.K4 == 2.5 and cfg.wave.atr_len == 14  # untouched keys keep defaults
    assert cfg.strategy.mode == "HYBRID" and cfg.strategy.Q_MIN == 0.55
    assert cfg.strategy.setups_enabled == ("W3", "W5")  # list -> tuple
    assert cfg.risk == AGGRESSIVE
    assert cfg.costs.apply_min_fee is True
    assert cfg.costs.slippage == 1.0 and isinstance(cfg.costs.slippage, float)  # int -> float
    assert cfg.initial_equity == 5000.0 and isinstance(cfg.initial_equity, float)
    assert cfg.seed == 42


def test_yaml_risk_as_dict(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  risk_pct: 0.02\n  max_leverage: 5\n")
    cfg = load_config(p)
    assert cfg.risk.name == "custom"
    assert cfg.risk.risk_pct == 0.02 and cfg.risk.max_leverage == 5.0


def test_yaml_unknown_keys_raise(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("wave:\n  K9: 1.0\n")
    with pytest.raises(KeyError):
        load_config(p)
    p.write_text("nonsense: 1\n")
    with pytest.raises(KeyError):
        load_config(p)
    p.write_text("risk: reckless\n")
    with pytest.raises(KeyError):
        load_config(p)


def test_with_overrides():
    base = BotConfig()
    cfg = with_overrides(base, **{"wave.K4": 2.5, "strategy.Q_MIN": 0.55, "costs.slippage": 0.001})
    assert cfg.wave.K4 == 2.5 and cfg.strategy.Q_MIN == 0.55 and cfg.costs.slippage == 0.001
    assert cfg.wave.atr_len == base.wave.atr_len
    assert base.wave.K4 == 3.0  # original is untouched (frozen)
    assert cfg is not base

    cfg2 = with_overrides(base, risk="aggressive", seed=7)
    assert cfg2.risk == AGGRESSIVE and cfg2.seed == 7

    cfg3 = with_overrides(base, **{"risk.risk_pct": 0.02, "strategy.mode": "C"})
    assert cfg3.risk.risk_pct == 0.02 and cfg3.risk.max_leverage == 3.0
    assert cfg3.strategy.mode == "C"

    cfg4 = with_overrides(base, **{"strategy.setups_enabled": ["W3"], "wave.M": 8})
    assert cfg4.strategy.setups_enabled == ("W3",) and cfg4.wave.M == 8

    cfg5 = with_overrides(base, **{"breakers.enabled": False})
    assert cfg5.breakers.enabled is False and base.breakers.enabled is True

    assert with_overrides(base) == base  # no overrides -> equal copy


@pytest.mark.parametrize(
    "key", ["wave.nope", "nope.K4", "nope", "strategy", "risk.nope", "risk_pct"]
)
def test_with_overrides_invalid_key_raises(key):
    with pytest.raises(KeyError):
        with_overrides(BotConfig(), **{key: 1.0})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_types.py tests/test_config.py -v`
Expected: collection ERROR for both files — `ModuleNotFoundError: No module named 'ewbot.types'` and `No module named 'ewbot.config'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/types.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

H1 = 3600
H4 = 14400


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar OPEN time, epoch seconds UTC."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    synthetic: bool = False  # True if gap-filled
```

`src/ewbot/config.py`:
```python
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class WaveParams:
    K4: float = 3.0
    atr_len: int = 14
    M: int = 12
    sigma: float = 0.12
    w0: float = 1.0
    T: float = 0.05
    h: float = 0.05
    top_k: int = 5
    wick_clip_atr1h: float = 6.0
    warmup_4h: int = 200


SETUPS = ("W3", "W5", "W5_REV", "C_END")


@dataclass(frozen=True, slots=True)
class StrategyParams:
    mode: str = "A"  # "A" | "C" | "HYBRID"
    setups_enabled: tuple[str, ...] = SETUPS
    Q_MIN: float = 0.6
    S_MIN: float = 0.25
    r_gate: float = 1.5
    tp1_frac: float = 0.5
    time_stop_mult: float = 1.5
    time_stop_min_4h: int = 12
    time_stop_max_4h: int = 60
    setup3_risk_mult: float = 0.5
    buf_struct_atr: float = 0.25  # setups W3, W5, FIB, trailing
    buf_ext_atr: float = 0.5  # setups W5_REV, C_END
    choch_bars: int = 3  # spec 5.2: trigger level = high/low of the 3 1h bars before the E bar


@dataclass(frozen=True, slots=True)
class RiskProfile:
    name: str
    risk_pct: float
    max_leverage: float


CONSERVATIVE = RiskProfile("conservative", 0.01, 3.0)
AGGRESSIVE = RiskProfile("aggressive", 0.03, 10.0)

_PROFILES = {"conservative": CONSERVATIVE, "aggressive": AGGRESSIVE}


@dataclass(frozen=True, slots=True)
class BreakerParams:
    daily_loss: float = 0.03
    total_dd: float = 0.15
    max_consec_losses: int = 5
    consec_pause_hours: int = 24
    enabled: bool = True  # False only in walk-forward grid runs (T21); holdout and backtests keep them on


@dataclass(frozen=True, slots=True)
class CostParams:
    fee_rate: float = 0.0005
    fee_min_per_contract: float = 0.15
    apply_min_fee: bool = False
    slippage: float = 0.0005
    funding_apr_long: float = 0.10
    contract_size: float = 0.01


@dataclass(frozen=True, slots=True)
class BotConfig:
    wave: WaveParams = WaveParams()
    strategy: StrategyParams = StrategyParams()
    risk: RiskProfile = CONSERVATIVE
    breakers: BreakerParams = BreakerParams()
    costs: CostParams = CostParams()
    initial_equity: float = 10_000.0
    seed: int = 0


_SECTIONS: dict[str, type] = {
    "wave": WaveParams,
    "strategy": StrategyParams,
    "breakers": BreakerParams,
    "costs": CostParams,
}
_SCALARS = ("initial_equity", "seed")


def _coerce(current: Any, value: Any) -> Any:
    """Make a YAML/override value match the type of the field's current value."""
    if isinstance(current, bool):
        return value
    if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(current, tuple) and isinstance(value, list):
        return tuple(value)
    return value


def _update(obj: Any, changes: dict[str, Any], label: str) -> Any:
    """dataclasses.replace with unknown-field detection (raises KeyError)."""
    names = {f.name for f in dataclasses.fields(obj)}
    for name in changes:
        if name not in names:
            raise KeyError(f"unknown config key '{label}.{name}'")
    return dataclasses.replace(
        obj, **{k: _coerce(getattr(obj, k), v) for k, v in changes.items()}
    )


def _resolve_risk(value: Any, current: RiskProfile) -> RiskProfile:
    if isinstance(value, RiskProfile):
        return value
    if isinstance(value, str):
        try:
            return _PROFILES[value.lower()]
        except KeyError:
            raise KeyError(f"unknown risk profile '{value}'") from None
    if isinstance(value, dict):
        return _update(dataclasses.replace(current, name="custom"), value, "risk")
    raise KeyError(f"invalid risk value {value!r}")


def _apply(cfg: BotConfig, section: str, value: Any) -> BotConfig:
    """Replace a whole top-level section/scalar with ``value`` (dict, instance, str, number)."""
    if section == "risk":
        return dataclasses.replace(cfg, risk=_resolve_risk(value, cfg.risk))
    if section in _SECTIONS:
        cls = _SECTIONS[section]
        if isinstance(value, cls):
            return dataclasses.replace(cfg, **{section: value})
        if not isinstance(value, dict):
            raise KeyError(f"section '{section}' must be a mapping, got {value!r}")
        return dataclasses.replace(cfg, **{section: _update(getattr(cfg, section), value, section)})
    if section in _SCALARS:
        return dataclasses.replace(cfg, **{section: _coerce(getattr(cfg, section), value)})
    raise KeyError(f"unknown config key '{section}'")


def load_config(path: str | Path | None) -> BotConfig:
    """Load a BotConfig from YAML; ``None`` returns the defaults.

    Top-level keys: wave / strategy / breakers / costs (mappings), risk (profile name
    "conservative" | "aggressive" or a mapping), initial_equity, seed. Missing keys keep
    their defaults; unknown keys raise KeyError.
    """
    cfg = BotConfig()
    if path is None:
        return cfg
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise KeyError(f"config root must be a mapping, got {type(raw).__name__}")
    for section, value in raw.items():
        cfg = _apply(cfg, section, value)
    return cfg


def with_overrides(cfg: BotConfig, **dotted: Any) -> BotConfig:
    """Return a copy of ``cfg`` with dotted-key overrides applied.

    Examples: ``with_overrides(cfg, **{"wave.K4": 2.5, "strategy.Q_MIN": 0.55})``,
    ``risk="aggressive"``, ``**{"risk.risk_pct": 0.02}``, ``seed=7``.
    Unknown keys raise KeyError.
    """
    for key, value in dotted.items():
        head, dot, field = key.partition(".")
        if not dot:
            cfg = _apply(cfg, head, value)
        elif head == "risk":
            cfg = dataclasses.replace(
                cfg, risk=_update(cfg.risk, {field: value}, "risk")
            )
        elif head in _SECTIONS:
            cfg = dataclasses.replace(
                cfg, **{head: _update(getattr(cfg, head), {field: value}, head)}
            )
        else:
            raise KeyError(f"unknown config key '{key}'")
    return cfg
```

`config/default.yaml` (mirrors the defaults; `test_default_yaml_mirrors_dataclass_defaults` enforces it):
```yaml
# Mirrors the dataclass defaults in src/ewbot/config.py (a test enforces this).
wave:
  K4: 3.0
  atr_len: 14
  M: 12
  sigma: 0.12
  w0: 1.0
  T: 0.05
  h: 0.05
  top_k: 5
  wick_clip_atr1h: 6.0
  warmup_4h: 200

strategy:
  mode: A                 # A | C | HYBRID
  setups_enabled: [W3, W5, W5_REV, C_END]
  Q_MIN: 0.6
  S_MIN: 0.25
  r_gate: 1.5
  tp1_frac: 0.5
  time_stop_mult: 1.5
  time_stop_min_4h: 12
  time_stop_max_4h: 60
  setup3_risk_mult: 0.5
  buf_struct_atr: 0.25    # setups W3, W5, FIB, trailing
  buf_ext_atr: 0.5        # setups W5_REV, C_END
  choch_bars: 3           # spec 5.2 trigger: high/low of the 3 1h bars before the E bar

# Either a profile name ("conservative" | "aggressive") or a mapping
# {name, risk_pct, max_leverage}.
risk: conservative

breakers:
  daily_loss: 0.03
  total_dd: 0.15
  max_consec_losses: 5
  consec_pause_hours: 24
  enabled: true           # walk-forward grid runs switch this off in code (T21)

costs:
  fee_rate: 0.0005
  fee_min_per_contract: 0.15
  apply_min_fee: false    # per-contract minimum is NOT applied in backtests
  slippage: 0.0005
  funding_apr_long: 0.10
  contract_size: 0.01

initial_equity: 10000.0
seed: 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS — 14 passed (2 in `test_types.py`, 12 in `test_config.py`; the parametrized invalid-key test counts 6).

Also confirm the install is importable: `uv run python -c "import ewbot, ewbot.config; print(ewbot.__version__)"` prints `0.1.0`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .gitignore config/default.yaml src/ewbot/__init__.py src/ewbot/types.py src/ewbot/config.py tests/test_types.py tests/test_config.py docs
git commit -m "feat: project scaffold, shared types and config loader"
```

---

### Task 2: Coinbase history fetcher

**Files:**
- Create: `src/ewbot/data/__init__.py`, `src/ewbot/data/coinbase_history.py`
- Test: `tests/data/test_coinbase_history.py`

**Interfaces:**
- Consumes: `ewbot.types.H1`.
- Produces: `API`, `fetch_1h(product, start_ts, end_ts, session=None, sleep_s=0.15) -> pd.DataFrame`, `update_cache(product="BTC-USD", cache_dir=Path("data/cache"), start_ts=1483228800) -> Path`. Semantics fixed here:
  - `fetch_1h` returns closed bars with open time in `[start_ts, end_ts)` (start rounded up to the hour). It walks ascending in windows of 300 bars (request `start`/`end` are inclusive bar-open times, ISO-8601 `Z`), dedupes, sorts, drops rows outside the range. Empty result still has the standard columns.
  - HTTP 429 and 5xx are retried (3 tries total, sleeps 1 s then 2 s); other non-200 statuses raise `RuntimeError` immediately; exhausted retries raise `RuntimeError("... after 3 tries ...")`.
  - `update_cache` writes `{cache_dir}/{product}_1h.parquet`, only closed hours (end = current time floored to the hour), and resumes from `last cached ts + 3600`. The clock is the module-private `_now_ts()` so tests can patch it.

- [ ] **Step 1: Write the failing test**

`tests/data/test_coinbase_history.py` (uses a fake session object; no network):
```python
from datetime import UTC, datetime

import pandas as pd
import pytest

from ewbot.data import coinbase_history as ch
from ewbot.types import H1

T0 = 1_700_000_000 // H1 * H1  # 2023-11-14 22:00:00 UTC, hour-aligned


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = f"fake body {status_code}"

    def json(self):
        return self._payload


def _row(ts: int) -> list[float]:
    """Coinbase row layout: [time, low, high, open, close, volume]."""
    k = (ts - T0) // H1
    base = 30_000.0 + k
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 2.5]


class FakeSession:
    """Behaves like the exchange: honours start/end (inclusive), max 300, newest first."""

    def __init__(self, first_ts: int, last_ts: int):
        self.first, self.last = first_ts, last_ts
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params)})
        assert params["granularity"] == 3600
        s = int(datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        e = int(datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        tss = [t for t in range(s, e + 1, H1) if self.first <= t <= self.last]
        assert len(tss) <= 300, "request window larger than the API maximum"
        return FakeResponse(200, [_row(t) for t in reversed(tss)])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)


def test_fetch_single_window_schema_and_order():
    sess = FakeSession(T0, T0 + 1000 * H1)
    df = ch.fetch_1h("BTC-USD", T0, T0 + 10 * H1, session=sess)
    assert len(sess.calls) == 1
    assert sess.calls[0]["url"] == "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert str(df["ts"].dtype) == "int64"
    assert all(str(df[c].dtype) == "float64" for c in ["open", "high", "low", "close", "volume"])
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(10)]  # ascending, end exclusive
    assert isinstance(df.index, pd.RangeIndex)
    first = df.iloc[0]
    # raw row is [ts, low, high, open, close, volume] = [T0, 29995, 30010, 30000, 30001, 2.5]
    assert (first["low"], first["high"], first["open"], first["close"], first["volume"]) == (
        29_995.0, 30_010.0, 30_000.0, 30_001.0, 2.5,
    )


def test_fetch_paginates_across_two_windows():
    n = 400  # > 300 -> needs two windows
    sess = FakeSession(T0, T0 + 10_000 * H1)
    df = ch.fetch_1h("BTC-USD", T0, T0 + n * H1, session=sess)
    assert len(sess.calls) == 2
    starts = [c["params"]["start"] for c in sess.calls]
    assert starts[0] == datetime.fromtimestamp(T0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert starts[1] == datetime.fromtimestamp(T0 + 300 * H1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(df) == n
    assert df["ts"].is_unique and df["ts"].is_monotonic_increasing
    assert df["ts"].iloc[0] == T0 and df["ts"].iloc[-1] == T0 + (n - 1) * H1
    assert (df["ts"].diff().dropna() == H1).all()


def test_fetch_exact_300_is_one_window_and_301_is_two():
    sess = FakeSession(T0, T0 + 10_000 * H1)
    assert len(ch.fetch_1h("BTC-USD", T0, T0 + 300 * H1, session=sess)) == 300
    assert len(sess.calls) == 1
    sess2 = FakeSession(T0, T0 + 10_000 * H1)
    assert len(ch.fetch_1h("BTC-USD", T0, T0 + 301 * H1, session=sess2)) == 301
    assert len(sess2.calls) == 2


def test_fetch_dedupes_and_sorts_unordered_duplicates():
    class Dup:
        def get(self, url, params=None, timeout=None):
            rows = [_row(T0 + 2 * H1), _row(T0), _row(T0 + H1), _row(T0 + H1)]
            return FakeResponse(200, rows)

    df = ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=Dup())
    assert df["ts"].tolist() == [T0, T0 + H1, T0 + 2 * H1]


def test_fetch_filters_rows_outside_requested_range():
    class Wide:
        def get(self, url, params=None, timeout=None):
            return FakeResponse(200, [_row(T0 + i * H1) for i in range(-2, 8)])

    df = ch.fetch_1h("BTC-USD", T0, T0 + 5 * H1, session=Wide())
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(5)]


def test_fetch_empty_range_and_empty_response():
    sess = FakeSession(T0, T0 + 10 * H1)
    assert ch.fetch_1h("BTC-USD", T0 + 5 * H1, T0 + 5 * H1, session=sess).empty
    assert sess.calls == []  # nothing requested for an empty range
    none = FakeSession(T0 + 1000 * H1, T0 + 2000 * H1)  # no data in the requested window
    df = ch.fetch_1h("BTC-USD", T0, T0 + 10 * H1, session=none)
    assert df.empty and list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_retry_on_429_then_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(ch.time, "sleep", lambda s: sleeps.append(s))

    class Flaky:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            if self.n == 1:
                return FakeResponse(429)
            if self.n == 2:
                return FakeResponse(503)
            return FakeResponse(200, [_row(T0 + i * H1) for i in range(3)])

    s = Flaky()
    df = ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=s, sleep_s=0.0)
    assert s.n == 3  # two failures + one success = 3 tries
    assert len(df) == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff between the tries


def test_retry_gives_up_after_three_tries():
    class AlwaysLimited:
        n = 0

        def get(self, url, params=None, timeout=None):
            type(self).n += 1
            return FakeResponse(429)

    with pytest.raises(RuntimeError, match="after 3 tries"):
        ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=AlwaysLimited())
    assert AlwaysLimited.n == 3


def test_client_error_is_not_retried():
    class NotFound:
        n = 0

        def get(self, url, params=None, timeout=None):
            type(self).n += 1
            return FakeResponse(404)

    with pytest.raises(RuntimeError, match="HTTP 404"):
        ch.fetch_1h("NOPE-USD", T0, T0 + 3 * H1, session=NotFound())
    assert NotFound.n == 1


def test_update_cache_creates_then_extends_incrementally(tmp_path, monkeypatch):
    calls: list[tuple[str, int, int]] = []

    def fake_fetch(product, start_ts, end_ts, session=None, sleep_s=0.15):
        calls.append((product, start_ts, end_ts))
        return pd.DataFrame(
            {
                "ts": pd.Series(range(start_ts, end_ts, H1), dtype="int64"),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0,
            }
        )

    monkeypatch.setattr(ch, "fetch_1h", fake_fetch)
    monkeypatch.setattr(ch, "_now_ts", lambda: T0 + 5 * H1 + 1234)  # mid-hour: bar T0+5h still open

    path = ch.update_cache("BTC-USD", tmp_path, start_ts=T0)
    assert path == tmp_path / "BTC-USD_1h.parquet"
    assert calls == [("BTC-USD", T0, T0 + 5 * H1)]  # only closed hours: T0 .. T0+4h
    df = pd.read_parquet(path)
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(5)]

    monkeypatch.setattr(ch, "_now_ts", lambda: T0 + 8 * H1)
    ch.update_cache("BTC-USD", tmp_path, start_ts=T0)
    assert calls[-1] == ("BTC-USD", T0 + 5 * H1, T0 + 8 * H1)  # resumes after the last cached bar
    df = pd.read_parquet(path)
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(8)]
    assert df["ts"].is_unique

    n_calls = len(calls)
    ch.update_cache("BTC-USD", tmp_path, start_ts=T0)  # already current -> no fetch
    assert len(calls) == n_calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/data/test_coinbase_history.py -v`
Expected: collection ERROR — `ImportError: cannot import name 'coinbase_history' from 'ewbot.data'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/data/__init__.py`:
```python
"""Market data access: Coinbase history fetcher and candle utilities."""
```

`src/ewbot/data/coinbase_history.py`:
```python
"""Hourly BTC-USD history from the public Coinbase Exchange API (no auth).

GET {API}?granularity=3600&start=<iso>&end=<iso> returns at most 300 rows
``[time, low, high, open, close, volume]``, newest first. ``time`` is the bar OPEN
time (epoch seconds). Both ``start`` and ``end`` are inclusive bar-open times.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from ewbot.types import H1

API = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_PER_REQUEST = 300
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
_RETRY_TRIES = 3
_BACKOFF_S = 1.0
_TIMEOUT_S = 15.0


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": pd.Series(dtype="int64"),
            **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]},
        }
    )


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> int:
    return int(time.time())


def _get_json(session: requests.Session, url: str, params: dict[str, object]) -> list:
    """GET with up to 3 tries; retries (exponential backoff) on HTTP 429 / 5xx."""
    last = ""
    for attempt in range(_RETRY_TRIES):
        resp = session.get(url, params=params, timeout=_TIMEOUT_S)
        status = resp.status_code
        if status == 200:
            return resp.json()
        if status == 429 or status >= 500:
            last = f"HTTP {status}"
            if attempt < _RETRY_TRIES - 1:
                time.sleep(_BACKOFF_S * 2**attempt)
            continue
        raise RuntimeError(f"Coinbase candles request failed: HTTP {status} {resp.text[:200]}")
    raise RuntimeError(f"Coinbase candles request failed after {_RETRY_TRIES} tries ({last})")


def fetch_1h(
    product: str,
    start_ts: int,
    end_ts: int,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> pd.DataFrame:
    """Fetch closed 1h bars with open time in ``[start_ts, end_ts)``.

    Walks ascending in windows of 300 bars, dedupes and sorts. Returns the standard schema
    (ts:int64, open/high/low/close/volume:float64, RangeIndex), possibly empty.
    """
    first = -(-start_ts // H1) * H1  # round start up to the hour grid
    last_open = (end_ts - 1) // H1 * H1  # last bar open time inside [start_ts, end_ts)
    if first > last_open:
        return _empty()
    sess = session if session is not None else requests.Session()
    url = API.format(product=product)

    rows: list[list[float]] = []
    w_start = first
    while w_start <= last_open:
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * H1, last_open)
        params = {"granularity": H1, "start": _iso(w_start), "end": _iso(w_end)}
        data = _get_json(sess, url, params)
        rows.extend(data)
        w_start = w_end + H1
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)

    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    df = df[(df["ts"] >= first) & (df["ts"] <= last_open)]
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in COLUMNS[1:]:
        df[c] = df[c].astype("float64")
    return df[COLUMNS]


def update_cache(
    product: str = "BTC-USD",
    cache_dir: Path = Path("data/cache"),
    start_ts: int = 1483228800,
) -> Path:
    """Create/extend ``{cache_dir}/{product}_1h.parquet`` up to the last CLOSED hour."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{product}_1h.parquet"

    old = pd.read_parquet(path) if path.exists() else _empty()
    begin = int(old["ts"].max()) + H1 if len(old) else start_ts
    end = _now_ts() // H1 * H1  # exclusive: the in-progress hour is not stored
    if begin >= end:
        if not path.exists():
            old.to_parquet(path, index=False)
        return path

    new = fetch_1h(product, begin, end)
    out = pd.concat([old, new], ignore_index=True)
    out = out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    out.to_parquet(path, index=False)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/data/test_coinbase_history.py -v`
Expected: PASS — 10 passed.

- [ ] **Step 5: Manual live check (NOT a test — needs network, run once)**

The unit tests use canned responses, so verify once that the real API matches the assumed row layout `[time, low, high, open, close, volume]`, newest first, with inclusive `start`/`end`:

```bash
uv run python -c "
import time
from ewbot.data.coinbase_history import fetch_1h
end = int(time.time()) // 3600 * 3600
df = fetch_1h('BTC-USD', end - 72 * 3600, end)
print(df.head())
print(len(df), df['ts'].iloc[0] == end - 72 * 3600, df['ts'].iloc[-1] == end - 3600)
print(df.dtypes.to_dict())
"
```

Expected output: a 5-row head with columns `ts open high low close volume`; `ts` values are multiples of 3600 and increase by 3600 per row; prices are plausible current BTC-USD levels with `low <= open,close <= high` on every row; the second line prints `72 True True` (exactly 72 bars, first = `end-72h`, last = `end-1h`); dtypes are `int64` for `ts` and `float64` for the rest. If the count is 71/73, or if `low`/`high` look swapped, the API semantics differ from the assumption — fix `fetch_1h` (window `end`, column order) before continuing; do not paper over it in the tests.

Optionally check pagination against the real API (expects `700 True`):

```bash
uv run python -c "
import time
from ewbot.data.coinbase_history import fetch_1h
end = int(time.time()) // 3600 * 3600
df = fetch_1h('BTC-USD', end - 700 * 3600, end)
print(len(df), bool((df['ts'].diff().dropna() == 3600).all()))
"
```

- [ ] **Step 6: Commit**

```bash
git add src/ewbot/data/__init__.py src/ewbot/data/coinbase_history.py tests/data/test_coinbase_history.py
git commit -m "feat: Coinbase 1h history fetcher with pagination, retry and parquet cache"
```

---

### Task 3: Candle utilities

**Files:**
- Create: `src/ewbot/data/candles.py`
- Test: `tests/data/test_candles.py`

**Interfaces:**
- Consumes: `ewbot.types.{Bar, H1, H4}`.
- Produces: `IntegrityReport`, `load_1h`, `check_integrity`, `fill_gaps`, `clip_wicks`, `aggregate_4h`, `to_bars`, `slice_ts`, `futures_closed`, `entry_blackout` exactly as in the contract. Details fixed here:
  - `IntegrityReport.nonpositive_ts` = ts of bars whose open/high/low/close is `<= 0` (corrupt rows).
  - `fill_gaps` keeps the first of duplicated ts, preserves existing `synthetic` flags (idempotent), and returns `ts:int64` + `synthetic:bool`.
  - `clip_wicks(df, atr1h, k)`: `atr1h` is row-aligned with `df`; bar `i` is clipped with `atr1h[i-1]` (no look-ahead); bar 0 and bars whose previous ATR is NaN are untouched; logs `INFO` when something was clipped; returns a copy.
  - `aggregate_4h` keeps only buckets with exactly 4 distinct hours; call `fill_gaps` first to keep buckets that lost an hour (they come out with `synthetic=True`).
  - `futures_closed` / `entry_blackout` use the real `America/New_York` DST rules, so the UTC hour of the halt moves between 22:00 (EST) and 21:00 (EDT).
- Epoch constants used in the tests (computed with `datetime(..., tzinfo=UTC).timestamp()`):

| Constant | UTC | New York | epoch |
|---|---|---|---|
| `JAN_FRI_17ET` | Fri 2024-01-12 22:00 | 17:00 EST | 1705096800 |
| `JUL_FRI_17ET` | Fri 2024-07-12 21:00 | 17:00 EDT | 1720818000 |
| `MAR_FRI_17ET_EST` | Fri 2024-03-08 22:00 | 17:00 EST (DST starts Mar 10) | 1709935200 |
| `MAR_FRI_17ET_EDT` | Fri 2024-03-15 21:00 | 17:00 EDT | 1710536400 |

- [ ] **Step 1: Write the failing test**

`tests/data/test_candles.py`:
```python
import logging

import numpy as np
import pandas as pd
import pytest

from ewbot.data.candles import (
    IntegrityReport,
    aggregate_4h,
    check_integrity,
    clip_wicks,
    entry_blackout,
    fill_gaps,
    futures_closed,
    load_1h,
    slice_ts,
    to_bars,
)
from ewbot.types import H1, H4, Bar

T = 1_704_067_200  # Mon 2024-01-01 00:00:00 UTC (also a 4h boundary)

# Friday 17:00 America/New_York as exact UTC epoch seconds (bar OPEN time of the halted hour):
JAN_FRI_17ET = 1_705_096_800  # Fri 2024-01-12 22:00 UTC  (EST = UTC-5)
JUL_FRI_17ET = 1_720_818_000  # Fri 2024-07-12 21:00 UTC  (EDT = UTC-4)
MAR_FRI_17ET_EST = 1_709_935_200  # Fri 2024-03-08 22:00 UTC (last Friday before DST starts Mar 10)
MAR_FRI_17ET_EDT = 1_710_536_400  # Fri 2024-03-15 21:00 UTC (first Friday after DST starts)


def make_df(hours, base: float = 100.0) -> pd.DataFrame:
    """1h bars at T + h*3600 with o=base+h, h=o+10, l=o-10, c=o+1, v=1 (h = hour offset)."""
    hours = list(hours)
    o = np.array([base + h for h in hours], dtype=float)
    return pd.DataFrame(
        {
            "ts": np.array([T + h * H1 for h in hours], dtype="int64"),
            "open": o,
            "high": o + 10,
            "low": o - 10,
            "close": o + 1,
            "volume": np.ones(len(hours)),
        }
    )


# ---------- integrity / load ----------

def test_check_integrity_gap_and_duplicate():
    df = make_df([0, 1, 2, 4, 5, 5, 6])  # hour 3 missing, hour 5 duplicated
    rep = check_integrity(df)
    assert rep == IntegrityReport(
        n=7, missing_ts=(T + 3 * H1,), duplicate_ts=(T + 5 * H1,), nonpositive_ts=()
    )


def test_check_integrity_clean_empty_and_nonpositive():
    assert check_integrity(make_df(range(5))) == IntegrityReport(5, (), (), ())
    assert check_integrity(make_df([])) == IntegrityReport(0, (), (), ())
    df = make_df(range(4))
    df.loc[2, "low"] = 0.0
    df.loc[3, "close"] = -1.0
    rep = check_integrity(df)
    assert rep.nonpositive_ts == (T + 2 * H1, T + 3 * H1)
    assert rep.missing_ts == () and rep.duplicate_ts == ()


def test_check_integrity_multi_hour_gap_and_other_tf():
    df = make_df([0, 1, 5])
    assert check_integrity(df).missing_ts == (T + 2 * H1, T + 3 * H1, T + 4 * H1)
    four = pd.DataFrame({"ts": [T, T + 3 * H4], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0})
    assert check_integrity(four, tf=H4).missing_ts == (T + H4, T + 2 * H4)


def test_load_1h_sorts_and_enforces_dtypes(tmp_path):
    df = make_df([2, 0, 1])
    df["ts"] = df["ts"].astype("int32")
    p = tmp_path / "x.parquet"
    df.to_parquet(p, index=False)
    out = load_1h(p)
    assert out["ts"].tolist() == [T, T + H1, T + 2 * H1]
    assert str(out["ts"].dtype) == "int64"
    assert str(out["close"].dtype) == "float64"
    assert isinstance(out.index, pd.RangeIndex)


# ---------- fill_gaps ----------

def test_fill_gaps_inserts_flat_synthetic_bar():
    df = make_df([0, 1, 3])  # closes 101, 102, 104 -> hour 2 missing
    out = fill_gaps(df)
    assert out["ts"].tolist() == [T + i * H1 for i in range(4)]
    assert out["synthetic"].tolist() == [False, False, True, False]
    row = out.iloc[2]
    assert (row["open"], row["high"], row["low"], row["close"]) == (102.0, 102.0, 102.0, 102.0)
    assert row["volume"] == 0.0
    assert out.iloc[3]["open"] == 103.0  # real bars untouched
    assert str(out["ts"].dtype) == "int64" and str(out["synthetic"].dtype) == "bool"
    assert check_integrity(out).missing_ts == ()


def test_fill_gaps_multi_hour_gap_dedupe_and_idempotent():
    df = make_df([0, 1, 1, 5])  # duplicate hour 1, hours 2-4 missing
    out = fill_gaps(df)
    assert len(out) == 6
    assert out["synthetic"].tolist() == [False, False, True, True, True, False]
    assert out.loc[2:4, "close"].tolist() == [102.0, 102.0, 102.0]  # all flat at hour-1 close
    assert out.loc[2:4, "volume"].tolist() == [0.0, 0.0, 0.0]
    again = fill_gaps(out)
    pd.testing.assert_frame_equal(again, out)  # flags preserved, nothing new inserted


def test_fill_gaps_no_gap_and_empty():
    out = fill_gaps(make_df(range(3)))
    assert out["synthetic"].tolist() == [False, False, False]
    empty = fill_gaps(make_df([]))
    assert len(empty) == 0 and "synthetic" in empty.columns


# ---------- aggregate_4h ----------

def test_aggregate_4h_values_and_trailing_incomplete_bucket_dropped():
    out = aggregate_4h(make_df(range(9)))  # hours 0-3, 4-7 complete; hour 8 starts a bucket
    assert out["ts"].tolist() == [T, T + H4]  # trailing partial bucket (hour 8) dropped
    b0, b1 = out.iloc[0], out.iloc[1]
    # hour h: open=100+h, high=110+h, low=90+h, close=101+h, volume=1
    assert (b0["open"], b0["high"], b0["low"], b0["close"], b0["volume"]) == (100.0, 113.0, 90.0, 104.0, 4.0)
    assert (b1["open"], b1["high"], b1["low"], b1["close"], b1["volume"]) == (104.0, 117.0, 94.0, 108.0, 4.0)
    assert out["synthetic"].tolist() == [False, False]
    assert (out["ts"] % H4 == 0).all()


def test_aggregate_4h_alignment_drops_leading_partial_bucket():
    out = aggregate_4h(make_df(range(1, 9)))  # starts at 01:00 -> bucket 0 has 3 bars
    assert out["ts"].tolist() == [T + H4]


def test_aggregate_4h_bucket_with_missing_hour_dropped_until_gaps_filled():
    raw = make_df([0, 1, 3, 4, 5, 6, 7])  # hour 2 missing
    assert aggregate_4h(raw)["ts"].tolist() == [T + H4]  # bucket 0 incomplete -> dropped
    out = aggregate_4h(fill_gaps(raw))
    assert out["ts"].tolist() == [T, T + H4]
    assert out["synthetic"].tolist() == [True, False]
    b0 = out.iloc[0]
    # hours 0,1 real, hour 2 synthetic (flat 102, vol 0), hour 3 real
    assert (b0["open"], b0["high"], b0["low"], b0["close"], b0["volume"]) == (100.0, 113.0, 90.0, 104.0, 3.0)


def test_aggregate_4h_to_bars_roundtrip_types():
    out = aggregate_4h(make_df(range(8)))
    bars = to_bars(out)
    assert bars[0] == Bar(T, 100.0, 113.0, 90.0, 104.0, 4.0, False)


# ---------- clip_wicks ----------

def test_clip_wicks_uses_previous_bar_atr():
    df = pd.DataFrame(
        {
            "ts": [T + i * H1 for i in range(4)],
            "open": [100.0, 101.0, 102.0, 100.0],
            "close": [101.0, 102.0, 100.0, 101.0],
            "high": [150.0, 130.0, 103.0, 102.0],
            "low": [99.0, 100.0, 60.0, 99.0],
            "volume": 1.0,
        }
    )
    atr = np.array([2.0, 2.0, np.nan, 2.0])
    out, n = clip_wicks(df, atr, k=3.0)
    assert n == 2
    # bar 0: no previous ATR -> untouched, even with a 150 wick (no wrap-around to atr[-1])
    assert out.loc[0, "high"] == 150.0
    # bar 1: cap = max(101,102) + 3*atr[0] = 108 -> high 130 clipped; floor = 101 - 6 = 95 <= low 100 -> unchanged
    assert out.loc[1, "high"] == 108.0 and out.loc[1, "low"] == 100.0
    # bar 2: floor = min(102,100) - 3*atr[1] = 94 -> low 60 clipped
    assert out.loc[2, "low"] == 94.0 and out.loc[2, "high"] == 103.0
    # bar 3: previous ATR is NaN -> untouched
    assert out.loc[3, "high"] == 102.0 and out.loc[3, "low"] == 99.0
    # original frame is not modified; open/close/volume are untouched
    assert df.loc[1, "high"] == 130.0
    pd.testing.assert_series_equal(out["open"], df["open"])
    pd.testing.assert_series_equal(out["close"], df["close"])


def test_clip_wicks_no_change_logs_nothing_and_validates_length(caplog):
    df = make_df(range(5))
    with caplog.at_level(logging.INFO, logger="ewbot.data.candles"):
        out, n = clip_wicks(df, np.full(5, 100.0), k=6.0)
    assert n == 0 and caplog.records == []
    pd.testing.assert_frame_equal(out, df)
    with pytest.raises(ValueError):
        clip_wicks(df, np.ones(4), k=6.0)


def test_clip_wicks_logs_when_clipped(caplog):
    df = make_df(range(3))
    with caplog.at_level(logging.INFO, logger="ewbot.data.candles"):
        _, n = clip_wicks(df, np.full(3, 1.0), k=1.0)  # wicks are 10 wide, cap is body +/- 1
    assert n == 2
    assert any("clipped 2" in r.getMessage() for r in caplog.records)


# ---------- futures_closed / entry_blackout across DST ----------

def test_constants_are_the_stated_new_york_times():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    for ts in (JAN_FRI_17ET, JUL_FRI_17ET, MAR_FRI_17ET_EST, MAR_FRI_17ET_EDT):
        d = datetime.fromtimestamp(ts, ny)
        assert (d.weekday(), d.hour, d.minute) == (4, 17, 0)


def test_futures_closed_friday_january_est():
    assert futures_closed(JAN_FRI_17ET) is True  # [22:00, 23:00) UTC = 17:00-18:00 EST
    assert futures_closed(JAN_FRI_17ET - H1) is False  # 16:00 EST bar ends exactly at the halt
    assert futures_closed(JAN_FRI_17ET + H1) is False  # 18:00 EST bar starts exactly at reopen
    assert futures_closed(JAN_FRI_17ET - 24 * H1) is False  # same time Thursday
    assert futures_closed(JAN_FRI_17ET + 24 * H1) is False  # same time Saturday


def test_futures_closed_friday_july_edt():
    assert futures_closed(JUL_FRI_17ET) is True  # [21:00, 22:00) UTC = 17:00-18:00 EDT
    assert futures_closed(JUL_FRI_17ET - H1) is False
    assert futures_closed(JUL_FRI_17ET + H1) is False  # 22:00 UTC is 18:00 EDT -> already open
    # a fixed-EST implementation would wrongly flag 22:00 UTC and miss 21:00 UTC in July
    assert futures_closed(JUL_FRI_17ET - 24 * H1) is False


def test_futures_closed_around_dst_change():
    assert futures_closed(MAR_FRI_17ET_EST) is True  # 22:00 UTC, still EST on 2024-03-08
    assert futures_closed(MAR_FRI_17ET_EST - H1) is False
    assert futures_closed(MAR_FRI_17ET_EDT) is True  # 21:00 UTC, EDT on 2024-03-15
    assert futures_closed(MAR_FRI_17ET_EDT + H1) is False


def test_futures_closed_handles_non_aligned_bar_start():
    assert futures_closed(JAN_FRI_17ET - 1800) is True  # [21:30, 22:30) UTC overlaps the halt
    assert futures_closed(JAN_FRI_17ET - 3600) is False


def test_entry_blackout_january_est():
    assert entry_blackout(JAN_FRI_17ET - 2 * H1) is False  # 15:00 EST
    assert entry_blackout(JAN_FRI_17ET - H1) is True  # 16:00 EST
    assert entry_blackout(JAN_FRI_17ET) is True  # 17:00 EST
    assert entry_blackout(JAN_FRI_17ET + H1) is False  # 18:00 EST
    assert entry_blackout(JAN_FRI_17ET - H1 - 24 * H1) is False  # Thursday 16:00 EST


def test_entry_blackout_july_edt():
    assert entry_blackout(JUL_FRI_17ET - 2 * H1) is False  # 15:00 EDT
    assert entry_blackout(JUL_FRI_17ET - H1) is True  # 16:00 EDT
    assert entry_blackout(JUL_FRI_17ET) is True  # 17:00 EDT
    assert entry_blackout(JUL_FRI_17ET + H1) is False  # 18:00 EDT


def test_entry_blackout_around_dst_change():
    assert entry_blackout(MAR_FRI_17ET_EST - H1) is True and entry_blackout(MAR_FRI_17ET_EST + H1) is False
    assert entry_blackout(MAR_FRI_17ET_EDT - H1) is True and entry_blackout(MAR_FRI_17ET_EDT + H1) is False


def test_exactly_two_blackout_hours_and_one_closed_hour_per_friday():
    week_start = T  # Monday 2024-01-01 00:00 UTC
    hours = [week_start + i * H1 for i in range(7 * 24)]
    assert sum(futures_closed(t) for t in hours) == 1
    assert sum(entry_blackout(t) for t in hours) == 2


# ---------- slice_ts / to_bars ----------

def test_slice_ts_half_open_interval():
    df = make_df(range(6))
    assert slice_ts(df, T + 2 * H1, T + 4 * H1)["ts"].tolist() == [T + 2 * H1, T + 3 * H1]
    assert slice_ts(df, None, T + 2 * H1)["ts"].tolist() == [T, T + H1]
    assert slice_ts(df, T + 4 * H1, None)["ts"].tolist() == [T + 4 * H1, T + 5 * H1]
    assert len(slice_ts(df, None, None)) == 6
    assert slice_ts(df, T + 10 * H1, None).empty
    out = slice_ts(df, T + 2 * H1, None)
    assert isinstance(out.index, pd.RangeIndex) and out.index[0] == 0


def test_to_bars_with_and_without_synthetic_column():
    df = make_df([0, 2])
    bars = to_bars(df)
    assert bars == [
        Bar(T, 100.0, 110.0, 90.0, 101.0, 1.0, False),
        Bar(T + 2 * H1, 102.0, 112.0, 92.0, 103.0, 1.0, False),
    ]
    assert type(bars[0].ts) is int and type(bars[0].open) is float

    filled = to_bars(fill_gaps(df))
    assert [b.synthetic for b in filled] == [False, True, False]
    assert filled[1] == Bar(T + H1, 101.0, 101.0, 101.0, 101.0, 0.0, True)
    assert to_bars(make_df([])) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/data/test_candles.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'ewbot.data.candles'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/data/candles.py`:
```python
"""Candle utilities: integrity checks, gap filling, 4h aggregation, wick clipping, calendar."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ewbot.types import H1, H4, Bar

log = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_PRICE_COLS = ["open", "high", "low", "close"]
_BASE_COLS = ["ts", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    n: int
    missing_ts: tuple[int, ...]
    duplicate_ts: tuple[int, ...]
    nonpositive_ts: tuple[int, ...]  # bars whose open/high/low/close is <= 0 (corrupt rows)


def load_1h(path: Path) -> pd.DataFrame:
    """Read a parquet cache, enforce the standard dtypes and sort by ts (duplicates are kept)."""
    df = pd.read_parquet(path)
    df = df.astype({"ts": "int64", **{c: "float64" for c in _BASE_COLS[1:]}})
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def check_integrity(df: pd.DataFrame, tf: int = H1) -> IntegrityReport:
    """Report missing bars (holes in the ``tf`` grid), duplicated ts and non-positive prices."""
    ts = df["ts"].to_numpy(dtype=np.int64)
    if ts.size == 0:
        return IntegrityReport(0, (), (), ())
    uniq, counts = np.unique(ts, return_counts=True)
    dups = uniq[counts > 1]
    grid = np.arange(uniq[0], uniq[-1] + tf, tf, dtype=np.int64)
    missing = np.setdiff1d(grid, uniq, assume_unique=True)
    bad = (df[_PRICE_COLS].to_numpy(dtype=np.float64) <= 0).any(axis=1)
    bad_ts = np.unique(ts[bad])
    return IntegrityReport(
        n=int(ts.size),
        missing_ts=tuple(int(t) for t in missing),
        duplicate_ts=tuple(int(t) for t in dups),
        nonpositive_ts=tuple(int(t) for t in bad_ts),
    )


def fill_gaps(df: pd.DataFrame, tf: int = H1) -> pd.DataFrame:
    """Return a gap-free frame with a bool ``synthetic`` column.

    Duplicate ts keep the first row. A missing bar is flat at the previous close with volume 0
    and ``synthetic=True``. Existing ``synthetic`` flags are preserved (idempotent).
    """
    d = df.drop_duplicates("ts", keep="first").sort_values("ts").reset_index(drop=True)
    had_flag = "synthetic" in d.columns
    flags = d["synthetic"].to_numpy(dtype=bool) if had_flag else np.zeros(len(d), dtype=bool)
    d = d[_BASE_COLS]
    if d.empty:
        return d.assign(synthetic=pd.Series(dtype=bool))

    ts = d["ts"].to_numpy(dtype=np.int64)
    grid = np.arange(ts[0], ts[-1] + tf, tf, dtype=np.int64)
    out = d.set_index("ts").reindex(grid)
    missing = out["close"].isna().to_numpy()
    prev_close = out["close"].ffill()
    for c in ("open", "high", "low"):
        out[c] = out[c].where(~missing, prev_close)
    out["close"] = prev_close
    out["volume"] = out["volume"].fillna(0.0)

    synthetic = np.ones(len(grid), dtype=bool)
    synthetic[~missing] = flags  # rows that existed keep their previous flag
    out["synthetic"] = synthetic
    out = out.rename_axis("ts").reset_index()
    return out.astype({"ts": "int64"})


def clip_wicks(df: pd.DataFrame, atr1h: np.ndarray, k: float) -> tuple[pd.DataFrame, int]:
    """Clip high/low to the candle body +/- k * ATR, using the ATR of the PREVIOUS bar.

    ``atr1h`` is aligned row-for-row with ``df``. The first bar and bars whose previous ATR is
    NaN are left untouched. Returns ``(new_df, n_bars_clipped)``; the input is not modified.
    """
    atr = np.asarray(atr1h, dtype=np.float64)
    if atr.shape != (len(df),):
        raise ValueError(f"atr1h length {atr.shape} does not match df length {len(df)}")
    out = df.copy()
    if len(df) < 2:
        return out, 0

    prev_atr = np.concatenate(([np.nan], atr[:-1]))
    o = df["open"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    hi = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    with np.errstate(invalid="ignore"):
        hi_cap = np.maximum(o, c) + k * prev_atr
        lo_cap = np.minimum(o, c) - k * prev_atr
        new_hi = np.where(np.isnan(prev_atr), hi, np.minimum(hi, hi_cap))
        new_lo = np.where(np.isnan(prev_atr), lo, np.maximum(lo, lo_cap))
    changed = (new_hi != hi) | (new_lo != lo)
    n_clipped = int(changed.sum())
    out["high"] = new_hi
    out["low"] = new_lo
    if n_clipped:
        log.info("clip_wicks: clipped %d bar(s) at k=%s", n_clipped, k)
    return out, n_clipped


def aggregate_4h(df1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1h bars into 4h bars aligned to UTC 00/04/08/12/16/20.

    Only COMPLETE buckets (exactly 4 distinct 1h bars) are returned; ``synthetic`` is True if
    any constituent was synthetic. Run ``fill_gaps`` first to keep buckets that lost a bar.
    """
    d = df1h.drop_duplicates("ts").sort_values("ts")
    syn = d["synthetic"].astype(bool) if "synthetic" in d.columns else False
    d = d.assign(bucket=(d["ts"] // H4) * H4, synthetic=syn)
    agg = d.groupby("bucket", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        synthetic=("synthetic", "any"),
        n=("ts", "size"),
    )
    agg = agg[agg["n"] == H4 // H1].drop(columns="n")
    out = agg.rename_axis("ts").reset_index()
    return out.astype({"ts": "int64", "synthetic": "bool"})


def to_bars(df: pd.DataFrame) -> list[Bar]:
    """Convert a frame into ``Bar`` objects (``synthetic`` defaults to False if absent)."""
    ts = df["ts"].to_numpy(dtype=np.int64)
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)
    syn = df["synthetic"].to_numpy(dtype=bool) if "synthetic" in df.columns else np.zeros(len(df), bool)
    return [
        Bar(int(ts[i]), float(o[i]), float(h[i]), float(lo[i]), float(c[i]), float(v[i]), bool(syn[i]))
        for i in range(len(df))
    ]


def slice_ts(df: pd.DataFrame, start_ts: int | None, end_ts: int | None) -> pd.DataFrame:
    """Rows with ``start_ts <= ts < end_ts`` (either bound may be None), index reset."""
    mask = np.ones(len(df), dtype=bool)
    if start_ts is not None:
        mask &= df["ts"].to_numpy() >= start_ts
    if end_ts is not None:
        mask &= df["ts"].to_numpy() < end_ts
    return df.loc[mask].reset_index(drop=True)


def _overlaps_friday_window(ts: int, start_hour: int, end_hour: int) -> bool:
    """True if the 1h bar [ts, ts+3600) overlaps Friday ``start_hour``:00-``end_hour``:00 New York time."""
    for probe in (ts, ts + H1 - 1):  # local dates touched by the bar
        day = datetime.fromtimestamp(probe, _NY).date()
        if day.weekday() != 4:  # Friday
            continue
        w_start = int(datetime(day.year, day.month, day.day, start_hour, tzinfo=_NY).timestamp())
        w_end = int(datetime(day.year, day.month, day.day, end_hour, tzinfo=_NY).timestamp())
        if ts < w_end and ts + H1 > w_start:
            return True
    return False


def futures_closed(ts: int) -> bool:
    """CFM weekly halt: the bar [ts, ts+3600) overlaps Fri 17:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, 17, 18)


def entry_blackout(ts: int) -> bool:
    """No NEW entries decided on / filled in this bar: overlaps Fri 16:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, 16, 18)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/data/test_candles.py -v`
Expected: PASS — 25 passed.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/data/candles.py tests/data/test_candles.py
git commit -m "feat: candle utilities (integrity, gap fill, 4h aggregation, wick clip, futures calendar)"
```

---

### Task 4: ATR and EMA indicators

**Files:**
- Create: `src/ewbot/indicators.py`
- Test: `tests/test_indicators.py`

**Interfaces:**
- Consumes: numpy only.
- Produces: `ATR` (streaming Wilder), `atr_series`, `ema_series` as in the contract. Details fixed here:
  - First bar's true range is `high - low`; the first ATR value is the SMA of the first `n` TRs (returned on the `n`-th `update`), later values use `ATR = (ATR_prev*(n-1) + TR)/n`.
  - `atr_series` is implemented by running the streaming `ATR`, so it is bit-identical to it and prefix-stable (no look-ahead).
  - `ema_series`: `alpha = 2/(n+1)`, seeded with `x[0]` (no NaN warm-up) — equals pandas `ewm(span=n, adjust=False).mean()`. Callers that need a warm-up (e.g. EMA 50/200 cross) skip the first `slow` bars themselves.

- [ ] **Step 1: Write the failing test**

`tests/test_indicators.py`:
```python
import numpy as np
import pandas as pd
import pytest

from ewbot.indicators import ATR, atr_series, ema_series

# Hand-computed fixture, n = 3 (bars: high, low, close)
HIGH = np.array([10.0, 12.0, 13.0, 15.0, 18.0])
LOW = np.array([8.0, 9.0, 11.0, 12.0, 16.0])
CLOSE = np.array([9.0, 11.0, 12.0, 14.0, 17.0])
# TR_0 = 10-8 = 2                       (no previous close)
# TR_1 = max(3, |12-9|, |9-9|)  = 3
# TR_2 = max(2, |13-11|, |11-11|) = 2
# ATR_2 = (2+3+2)/3 = 7/3                 (SMA seed)
# TR_3 = max(3, |15-12|, |12-12|) = 3     ATR_3 = (7/3*2 + 3)/3 = 23/9
# TR_4 = max(2, |18-14|, |16-14|) = 4     ATR_4 = (23/9*2 + 4)/3 = 82/27   (gap up: |H-prev close| wins)
EXPECTED = [None, None, 7 / 3, 23 / 9, 82 / 27]


def test_atr_streaming_hand_computed():
    atr = ATR(3)
    assert atr.value is None
    got = [atr.update(h, l, c) for h, l, c in zip(HIGH, LOW, CLOSE)]
    assert got[0] is None and got[1] is None
    assert got[2] == pytest.approx(7 / 3)
    assert got[3] == pytest.approx(23 / 9)
    assert got[4] == pytest.approx(82 / 27)
    assert atr.value == got[4]


def test_atr_series_hand_computed_has_nan_warmup():
    out = atr_series(HIGH, LOW, CLOSE, n=3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [7 / 3, 23 / 9, 82 / 27])


def test_atr_series_equals_streaming_exactly_on_random_walk():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 500))
    high = close + rng.uniform(0.1, 2.0, 500)
    low = close - rng.uniform(0.1, 2.0, 500)
    series = atr_series(high, low, close, n=14)
    atr = ATR(14)
    stream = [atr.update(h, l, c) for h, l, c in zip(high, low, close)]
    assert np.isnan(series[:13]).all() and not np.isnan(series[13:]).any()
    assert all(s is None for s in stream[:13])
    np.testing.assert_array_equal(series[13:], np.array(stream[13:]))  # bit-identical


def test_atr_prefix_property_no_lookahead():
    rng = np.random.default_rng(3)
    close = 50 + np.cumsum(rng.normal(0, 1, 120))
    high, low = close + 1.0, close - 1.0
    full = atr_series(high, low, close, 14)
    cut = atr_series(high[:80], low[:80], close[:80], 14)
    np.testing.assert_array_equal(full[:80], cut)


def test_atr_short_input_all_nan_and_empty():
    out = atr_series(HIGH[:2], LOW[:2], CLOSE[:2], n=3)
    assert np.isnan(out).all()
    assert atr_series(np.array([]), np.array([]), np.array([]), n=3).shape == (0,)


def test_atr_rejects_bad_args():
    with pytest.raises(ValueError):
        ATR(0)
    with pytest.raises(ValueError):
        atr_series(HIGH, LOW[:3], CLOSE, n=3)


def test_ema_series_hand_computed():
    # n = 3 -> alpha = 0.5, seeded with x[0]
    # e0 = 1; e1 = .5*2 + .5*1 = 1.5; e2 = .5*3 + .5*1.5 = 2.25; e3 = .5*4 + .5*2.25 = 3.125
    out = ema_series(np.array([1.0, 2.0, 3.0, 4.0]), 3)
    np.testing.assert_allclose(out, [1.0, 1.5, 2.25, 3.125])


def test_ema_series_matches_pandas_ewm_and_edge_cases():
    rng = np.random.default_rng(1)
    x = 100 + np.cumsum(rng.normal(0, 1, 300))
    for n in (1, 5, 50, 200):
        expected = pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()
        np.testing.assert_allclose(ema_series(x, n), expected, rtol=1e-12)
    assert ema_series(np.array([]), 5).shape == (0,)
    np.testing.assert_array_equal(ema_series(np.array([2.0, 5.0]), 1), [2.0, 5.0])  # n=1 -> alpha=1
    with pytest.raises(ValueError):
        ema_series(x, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_indicators.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'ewbot.indicators'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/indicators.py`:
```python
"""Streaming-friendly indicators: Wilder ATR and EMA."""
from __future__ import annotations

import numpy as np


class ATR:
    """Wilder ATR (RMA of true range). First value = SMA of the first ``n`` true ranges.

    The first bar's true range is ``high - low`` (no previous close). Afterwards
    ``TR = max(high - low, |high - prev_close|, |low - prev_close|)`` and
    ``ATR_t = (ATR_{t-1} * (n - 1) + TR_t) / n``.
    """

    def __init__(self, n: int = 14) -> None:
        if n < 1:
            raise ValueError("n must be >= 1")
        self._n = n
        self._prev_close: float | None = None
        self._count = 0
        self._sum = 0.0
        self._value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self._value is None:
            self._count += 1
            self._sum += tr
            if self._count == self._n:
                self._value = self._sum / self._n
        else:
            self._value = (self._value * (self._n - 1) + tr) / self._n
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


def atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """ATR for every bar; NaN until ``n`` bars have been seen. Identical to streaming ``ATR``."""
    h = np.asarray(high, dtype=np.float64)
    lo = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    if not (h.shape == lo.shape == c.shape):
        raise ValueError("high/low/close must have the same shape")
    out = np.full(h.shape[0], np.nan, dtype=np.float64)
    atr = ATR(n)
    for i in range(h.shape[0]):
        v = atr.update(float(h[i]), float(lo[i]), float(c[i]))
        if v is not None:
            out[i] = v
    return out


def ema_series(x: np.ndarray, n: int) -> np.ndarray:
    """EMA with alpha = 2/(n+1), seeded with ``x[0]`` (same as pandas ``ewm(span=n, adjust=False)``)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    arr = np.asarray(x, dtype=np.float64)
    out = np.empty_like(arr)
    if arr.size == 0:
        return out
    alpha = 2.0 / (n + 1)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_indicators.py -v` — Expected: PASS, 8 passed.
Then the whole suite: `uv run pytest -q` — Expected: 57 passed (14 + 10 + 25 + 8).

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/indicators.py tests/test_indicators.py
git commit -m "feat: Wilder ATR (streaming + series) and EMA"
```


---

## Part B1 — Wave Engine

Tasks T5–T10 build `ewbot.waves`: 4h ZigZag → count enumeration with hard rules → Fib scoring → streaming `WaveEngine` → synthetic and no-lookahead verification. Spec section 4 is normative. Every code block below was run against stand-ins for the Part A modules (`Bar/H1/H4`, `WaveParams`, `ATR`, `aggregate_4h`, `to_bars`). The full B1 suite (123 tests) passed under both ATR first-bar conventions: with bar 0's `high-low` counted as a TR, and with it skipped.

### B1 conventions other parts rely on

- **u-space.** Rules and ratios are computed on `u = d * price`. In u-space every pattern is bullish: IMP is `S(low) P1 P2 P3 P4 P5`, ABC is `X(low) A(high) B(low) C(high)`. For ABC, `d` is the direction of wave A, so `ABC, d=-1` means A down, B up, C down (bull C_END).
- **Leg parity.** Every count in `WaveState.counts` has `c.leg_dir == ws.leg_dir`.
- **Before the ZigZag initialises,** `WaveState.E_live == 0.0` and `E_live_ts == -1`. No NaN is used, so states compare with `==`. Counts are computed even while `ready` is False, so strategies must check `ws.ready`.
- **Size of `WaveState.counts`.** It holds every deduped group, because S_rev is computed over all of them. Consumers slice `counts[:params.top_k]` for snapshots.
- **`probs`.** They are `softmax(q'/T)` fixed at the 4h close and renormalised over the survivors. `probs == ()` and `s_rev == 0.0` when no counts are alive.
- **Invalidation kill.** A count dies if a closed 1h bar goes strictly beyond its level (`low < lo` or `high > hi`). On the leg side the level is compared with `E_live`, on the opposite side with the bar's low or high. Dead keys are remembered, and any later count that extends a dead key's labeled points (same pattern and `d`) stays dead. Without this, a wick through P2 or P4 that never became a pivot would be forgotten at the next 4h re-enumeration. A count that only becomes enumerable later is checked at that 4h close against every closed 1h bar after its last labeled point, so a wick that happened before it existed still kills it (and its key joins the dead list).
- **`ws.lineages` covers lineage kills (C1).** It contains every alive raw count's lineage (before dedupe). It also contains completed patterns (IMP `S..P5`, ABC `X..C`, all confirmed, every hard rule held, end still the extreme). Finishing W5 or C is not a hard-rule violation, so a W5_REV or C_END trade must not be lineage-killed when its pattern completes. Known limit: a lineage also disappears when its S falls out of the 12-pivot window.
- **Wick clipping (spec 4.1) is not done inside `WaveEngine`/`ZigZag`.** The caller (C1 `TradingEngine`) feeds bars already clipped with `clip_wicks(df, atr_series(...), wave.wick_clip_atr1h)` and aggregates 4h bars from the clipped 1h bars. The broker keeps the raw bars.
- **Feeding contract.** On the 1h bar whose `(ts + H1) % H4 == 0` closes a complete UTC-aligned bucket, pass `bar4h` (the aggregate) and `sub_bars` (the four 1h bars, last one == `bar1h`). Otherwise pass `None, None`; any other `bar4h` raises `ValueError`. `tests/synth.feed` is the reference streaming implementation and is tested equal to the `aggregate_4h` batch path.
- **Tie-break.** The spec's four keys (q, #points, S.i, pivot indices) leave exact ties: IMP W2/W3 and ABC B/C on the same points score the same whenever both `g_start` values are 1 and the ratio lies in both zones. Those ties go to IMP. This decides between setup ① (W3 entry) and setup ④ (C_END) when the evidence is identical; ABC wins only on strictly better evidence (flats, shallow B, context).

### Performance strategy

- Enumeration runs only on 4h closes. The 1h path updates `E_live`, kills by level (O(1) fast path through `max(lo)` and `min(hi)`), renormalises, and recomputes S_rev. The 4h-close 1h-history check costs one `bisect` per distinct last labeled point (monotonic stacks of closed 1h bars).
- The confirmed-point part of each count is a cached "skeleton": point choice, extreme constraints, static hard rules, `g_start` and completed-wave scores, keyed on the pivot window. A 4h close without a new pivot only re-checks E and recomputes the in-progress term. A new pivot rebuilds the skeletons, which costs under 500 DFS nodes in practice (bounded below 10k and property-tested).
- Measured in pure Python on 80,000 hourly GBM bars: about 1.2–1.4 s with the cache and about 5 s without it, against a 3-minute budget. `tests/waves/test_engine_perf.py` asserts this.

---

### Task 5: ZigZag (close-confirmed ATR ZigZag, spec 4.1)

**Files:**
- Create: `src/ewbot/waves/__init__.py`
- Create: `src/ewbot/waves/zigzag.py`
- Create if absent: `tests/__init__.py` and `tests/waves/__init__.py` (both empty, so tests can `from tests.synth import ...`)
- Test: `tests/waves/test_zigzag.py`

**Interfaces:**
- Consumes: `ewbot.types.Bar, H1, H4`, `ewbot.indicators.ATR` (`update(h, l, c) -> float | None`)
- Produces: `Pivot(i, sub_ts, price, kind, confirmed_at)` and `ZigZag(k, atr_len)` with `.update(i, bar, sub_bars) -> Pivot | None`, `.pivots`, `.leg_dir`, `.E`, `.E_i`, `.E_sub_ts`, `.atr`. Used by T8/T9. (The 1h trigger in B2 needs no ZigZag; `sub_bars=None` still works for plain single-timeframe use and in tests.)
- Semantics:
  - Extremes are taken on high/low, strictly beyond, so the first occurrence wins ties.
  - A pivot is confirmed when `leg_dir * (E - close) >= k * ATR`, with the ATR including the current bar. At most one pivot is confirmed per bar.
  - Each bar is processed as "atoms": its 1h sub-bars if given, else the bar itself. On a reversal, the next leg's E comes from the close of the atom that made the old extreme and the full high/low of the later atoms. This is spec 4.1's same-4h-bar rule, applied to every reversal.
  - Initialisation: once ATR exists and a close is `k*ATR` away from the running highest high or lowest low, the more recent of those two extremes becomes the first pivot. If both are the same atom, the ZigZag waits, which keeps mirror symmetry.

- [ ] **Step 1: Write the failing test**

Create empty `tests/__init__.py` (only if it does not exist) and empty `tests/waves/__init__.py`, then:

```python
from __future__ import annotations

import pytest

from ewbot.types import H1, H4, Bar
from ewbot.waves.zigzag import Pivot, ZigZag

T0 = 1_700_006_400  # 2023-11-15 00:00 UTC, multiple of H4


def _walk_bars(closes: list[float], first_open: float, tf: int = H1) -> list[Bar]:
    """open = previous close, wick 0.5 beyond the body on both sides -> TR == 2 for 1-point moves."""
    out, o = [], first_open
    for k, c in enumerate(closes):
        out.append(Bar(T0 + k * tf, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1.0))
        o = c
    return out


CLOSES = [100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100, 101, 102, 103, 104]


def test_close_confirmed_pivots_and_constant_atr() -> None:
    zz = ZigZag(k=2.0, atr_len=3)          # ATR == 2.0 -> thr == 4.0
    bars = _walk_bars([float(c) for c in CLOSES], first_open=99.0)
    got = {}
    for i, b in enumerate(bars):
        p = zz.update(i, b)
        if p is not None:
            got[i] = p
    assert zz.atr == pytest.approx(2.0)
    # bar 3: close 103 - lowest low 98.5 = 4.5 >= 4 -> first pivot = low of bar 0
    # bar 9: E 105.5 (bar 5; bar 6 only TIES the high so E stays on bar 5) - close 101 = 4.5 >= 4
    # bar 14: close 104 - E 99.5 (bar 10) = 4.5 >= 4
    assert got == {
        3: Pivot(0, T0, 98.5, -1, 3),
        9: Pivot(5, T0 + 5 * H1, 105.5, 1, 9),
        14: Pivot(10, T0 + 10 * H1, 99.5, -1, 14),
    }
    assert zz.pivots == [got[3], got[9], got[14]]
    # new up leg after bar 14: E = max(close of the extreme bar 10 = 100, highs of bars 11..14) = 104.5
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (1, 104.5, 14, T0 + 14 * H1)


def test_no_pivot_before_atr_is_ready() -> None:
    zz = ZigZag(k=0.1, atr_len=5)
    bars = _walk_bars([100.0, 150.0, 90.0, 160.0], first_open=100.0)
    assert [zz.update(i, b) for i, b in enumerate(bars)] == [None, None, None, None]
    assert zz.leg_dir == 0 and zz.atr is None and zz.pivots == []


def _bar(ts: int, o: float, h: float, lo: float, c: float) -> Bar:
    return Bar(ts, o, h, lo, c, 1.0)


def _prefix_4h(zz: ZigZag) -> None:
    # four 4h bars with TR == 4 -> ATR(3) == 4 by bar 3 whether or not bar 0's high-low counts as a TR;
    # k = 1 -> thr = 4. Bar 2 closes 3.5 from both extremes, so the first pivot appears on bar 3.
    zz.update(0, _bar(T0, 100, 102, 98, 101))
    zz.update(1, _bar(T0 + H4, 101, 104, 100, 103))
    assert zz.update(2, _bar(T0 + 2 * H4, 103, 105, 101, 101.5)) is None
    p = zz.update(3, _bar(T0 + 3 * H4, 101.5, 105.5, 101.5, 105))
    # close 105 - lowest low 98 = 7 >= 4 -> pivot low at bar 0; up leg E = max(101 close, 104, 105, 105.5)
    assert p == Pivot(0, T0, 98.0, -1, 3)
    assert (zz.leg_dir, zz.E, zz.E_i, zz.atr) == (1, 105.5, 3, 4.0)


def test_same_4h_bar_extreme_and_reversal_uses_close_of_extreme_1h_bar() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    subs = [
        _bar(t4, 105, 120, 100, 118),            # extreme 1h bar: high 120, its low 100 has unknown order
        _bar(t4 + H1, 118, 118.5, 112, 113),
        _bar(t4 + 2 * H1, 113, 113.5, 109, 110),
        _bar(t4 + 3 * H1, 110, 110.5, 108, 109),
    ]
    p = zz.update(4, _bar(t4, 105, 120, 100, 109), subs)
    # TR = max(20, |120-105|, |100-105|) = 20 -> ATR = (2*4 + 20)/3 = 9.333; 120 - 109 = 11 >= 9.333
    assert zz.atr == pytest.approx(28.0 / 3.0)
    assert p == Pivot(4, t4, 120.0, 1, 4)
    # new down leg: min(close 118 of the extreme 1h bar, lows 112, 109, 108) = 108 -- NOT the 100 low
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (-1, 108.0, 4, t4 + 3 * H1)


def test_same_bar_without_sub_bars_seeds_next_leg_with_bar_close() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    p = zz.update(4, _bar(t4, 105, 120, 100, 109))
    assert p == Pivot(4, t4, 120.0, 1, 4)
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (-1, 109.0, 4, t4)


def test_extreme_sub_ts_is_the_1h_bar_holding_the_high() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    subs = [_bar(t4, 105, 107, 104, 106), _bar(t4 + H1, 106, 111, 105, 110),
            _bar(t4 + 2 * H1, 110, 111, 108, 109), _bar(t4 + 3 * H1, 109, 110, 107, 108)]
    assert zz.update(4, _bar(t4, 105, 111, 104, 108), subs) is None
    # 111 appears twice: the FIRST 1h bar reaching it wins
    assert (zz.E, zz.E_i, zz.E_sub_ts) == (111.0, 4, t4 + H1)


def test_mirror_prices_give_mirrored_pivots() -> None:
    up, dn = ZigZag(k=2.0, atr_len=3), ZigZag(k=2.0, atr_len=3)
    for i, b in enumerate(_walk_bars([float(c) for c in CLOSES], first_open=99.0)):
        up.update(i, b)
        dn.update(i, Bar(b.ts, 200 - b.open, 200 - b.low, 200 - b.high, 200 - b.close, 1.0))
    assert [(p.i, p.sub_ts, 200 - p.price, -p.kind, p.confirmed_at) for p in up.pivots] == [
        (p.i, p.sub_ts, p.price, p.kind, p.confirmed_at) for p in dn.pivots
    ]


def test_index_must_increase() -> None:
    zz = ZigZag(k=2.0)
    zz.update(5, _bar(T0, 1, 2, 0.5, 1.5))
    with pytest.raises(ValueError):
        zz.update(5, _bar(T0 + H1, 1, 2, 0.5, 1.5))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_zigzag.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.waves'"

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/waves/__init__.py`:
```python
"""Elliott wave engine: ZigZag -> enumeration/hard rules -> scoring -> WaveState."""
```

`src/ewbot/waves/zigzag.py`:
```python
"""Close-confirmed ATR ZigZag (spec 4.1). Used by the 4h structure engine (WaveEngine).

Rules implemented here:
- threshold thr_t = k * ATR(atr_len) computed on this ZigZag's own bars up to and including bar t;
- extremes are tracked on high/low (wicks); a pivot is confirmed only when a bar CLOSE has retraced
  >= thr from the provisional extreme E; at most one pivot is confirmed per bar;
- every bar is processed as a sequence of "atoms" (its 1h sub-bars when given, else the bar itself).
  When a reversal is confirmed, the next leg's initial E is computed from the CLOSE of the atom that
  made the old extreme plus the full high/low of the atoms after it. That is spec 4.1's same-4h-bar
  rule ("only the close of the extreme 1h bar and the later 1h bars"), applied uniformly to every bar,
  so it holds whether or not the reversal happens inside the extreme's own 4h bar;
- pivots are immutable and append-only; ``confirmed_at`` is the index of the confirming bar.
Wick clipping (spec 4.1, 6 x ATR1h) is NOT done here: callers feed already-clipped bars.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ewbot.indicators import ATR
from ewbot.types import Bar


@dataclass(frozen=True, slots=True)
class Pivot:
    i: int             # index of the bar (in this ZigZag's own bar sequence) where the extreme occurred
    sub_ts: int        # ts of the 1h bar where the extreme occurred (for a 1h ZigZag = that bar's ts)
    price: float       # extreme price (high for kind=+1, low for kind=-1)
    kind: int          # +1 high, -1 low
    confirmed_at: int  # index of the bar on whose CLOSE the pivot was confirmed (>= i)


# atom = (bar index, ts, high, low, close)
_Atom = tuple[int, int, float, float, float]


class ZigZag:
    def __init__(self, k: float, atr_len: int = 14) -> None:
        if k <= 0:
            raise ValueError("k must be > 0")
        self.k = k
        self._atr = ATR(atr_len)
        self.atr: float | None = None
        self.pivots: list[Pivot] = []
        self.leg_dir: int = 0
        self.E: float = math.nan
        self.E_i: int = -1
        self.E_sub_ts: int = -1
        # After init: atoms from the atom holding E (index 0) to the latest atom.
        # Before init: every atom seen so far.
        self._atoms: list[_Atom] = []
        self._last_i: int | None = None

    # ------------------------------------------------------------------ public
    def update(self, i: int, bar: Bar, sub_bars: Sequence[Bar] | None = None) -> Pivot | None:
        """Feed the CLOSED bar ``i``; return the pivot confirmed on its close, else None."""
        if self._last_i is not None and i <= self._last_i:
            raise ValueError(f"bar index must increase: got {i} after {self._last_i}")
        self._last_i = i
        if sub_bars:
            atoms = [(i, b.ts, b.high, b.low, b.close) for b in sub_bars]
        else:
            atoms = [(i, bar.ts, bar.high, bar.low, bar.close)]
        self.atr = self._atr.update(bar.high, bar.low, bar.close)
        if self.leg_dir == 0:
            return self._update_uninitialised(i, bar.close, atoms)
        self._extend(atoms)
        if self.atr is None:
            return None
        if self.leg_dir * (self.E - bar.close) >= self.k * self.atr:
            return self._confirm(i)
        return None

    # ----------------------------------------------------------------- private
    def _extend(self, atoms: list[_Atom]) -> None:
        d = self.leg_dir
        for a in atoms:
            ext = a[2] if d > 0 else a[3]
            if d * (ext - self.E) > 0:          # strictly beyond -> first occurrence wins ties
                self.E, self.E_i, self.E_sub_ts = ext, a[0], a[1]
                self._atoms = [a]
            else:
                self._atoms.append(a)

    def _confirm(self, i: int) -> Pivot:
        d = self.leg_dir
        piv = Pivot(self.E_i, self.E_sub_ts, self.E, d, i)
        self.pivots.append(piv)
        nd = -d
        atoms = self._atoms
        # atom 0 holds the old extreme: only its close may seed the new leg (spec 4.1 same-bar rule)
        best_k, best_v = 0, atoms[0][4]
        for k in range(1, len(atoms)):
            v = atoms[k][2] if nd > 0 else atoms[k][3]
            if nd * (v - best_v) > 0:
                best_k, best_v = k, v
        a = atoms[best_k]
        self.leg_dir = nd
        self.E, self.E_i, self.E_sub_ts = best_v, a[0], a[1]
        self._atoms = atoms[best_k:]
        return piv

    def _update_uninitialised(self, i: int, close: float, atoms: list[_Atom]) -> Pivot | None:
        self._atoms.extend(atoms)
        if self.atr is None:
            return None
        thr = self.k * self.atr
        all_atoms = self._atoms
        hi_k = lo_k = 0
        for k in range(1, len(all_atoms)):
            if all_atoms[k][2] > all_atoms[hi_k][2]:
                hi_k = k
            if all_atoms[k][3] < all_atoms[lo_k][3]:
                lo_k = k
        cond_hi = all_atoms[hi_k][2] - close >= thr     # close retraced thr below the highest high
        cond_lo = close - all_atoms[lo_k][3] >= thr     # close retraced thr above the lowest low
        if cond_hi and cond_lo:
            if hi_k == lo_k:                             # ambiguous single atom: wait (keeps mirror symmetry)
                return None
            use_hi = hi_k > lo_k                         # the more recent extreme becomes the first pivot
        elif cond_hi or cond_lo:
            use_hi = cond_hi
        else:
            return None
        k0 = hi_k if use_hi else lo_k
        a = all_atoms[k0]
        self.leg_dir = 1 if use_hi else -1
        self.E = a[2] if use_hi else a[3]
        self.E_i, self.E_sub_ts = a[0], a[1]
        self._atoms = all_atoms[k0:]
        return self._confirm(i)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_zigzag.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/waves/__init__.py src/ewbot/waves/zigzag.py tests/__init__.py tests/waves/__init__.py tests/waves/test_zigzag.py
git commit -m "feat(waves): close-confirmed ATR ZigZag with same-bar extreme rule"
```

---

### Task 6: Patterns, hard rules, invalidation levels, leg parity (spec 4.3)

**Files:**
- Create: `src/ewbot/waves/patterns.py`
- Test: `tests/waves/test_patterns.py`

**Interfaces:**
- Consumes: nothing from other parts.
- Produces (contract): `PHASES_IMP`, `PHASES_ABC`, `N_POINTS`, `Invalidation(lo, hi)`, `hard_rules_ok(pattern, d, phase, pts, E)`, `invalidation(pattern, d, phase, pts)`, `next_leg_dir(pattern, d, phase)`.
- Private, used by T7–T9: `_static_rules`, `_bounds_u`, `_e_ok_u`, `_completed_ok`, `_N_COMPLETE`, `_pattern_of`.

Rule table as implemented (bull/u-space; `n` = confirmed points; E sits at position `n`):

| Rule | Confirmed form | While in progress (rule on E / invalidation) |
|---|---|---|
| W1/W2/W3/W4/W5 direction | P1>S, P2<P1, P3>P2, P4<P3, P5>P4 (strict) | E strictly beyond the last point in the leg direction |
| H1 | P2 > S | W2: E ≥ S (dies if LOW < S) |
| H2 | P3 > P1 (only once P3 is confirmed) | W3: inval lo = P2 (opposite side, checked on bar LOW) |
| H3 | not (W3<W1 and W3<W5) (completed impulse only) | W5: if W3<W1 then E ≤ P4+W3 (dies if HIGH > P4+W3) |
| H4 | P4 > P1 | W4: E ≥ P1 |
| H5 | (completed only) P5 > P4 | W5: inval lo = P4 (opposite side) |
| A1 | B ≥ A − 1.382·\|A\| (u-space) | B: E ≥ A − 1.382·\|A\| |
| A2 | (completed only) C ≤ B + 2.618·\|A\| | C: E ≤ B + 2.618·\|A\| |

E-side rules let E touch the level. That makes `hard_rules_ok(..., E)` agree exactly with the contract's strict kill (`LOW < lo`, `HIGH > hi`). Rules between confirmed points are strict, except A1 and A2, which the spec writes as "≤".

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import pytest

from ewbot.waves.patterns import (
    N_POINTS,
    Invalidation,
    _completed_ok,
    hard_rules_ok,
    invalidation,
    next_leg_dir,
)

# Reference bull impulse: S=100 P1=120 P2=108 P3=150 P4=138
# W1=20, W2=12 (r2=0.6), W3=42 (2.1 x W1), W4=12 (r4=0.2857); H1 108>100, H2 150>120, H4 138>120
BULL = (100.0, 120.0, 108.0, 150.0, 138.0)
BEAR = tuple(200.0 - p for p in BULL)           # mirror: 100, 80, 92, 50, 62


def test_n_points() -> None:
    assert N_POINTS == {"W2": 2, "W3": 3, "W4": 4, "W5": 5, "B": 2, "C": 3}


@pytest.mark.parametrize(
    ("phase", "E"),
    [("W2", 110.0), ("W3", 130.0), ("W4", 125.0), ("W5", 160.0)],
)
def test_reference_bull_impulse_is_valid_in_every_phase(phase: str, E: float) -> None:
    pts = BULL[: N_POINTS[phase]]
    assert hard_rules_ok("IMP", 1, phase, pts, E)
    assert hard_rules_ok("IMP", -1, phase, tuple(200.0 - p for p in pts), 200.0 - E)


# ---- H1: P2 > S -------------------------------------------------------------------------------
def test_h1_confirmed_p2_below_s_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 99.0), 130.0)


def test_h1_in_progress_w2_boundary() -> None:
    assert hard_rules_ok("IMP", 1, "W2", (100.0, 120.0), 100.0)     # touching S is allowed (inval is LOW < S)
    assert not hard_rules_ok("IMP", 1, "W2", (100.0, 120.0), 99.99)
    assert not hard_rules_ok("IMP", -1, "W2", (100.0, 80.0), 100.01)  # bear: E above S


# ---- H2: P3 > P1 ------------------------------------------------------------------------------
def test_h2_confirmed_p3_not_above_p1_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W4", (100.0, 120.0, 108.0, 119.0), 112.0)
    assert not hard_rules_ok("IMP", 1, "W4", (100.0, 120.0, 108.0, 120.0), 112.0)


def test_h2_not_applied_while_w3_in_progress() -> None:
    assert hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 108.0), 119.0)   # W3 below P1 is still fine
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 108.0), 107.0)  # E must be above P2


# ---- H3: W3 never the shortest ----------------------------------------------------------------
SHORT_W3 = (100.0, 120.0, 110.0, 128.0, 122.0)  # W1=20, W3=18 < W1 -> W5 cap = 122 + 18 = 140


def test_h3_w5_cap_when_w3_shorter_than_w1() -> None:
    assert hard_rules_ok("IMP", 1, "W5", SHORT_W3, 140.0)
    assert not hard_rules_ok("IMP", 1, "W5", SHORT_W3, 140.5)


def test_h3_no_cap_when_w3_longer_than_w1() -> None:
    assert hard_rules_ok("IMP", 1, "W5", BULL, 1000.0)


def test_h3_completed_impulse() -> None:
    assert _completed_ok("IMP", 1, SHORT_W3 + (139.0,))       # W5 = 17 < W3 = 18 -> W3 not shortest
    assert not _completed_ok("IMP", 1, SHORT_W3 + (141.0,))   # W5 = 19 > W3 = 18 < W1 = 20


# ---- H4: P4 > P1 ------------------------------------------------------------------------------
def test_h4_confirmed_p4_overlapping_p1_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W5", (100.0, 120.0, 108.0, 150.0, 119.0), 160.0)


def test_h4_in_progress_w4_boundary() -> None:
    assert hard_rules_ok("IMP", 1, "W4", BULL[:4], 120.0)
    assert not hard_rules_ok("IMP", 1, "W4", BULL[:4], 119.5)


# ---- H5: W5 must stay above P4 ----------------------------------------------------------------
def test_h5_w5_end_must_be_above_p4() -> None:
    assert not hard_rules_ok("IMP", 1, "W5", BULL, 137.0)
    assert not hard_rules_ok("IMP", -1, "W5", BEAR, 63.0)


# ---- direction sanity ---------------------------------------------------------------------------
def test_w2_must_retrace_below_p1() -> None:
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 125.0), 130.0)


# ---- ABC (d = direction of A) --------------------------------------------------------------------
# d=-1: X=200 high, A_end=150 (|A| = 50), B up, C down. B cap = 150 + 1.382*50 = 219.1
def test_a1_b_in_progress_boundary() -> None:
    assert hard_rules_ok("ABC", -1, "B", (200.0, 150.0), 219.1)
    assert not hard_rules_ok("ABC", -1, "B", (200.0, 150.0), 219.2)


def test_a1_confirmed_b_too_long() -> None:
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 219.0), 170.0)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 220.0), 170.0)


def test_a2_c_in_progress_boundary() -> None:
    # B_end = 180, C floor = 180 - 2.618*50 = 49.1
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), 49.1)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), 49.0)


def test_abc_up_correction_mirror() -> None:
    assert hard_rules_ok("ABC", 1, "C", (0.0, 50.0, 20.0), 150.9)
    assert not hard_rules_ok("ABC", 1, "C", (0.0, 50.0, 20.0), 151.0)


def test_wrong_number_of_points_raises() -> None:
    with pytest.raises(ValueError):
        hard_rules_ok("IMP", 1, "W3", (100.0, 120.0), 130.0)
    with pytest.raises(ValueError):
        hard_rules_ok("ABC", 1, "W3", (100.0, 120.0, 110.0), 130.0)


# ---- invalidation levels -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "d", "phase", "pts", "expected"),
    [
        ("IMP", 1, "W2", BULL[:2], Invalidation(100.0, None)),             # H1
        ("IMP", 1, "W3", BULL[:3], Invalidation(108.0, None)),             # H2 in progress: > P2
        ("IMP", 1, "W4", BULL[:4], Invalidation(120.0, None)),             # H4: > P1
        ("IMP", 1, "W5", BULL, Invalidation(138.0, None)),                 # H5, W3 >= W1 -> no cap
        ("IMP", 1, "W5", SHORT_W3, Invalidation(122.0, 140.0)),            # H5 + H3 cap
        ("IMP", -1, "W2", BEAR[:2], Invalidation(None, 100.0)),
        ("IMP", -1, "W3", BEAR[:3], Invalidation(None, 92.0)),
        ("IMP", -1, "W4", BEAR[:4], Invalidation(None, 80.0)),
        ("IMP", -1, "W5", tuple(200 - p for p in SHORT_W3), Invalidation(60.0, 78.0)),
        ("ABC", -1, "B", (200.0, 150.0), Invalidation(None, pytest.approx(219.1))),   # A1
        ("ABC", -1, "C", (200.0, 150.0, 180.0), Invalidation(pytest.approx(49.1), None)),  # A2
        ("ABC", 1, "B", (100.0, 150.0), Invalidation(pytest.approx(80.9), None)),
        ("ABC", 1, "C", (100.0, 150.0, 120.0), Invalidation(None, pytest.approx(250.9))),
    ],
)
def test_invalidation_levels(pattern: str, d: int, phase: str, pts: tuple[float, ...],
                             expected: Invalidation) -> None:
    assert invalidation(pattern, d, phase, pts) == expected


def test_invalidation_matches_hard_rule_boundary_on_e_side() -> None:
    inv = invalidation("IMP", 1, "W5", SHORT_W3)
    assert hard_rules_ok("IMP", 1, "W5", SHORT_W3, inv.hi)
    assert not hard_rules_ok("IMP", 1, "W5", SHORT_W3, inv.hi + 1e-9)
    inv = invalidation("ABC", -1, "C", (200.0, 150.0, 180.0))
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), inv.lo)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), inv.lo - 1e-9)


@pytest.mark.parametrize(
    ("pattern", "d", "phase", "leg"),
    [("IMP", 1, "W2", -1), ("IMP", 1, "W3", 1), ("IMP", 1, "W4", -1), ("IMP", 1, "W5", 1),
     ("IMP", -1, "W2", 1), ("IMP", -1, "W5", -1),
     ("ABC", -1, "B", 1), ("ABC", -1, "C", -1), ("ABC", 1, "B", -1), ("ABC", 1, "C", 1)],
)
def test_next_leg_dir(pattern: str, d: int, phase: str, leg: int) -> None:
    assert next_leg_dir(pattern, d, phase) == leg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_patterns.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.waves.patterns'"

- [ ] **Step 3: Write minimal implementation**

```python
"""Elliott patterns, hard rules (spec 4.3), invalidation levels and leg parity.

Everything is evaluated in "u-space": u = d * price. In u-space every pattern looks bullish:
IMP = S(low) P1(high) P2(low) P3(high) P4(low) P5(high); ABC = X(low) A(high) B(low) C(high).
For ABC, ``d`` is the direction of wave A (d=-1: A down, B up, C down).

Positions: IMP S=0,P1=1,...,P5=5; ABC X=0,A=1,B=2,C=3. For a phase with n confirmed points the
provisional end E (the in-progress wave's end) sits at position n.

Hard rules (bull form):
  IMP  W1 dir  P1 > S          W2 dir  P2 < P1         H1  P2 > S
       W3 dir  P3 > P2         H2  P3 > P1 (only once P3 is confirmed; while W3 runs: inval P2)
       W4 dir  P4 < P3         H4  P4 > P1             W5 dir (H5) P5 > P4
       H3  W3 not the shortest of W1/W3/W5 (while W5 runs: if |W3|<|W1| then P5 <= P4+|W3|)
  ABC  A dir X < A, B dir B < A, A1 |B| <= 1.382|A|, C dir C > B, A2 |C| <= 2.618|A|
Confirmed points use strict inequalities except A1/A2 ("<=" in the spec). Rules that bound the
provisional end E use the invalidation level with "E may touch the level" so that
``hard_rules_ok(..., E)`` is True exactly when no closed bar has gone strictly beyond
``invalidation(...)`` on the E side (the engine kills on LOW < lo / HIGH > hi).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

PHASES_IMP = ("W2", "W3", "W4", "W5")
PHASES_ABC = ("B", "C")
N_POINTS = {"W2": 2, "W3": 3, "W4": 4, "W5": 5, "B": 2, "C": 3}

A1_MAX = 1.382   # |B|/|A| upper limit
A2_MAX = 2.618   # |C|/|A| upper limit
_N_COMPLETE = {"IMP": 6, "ABC": 4}   # S..P5 / X..C all confirmed (used for lineage bookkeeping only)


@dataclass(frozen=True, slots=True)
class Invalidation:
    lo: float | None   # count dies if a closed 1h bar LOW < lo
    hi: float | None   # count dies if a closed 1h bar HIGH > hi


_Pred = Callable[..., bool]


def _h3(u0: float, u1: float, u2: float, u3: float, u4: float, u5: float) -> bool:
    w1, w3, w5 = u1 - u0, u3 - u2, u5 - u4
    return not (w3 < w1 and w3 < w5)


def _a1(u0: float, u1: float, u2: float) -> bool:
    return u2 >= u1 - A1_MAX * (u1 - u0)


def _a2(u0: float, u1: float, u2: float, u3: float) -> bool:
    return u3 <= u2 + A2_MAX * (u1 - u0)


# Rules between CONFIRMED points: (positions, predicate on the u-values at those positions).
# A rule applies to a phase with n confirmed points iff max(positions) < n.
_STATIC_RULES: dict[str, tuple[tuple[tuple[int, ...], _Pred], ...]] = {
    "IMP": (
        ((0, 1), lambda a, b: b > a),            # W1 up
        ((1, 2), lambda a, b: b < a),            # W2 down
        ((0, 2), lambda a, b: b > a),            # H1  P2 > S
        ((2, 3), lambda a, b: b > a),            # W3 up
        ((1, 3), lambda a, b: b > a),            # H2  P3 > P1
        ((3, 4), lambda a, b: b < a),            # W4 down
        ((1, 4), lambda a, b: b > a),            # H4  P4 > P1
        ((4, 5), lambda a, b: b > a),            # H5  P5 > P4      (completed impulses only)
        ((0, 1, 2, 3, 4, 5), _h3),               # H3 final         (completed impulses only)
    ),
    "ABC": (
        ((0, 1), lambda a, b: b > a),            # A
        ((1, 2), lambda a, b: b < a),            # B against A
        ((0, 1, 2), _a1),                        # A1 |B| <= 1.382 |A|
        ((2, 3), lambda a, b: b > a),            # C            (completed ABC only)
        ((0, 1, 2, 3), _a2),                     # A2           (completed ABC only)
    ),
}


def _pattern_of(phase: str) -> str:
    if phase in PHASES_IMP:
        return "IMP"
    if phase in PHASES_ABC:
        return "ABC"
    raise ValueError(f"unknown phase {phase!r}")


def _static_rules(pattern: str, n: int) -> tuple[tuple[tuple[int, ...], _Pred], ...]:
    return tuple(r for r in _STATIC_RULES[pattern] if max(r[0]) < n)


def _u_leg(phase: str) -> int:
    """Direction (in u-space) of the in-progress wave of ``phase``."""
    return 1 if phase in ("W3", "W5", "C") else -1


def _bounds_u(phase: str, u: Sequence[float]) -> tuple[float | None, float | None]:
    """(lower, upper) bounds in u-space for prices while ``phase`` is in progress (spec 4.3 column 3)."""
    if phase == "W2":
        return u[0], None                                    # H1: E_live > S
    if phase == "W3":
        return u[2], None                                    # H2 in progress: price > P2
    if phase == "W4":
        return u[1], None                                    # H4: E_live > P1
    if phase == "W5":
        w1, w3 = u[1] - u[0], u[3] - u[2]
        return u[4], (u[4] + w3 if w3 < w1 else None)        # H5: > P4 ; H3: <= P4 + |W3| if |W3| < |W1|
    if phase == "B":
        return u[1] - A1_MAX * (u[1] - u[0]), None           # A1: B upper limit
    if phase == "C":
        return None, u[2] + A2_MAX * (u[1] - u[0])          # A2: C upper limit
    raise ValueError(f"unknown phase {phase!r}")


def _e_ok_u(phase: str, u_last: float, lo: float | None, hi: float | None, ue: float) -> bool:
    """Rules on the provisional end E (u-space): strict direction vs the last point + level bounds."""
    if _u_leg(phase) * (ue - u_last) <= 0:
        return False
    if lo is not None and ue < lo:
        return False
    if hi is not None and ue > hi:
        return False
    return True


def _check_static(pattern: str, n: int, u: Sequence[float]) -> bool:
    for pos, fn in _static_rules(pattern, n):
        if not fn(*(u[p] for p in pos)):
            return False
    return True


def _check_args(pattern: str, phase: str, pts: Sequence[float]) -> int:
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    n = N_POINTS[phase]
    if len(pts) != n:
        raise ValueError(f"{phase} needs {n} confirmed points, got {len(pts)}")
    return n


def hard_rules_ok(pattern: str, d: int, phase: str, pts: Sequence[float], E: float) -> bool:
    """True iff the count (pts = confirmed labeled prices in order, E = provisional end) obeys spec 4.3."""
    n = _check_args(pattern, phase, pts)
    u = [d * p for p in pts]
    if not _check_static(pattern, n, u):
        return False
    lo, hi = _bounds_u(phase, u)
    return _e_ok_u(phase, u[-1], lo, hi, d * E)


def _completed_ok(pattern: str, d: int, pts: Sequence[float]) -> bool:
    """All hard rules for a fully confirmed pattern (IMP: S..P5, ABC: X..C)."""
    n = _N_COMPLETE[pattern]
    if len(pts) != n:
        raise ValueError(f"completed {pattern} needs {n} points")
    return _check_static(pattern, n, [d * p for p in pts])


def _to_price_bounds(d: int, lo_u: float | None, hi_u: float | None) -> Invalidation:
    if d > 0:
        return Invalidation(lo_u, hi_u)
    return Invalidation(None if hi_u is None else -hi_u, None if lo_u is None else -lo_u)


def invalidation(pattern: str, d: int, phase: str, pts: Sequence[float]) -> Invalidation:
    """Price levels that kill the in-progress count (spec 4.3 'invalidation while in progress')."""
    _check_args(pattern, phase, pts)
    lo_u, hi_u = _bounds_u(phase, [d * p for p in pts])
    return _to_price_bounds(d, lo_u, hi_u)


def next_leg_dir(pattern: str, d: int, phase: str) -> int:
    """Price direction of the in-progress leg (e.g. IMP,+1,W2 -> -1; ABC,-1,B -> +1)."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    return d * _u_leg(phase)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_patterns.py -v`
Expected: PASS (45 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/waves/patterns.py tests/waves/test_patterns.py
git commit -m "feat(waves): Elliott hard rules, invalidation levels and leg parity"
```

---

### Task 7: Scoring (spec 4.4)

**Files:**
- Create: `src/ewbot/waves/scoring.py`
- Test: `tests/waves/test_scoring.py`

**Interfaces:**
- Consumes: `WaveParams` (`sigma`, `w0`), `Pivot` (T5), `N_POINTS` and `_pattern_of` (T6).
- Produces (contract): `f_pt`, `f_zone`, `f_ip`, `fit_q`, `progress_x`, `is_done`, `softmax`.
- Private, used by T8: `_g_start_at`, `_static_sums`, `_ip_term`, `_combine`. The enumerator and `fit_q` build q through the same `_combine`, so a cached q is bit-identical to `fit_q`.
- Resolved details:
  - `g_start` IMP = 0.5·[S ≤ each of the up to 4 same-kind pivots before S] + 0.5·[P1 strictly beyond the pivot immediately before S].
  - `g_start` ABC = [X ≤ (u-space) each of the up to 4 pivots before X].
  - With no prior pivot, the indicator is 0.
  - `fit_q` finds S as the most recent pivot in `ctx` with S's kind and `price == pts[0]`.
  - `done` for W5 and C also needs E strictly beyond P3 or A_end.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import math

import pytest

from ewbot.config import WaveParams
from ewbot.waves.scoring import f_ip, f_pt, f_zone, fit_q, is_done, progress_x, softmax
from ewbot.waves.zigzag import Pivot

SIG = 0.12
P = WaveParams()


def test_f_pt() -> None:
    assert f_pt(1.618, (1.618, 2.618), SIG) == 1.0
    assert f_pt(2.0, (1.618, 2.618), SIG) == pytest.approx(0.2101543176, rel=1e-9)   # nearer 1.618
    assert f_pt(2.1, (1.618, 2.618), SIG) == pytest.approx(0.1849269023, rel=1e-9)   # nearer 2.618
    assert f_pt(0.0, (1.0,), SIG) == 0.0


def test_f_zone() -> None:
    assert f_zone(0.55, 0.5, 0.618, SIG) == 1.0
    assert f_zone(0.5, 0.5, 0.618, SIG) == 1.0
    assert f_zone(0.7, 0.5, 0.618, SIG) == pytest.approx(0.5833325892, rel=1e-9)   # vs 0.618
    assert f_zone(0.45, 0.5, 0.618, SIG) == pytest.approx(0.6801473773, rel=1e-9)  # vs 0.5


def test_f_ip_penalises_only_overshoot() -> None:
    assert f_ip(0.1, 2.618, SIG) == 1.0
    assert f_ip(2.618, 2.618, SIG) == 1.0
    assert f_ip(3.0, 2.618, SIG) == pytest.approx(0.5251203347, rel=1e-9)


def test_softmax() -> None:
    p = softmax([0.80, 0.75], 0.05)
    assert p[0] == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
    assert sum(p) == pytest.approx(1.0)
    assert softmax([], 0.05) == []
    assert softmax([10.0, 10.0, 10.0], 0.05) == pytest.approx([1 / 3] * 3)


# Fib-perfect bull impulse after a falling lead-in:
# S=1000 P1=1100 (W1=100) P2=1045 (r2=.55) P3=1206.8 (W3=1.618 W1) P4=1158.26 (r4=.3) P5=1258.26 (W5=W1)
LEAD = [(0, 1400.0, 1), (4, 1250.0, -1), (8, 1350.0, 1), (12, 1150.0, -1), (16, 1250.0, 1),
        (20, 1080.0, -1), (24, 1095.0, 1)]
IMP = [(28, 1000.0, -1), (32, 1100.0, 1), (36, 1045.0, -1), (44, 1206.8, 1), (48, 1158.26, -1)]


def _ctx(rows: list[tuple[int, float, int]]) -> list[Pivot]:
    return [Pivot(i, 1_700_006_400 + i * 14_400, p, k, i + 2) for i, p, k in rows]


CTX = _ctx(LEAD + IMP)
PTS = tuple(p for _, p, _ in IMP)


@pytest.mark.parametrize(
    ("phase", "E", "q"),
    [
        ("W2", 1045.0, 2.0 / 2.5),              # g0=1, ip r2=.55<=.786 (w .5)
        ("W3", 1206.8, 3.25 / 3.75),            # g0, r2 zone, ip W3/W1=1.618<=2.618 (w .75)
        ("W4", 1158.26, 4.5 / 5.0),             # + W3 at 1.618 (w 1.5), ip r4=.3<=.5
        ("W5", 1258.26, 6.0 / 6.5),             # + r4 zone, alt=min(1,.25/.2)=1, ip W5/W1=1<=1.618
    ],
)
def test_fit_q_fib_perfect_impulse(phase: str, E: float, q: float) -> None:
    n = {"W2": 2, "W3": 3, "W4": 4, "W5": 5}[phase]
    assert fit_q("IMP", 1, phase, PTS[:n], E, CTX, P) == pytest.approx(q, abs=1e-9)


def test_fit_q_in_progress_overshoot_is_penalised_at_half_weight() -> None:
    # W3/W1 = 3.0 > 2.618: s = 0.5251203347, weight 0.75
    q = fit_q("IMP", 1, "W3", PTS[:3], 1345.0, CTX, P)
    assert q == pytest.approx((2.5 + 0.75 * 0.5251203347) / 3.75, rel=1e-9)


def test_g_start_half_when_s_is_not_the_lowest_recent_low() -> None:
    ctx = _ctx(LEAD[:5] + [(20, 990.0, -1), (24, 1095.0, 1)] + IMP)   # a lower low (990) before S
    assert fit_q("IMP", 1, "W2", PTS[:2], 1045.0, ctx, P) == pytest.approx((0.5 + 0.5 + 0.5) / 2.5)


def test_g_start_zero_without_history() -> None:
    ctx = _ctx(IMP)
    assert fit_q("IMP", 1, "W2", PTS[:2], 1045.0, ctx, P) == pytest.approx((0.0 + 0.5 + 0.5) / 2.5)


def test_fit_q_abc_after_the_impulse() -> None:
    # X = P5 = 1258.26 (above the previous 4 pivots), A -> 1158.26 (|A|=100), B -> 1220.06 (.618), C/A = 1
    ctx = _ctx(LEAD + IMP + [(60, 1258.26, 1), (66, 1158.26, -1), (70, 1220.06, 1)])
    assert fit_q("ABC", -1, "B", (1258.26, 1158.26), 1220.06, ctx, P) == pytest.approx(2.0 / 2.5)
    assert fit_q("ABC", -1, "C", (1258.26, 1158.26, 1220.06), 1120.06, ctx, P) == pytest.approx(3.25 / 3.75)


def test_progress_x_and_done_impulse() -> None:
    assert progress_x("IMP", 1, "W2", (1000.0, 1100.0), 1065.0) == pytest.approx(0.35)
    assert not is_done("IMP", 1, "W2", (1000.0, 1100.0), 1065.0)
    assert is_done("IMP", 1, "W2", (1000.0, 1100.0), 1060.0)                  # r2 = .40 >= .382
    assert not is_done("IMP", 1, "W3", PTS[:3], 1140.0)                       # W3/W1 = .95
    assert is_done("IMP", 1, "W3", PTS[:3], 1150.0)                           # 1.05 >= 1
    assert progress_x("IMP", 1, "W4", PTS[:4], 1174.44) == pytest.approx(0.2)
    assert not is_done("IMP", 1, "W4", PTS[:4], 1174.44)
    assert is_done("IMP", 1, "W4", PTS[:4], 1166.35)                          # r4 = .25 >= .236


def test_w5_done_needs_ratio_and_e_beyond_p3() -> None:
    pts = (100.0, 120.0, 105.0, 150.0, 125.0)     # W1 = 20, deep W4 (r4 = .556)
    assert progress_x("IMP", 1, "W5", pts, 140.0) == pytest.approx(0.75)
    assert not is_done("IMP", 1, "W5", pts, 140.0)    # ratio ok, but 140 < P3 = 150
    assert is_done("IMP", 1, "W5", pts, 151.0)


def test_abc_done() -> None:
    assert is_done("ABC", -1, "B", (200.0, 150.0), 170.0)          # B/A = .4
    assert not is_done("ABC", -1, "B", (200.0, 150.0), 165.0)      # .3
    assert is_done("ABC", -1, "C", (200.0, 150.0, 180.0), 140.0)   # C/A = .8, below A_end
    assert not is_done("ABC", -1, "C", (200.0, 150.0, 180.0), 145.0)   # .7
    assert not is_done("ABC", -1, "C", (200.0, 150.0, 195.0), 152.0)   # .86 but above A_end 150


def test_scores_are_mirror_symmetric() -> None:
    mctx = [Pivot(p.i, p.sub_ts, 3000.0 - p.price, -p.kind, p.confirmed_at) for p in CTX]
    q_up = fit_q("IMP", 1, "W5", PTS, 1250.0, CTX, P)
    q_dn = fit_q("IMP", -1, "W5", tuple(3000.0 - x for x in PTS), 1750.0, mctx, P)
    assert q_up == pytest.approx(q_dn, abs=1e-12)
```

Hand-checked numbers used above:
- f_pt(2.0) = exp(−ln²(2/1.618)/0.0288) = 0.21015.
- f_pt(2.1) is nearer 2.618: exp(−ln²(2.1/2.618)/0.0288) = 0.18493.
- f_zone(0.7) = exp(−ln²(0.7/0.618)/0.0288) = 0.58333.
- f_zone(0.45) = 0.68015.
- f_ip(3.0; 2.618) = 0.52512.
- On the Fib-perfect impulse with g_start = 1:
  - W2 (1 + 0.5 + 0.5)/2.5 = 0.8
  - W3 (1 + 1 + 0.75 + 0.5)/3.75 = 0.8667
  - W4 (1 + 1 + 1.5 + 0.5 + 0.5)/5 = 0.9
  - W5 (1 + 1 + 1.5 + 1 + 0.5 + 0.5 + 0.5)/6.5 = 0.9231

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_scoring.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.waves.scoring'"

- [ ] **Step 3: Write minimal implementation**

```python
"""Fib-fit scoring of counts (spec 4.4). All ratios are arithmetic price ratios computed in u-space.

Terms (w = weight). Completed waves use the target function; the in-progress wave (ending at E)
uses f_ip(x; hi) with half weight. q = (sum w*s + w0*0.5) / (sum w + w0).

  g_start 1.0  IMP: 0.5*[S lowest of the (up to) 4 same-kind pivots before S]
                    + 0.5*[P1 beyond the pivot immediately before S]
               ABC: [X beyond all of the (up to) 4 pivots before X]   (no prior pivot -> indicator 0)
  g_r2    1.0  |W2|/|W1| zone [0.5, 0.618]                in progress: f_ip(., 0.786) w 0.5
  g_w3    1.5  |W3|/|W1| points {1.618, 2.618}            in progress: f_ip(., 2.618) w 0.75
  g_r4    1.0  |W4|/|W3| zone [0.236, 0.382]              in progress: f_ip(., 0.5)   w 0.5
  g_alt   0.5  min(1, |r2 - r4| / 0.2)                    only once W4 is complete (phase W5)
  g_w5    --   (W5 is never complete in a phase)         in progress: f_ip(W5/W1, 1.618) w 0.5
  g_rB    1.0  |B|/|A| zone [0.382,0.618] U [0.9,1.236]   in progress: f_ip(., 1.382) w 0.5
  g_C     --   (C is never complete in a phase)          in progress: f_ip(C/A, 1.618) w 0.75
"""
from __future__ import annotations

import math
from typing import Sequence

from ewbot.config import WaveParams
from ewbot.waves.patterns import N_POINTS, _pattern_of
from ewbot.waves.zigzag import Pivot

ZONE_LO = {"W2": 0.382, "W3": 1.0, "W4": 0.236, "W5": 0.618, "B": 0.382, "C": 0.786}
_IP = {  # phase -> (hi, weight) of the in-progress term
    "W2": (0.786, 0.5), "W3": (2.618, 0.75), "W4": (0.5, 0.5), "W5": (1.618, 0.5),
    "B": (1.382, 0.5), "C": (1.618, 0.75),
}


def _sim(x: float, t: float, sigma: float) -> float:
    z = math.log(x / t)
    return math.exp(-(z * z) / (2.0 * sigma * sigma))


def f_pt(x: float, targets: Sequence[float], sigma: float) -> float:
    if x <= 0.0:
        return 0.0
    return max(_sim(x, t, sigma) for t in targets)


def f_zone(x: float, a: float, b: float, sigma: float) -> float:
    if x <= 0.0:
        return 0.0
    if a <= x <= b:
        return 1.0
    return _sim(x, a if x < a else b, sigma)


def f_ip(x: float, hi: float, sigma: float) -> float:
    if x <= hi:
        return 1.0
    return _sim(x, hi, sigma)


def softmax(qs: Sequence[float], T: float) -> list[float]:
    if not qs:
        return []
    m = max(qs)
    ex = [math.exp((q - m) / T) for q in qs]
    s = sum(ex)
    return [e / s for e in ex]


def _u(d: int, pts: Sequence[float]) -> list[float]:
    return [d * p for p in pts]


def progress_x(pattern: str, d: int, phase: str, pts: Sequence[float], E_live: float) -> float:
    """Ratio of the in-progress wave used for done-ness: r2, W3/W1, r4, W5/W1, B/A, C/A."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    u = _u(d, pts)
    ue = d * E_live
    if phase == "W2":
        return (u[1] - ue) / (u[1] - u[0])
    if phase == "W3":
        return (ue - u[2]) / (u[1] - u[0])
    if phase == "W4":
        return (u[3] - ue) / (u[3] - u[2])
    if phase == "W5":
        return (ue - u[4]) / (u[1] - u[0])
    if phase == "B":
        return (u[1] - ue) / (u[1] - u[0])
    return (ue - u[2]) / (u[1] - u[0])           # C


def is_done(pattern: str, d: int, phase: str, pts: Sequence[float], E_live: float) -> bool:
    """done_i = x_i >= zone_lo(phase); W5 also needs E beyond P3, C also needs E beyond A_end."""
    if progress_x(pattern, d, phase, pts, E_live) < ZONE_LO[phase]:
        return False
    ue = d * E_live
    if phase == "W5":
        return ue > d * pts[3]
    if phase == "C":
        return ue > d * pts[1]
    return True


def _static_terms(pattern: str, phase: str, u: Sequence[float], sigma: float) -> list[tuple[float, float]]:
    """(weight, score) of the COMPLETED-wave terms, in a fixed order (g_start excluded)."""
    n = N_POINTS[phase]
    out: list[tuple[float, float]] = []
    if pattern == "IMP":
        w1 = u[1] - u[0]
        if n >= 3:
            r2 = (u[1] - u[2]) / w1
            out.append((1.0, f_zone(r2, 0.5, 0.618, sigma)))
        if n >= 4:
            w3 = u[3] - u[2]
            out.append((1.5, f_pt(w3 / w1, (1.618, 2.618), sigma)))
        if n >= 5:
            r4 = (u[3] - u[4]) / (u[3] - u[2])
            out.append((1.0, f_zone(r4, 0.236, 0.382, sigma)))
            out.append((0.5, min(1.0, abs(r2 - r4) / 0.2)))
    else:
        if n >= 3:
            rb = (u[1] - u[2]) / (u[1] - u[0])
            out.append((1.0, max(f_zone(rb, 0.382, 0.618, sigma), f_zone(rb, 0.9, 1.236, sigma))))
    return out


def _ip_term(pattern: str, d: int, phase: str, pts: Sequence[float], E: float, sigma: float) -> tuple[float, float]:
    hi, w = _IP[phase]
    return w, f_ip(progress_x(pattern, d, phase, pts, E), hi, sigma)


def _g_start_at(pattern: str, d: int, pts: Sequence[float], s_pos: int, ctx: Sequence[Pivot]) -> float:
    """g_start with the start pivot located at ctx[s_pos]."""
    s = ctx[s_pos]
    us = d * s.price
    if pattern == "IMP":
        prior_same: list[Pivot] = []
        k = s_pos - 1
        while k >= 0 and len(prior_same) < 4:
            if ctx[k].kind == s.kind:
                prior_same.append(ctx[k])
            k -= 1
        a = 1.0 if prior_same and all(us <= d * p.price for p in prior_same) else 0.0
        b = 1.0 if s_pos >= 1 and d * pts[1] > d * ctx[s_pos - 1].price else 0.0
        return 0.5 * a + 0.5 * b
    prior = ctx[max(0, s_pos - 4):s_pos]
    return 1.0 if prior and all(us <= d * p.price for p in prior) else 0.0


def _locate_start(pattern: str, d: int, pts: Sequence[float], ctx: Sequence[Pivot]) -> int:
    kind = -d   # IMP S and ABC X are u-space lows -> price kind -d
    for k in range(len(ctx) - 1, -1, -1):
        if ctx[k].kind == kind and ctx[k].price == pts[0]:
            return k
    raise ValueError("start point not found in ctx")


def _combine(static_ws: float, static_w: float, ip_w: float, ip_s: float, w0: float) -> float:
    return (static_ws + ip_w * ip_s + w0 * 0.5) / (static_w + ip_w + w0)


def _static_sums(pattern: str, d: int, phase: str, pts: Sequence[float], g_start: float,
                 sigma: float) -> tuple[float, float]:
    ws, w = 1.0 * g_start, 1.0
    for wj, sj in _static_terms(pattern, phase, _u(d, pts), sigma):
        ws += wj * sj
        w += wj
    return ws, w


def fit_q(pattern: str, d: int, phase: str, pts: Sequence[float], E: float, ctx: Sequence[Pivot],
          params: WaveParams) -> float:
    """Fit q in [0, 1]. ctx = confirmed pivots up to now; S/X is located as the most recent pivot of
    the start kind whose price equals pts[0] (the enumerator passes the exact position instead)."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    g0 = _g_start_at(pattern, d, pts, _locate_start(pattern, d, pts, ctx), ctx)
    ws, w = _static_sums(pattern, d, phase, pts, g0, params.sigma)
    ip_w, ip_s = _ip_term(pattern, d, phase, pts, E, params.sigma)
    return _combine(ws, w, ip_w, ip_s, params.w0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_scoring.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/waves/scoring.py tests/waves/test_scoring.py
git commit -m "feat(waves): Fib-fit scoring, done-ness and softmax"
```

---

### Task 8: Count enumeration, dedupe, ordering, lineages (spec 4.2, 4.4)

**Files:**
- Create: `src/ewbot/waves/enumerate.py`
- Test: `tests/waves/test_enumerate.py`

**Interfaces:**
- Consumes: T5 `Pivot`, T6 rules and levels, T7 score helpers, `WaveParams` (`M`, `sigma`, `w0`).
- Produces (contract): `Count` (with `prices`, `lineage`, `instance`, `inval`, `leg_dir`) and `enumerate_counts(pivots, leg_dir, E, params) -> (deduped counts in tie-break order, frozenset of all alive lineages before dedupe incl. completed patterns)`.
- Private, used by T9: `_Enumerator` (skeleton cache, `.run(pivots, leg_dir, E) -> (raw counts, completed patterns)`, `.nodes`), `_Completed`, `_dedupe_sorted`, `_sort_key(count, q_prime)`, `_enumerate_raw`.
- Verification: a Hypothesis brute-force oracle enumerates every subsequence of random pivot windows and checks parity, the extreme constraint and hard rules against `_enumerate_raw` and the lineage set. It also checks q against `fit_q` bit-for-bit, the DFS bound (< 10k nodes), cached vs fresh results, dedupe and order.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.waves.enumerate import _enumerate_raw, _Enumerator, enumerate_counts
from ewbot.waves.patterns import (
    N_POINTS,
    PHASES_ABC,
    PHASES_IMP,
    Invalidation,
    _completed_ok,
    hard_rules_ok,
    next_leg_dir,
)
from ewbot.waves.scoring import fit_q
from ewbot.waves.zigzag import Pivot

P = WaveParams()
T0 = 1_700_006_400


def _pv(rows: list[tuple[int, float, int]]) -> list[Pivot]:
    return [Pivot(i, T0 + i * 14_400, p, k, i + 2) for i, p, k in rows]


# falling lead-in, then the Fib-perfect bull impulse S..P4 (see test_scoring.py)
LEAD = [(0, 1400.0, 1), (4, 1250.0, -1), (8, 1350.0, 1), (12, 1150.0, -1), (16, 1250.0, 1),
        (20, 1080.0, -1), (24, 1095.0, 1)]
IMP = [(28, 1000.0, -1), (32, 1100.0, 1), (36, 1045.0, -1), (44, 1206.8, 1), (48, 1158.26, -1)]
PIVOTS = _pv(LEAD + IMP)


def test_fib_perfect_impulse_top_is_w5() -> None:
    counts, lineages = enumerate_counts(PIVOTS, 1, 1258.26, P)
    top = counts[0]
    assert (top.pattern, top.d, top.phase) == ("IMP", 1, "W5")
    assert top.points == tuple(PIVOTS[7:12])
    assert top.q == pytest.approx(6.0 / 6.5, abs=1e-9)
    assert top.lineage == ("IMP", 1, 28) and top.instance == ("IMP", 1, 28, "W5")
    assert top.prices == (1000.0, 1100.0, 1045.0, 1206.8, 1158.26)
    assert top.inval == Invalidation(1158.26, None) and top.leg_dir == 1
    assert counts[1].phase == "W3" and counts[1].points == tuple(PIVOTS[7:10])
    assert ("IMP", 1, 28) in lineages


def test_parity_only_up_leg_phases() -> None:
    counts, _ = enumerate_counts(PIVOTS, 1, 1230.0, P)
    assert counts and all(c.leg_dir == 1 for c in counts)
    assert {(c.pattern, c.d, c.phase) for c in counts} <= {
        ("IMP", 1, "W3"), ("IMP", 1, "W5"), ("IMP", -1, "W2"), ("IMP", -1, "W4"),
        ("ABC", 1, "C"), ("ABC", -1, "B")}


def test_extreme_constraint_when_skipping_pivots() -> None:
    # S=1000, h=1120, l=1050, h=1100, leg down. P1 must be the 1120 high (>= skipped highs),
    # and E must undercut the skipped 1050 low.
    piv = _pv([(0, 1000.0, -1), (4, 1120.0, 1), (8, 1050.0, -1), (12, 1100.0, 1)])
    w2 = lambda cs: [c for c in cs if (c.pattern, c.d, c.phase) == ("IMP", 1, "W2")]  # noqa: E731
    counts, _ = enumerate_counts(piv, -1, 1040.0, P)
    assert [tuple(p.i for p in c.points) for c in w2(counts)] == [(0, 4)]
    # E=1060 is above the skipped 1050 low -> (0,4) illegal; (0,12) would skip the higher 1120 high
    counts, _ = enumerate_counts(piv, -1, 1060.0, P)
    assert [tuple(p.i for p in c.points) for c in w2(counts)] == [(8, 12)]


def test_lineages_include_deduped_away_counts() -> None:
    piv = PIVOTS + _pv([(60, 1258.26, 1)])
    counts, lineages = enumerate_counts(piv, -1, 1158.26, P)
    raw = _enumerate_raw(piv, -1, 1158.26, P)
    # ABC up with X=P4(1158.26): B/A = 1.0 is valid but loses its dedupe group to X=S
    assert any(c.lineage == ("ABC", 1, 48) for c in raw)
    assert not any(c.lineage == ("ABC", 1, 48) for c in counts)
    assert ("ABC", 1, 48) in lineages


def test_completed_impulse_keeps_its_lineage_alive() -> None:
    # after P5 (1258.26) price fell to 1180, bounced to 1240, now falls with E=1200: no in-progress
    # IMP+ count can start at S=1000 any more, but the completed impulse S..P5 is intact.
    piv = PIVOTS + _pv([(60, 1258.26, 1), (66, 1180.0, -1), (70, 1240.0, 1)])
    counts, lineages = enumerate_counts(piv, -1, 1200.0, P)
    assert not any(c.lineage == ("IMP", 1, 28) for c in counts)
    assert ("IMP", 1, 28) in lineages


def test_dedupe_keeps_max_q_per_group_and_sorting() -> None:
    raw = _enumerate_raw(PIVOTS, 1, 1230.0, P)
    counts, _ = enumerate_counts(PIVOTS, 1, 1230.0, P)
    groups: dict[tuple, float] = {}
    for c in raw:
        g = (c.pattern, c.d, c.phase, c.points[-1].i)
        groups[g] = max(groups.get(g, -1.0), c.q)
    assert len(counts) == len(groups)
    assert all(groups[(c.pattern, c.d, c.phase, c.points[-1].i)] == c.q for c in counts)
    keys = [(-c.q, -len(c.points), -c.points[0].i, tuple(p.i for p in c.points)) for c in counts]
    assert keys == sorted(keys)


def test_not_initialised_returns_nothing() -> None:
    assert enumerate_counts([], 0, 0.0, P) == ([], frozenset())


# ----------------------------------------------------------------------------- brute force oracle
def _extreme_ok(w: list[Pivot], seq: list[int], E: float, leg_dir: int) -> bool:
    n = len(seq) - 1                                  # seq[-1] == len(w) stands for E
    for k in range(n + 1):
        lo = seq[k - 1] if k > 0 else seq[k]
        hi = seq[k + 1] if k < n else seq[k]
        price = E if k == n else w[seq[k]].price
        kind = leg_dir if k == n else w[seq[k]].kind
        for j in range(lo + 1, hi):
            if j != seq[k] and w[j].kind == kind and kind * (w[j].price - price) > 0:
                return False
    return True


def _brute(pivots: list[Pivot], leg_dir: int, E: float, M: int) -> tuple[set, set]:
    w = pivots[-M:]
    keys, lineages = set(), set()
    for pattern, phases in (("IMP", PHASES_IMP), ("ABC", PHASES_ABC)):
        for d in (1, -1):
            for phase in phases:
                if next_leg_dir(pattern, d, phase) != leg_dir:
                    continue
                n = N_POINTS[phase]
                for combo in itertools.combinations(range(len(w)), n):
                    pts = [w[j] for j in combo]
                    if any(pts[k].kind != leg_dir * (-1) ** (n - k) for k in range(n)):
                        continue
                    if not _extreme_ok(w, list(combo) + [len(w)], E, leg_dir):
                        continue
                    if hard_rules_ok(pattern, d, phase, [p.price for p in pts], E):
                        keys.add((pattern, d, phase, tuple(p.i for p in pts)))
                        lineages.add((pattern, d, pts[0].i))
            # completed patterns: end point is a u-high and stays the extreme afterwards
            n = 6 if pattern == "IMP" else 4
            for combo in itertools.combinations(range(len(w)), n):
                pts = [w[j] for j in combo]
                if any(pts[k].kind != d * (-1) ** (n - 1 - k) for k in range(n)):
                    continue
                seq = list(combo)
                ok = all(
                    not (w[j].kind == pts[k].kind and pts[k].kind * (w[j].price - pts[k].price) > 0)
                    for k in range(n)
                    for j in range(seq[k - 1] + 1 if k > 0 else seq[k] + 1, seq[k + 1] if k < n - 1 else len(w))
                    if j != seq[k]
                )
                if ok and leg_dir == d and d * E > d * pts[-1].price:
                    ok = False
                if ok and _completed_ok(pattern, d, [p.price for p in pts]):
                    lineages.add((pattern, d, pts[0].i))
    return keys, lineages


@st.composite
def pivot_windows(draw: st.DrawFn) -> tuple[list[Pivot], int, float]:
    n = draw(st.integers(min_value=2, max_value=14))
    kind = draw(st.sampled_from([1, -1]))
    price = 1000.0
    rows = []
    for k in range(n):
        if k:
            price += kind * draw(st.floats(min_value=1.0, max_value=200.0, allow_nan=False))
        rows.append((3 * k, round(price, 4), kind))
        kind = -kind
    leg_dir = kind                                   # = -(last pivot kind)
    E = rows[-1][1] + leg_dir * draw(st.floats(min_value=0.5, max_value=300.0, allow_nan=False))
    return _pv(rows), leg_dir, round(E, 4)


@settings(max_examples=300, deadline=None)
@given(pivot_windows())
def test_enumeration_matches_brute_force(case: tuple[list[Pivot], int, float]) -> None:
    pivots, leg_dir, E = case
    en = _Enumerator(P)
    raw, _ = en.run(pivots, leg_dir, E)
    assert en.nodes < 10_000
    counts, lineages = enumerate_counts(pivots, leg_dir, E, P)
    keys, brute_lineages = _brute(pivots, leg_dir, E, P.M)
    assert {(c.pattern, c.d, c.phase, tuple(p.i for p in c.points)) for c in raw} == keys
    assert lineages == frozenset(brute_lineages)
    for c in raw:
        assert c.leg_dir == leg_dir
        # ctx ends at S so fit_q's "most recent pivot with S's price" lookup is exact even with ties
        ctx = pivots[: pivots.index(c.points[0]) + 1]
        assert c.q == fit_q(c.pattern, c.d, c.phase, c.prices, E, ctx, P)
        assert 0.0 < c.q < 1.0
    # dedupe key unique, sort order
    assert len({(c.pattern, c.d, c.phase, c.points[-1].i) for c in counts}) == len(counts)
    keys_sorted = [(-c.q, -len(c.points), -c.points[0].i, tuple(p.i for p in c.points)) for c in counts]
    assert keys_sorted == sorted(keys_sorted)


@settings(max_examples=100, deadline=None)
@given(pivot_windows(), st.lists(st.floats(min_value=0.5, max_value=300.0), min_size=1, max_size=4))
def test_cached_enumerator_equals_fresh(case: tuple[list[Pivot], int, float], moves: list[float]) -> None:
    pivots, leg_dir, E = case
    en = _Enumerator(P)
    for mv in [0.0] + moves:
        e = E + leg_dir * mv
        assert en.run(pivots, leg_dir, e) == _Enumerator(P).run(pivots, leg_dir, e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_enumerate.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.waves.enumerate'"

- [ ] **Step 3: Write minimal implementation**

```python
"""Count enumeration (spec 4.2): DFS backwards from E over the last M confirmed pivots.

- parity: only (pattern, d, phase) whose in-progress leg direction equals the ZigZag leg direction;
  labeled points alternate kinds and the last confirmed one has kind -leg_dir;
- pivot skipping with the extreme constraint: a labeled high is >= every pivot high strictly between
  its labeled neighbours, a labeled low <= every pivot low between them; S only has its right side
  ([S, P1]) and E only its left side (E vs the pivots after the last labeled point);
- hard rules (patterns.py) are checked incrementally: a rule is tested as soon as the DFS has assigned
  all of its positions, so dead prefixes are pruned (rules on E are applied when E is attached);
- dedupe: per (pattern, d, phase, .i of the last confirmed labeled point) keep the max-q count;
- order: q desc -> #points desc -> S.i desc -> tuple of pivot .i ascending -> IMP before ABC.

Incremental strategy: the confirmed-point part of a count (point choice, extreme constraints between
confirmed points, static hard rules, g_start and completed-wave scores) is a "skeleton" cached per
pivot window. A new E only re-runs the E checks and the in-progress score term, so a 4h close without
a new pivot costs O(#skeletons); a new pivot rebuilds the skeletons (< 10k DFS nodes).

Lineages: (pattern, d, S.i). A lineage is alive if any valid in-progress count has it OR the pattern
has completed (IMP S..P5 / ABC X..C all confirmed with every hard rule, end point still the extreme:
no later same-kind pivot and no E beyond it). Finishing W5/C is not a hard-rule violation, so the
lineage-kill exit must not fire just because the pattern completed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from ewbot.config import WaveParams
from ewbot.waves.patterns import (
    _N_COMPLETE,
    N_POINTS,
    PHASES_ABC,
    PHASES_IMP,
    Invalidation,
    _bounds_u,
    _e_ok_u,
    _static_rules,
    invalidation,
    next_leg_dir,
)
from ewbot.waves.scoring import _combine, _g_start_at, _ip_term, _static_sums
from ewbot.waves.zigzag import Pivot

_COMBOS: tuple[tuple[str, int, str], ...] = tuple(
    (pat, d, ph)
    for pat, phases in (("IMP", PHASES_IMP), ("ABC", PHASES_ABC))
    for d in (1, -1)
    for ph in phases
)
_CTX_BACK = 8                      # g_start looks at most 8 pivots before S (4 same-kind pivots)
_PATTERN_RANK = {"IMP": 0, "ABC": 1}
_Rule = tuple[tuple[int, ...], Callable[..., bool]]


@dataclass(frozen=True, slots=True)
class Count:
    pattern: str
    d: int
    phase: str
    points: tuple[Pivot, ...]     # confirmed labeled points, len == N_POINTS[phase]
    q: float                      # fit at the 4h close it was scored on

    @property
    def prices(self) -> tuple[float, ...]:
        return tuple(p.price for p in self.points)

    @property
    def lineage(self) -> tuple[str, int, int]:
        return (self.pattern, self.d, self.points[0].i)

    @property
    def instance(self) -> tuple[str, int, int, str]:
        return (self.pattern, self.d, self.points[0].i, self.phase)

    @property
    def inval(self) -> Invalidation:
        return invalidation(self.pattern, self.d, self.phase, self.prices)

    @property
    def leg_dir(self) -> int:
        return next_leg_dir(self.pattern, self.d, self.phase)


@dataclass(frozen=True, slots=True)
class _Skeleton:
    pattern: str
    d: int
    phase: str
    points: tuple[Pivot, ...]
    prices: tuple[float, ...]
    ws: float                 # sum w*s over g_start + completed-wave terms
    w: float                  # sum w over the same terms
    e_floor: float            # leg_dir * E must be >= this (E vs skipped E-kind pivots)
    u_last: float             # d * price of the last confirmed point
    lo_u: float | None        # E bounds in u-space (patterns._bounds_u)
    hi_u: float | None


@dataclass(frozen=True, slots=True)
class _Completed:
    lineage: tuple[str, int, int]
    key: tuple[int, ...]      # .i of every point (engine dead-set key)
    inval: Invalidation       # dies if price goes beyond the pattern end
    end_u: float              # d * end price


def _sort_key(c: Count, q: float | None = None) -> tuple:
    """Spec 4.4 order with q (or q' if given). Exact ties left after the spec keys (IMP W2/W3 and
    ABC B/C on the same points score identically when both zones agree) go to IMP; the key is total."""
    return (-(c.q if q is None else q), -len(c.points), -c.points[0].i,
            tuple(p.i for p in c.points), _PATTERN_RANK[c.pattern], c.phase)


def _dedupe_sorted(counts: Sequence[Count]) -> list[Count]:
    best: dict[tuple[str, int, str, int], Count] = {}
    for c in counts:
        g = (c.pattern, c.d, c.phase, c.points[-1].i)
        cur = best.get(g)
        if cur is None or _sort_key(c) < _sort_key(cur):
            best[g] = c
    return sorted(best.values(), key=_sort_key)


class _Window:
    """The last-M pivots with O(1) 'max high / min low strictly between two positions' lookups."""

    def __init__(self, pivots: Sequence[Pivot], M: int) -> None:
        self.base = max(0, len(pivots) - M)
        self.w = list(pivots[self.base:])
        m = self.m = len(self.w)
        self.hi_btw = [[-math.inf] * (m + 1) for _ in range(m + 1)]
        self.lo_btw = [[math.inf] * (m + 1) for _ in range(m + 1)]
        for a in range(m):
            mh, ml = -math.inf, math.inf
            for b in range(a + 1, m + 1):
                self.hi_btw[a][b], self.lo_btw[a][b] = mh, ml
                if b < m:
                    if self.w[b].kind > 0:
                        mh = max(mh, self.w[b].price)
                    else:
                        ml = min(ml, self.w[b].price)

    def extreme_ok(self, j: int, a: int, b: int) -> bool:
        """Pivot at position j is >= every high (kind +1) / <= every low (kind -1) strictly in (a, b)."""
        pv = self.w[j]
        return pv.price >= self.hi_btw[a][b] if pv.kind > 0 else pv.price <= self.lo_btw[a][b]


class _Enumerator:
    """Skeleton cache keyed on the pivot window; ``run`` is a pure function of its arguments."""

    def __init__(self, params: WaveParams) -> None:
        self.params = params
        self._key: tuple | None = None
        self._skels: list[_Skeleton] = []
        self._done: list[_Completed] = []
        self.nodes = 0                      # DFS nodes visited by the last skeleton build

    def run(self, pivots: Sequence[Pivot], leg_dir: int, E: float) -> tuple[list[Count], list[_Completed]]:
        """(all valid in-progress counts before dedupe, completed patterns still intact)."""
        if leg_dir == 0 or not pivots:
            return [], []
        key = (leg_dir, len(pivots), tuple(pivots[-(self.params.M + _CTX_BACK):]))
        if key != self._key:
            self._build(pivots, leg_dir)
            self._key = key
        p = self.params
        le = leg_dir * E
        counts: list[Count] = []
        for sk in self._skels:
            if le < sk.e_floor or not _e_ok_u(sk.phase, sk.u_last, sk.lo_u, sk.hi_u, sk.d * E):
                continue
            ip_w, ip_s = _ip_term(sk.pattern, sk.d, sk.phase, sk.prices, E, p.sigma)
            counts.append(Count(sk.pattern, sk.d, sk.phase, sk.points,
                                _combine(sk.ws, sk.w, ip_w, ip_s, p.w0)))
        done = [c for c in self._done if not (leg_dir == c.lineage[1] and c.lineage[1] * E > c.end_u)]
        return counts, done

    # ---------------------------------------------------------------- building
    def _dfs(self, win: _Window, kinds: list[int], rules_at: list[tuple[_Rule, ...]], d: int,
             k: int, right: int, js: list[int], out: list[tuple[int, ...]]) -> None:
        """Assign point k (kind kinds[k]) left of window position ``right``, backwards."""
        w = win.w
        for j in range(right - 1, k - 1, -1):
            if w[j].kind != kinds[k]:
                continue
            self.nodes += 1
            if not win.extreme_ok(j, j, right):                          # point k vs (j, right)
                continue
            if right < win.m and not win.extreme_ok(right, j, right):    # right neighbour's left side
                continue
            js[k] = j
            if any(not fn(*(d * w[js[p]].price for p in pos)) for pos, fn in rules_at[k]):
                continue
            if k > 0:
                self._dfs(win, kinds, rules_at, d, k - 1, j, js, out)
            else:
                out.append(tuple(js))

    def _points(self, win: _Window, pattern: str, d: int, n: int, last_kind: int) -> list[tuple[int, ...]]:
        rules = _static_rules(pattern, n)
        rules_at = [tuple(r for r in rules if min(r[0]) == k) for k in range(n)]
        kinds = [last_kind * (-1) ** (n - 1 - k) for k in range(n)]
        out: list[tuple[int, ...]] = []
        if n <= win.m:
            self._dfs(win, kinds, rules_at, d, n - 1, win.m, [0] * n, out)
        return out

    def _build(self, pivots: Sequence[Pivot], leg_dir: int) -> None:
        self.nodes = 0
        win = _Window(pivots, self.params.M)
        sigma = self.params.sigma
        skels: list[_Skeleton] = []
        for pattern, d, phase in _COMBOS:
            if next_leg_dir(pattern, d, phase) != leg_dir:
                continue
            n = N_POINTS[phase]
            for js in self._points(win, pattern, d, n, -leg_dir):
                pts = tuple(win.w[j] for j in js)
                prices = tuple(p.price for p in pts)
                g0 = _g_start_at(pattern, d, prices, win.base + js[0], pivots)
                ws, wsum = _static_sums(pattern, d, phase, prices, g0, sigma)
                e_floor = win.hi_btw[js[-1]][win.m] if leg_dir > 0 else -win.lo_btw[js[-1]][win.m]
                lo_u, hi_u = _bounds_u(phase, [d * x for x in prices])
                skels.append(_Skeleton(pattern, d, phase, pts, prices, ws, wsum, e_floor,
                                       d * prices[-1], lo_u, hi_u))
        done: list[_Completed] = []
        for pattern in ("IMP", "ABC"):
            n = _N_COMPLETE[pattern]
            for d in (1, -1):
                for js in self._points(win, pattern, d, n, d):       # end point is a u-high
                    pts = [win.w[j] for j in js]
                    end = pts[-1].price
                    inval = Invalidation(None, end) if d > 0 else Invalidation(end, None)
                    done.append(_Completed((pattern, d, pts[0].i), tuple(p.i for p in pts), inval, d * end))
        self._skels, self._done = skels, done


def _enumerate_raw(pivots: Sequence[Pivot], leg_dir: int, E: float, params: WaveParams) -> list[Count]:
    """All valid in-progress counts before dedupe (fresh enumerator, no cache)."""
    return _Enumerator(params).run(pivots, leg_dir, E)[0]


def enumerate_counts(pivots: Sequence[Pivot], leg_dir: int, E: float,
                     params: WaveParams) -> tuple[list[Count], frozenset[tuple[str, int, int]]]:
    """(deduped counts in spec 4.4 tie-break order, every alive lineage before dedupe)."""
    raw, done = _Enumerator(params).run(pivots, leg_dir, E)
    lineages = frozenset(c.lineage for c in raw) | frozenset(c.lineage for c in done)
    return _dedupe_sorted(raw), lineages
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_enumerate.py -v`
Expected: PASS (9 passed; the brute-force property runs 300 examples in under 1 s)

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/waves/enumerate.py tests/waves/test_enumerate.py
git commit -m "feat(waves): backward DFS count enumeration with skeleton cache, dedupe and lineages"
```

---

### Task 9: WaveEngine (streaming state per closed 1h bar)

**Files:**
- Create: `src/ewbot/waves/engine.py`
- Test: `tests/waves/test_engine.py`

**Interfaces:**
- Consumes: T5–T8, `WaveParams` (`K4`, `atr_len`, `h`, `T`, `warmup_4h`), `Bar`.
- Produces (contract): `WaveState` (all 17 fields in contract order) and `WaveEngine(params).on_1h_close(bar1h, bar4h, sub_bars) -> WaveState`.
- Consumers:
  - B2 strategies read `ready`, `top`, `q_top`, `s_rev`, `E_live`, `E_live_ts`, `leg_start`, `atr4h`, `counts` and `probs`.
  - C1 uses `lineages` (lineage kill), `new_pivot_4h` (trailing stop) and `top` (cooldown clear).
- Per-step order (spec 3, step 2–3):
  1. On a 4h close: ZigZag update, then `E_live := E`, then enumerate, drop dead keys, drop every count or completed pattern that a closed 1h bar after its last labeled point already went beyond (`low < lo` or `high > hi`; its key joins the dead list), dedupe, hysteresis `q' = q + h` for the previous step's top lineage when the points are a prefix extension, sort by `q'`, and compute `p = softmax(q'/T)`.
  2. Otherwise: update E_live from the closed 1h bar (strictly beyond).
  3. Kill by invalidation.
  4. Renormalise the probabilities.
  5. Recompute S_rev with E_live.
- `ready = (i4h + 1 >= warmup_4h) and zigzag initialised`. An `assert` guards that no pivot is confirmed after the current `i4h`.
- 1h history check (step 1): a count can first become enumerable after a wick that would have killed it (E crosses a skipped pivot, or a new pivot opens new combinations), so the dead list alone is not enough. The engine keeps two monotonic stacks of closed 1h bars (lows strictly increasing, highs strictly decreasing) from which the min low / max high after any `ts` is one `bisect` away; bars before the window's first pivot are dropped on every new pivot.
- Input checks: `bar4h` must be the UTC 4h bucket that `bar1h` closes (`(bar1h.ts + H1) % H4 == 0` and `bar4h.ts == bar1h.ts + H1 - H4`), and `sub_bars` must end with `bar1h`; otherwise `ValueError`, before any state changes.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import dataclasses

import pytest

from ewbot.config import WaveParams
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.patterns import Invalidation
from ewbot.waves.scoring import is_done, softmax

T0 = 1_700_006_400        # multiple of H4
P = WaveParams(warmup_4h=50)
# lead-in, S=1000, P1=1100, P2=1045, P3=1206.8, P4=1158.26, P5=1258.26, A=1168.26, B=1223.88, C=1133.88, tail
WAY = [1250.0, 1150.0, 1200.0, 1080.0, 1130.0, 1040.0, 1090.0, 1000.0, 1100.0, 1045.0, 1206.8, 1158.26,
       1258.26, 1168.26, 1223.88, 1133.88, 1187.88]


def _linear(way: list[float]) -> tuple[list[Bar], list[int]]:
    """1h bars moving ~1 price unit per hour along straight lines; every waypoint is hit exactly."""
    closes, hours = [way[0]], [0]
    for a, b in zip(way, way[1:]):
        n = round(abs(b - a))
        closes += [a + (b - a) * k / n for k in range(1, n)] + [b]
        hours.append(len(closes) - 1)
    bars, o = [], closes[0]
    for k, c in enumerate(closes):
        bars.append(Bar(T0 + k * H1, o, max(o, c), min(o, c), c, 1.0))
        o = c
    return bars, hours


def _feed(bars: list[Bar]):
    buf: list[Bar] = []
    for b in bars:
        if not buf or b.ts // H4 != buf[0].ts // H4:
            buf = []
        buf.append(b)
        if len(buf) == 4:
            yield b, Bar(buf[0].ts, buf[0].open, max(x.high for x in buf), min(x.low for x in buf),
                         buf[-1].close, 4.0), tuple(buf)
        else:
            yield b, None, None


def _run(bars: list[Bar], params: WaveParams = P) -> list[WaveState]:
    eng = WaveEngine(params)
    return [eng.on_1h_close(*x) for x in _feed(bars)]


BARS, HOURS = _linear(WAY)
STATES = _run(BARS)
H = dict(zip(["", "", "", "", "", "", "", "S", "1", "2", "3", "4", "5", "A", "B", "C", "end"], HOURS))


def test_bookkeeping_and_ready() -> None:
    first = STATES[0]
    assert (first.ready, first.counts, first.top, first.q_top, first.s_rev) == (False, (), None, 0.0, 0.0)
    assert (first.E_live, first.E_live_ts, first.leg_dir, first.i4h, first.closed_4h) == (0.0, -1, 0, -1, False)
    assert STATES[3].closed_4h and STATES[3].i4h == 0 and not STATES[4].closed_4h
    assert all(s.i4h == (k + 1) // 4 - 1 for k, s in enumerate(STATES))
    # ATR(14) on 4h needs 14 closed 4h bars; the first pivot (1250 at hour 0) confirms soon after
    first_init = next(k for k, s in enumerate(STATES) if s.leg_dir != 0)
    assert STATES[first_init].new_pivot_4h is not None and STATES[first_init].new_pivot_4h.price == 1250.0
    first_ready = next(k for k, s in enumerate(STATES) if s.ready)
    assert first_ready == 4 * P.warmup_4h - 1          # warmup is the binding constraint here
    assert not any(s.new_pivot_4h for s in STATES if not s.closed_4h)


def test_pivots_are_the_waypoints() -> None:
    piv = STATES[-1].pivots4h
    assert [(p.sub_ts, p.price) for p in piv] == [(T0 + h * H1, w) for h, w in zip(HOURS[:-1], WAY[:-1])]
    assert all(p.i == (p.sub_ts - T0) // H4 for p in piv)
    assert all(p.confirmed_at >= p.i for p in piv)


def test_e_live_tracks_closed_1h_bars_between_4h_closes() -> None:
    h = H["S"] + 40                                   # mid W1, leg up
    while not STATES[h].closed_4h:
        h += 1
    for k in range(h + 1, h + 4):                     # the three 1h bars after a 4h close
        s = STATES[k]
        assert not s.closed_4h and s.leg_dir == 1
        assert s.E_live == BARS[k].high and s.E_live_ts == BARS[k].ts
    assert STATES[h + 4].closed_4h and STATES[h + 4].E_live == BARS[h + 4].high


def test_top_through_the_impulse_and_correction() -> None:
    def top(label: str) -> tuple:
        t = STATES[H[label]].top
        return (t.pattern, t.d, t.phase, t.prices)

    assert top("2") == ("IMP", 1, "W2", (1000.0, 1100.0))
    assert top("3") == ("IMP", 1, "W3", (1000.0, 1100.0, 1045.0))
    assert top("4") == ("IMP", 1, "W4", (1000.0, 1100.0, 1045.0, 1206.8))
    assert top("5") == ("IMP", 1, "W5", (1000.0, 1100.0, 1045.0, 1206.8, 1158.26))
    assert top("C") == ("ABC", -1, "C", (1258.26, 1168.26, 1223.88))
    s5 = STATES[H["5"]]
    assert s5.q_top == pytest.approx(6.0 / 6.5, abs=1e-6)
    assert s5.s_rev > 0.9                              # W5 = W1 and beyond P3: done
    k4 = next(k for k, s in enumerate(STATES) if s.new_pivot_4h and s.new_pivot_4h.price == 1158.26)
    assert STATES[k4].top.phase == "W5" and STATES[k4].s_rev < 0     # W5 just started: not done


def _check_probs_and_s_rev(states: list[WaveState], params: WaveParams) -> int:
    """Recompute q' (hysteresis at the last 4h close), order, probs and S_rev for every state."""
    bonus_used, prev_at_close = 0, None
    for k, s in enumerate(states):
        if s.closed_4h:
            prev_at_close = states[k - 1].top if k else None
        if not s.counts:
            assert s.probs == () and s.top is None and s.s_rev == 0.0
            continue
        pv = prev_at_close
        qp = [c.q + params.h if pv is not None and c.lineage == pv.lineage
              and c.points[:len(pv.points)] == pv.points else c.q for c in s.counts]
        bonus_used += sum(1 for c, x in zip(s.counts, qp) if x != c.q)
        assert all(a >= b for a, b in zip(qp, qp[1:]))
        assert list(s.probs) == pytest.approx(softmax(qp, params.T), abs=1e-12)
        assert sum(s.probs) == pytest.approx(1.0)
        exp = sum(p if is_done(c.pattern, c.d, c.phase, c.prices, s.E_live) else -p
                  for c, p in zip(s.counts, s.probs))
        assert s.s_rev == pytest.approx(exp, abs=1e-12)
        assert s.top is s.counts[0] and s.q_top == s.counts[0].q
    return bonus_used


def test_probs_hysteresis_and_s_rev_are_consistent() -> None:
    assert _check_probs_and_s_rev(STATES, P) > 0


def _first_non_4h_hour(start: int) -> int:
    h = start
    while (h + 1) % 4 == 0:
        h += 1
    return h


def test_wick_through_p2_kills_w3_and_every_extension() -> None:
    hw = _first_non_4h_hour(H["2"] + 29)                  # mid W3, not a 4h close
    bars = list(BARS)
    bars[hw] = dataclasses.replace(bars[hw], low=1043.0)  # wick 2 below P2 = 1045, close unchanged
    states = _run(bars)
    w3 = (1000.0, 1100.0, 1045.0)
    has = lambda s, pts: any(c.pattern == "IMP" and c.d == 1 and c.prices[:3] == pts for c in s.counts)  # noqa: E731
    assert has(states[hw - 1], w3) and states[hw - 1].top.prices == w3
    assert not has(states[hw], w3)                         # killed on the wick bar
    assert sum(states[hw].probs) == pytest.approx(1.0)
    assert states[hw].probs[0] == pytest.approx(states[hw - 1].probs[1] / sum(states[hw - 1].probs[1:]))
    # never resurrected: not at later 4h closes of W3, and no W4/W5 built on (S, P1, P2)
    assert not any(has(s, w3) for s in states[hw:H["5"] + 1])
    assert [p.price for p in states[-1].pivots4h] == [p.price for p in STATES[-1].pivots4h]
    assert _check_probs_and_s_rev(states, P) > 0


def test_upper_wick_through_w5_cap_kills_count() -> None:
    # short W3 impulse: S=1000 P1=1100 P2=1050 P3=1140 (W3=90 < W1=100) P4=1110 -> W5 cap = 1110 + 90 = 1200
    way = WAY[:7] + [1000.0, 1100.0, 1050.0, 1140.0, 1110.0, 1190.0, 1120.0]
    bars, hours = _linear(way)
    h5 = hours[11] + 40                                  # in W5 (leg up from P4 = 1110)
    h5 = _first_non_4h_hour(h5)
    pts = (1000.0, 1100.0, 1050.0, 1140.0, 1110.0)
    ok = _run(bars)
    w5 = next(c for c in ok[h5].counts if c.prices == pts)
    assert (w5.phase, w5.inval) == ("W5", Invalidation(1110.0, 1200.0))
    # without the spike the count lives until P5 (every high stays below the cap)
    assert bars[h5].high < 1200.0 and all(any(c.prices == pts for c in s.counts) for s in ok[h5 - 1:hours[12] + 1])
    bars[h5] = dataclasses.replace(bars[h5], high=1200.5)      # one 1h spike above the H3 cap
    hit = _run(bars)
    assert any(c.prices == pts for c in hit[h5 - 1].counts)
    assert not any(c.prices == pts for s in hit[h5:] for c in s.counts)
    assert hit[h5].E_live == 1200.5 and hit[h5].E_live_ts == bars[h5].ts


def test_wick_before_a_count_is_enumerable_still_kills_it() -> None:
    # S=1000 P1=1100 P2=1045, a small 1080/1050 swing, then up to 1200 and back to 1150. (S, P1, P2) as W3
    # skips 1080/1050, so it is only enumerable once E >= 1080. A 1h wick to 1040 (< P2) before that must
    # kill it when it first shows up, and every W4/W5 built on it.
    way = WAY[:7] + [1000.0, 1100.0, 1045.0, 1080.0, 1050.0, 1200.0, 1150.0]
    bars, hours = _linear(way)
    w3 = (1000.0, 1100.0, 1045.0)
    has = lambda s: any(c.pattern == "IMP" and c.d == 1 and c.prices[:3] == w3 for c in s.counts)  # noqa: E731
    ok = _run(bars)
    up = next(k for k in range(hours[11], len(ok)) if ok[k].pivots4h[-1].price == 1050.0)
    hw = _first_non_4h_hour(up + 1)                       # leg up from 1050, E still below 1080
    assert ok[hw].E_live < 1080.0 and not has(ok[hw])
    appear = next(k for k in range(hw, len(ok)) if has(ok[k]))
    assert ok[appear].closed_4h and ok[appear].top.prices == w3
    assert ok[-1].top.prices == w3 + (1200.0,) and ok[-1].top.phase == "W4"
    bars[hw] = dataclasses.replace(bars[hw], low=1040.0)  # wick 5 below P2, close unchanged
    states = _run(bars)
    assert [p.price for p in states[-1].pivots4h] == [p.price for p in ok[-1].pivots4h]
    assert not any(has(s) for s in states[hw:])
    assert _check_probs_and_s_rev(states, P) > 0


REF = 2000.0                                              # mirror p' = 2*REF - p


def _mirror(bars: list[Bar]) -> list[Bar]:
    return [Bar(b.ts, 2 * REF - b.open, 2 * REF - b.low, 2 * REF - b.high, 2 * REF - b.close, b.volume)
            for b in bars]


def test_mirrored_leg_down_tracks_lows_and_dies_on_an_upper_wick() -> None:
    m = lambda *xs: tuple(2 * REF - x for x in xs)  # noqa: E731
    bars = _mirror(BARS)
    states = _run(bars)
    assert [p.price for p in states[-1].pivots4h] == list(m(*WAY[:-1]))
    h = H["S"] + 40                                       # mid W1, now a leg down
    while not states[h].closed_4h:
        h += 1
    for k in range(h + 1, h + 4):
        s = states[k]
        assert not s.closed_4h and s.leg_dir == -1
        assert s.E_live == bars[k].low and s.E_live_ts == bars[k].ts
    t3 = states[H["3"]].top
    assert (t3.pattern, t3.d, t3.phase, t3.prices) == ("IMP", -1, "W3", m(1000.0, 1100.0, 1045.0))
    hw = _first_non_4h_hour(H["2"] + 29)
    w3 = m(1000.0, 1100.0, 1045.0)
    assert t3.inval == Invalidation(None, w3[2])
    bars[hw] = dataclasses.replace(bars[hw], high=w3[2] + 2.0)       # wick 2 above P2
    hit = _run(bars)
    has = lambda s: any(c.pattern == "IMP" and c.d == -1 and c.prices[:3] == w3 for c in s.counts)  # noqa: E731
    assert has(hit[hw - 1]) and not any(has(s) for s in hit[hw:H["5"] + 1])
    assert _check_probs_and_s_rev(hit, P) > 0


def test_4h_close_inputs_are_validated() -> None:
    b = BARS[:4]
    agg = Bar(b[0].ts, b[0].open, max(x.high for x in b), min(x.low for x in b), b[3].close, 4.0)
    eng = WaveEngine(P)
    with pytest.raises(ValueError):                       # sub_bars must end with the closing 1h bar
        eng.on_1h_close(b[3], agg, b[:3])
    with pytest.raises(ValueError):                       # bar1h does not close a 4h bucket
        eng.on_1h_close(b[2], agg, b[:3])
    with pytest.raises(ValueError):                       # bar4h is not the bucket bar1h closes
        eng.on_1h_close(b[3], dataclasses.replace(agg, ts=b[0].ts + H4), b)
    assert eng.on_1h_close(b[3], agg, b).i4h == 0          # rejected calls left no trace
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_engine.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.waves.engine'"

- [ ] **Step 3: Write minimal implementation**

```python
"""WaveEngine: streaming 4h Elliott state, updated on every closed 1h bar (spec 3 event order, 4.1-4.4).

Per closed 1h bar:
 1. if it closes a 4h bucket: ZigZag(4h) update; E_live := E; enumerate (skeleton cache, see
    enumerate.py) -> drop counts on the dead list -> drop counts the 1h history already invalidated
    (below) -> dedupe -> hysteresis q' -> sort -> p = softmax(q'/T);
 2. otherwise: E_live/E_live_ts := the 1h bar's high (leg up) / low (leg down) if strictly beyond;
 3. invalidation: a count (or a completed pattern kept for lineage bookkeeping) dies if the bar went
    strictly beyond its level. On the E side the level is compared with E_live, on the opposite side
    with the bar's low (leg up) / high (leg down). Dead keys are remembered; any later count whose
    labeled points EXTEND a dead key (same pattern and d) stays dead, so a wick through P2/P4 that
    never became a pivot cannot be "forgotten" at the next 4h re-enumeration;
 4. probabilities fixed at the 4h close are renormalised over the survivors; done-ness and S_rev are
    recomputed with E_live.

1h history (step 1): a count can become enumerable only later (E crosses a skipped pivot, a new pivot
opens new combinations), after a wick that would have killed it. So at every 4h close each enumerated
count / completed pattern is also checked against EVERY closed 1h bar strictly after its last labeled
point (that point's sub_ts) up to and including the current bar: min LOW < lo or max HIGH > hi kills
it and its key joins the dead list. The suffix extremes come from two monotonic stacks of closed 1h
bars, so a query costs O(log n) and only bars that are already closed are ever seen.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from ewbot.config import WaveParams
from ewbot.types import H1, H4, Bar
from ewbot.waves.enumerate import Count, _Completed, _dedupe_sorted, _Enumerator, _sort_key
from ewbot.waves.scoring import is_done, softmax
from ewbot.waves.zigzag import Pivot, ZigZag

_DeadKey = tuple[str, int, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class WaveState:
    ts: int
    ready: bool
    counts: tuple[Count, ...]
    probs: tuple[float, ...]
    top: Count | None
    q_top: float
    s_rev: float
    lineages: frozenset[tuple[str, int, int]]
    E_live: float
    E_live_ts: int
    leg_dir: int
    leg_start: Pivot | None
    pivots4h: tuple[Pivot, ...]
    new_pivot_4h: Pivot | None
    atr4h: float | None
    closed_4h: bool
    i4h: int


@dataclass(frozen=True, slots=True)
class _Item:
    key: _DeadKey
    lo: float | None
    hi: float | None
    lineage: tuple[str, int, int]
    since: int                      # sub_ts of the last labeled point (1h history starts after it)


def _count_item(c: Count) -> _Item:
    inv = c.inval
    return _Item((c.pattern, c.d, tuple(p.i for p in c.points)), inv.lo, inv.hi, c.lineage, c.points[-1].sub_ts)


def _completed_item(c: _Completed, end_ts: int) -> _Item:
    return _Item((c.lineage[0], c.lineage[1], c.key), c.inval.lo, c.inval.hi, c.lineage, end_ts)


class _SuffixExtremes:
    """min LOW / max HIGH over the closed 1h bars with ts > t, for any t: lows are kept on a stack that
    strictly increases (a new bar pops every low >= its own), highs on one that strictly decreases. The
    first entry after t is then the extreme of everything after t."""

    def __init__(self) -> None:
        self._lo_ts: list[int] = []
        self._lo: list[float] = []
        self._hi_ts: list[int] = []
        self._hi: list[float] = []

    def push(self, bar: Bar) -> None:
        lo_ts, lo, hi_ts, hi = self._lo_ts, self._lo, self._hi_ts, self._hi
        while lo and lo[-1] >= bar.low:
            lo.pop()
            lo_ts.pop()
        lo.append(bar.low)
        lo_ts.append(bar.ts)
        while hi and hi[-1] <= bar.high:
            hi.pop()
            hi_ts.pop()
        hi.append(bar.high)
        hi_ts.append(bar.ts)

    def after(self, t: int) -> tuple[float, float]:
        """(min LOW, max HIGH) over the bars with ts > t; (inf, -inf) if there are none."""
        k = bisect_right(self._lo_ts, t)
        lo = self._lo[k] if k < len(self._lo) else math.inf
        k = bisect_right(self._hi_ts, t)
        hi = self._hi[k] if k < len(self._hi) else -math.inf
        return lo, hi

    def forget_until(self, t: int) -> None:
        """Drop bars with ts <= t (no later query starts before t)."""
        k = bisect_right(self._lo_ts, t)
        del self._lo[:k], self._lo_ts[:k]
        k = bisect_right(self._hi_ts, t)
        del self._hi[:k], self._hi_ts[:k]


class WaveEngine:
    def __init__(self, params: WaveParams) -> None:
        self.params = params
        self._zz = ZigZag(params.K4, params.atr_len)
        self._enum = _Enumerator(params)
        self._use_cache = True                 # tests flip this to compare against fresh enumeration
        self._i4h = -1
        self._E_live = 0.0
        self._E_live_ts = -1
        self._counts: list[Count] = []         # deduped, sorted by q'
        self._keys: list[_DeadKey] = []
        self._prices: list[tuple[float, ...]] = []
        self._p0: list[float] = []             # probabilities fixed at the last 4h close
        self._items: list[_Item] = []          # every alive raw count + alive completed pattern
        self._max_lo = float("-inf")
        self._min_hi = float("inf")
        self._dead: set[_DeadKey] = set()
        self._prev_top: Count | None = None
        self._pivots_t: tuple[Pivot, ...] = ()
        self._lineages: frozenset[tuple[str, int, int]] = frozenset()
        self._hist = _SuffixExtremes()         # closed 1h bars after the oldest pivot in the window
        self._sub_ts: dict[tuple[int, int], int] = {}   # (i, kind) -> sub_ts of the window's pivots

    # ------------------------------------------------------------------ public
    def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
        zz = self._zz
        new_piv: Pivot | None = None
        if bar4h is not None:
            if (bar1h.ts + H1) % H4 != 0 or bar4h.ts != bar1h.ts + H1 - H4:
                raise ValueError("bar4h must be the UTC 4h bucket that bar1h closes")
            if sub_bars is not None and (len(sub_bars) == 0 or sub_bars[-1].ts != bar1h.ts):
                raise ValueError("sub_bars must end with the 1h bar that closes the 4h bucket")
        self._hist.push(bar1h)
        if bar4h is not None:
            self._i4h += 1
            new_piv = zz.update(self._i4h, bar4h, sub_bars)
            if new_piv is not None:
                self._pivots_t = tuple(zz.pivots)
                self._on_new_pivot()
            if zz.leg_dir != 0:
                self._E_live, self._E_live_ts = zz.E, zz.E_sub_ts
                self._rescore()
        elif zz.leg_dir != 0:
            ext = bar1h.high if zz.leg_dir > 0 else bar1h.low
            if zz.leg_dir * (ext - self._E_live) > 0:
                self._E_live, self._E_live_ts = ext, bar1h.ts
        if zz.leg_dir != 0:
            self._kill(bar1h)
        state = self._state(bar1h.ts, new_piv, bar4h is not None)
        self._prev_top = state.top
        return state

    # ----------------------------------------------------------------- private
    def _on_new_pivot(self) -> None:
        """Slide the M-pivot window: forget dead keys whose first point left it (they can never recur)
        and 1h bars before its first pivot (no count's history starts there any more)."""
        win = self._zz.pivots[-self.params.M:]
        lo_i = win[0].i
        if self._dead and len(self._zz.pivots) > self.params.M:
            self._dead = {k for k in self._dead if k[2][0] >= lo_i}
        self._hist.forget_until(win[0].sub_ts)
        self._sub_ts = {(p.i, p.kind): p.sub_ts for p in win}

    def _is_dead(self, pattern: str, d: int, idx: tuple[int, ...]) -> bool:
        dead = self._dead
        if not dead:
            return False
        return any((pattern, d, idx[:k]) in dead for k in range(2, len(idx) + 1))

    def _history_kills(self, items: list[_Item]) -> set[_DeadKey]:
        """Keys of the items that some closed 1h bar after their last labeled point went beyond."""
        ext: dict[int, tuple[float, float]] = {}
        killed: set[_DeadKey] = set()
        for it in items:
            e = ext.get(it.since)
            if e is None:
                e = ext[it.since] = self._hist.after(it.since)
            if (it.lo is not None and e[0] < it.lo) or (it.hi is not None and e[1] > it.hi):
                killed.add(it.key)
        return killed

    def _rescore(self) -> None:
        zz, p = self._zz, self.params
        assert not zz.pivots or zz.pivots[-1].confirmed_at <= self._i4h, "lookahead: future pivot"
        enum = self._enum if self._use_cache else _Enumerator(p)
        raw, done = enum.run(zz.pivots, zz.leg_dir, zz.E)
        raw = [c for c in raw if not self._is_dead(c.pattern, c.d, tuple(x.i for x in c.points))]
        done = [c for c in done if not self._is_dead(c.lineage[0], c.lineage[1], c.key)]
        raw_items = [_count_item(c) for c in raw]
        # the end point of a completed pattern is the window pivot with .i == key[-1] and kind == d
        done_items = [_completed_item(c, self._sub_ts[(c.key[-1], c.lineage[1])]) for c in done]
        killed = self._history_kills(raw_items + done_items)
        if killed:
            self._dead |= killed
            raw = [c for c, it in zip(raw, raw_items) if it.key not in killed]
            raw_items = [it for it in raw_items if it.key not in killed]
            done_items = [it for it in done_items if it.key not in killed]
        counts = _dedupe_sorted(raw)
        prev = self._prev_top
        qp = [
            c.q + p.h
            if prev is not None and c.lineage == prev.lineage and c.points[:len(prev.points)] == prev.points
            else c.q
            for c in counts
        ]
        order = sorted(range(len(counts)), key=lambda k: _sort_key(counts[k], qp[k]))
        self._counts = [counts[k] for k in order]
        self._keys = [(c.pattern, c.d, tuple(x.i for x in c.points)) for c in self._counts]
        self._prices = [c.prices for c in self._counts]
        self._p0 = softmax([qp[k] for k in order], p.T)
        self._items = raw_items + done_items
        self._refresh_items()

    def _refresh_items(self) -> None:
        los = [it.lo for it in self._items if it.lo is not None]
        his = [it.hi for it in self._items if it.hi is not None]
        self._max_lo = max(los) if los else float("-inf")
        self._min_hi = min(his) if his else float("inf")
        self._lineages = frozenset(it.lineage for it in self._items)

    def _kill(self, bar1h: Bar) -> None:
        if self._zz.leg_dir > 0:
            lo_val, hi_val = bar1h.low, self._E_live
        else:
            lo_val, hi_val = self._E_live, bar1h.high
        if lo_val >= self._max_lo and hi_val <= self._min_hi:
            return
        killed = {
            it.key for it in self._items
            if (it.lo is not None and lo_val < it.lo) or (it.hi is not None and hi_val > it.hi)
        }
        self._dead |= killed
        self._items = [it for it in self._items if it.key not in killed]
        self._refresh_items()
        keep = [k for k, key in enumerate(self._keys) if key not in killed]
        if len(keep) != len(self._keys):
            self._counts = [self._counts[k] for k in keep]
            self._keys = [self._keys[k] for k in keep]
            self._prices = [self._prices[k] for k in keep]
            self._p0 = [self._p0[k] for k in keep]

    def _state(self, ts: int, new_piv: Pivot | None, closed_4h: bool) -> WaveState:
        zz = self._zz
        s = sum(self._p0)
        probs = tuple(x / s for x in self._p0) if s > 0 else ()
        e = self._E_live
        s_rev = 0.0
        for c, pr, pp in zip(self._counts, self._prices, probs):
            s_rev += pp if is_done(c.pattern, c.d, c.phase, pr, e) else -pp
        top = self._counts[0] if self._counts else None
        ready = self._i4h + 1 >= self.params.warmup_4h and zz.leg_dir != 0
        return WaveState(
            ts=ts, ready=ready, counts=tuple(self._counts), probs=probs, top=top,
            q_top=top.q if top is not None else 0.0, s_rev=s_rev, lineages=self._lineages,
            E_live=e, E_live_ts=self._E_live_ts, leg_dir=zz.leg_dir,
            leg_start=zz.pivots[-1] if zz.pivots else None, pivots4h=self._pivots_t,
            new_pivot_4h=new_piv, atr4h=zz.atr, closed_4h=closed_4h, i4h=self._i4h,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_engine.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/waves/engine.py tests/waves/test_engine.py
git commit -m "feat(waves): streaming WaveEngine with E_live, invalidation kills, hysteresis and S_rev"
```

---

### Task 10: Synthetic path generator, synthetic recovery, no-lookahead and mirror property tests

**Files:**
- Create: `tests/synth.py`
- Test: `tests/waves/test_synthetic.py`, `tests/waves/test_no_lookahead.py`, `tests/waves/test_mirror.py`, `tests/waves/test_engine_perf.py`

**Interfaces:**
- Consumes: `WaveEngine`, `WaveState`, `WaveParams`, `ATR`, `aggregate_4h`, `to_bars`, `Bar`, `H1`, `H4`.
- Produces (test helpers C2 may reuse for golden tests):
  - `impulse_abc(...)`: pivot grammar with chosen Fib ratios.
  - `make_path(way, noise, seed, params, thr=None) -> (bars1h, waypoint_hours)`: Brownian bridge with knots every 8 h, pinned at the waypoints. Amplitude = `noise * nominal_thr`, and prices sit on a 1/64 grid.
  - `nominal_thr`, `feed` (streaming 4h aggregation), `batch_feed` (via `aggregate_4h`), `run_engine(bars, params, use_cache=True, batch=False)`, `mirror_bars`, `recovered`, `recovery_curve`, `random_bars`, `label_hours`.
- Spec 10 coverage:
  - With noise ≤ 0.3·thr (5 seeds × 4 levels), all of these must hold:
    - every designed pivot is recovered exactly;
    - the top label at each leg end matches the ground truth (W2, W3, W4, W5, C);
    - the setup phases match (W2 for ①, W4 for ②, W5 for ③, ABC−1 C for ④);
    - S_rev > 0.
  - The recovery curve is recorded. Measured: 1.0 up to 1.0·thr, 0.83 at 2·thr, 0.0 at 3·thr.
  - No-lookahead checks (Hypothesis): prefix invariance, poisoned future (×10 and NaN), streaming == batch (`aggregate_4h`), cache == fresh enumeration, an independent oracle (raw 1h/4h bars + `atr_series`) that each pivot is the extreme of its own leg (from the previous pivot's 1h bar, whose close only counts) and appears in the state exactly at the first 4h close of that leg retracing K4·ATR (its leg can confirm it only from the bar after the previous pivot was confirmed), with explicit-bar regressions for both cases, and a causality assertion on every state (`confirmed_at <= i4h`, `sub_ts <= ts`, append-only pivots, 4h visible only at its close).
  - Mirror symmetry `p' = 2·4096 − p`: d and kinds flip, while phases, points, q, probs and S_rev stay identical.

- [ ] **Step 1: Write the failing test**

`tests/waves/test_synthetic.py`:
```python
from __future__ import annotations

import json

import pytest

from ewbot.config import WaveParams
from tests.synth import (
    impulse_abc,
    label_hours,
    make_path,
    nominal_thr,
    recovered,
    recovery_curve,
    run_engine,
)

P = WaveParams(warmup_4h=50)
WAY = impulse_abc()
THR = nominal_thr(WAY, P)
PRICE = {w.label: w.price for w in WAY if w.label}
# leg-end label -> expected top (pattern, d, phase, labels of its confirmed points)
EXPECTED_TOP = {
    "2": ("IMP", 1, "W2", "S1"),      # setup 1 (W3 entry) phase
    "3": ("IMP", 1, "W3", "S12"),
    "4": ("IMP", 1, "W4", "S123"),    # setup 2 (W5 entry) phase
    "5": ("IMP", 1, "W5", "S1234"),   # setup 3 (W5 reversal) phase
    "C": ("ABC", -1, "C", "5AB"),     # setup 4 (C end) phase
}


def test_grammar_ratios() -> None:
    w1 = PRICE["1"] - PRICE["S"]
    assert w1 == 100.0
    assert (PRICE["1"] - PRICE["2"]) / w1 == pytest.approx(0.55)
    assert (PRICE["3"] - PRICE["2"]) / w1 == pytest.approx(1.618, abs=1e-3)
    assert (PRICE["3"] - PRICE["4"]) / (PRICE["3"] - PRICE["2"]) == pytest.approx(0.3, abs=1e-3)
    assert (PRICE["5"] - PRICE["4"]) / w1 == pytest.approx(1.0, abs=1e-3)
    a = PRICE["5"] - PRICE["A"]
    assert (PRICE["B"] - PRICE["A"]) / a == pytest.approx(0.618, abs=1e-3)
    assert (PRICE["B"] - PRICE["C"]) / a == pytest.approx(1.0, abs=1e-3)
    assert 11.0 < THR < 13.0          # ~1 price unit per hour -> 4h ATR ~4 -> thr ~12


@pytest.mark.parametrize("noise", [0.0, 0.1, 0.2, 0.3])
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_low_noise_recovers_pivots_top_labels_and_setup_phases(noise: float, seed: int) -> None:
    bars, hours = make_path(WAY, noise, seed, P, thr=THR)
    states = run_engine(bars, P)
    assert recovered(states, WAY, hours)
    at = label_hours(WAY, hours)
    for label, (pattern, d, phase, pts) in EXPECTED_TOP.items():
        s = states[at[label]]                      # last 1h bar of the leg (E_live == the waypoint)
        assert s.ready and s.E_live == PRICE[label]
        assert s.top is not None
        assert (s.top.pattern, s.top.d, s.top.phase) == (pattern, d, phase)
        assert s.top.prices == tuple(PRICE[c] for c in pts)
        assert s.s_rev > 0                         # the leg is "done" by consensus at its end


def test_recovery_curve_is_recorded(tmp_path) -> None:
    levels = [0.0, 0.3, 1.0, 2.0, 3.0]
    curve = recovery_curve(levels, range(6), P, WAY)
    out = tmp_path / "recovery_curve.json"
    out.write_text(json.dumps({str(k): v for k, v in curve.items()}))
    print("pivot recovery rate by noise/thr:", curve)
    assert list(curve) == levels
    assert curve[0.0] == curve[0.3] == 1.0
    assert all(0.0 <= v <= 1.0 for v in curve.values())
    assert curve[3.0] < 1.0                       # heavy noise must break exact recovery
```

`tests/waves/test_no_lookahead.py`:
```python
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.indicators import atr_series
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveState
from tests.synth import T0, impulse_abc, make_path, random_bars, run_engine

# small K4 / ATR / warmup so a few hundred bars produce many pivots and counts
P = WaveParams(K4=1.5, atr_len=5, warmup_4h=10)
paths = st.builds(random_bars, st.integers(0, 2**31 - 1), st.integers(120, 720),
                  st.sampled_from([3.0, 6.0, 12.0]))


def _assert_causal(states: list[WaveState]) -> None:
    """Spec 10 no-lookahead (3): every pivot used was confirmed on a CLOSED 4h bar <= now."""
    prev: tuple = ()
    for k, s in enumerate(states):
        assert s.ts == T0 + k * H1
        assert s.closed_4h == ((s.ts + H1) % H4 == 0)          # 4h bar visible only at its close
        assert s.i4h == (k + 1) // 4 - 1
        assert s.pivots4h[: len(prev)] == prev                 # append-only
        prev = s.pivots4h
        known = set(s.pivots4h)
        for p in s.pivots4h:
            assert p.i <= p.confirmed_at <= s.i4h and p.sub_ts <= s.ts
        for c in s.counts:
            assert c.leg_dir == s.leg_dir
            for p in c.points:
                assert p in known and p.confirmed_at <= s.i4h and p.sub_ts <= s.ts
        assert s.leg_start == (s.pivots4h[-1] if s.pivots4h else None)
        assert s.E_live_ts <= s.ts
        if s.new_pivot_4h is not None:
            assert s.closed_4h and s.new_pivot_4h == s.pivots4h[-1] and s.new_pivot_4h.confirmed_at == s.i4h
        assert len(s.probs) == len(s.counts)
        assert -1.0 - 1e-9 <= s.s_rev <= 1.0 + 1e-9


@settings(max_examples=40, deadline=None)
@given(paths, st.data())
def test_prefix_invariance(bars: list[Bar], data: st.DataObject) -> None:
    full = run_engine(bars, P)
    _assert_causal(full)
    t = data.draw(st.integers(1, len(bars)))
    assert run_engine(bars[:t], P) == full[:t]


def _poison(b: Bar, how: str) -> Bar:
    if how == "x10":
        return dataclasses.replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10)
    nan = math.nan
    return dataclasses.replace(b, open=nan, high=nan, low=nan, close=nan)


@settings(max_examples=40, deadline=None)
@given(paths, st.data(), st.sampled_from(["x10", "nan"]))
def test_poisoned_future_does_not_change_the_past(bars: list[Bar], data: st.DataObject, how: str) -> None:
    """Guards hidden cross-run/global state only. In a sequential streaming harness the engine can never
    see the poisoned future, so this cannot catch a pivot whose confirmed_at is backdated: that is the job
    of the independent oracle test below."""
    t = data.draw(st.integers(1, len(bars) - 1))
    clean = run_engine(bars, P)
    poisoned = bars[:t] + [_poison(b, how) for b in bars[t:]]
    assert run_engine(poisoned, P)[:t] == clean[:t]


@settings(max_examples=30, deadline=None)
@given(paths)
def test_streaming_equals_batch_and_cache_equals_fresh(bars: list[Bar]) -> None:
    streamed = run_engine(bars, P)
    assert run_engine(bars, P, batch=True) == streamed           # 4h bars from aggregate_4h
    assert run_engine(bars, P, use_cache=False) == streamed      # fresh enumeration at every 4h close


def test_synthetic_path_is_causal_too() -> None:
    bars, _ = make_path(impulse_abc(), 0.3, 7, WaveParams(warmup_4h=50))
    _assert_causal(run_engine(bars, WaveParams(warmup_4h=50)))


def _four_hour(bars: list[Bar]) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
    """First complete UTC 4h bucket (ts // H4) and the high/low/close of every complete bucket, built straight
    from the raw 1h bars."""
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        buckets.setdefault(b.ts // H4, []).append(b)
    keys = sorted(k for k, v in buckets.items() if len(v) == H4 // H1)
    assert keys == list(range(keys[0], keys[0] + len(keys)))
    return (keys[0],
            np.array([max(x.high for x in buckets[k]) for k in keys]),
            np.array([min(x.low for x in buckets[k]) for k in keys]),
            np.array([buckets[k][-1].close for k in keys]))


def _assert_pivots_confirmed_by_the_book(bars: list[Bar], params: WaveParams) -> int:
    """Independent oracle for spec 4.1: re-derive, from raw 1h/4h bars and a separate ATR, that every pivot the
    engine reports is the extreme of its own leg and is confirmed on exactly the first 4h CLOSE of that leg that
    retraced K4*ATR from it, and that the state shows it at exactly that bar (the engine's own ``confirmed_at``
    is not trusted). A pivot's leg starts at the previous pivot's 1h bar (only that bar's CLOSE counts: spec 4.1
    same-bar rule) and can confirm it only from the bar after the previous pivot was confirmed (pivots alternate,
    at most one per bar), even when its extreme lies before that bar. The init rule's "ambiguous single atom:
    wait" branch is not modelled (never reached by these generators)."""
    states = run_engine(bars, params)
    k0, high, low, close = _four_hour(bars)
    atr = atr_series(high, low, close, params.atr_len)
    close1h = {b.ts: b.close for b in bars}
    seen = 0
    for s in states:
        new = s.pivots4h[seen:]
        assert len(new) <= 1
        if new:
            # (a) the pivot first appears on the 4h close at which it claims to be confirmed
            assert s.closed_4h and s.new_pivot_4h == new[0] and s.i4h == new[0].confirmed_at
        else:
            assert s.new_pivot_4h is None
        seen = len(s.pivots4h)
    pivots = states[-1].pivots4h
    for n, p in enumerate(pivots):
        c, k, thr = p.confirmed_at, p.kind, params.K4
        prev = pivots[n - 1] if n else None
        assert low[p.i] - 1e-9 <= p.price <= high[p.i] + 1e-9           # the extreme lies in its own bar
        leg = [b.high if k > 0 else b.low for b in bars                 # nothing in its leg went further
               if 0 <= b.ts // H4 - k0 <= c and (prev is None or b.ts > prev.sub_ts)]
        leg += [close1h[prev.sub_ts]] if prev is not None else []
        assert all(k * (x - p.price) <= 1e-9 for x in leg), p
        assert not math.isnan(atr[c])
        assert k * (p.price - close[c]) >= thr * atr[c] - 1e-9          # (b) retraced K4*ATR on that close
        start = p.i if prev is None else max(p.i, prev.confirmed_at + 1)
        for j in range(start, c):                                       # ... and on no earlier close of its leg
            assert math.isnan(atr[j]) or k * (p.price - close[j]) < thr * atr[j] + 1e-9, (p, j)
    return len(pivots)


@settings(max_examples=40, deadline=None)
@given(paths)
def test_pivots_are_confirmed_exactly_when_the_close_retraces_k_atr(bars: list[Bar]) -> None:
    _assert_pivots_confirmed_by_the_book(bars, P)


def _bars_from_hlc(hlc: list[tuple[float, float, float]]) -> list[Bar]:
    """1h bars from T0 with open = previous close (the first open = its own close), as random_bars builds them."""
    bars, o = [], hlc[0][2]
    for k, (h, lo, c) in enumerate(hlc):
        bars.append(Bar(T0 + k * H1, o, h, lo, c, 1.0))
        o = c
    return bars


# 1h (high, low, close) of random_bars(245, 584, 3.0)[:60] = 15 complete 4h bars: the Hypothesis counterexample
# that the first version of the oracle (which checked closes from the extreme's own bar on) rejected.
_LATE_LEG_HLC = [
    (5002.46875, 4999.40625, 4999.65625), (5000.71875, 4996.109375, 4998.828125),
    (5000.34375, 4997.765625, 4999.328125), (5001.359375, 4990.015625, 4991.21875),
    (4991.5625, 4987.140625, 4987.921875), (4988.25, 4985.703125, 4986.40625),
    (4993.859375, 4986.34375, 4993.265625), (5011.171875, 4991.734375, 5010.234375),
    (5010.6875, 4999.0, 5001.46875), (5003.015625, 4999.1875, 4999.703125),
    (5000.171875, 4994.890625, 4996.203125), (5003.359375, 4994.078125, 5001.546875),
    (5001.921875, 5000.328125, 5000.59375), (5001.71875, 4996.9375, 4998.015625),
    (5001.234375, 4997.09375, 5001.109375), (5004.8125, 4999.375, 4999.625),
    (5002.46875, 4997.59375, 5001.921875), (5004.046875, 4996.96875, 4997.84375),
    (4999.859375, 4996.984375, 4997.75), (5000.515625, 4992.078125, 4995.78125),
    (4997.890625, 4989.53125, 4989.640625), (4990.09375, 4986.171875, 4988.265625),
    (4993.171875, 4988.046875, 4993.140625), (4995.1875, 4991.9375, 4992.28125),
    (4995.6875, 4991.328125, 4994.421875), (4997.84375, 4993.859375, 4995.828125),
    (4998.296875, 4995.703125, 4997.046875), (4998.546875, 4996.59375, 4997.96875),
    (5000.1875, 4995.8125, 4999.1875), (5000.5, 4997.390625, 4999.984375),
    (5001.78125, 4999.484375, 5000.265625), (5001.234375, 4992.3125, 4994.546875),
    (4995.640625, 4994.3125, 4995.234375), (4999.75, 4993.875, 4999.171875),
    (5002.640625, 4998.21875, 5001.5), (5001.734375, 5001.328125, 5001.40625),
    (5004.28125, 5001.046875, 5001.765625), (5008.171875, 5001.453125, 5005.34375),
    (5009.515625, 5003.96875, 5007.109375), (5007.859375, 5001.15625, 5003.234375),
    (5009.625, 5002.765625, 5008.09375), (5008.8125, 5002.296875, 5003.1875),
    (5006.78125, 5002.921875, 5002.96875), (5006.25, 5001.796875, 5003.40625),
    (5003.484375, 4999.796875, 5002.078125), (5006.296875, 5001.015625, 5006.046875),
    (5006.609375, 4995.375, 4996.4375), (4997.734375, 4993.421875, 4994.125),
    (4997.03125, 4993.90625, 4996.59375), (4999.71875, 4995.96875, 4996.890625),
    (4999.125, 4996.484375, 4997.484375), (5002.5, 4996.65625, 5000.59375),
    (5002.1875, 4992.640625, 4994.921875), (4999.078125, 4994.28125, 4997.53125),
    (5002.546875, 4997.28125, 5002.203125), (5004.484375, 5001.921875, 5002.203125),
    (5006.890625, 5000.609375, 5004.375), (5005.46875, 5002.5625, 5005.171875),
    (5005.296875, 4999.78125, 5003.6875), (5003.890625, 5002.078125, 5003.234375),
]


def test_extreme_retraced_before_its_leg_began_is_confirmed_only_after_the_previous_pivot() -> None:
    """The low at 4h bar 5 is already 1.5 ATR below the bar-9 close, but it only becomes a leg extreme when the
    high at bar 1 is confirmed (bar 11); the first close of its own leg that retraces K4*ATR is bar 14's."""
    bars = _bars_from_hlc(_LATE_LEG_HLC)
    piv = run_engine(bars, P)[-1].pivots4h
    assert [(p.i, p.price, p.kind, p.confirmed_at) for p in piv] == [
        (1, 4985.703125, -1, 9), (1, 5011.171875, 1, 11), (5, 4986.171875, -1, 14)]
    _, high, low, close = _four_hour(bars)
    atr = atr_series(high, low, close, P.atr_len)
    assert close[9] - 4986.171875 >= P.K4 * atr[9]          # what the first oracle mistook for a missed confirmation
    assert _assert_pivots_confirmed_by_the_book(bars, P) == 3


def test_previous_pivots_own_1h_bar_counts_only_by_its_close() -> None:
    """Spec 4.1 same-bar rule: the 1h bar of the low pivot (95) spikes to 100.75, but only its close seeds the up
    leg, so the high pivot is 99.75 although its 4h bar's high is 100.75."""
    bars = _bars_from_hlc([(101.0, 99.0, 100.0)] * 8 + [
        (100.75, 95.0, 95.5), (98.0, 95.25, 97.75), (99.75, 97.5, 99.5), (99.6, 99.0, 99.25),
        (99.5, 97.0, 97.25), (97.5, 95.75, 96.0), (96.25, 95.5, 95.75), (96.0, 95.25, 95.5)])
    params = WaveParams(K4=1.0, atr_len=2, warmup_4h=2)
    piv = run_engine(bars, params)[-1].pivots4h
    assert [(p.i, p.price, p.kind, p.confirmed_at) for p in piv] == [(2, 95.0, -1, 2), (2, 99.75, 1, 3)]
    assert _four_hour(bars)[1][2] == 100.75
    assert _assert_pivots_confirmed_by_the_book(bars, params) == 2


@pytest.mark.parametrize("noise", [0.3, 1.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_pivots_are_confirmed_by_the_book(noise: float, seed: int) -> None:
    params = WaveParams(warmup_4h=50)
    bars, _ = make_path(impulse_abc(), noise, seed, params)
    assert _assert_pivots_confirmed_by_the_book(bars, params) >= 8
```

`tests/waves/test_mirror.py`:
```python
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.waves.engine import WaveState
from tests.synth import impulse_abc, make_path, mirror_bars, random_bars, run_engine

P_REF = 4096.0          # p' = 2*P_REF - p is exact on the 1/64 grid


def _view(s: WaveState, flip: int) -> tuple:
    """Direction-free summary: d flips, phases/points/q/probs/S_rev stay equal under the mirror."""
    return (
        s.ready, s.closed_4h, s.i4h, flip * s.leg_dir, s.E_live_ts, s.s_rev, s.probs, s.q_top,
        tuple((c.pattern, flip * c.d, c.phase, tuple((p.i, p.sub_ts, p.confirmed_at) for p in c.points), c.q)
              for c in s.counts),
        frozenset((pat, flip * d, i) for pat, d, i in s.lineages),
        tuple((p.i, p.sub_ts, flip * p.kind, p.confirmed_at) for p in s.pivots4h),
        None if s.top is None else (s.top.pattern, flip * s.top.d, s.top.phase),
    )


def _assert_mirrored(bars: list, params: WaveParams) -> int:
    up = run_engine(bars, params)
    dn = run_engine(mirror_bars(bars, P_REF), params)
    n_counts = 0
    for a, b in zip(up, dn, strict=True):
        assert _view(a, 1) == _view(b, -1)
        if a.leg_dir:
            assert b.E_live == 2 * P_REF - a.E_live
        assert [2 * P_REF - p.price for p in a.pivots4h] == [p.price for p in b.pivots4h]
        n_counts += len(a.counts)
    return n_counts


def test_mirror_of_synthetic_impulse() -> None:
    params = WaveParams(warmup_4h=50)
    bars, _ = make_path(impulse_abc(), 0.2, 3, params)
    assert _assert_mirrored(bars, params) > 0
    up_top = run_engine(bars, params)[-60].top
    dn_top = run_engine(mirror_bars(bars, P_REF), params)[-60].top
    assert up_top.d == -dn_top.d and up_top.phase == dn_top.phase


def test_mirror_property_on_random_paths() -> None:
    params = WaveParams(K4=1.5, atr_len=5, warmup_4h=10)
    total = []

    @settings(max_examples=25, deadline=None)
    @given(st.integers(0, 2**31 - 1), st.integers(120, 600))
    def check(seed: int, n: int) -> None:
        total.append(_assert_mirrored(random_bars(seed, n), params))

    check()
    assert sum(total) > 0, "vacuous: no random path produced a single count"
```

`tests/waves/test_engine_perf.py`:
```python
from __future__ import annotations

import math
import time

import numpy as np

from ewbot.config import WaveParams
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine
from tests.synth import T0, feed


def _gbm_bars(n: int, seed: int = 0, vol: float = 0.007) -> list[Bar]:
    """BTC-like hourly GBM (fat tails, ~0.7%/h) starting at 1000."""
    rng = np.random.default_rng(seed)
    c = 1000.0 * np.exp(np.cumsum(rng.standard_t(4, n) * vol / math.sqrt(2.0)))
    o = np.concatenate([[1000.0], c[:-1]])
    up = np.abs(rng.normal(0.0, vol / 2.0, n)) * c
    dn = np.abs(rng.normal(0.0, vol / 2.0, n)) * c
    return [Bar(T0 + k * H1, float(o[k]), float(max(o[k], c[k]) + up[k]), float(min(o[k], c[k]) - dn[k]),
                float(c[k]), 1.0) for k in range(n)]


def test_engine_runs_80k_hourly_bars_well_under_three_minutes() -> None:
    bars = _gbm_bars(80_000)
    for k4 in (2.0, 3.0):                       # the grid's most pivot-dense and the default K4
        eng = WaveEngine(WaveParams(K4=k4))
        t = time.perf_counter()
        n_counts = sum(len(eng.on_1h_close(*x).counts) for x in feed(bars))
        dt = time.perf_counter() - t
        print(f"K4={k4}: 80k bars in {dt:.1f}s, avg counts {n_counts / 80_000:.1f}")
        assert n_counts > 0
        assert dt < 180.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/waves/test_synthetic.py tests/waves/test_no_lookahead.py tests/waves/test_mirror.py tests/waves/test_engine_perf.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'tests.synth'"

- [ ] **Step 3: Write minimal implementation**

`tests/synth.py`:
```python
"""Synthetic Elliott paths for engine tests (spec 10).

pivot grammar (labelled waypoints with chosen Fib ratios)
  -> 1h closes: straight lines between waypoints + Brownian-bridge noise pinned at the waypoints
  -> 1h OHLC (open = previous close, small wicks, each waypoint is the strict first extreme of its legs)
  -> 4h bars by streaming aggregation (``feed``) or by ewbot.data.candles.aggregate_4h (``batch_feed``).
Prices live on a 1/64 grid so mirrored paths (p' = 2*p_ref - p) are bit-exact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

import numpy as np
import pandas as pd

from ewbot.config import WaveParams
from ewbot.data.candles import aggregate_4h, to_bars
from ewbot.indicators import ATR
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState

T0 = 1_700_006_400        # 2023-11-15 00:00 UTC, a multiple of H4
TICK = 1.0 / 64.0

Feed = tuple[Bar, Bar | None, tuple[Bar, ...] | None]


@dataclass(frozen=True, slots=True)
class Waypoint:
    price: float
    label: str            # "" = unlabelled lead-in pivot; else S,1..5 (impulse) or X,A,B,C


def q64(x: float) -> float:
    return round(x / TICK) * TICK


def impulse_abc(s: float = 1000.0, w1: float = 100.0, r2: float = 0.55, w3: float = 1.618,
                r4: float = 0.3, w5: float = 1.0, a: float = 0.9, rb: float = 0.618,
                ca: float = 1.0) -> list[Waypoint]:
    """Falling lead-in (S is the lowest low, P1 breaks the high before S), bull impulse S..P5 with
    the given ratios, a down ABC (|A| = a*W1, |B| = rb*|A|, |C| = ca*|A|), then a bounce so C confirms."""
    k = w1 / 100.0
    lead = [s + 250 * k, s + 150 * k, s + 200 * k, s + 80 * k, s + 130 * k, s + 40 * k, s + 90 * k]
    p1 = s + w1
    p2 = p1 - r2 * w1
    p3 = p2 + w3 * w1
    p4 = p3 - r4 * (p3 - p2)
    p5 = p4 + w5 * w1
    a_end = p5 - a * w1
    b_end = a_end + rb * a * w1
    c_end = b_end - ca * a * w1
    tail = c_end + 0.6 * a * w1
    pts = [Waypoint(q64(x), "") for x in lead]
    pts += [Waypoint(q64(x), lab) for x, lab in
            ((s, "S"), (p1, "1"), (p2, "2"), (p3, "3"), (p4, "4"), (p5, "5"), (a_end, "A"),
             (b_end, "B"), (c_end, "C"), (tail, ""))]
    return pts


def _hours(way: Sequence[Waypoint], hours_per_point: float) -> list[int]:
    hours = [0]
    for a, b in zip(way, way[1:]):
        hours.append(hours[-1] + max(4, round(abs(b.price - a.price) * hours_per_point)))
    return hours


def _ohlc(closes: np.ndarray, hours: list[int], way: Sequence[Waypoint], wick: np.ndarray) -> list[Bar]:
    bars: list[Bar] = []
    seg = 0
    o = closes[0]
    for k, c in enumerate(closes):
        while seg + 1 < len(hours) - 1 and k > hours[seg + 1]:
            seg += 1
        a, b = way[seg].price, way[seg + 1].price
        if k == 0:
            a = b = way[0].price
        top, bot = max(a, b), min(a, b)
        hi_body, lo_body = max(o, c), min(o, c)
        cap_hi = top if hi_body >= top else top - TICK          # only waypoint bars may touch the band edge
        cap_lo = bot if lo_body <= bot else bot + TICK
        h = q64(min(hi_body + wick[k, 0], cap_hi))
        lo = q64(max(lo_body - wick[k, 1], cap_lo))
        bars.append(Bar(T0 + k * H1, float(o), float(max(h, hi_body)), float(min(lo, lo_body)), float(c), 1.0))
        o = c
    return bars


def nominal_thr(way: Sequence[Waypoint], params: WaveParams, hours_per_point: float = 1.0) -> float:
    """K4 x median 4h ATR of the noiseless path (the noise scale unit)."""
    bars = make_path(way, 0.0, 0, params, hours_per_point, thr=1.0)[0]
    atr = ATR(params.atr_len)
    vals = []
    for _, b4, _ in feed(bars):
        if b4 is not None:
            v = atr.update(b4.high, b4.low, b4.close)
            if v is not None:
                vals.append(v)
    return params.K4 * float(np.median(vals))


KNOT_H = 8   # bridge knots every 8 hours: low-frequency noise that barely moves the 4h ATR


def _bridge(n: int, amp: float, rng: np.random.Generator) -> np.ndarray:
    """Brownian bridge over n hourly steps (0 at both ends), knots every KNOT_H hours, linearly
    interpolated, scaled so that max |deviation| == amp."""
    knots = np.unique(np.concatenate([np.arange(0, n, KNOT_H), [n]]))
    walk = np.concatenate([[0.0], np.cumsum(rng.standard_normal(len(knots) - 1))])
    bridge = walk - (knots / n) * walk[-1]
    out = np.interp(np.arange(n + 1), knots, bridge)
    m = float(np.max(np.abs(out)))
    return out * (amp / m) if m > 0 else out


def make_path(way: Sequence[Waypoint], noise: float, seed: int, params: WaveParams,
              hours_per_point: float = 1.0, thr: float | None = None) -> tuple[list[Bar], list[int]]:
    """1h bars through ``way``; bridge amplitude = noise * thr (thr defaults to nominal_thr).
    Returns (bars, hour index of every waypoint)."""
    hours = _hours(way, hours_per_point)
    if thr is None:
        thr = nominal_thr(way, params, hours_per_point)
    amp = noise * thr
    rng = np.random.default_rng(seed)
    closes = np.empty(hours[-1] + 1)
    for (a, b), (h0, h1) in zip(zip(way, way[1:]), zip(hours, hours[1:])):
        n = h1 - h0
        base = a.price + (b.price - a.price) * np.arange(n + 1) / n
        if amp > 0 and n > 1:
            base = base + _bridge(n, amp, rng)
        lo, hi = min(a.price, b.price), max(a.price, b.price)
        seg = np.clip(base, lo + TICK, hi - TICK)
        seg[0], seg[-1] = a.price, b.price
        closes[h0:h1 + 1] = seg
    closes = np.round(closes / TICK) * TICK
    wick = rng.uniform(0.0, 0.3 * amp, size=(len(closes), 2)) if amp > 0 else np.zeros((len(closes), 2))
    return _ohlc(closes, hours, way, wick), hours


def feed(bars1h: Sequence[Bar]) -> Iterator[Feed]:
    """Streaming 1h -> (bar1h, bar4h | None, sub_bars | None); 4h buckets aligned to UTC multiples of 4h,
    emitted only when complete (same semantics as aggregate_4h)."""
    buf: list[Bar] = []
    for b in bars1h:
        if not buf or b.ts // H4 != buf[0].ts // H4:
            buf = []
        buf.append(b)
        if len(buf) == 4 and buf[0].ts % H4 == 0:
            agg = Bar(buf[0].ts, buf[0].open, max(x.high for x in buf), min(x.low for x in buf),
                      buf[-1].close, sum(x.volume for x in buf), any(x.synthetic for x in buf))
            yield b, agg, tuple(buf)
        else:
            yield b, None, None


def batch_feed(bars1h: Sequence[Bar]) -> Iterator[Feed]:
    """Same stream, but the 4h bars come from the batch aggregate_4h on a DataFrame."""
    df = pd.DataFrame({"ts": [b.ts for b in bars1h], "open": [b.open for b in bars1h],
                       "high": [b.high for b in bars1h], "low": [b.low for b in bars1h],
                       "close": [b.close for b in bars1h], "volume": [b.volume for b in bars1h],
                       "synthetic": [b.synthetic for b in bars1h]})
    by_close = {b4.ts + H4 - H1: b4 for b4 in to_bars(aggregate_4h(df))}
    pos = {b.ts: k for k, b in enumerate(bars1h)}
    for b in bars1h:
        b4 = by_close.get(b.ts)
        if b4 is None:
            yield b, None, None
        else:
            k = pos[b.ts]
            yield b, b4, tuple(bars1h[k - 3:k + 1])


def run_engine(bars1h: Sequence[Bar], params: WaveParams, use_cache: bool = True,
               batch: bool = False) -> list[WaveState]:
    eng = WaveEngine(params)
    eng._use_cache = use_cache
    src = batch_feed(bars1h) if batch else feed(bars1h)
    return [eng.on_1h_close(b, b4, sub) for b, b4, sub in src]


def mirror_bars(bars: Sequence[Bar], p_ref: float) -> list[Bar]:
    two = 2.0 * p_ref
    return [Bar(b.ts, two - b.open, two - b.low, two - b.high, two - b.close, b.volume, b.synthetic)
            for b in bars]


def recovered(states: Sequence[WaveState], way: Sequence[Waypoint], hours: Sequence[int]) -> bool:
    """True iff the final 4h pivots are exactly the designed waypoints (all but the unconfirmed tail)."""
    piv = states[-1].pivots4h
    want = [(T0 + h * H1, w.price) for w, h in zip(way[:-1], hours[:-1])]
    return [(p.sub_ts, p.price) for p in piv] == want


def recovery_curve(levels: Sequence[float], seeds: Sequence[int], params: WaveParams,
                   way: Sequence[Waypoint] | None = None) -> dict[float, float]:
    """Exact-pivot-recovery rate per noise level (noise = bridge amplitude / nominal thr)."""
    way = impulse_abc() if way is None else way
    thr = nominal_thr(way, params)
    out: dict[float, float] = {}
    for lv in levels:
        ok = 0
        for sd in seeds:
            bars, hours = make_path(way, lv, sd, params, thr=thr)
            ok += recovered(run_engine(bars, params), way, hours)
        out[lv] = ok / len(seeds)
    return out


def state_at(states: Sequence[WaveState], hour: int) -> WaveState:
    s = states[hour]
    assert s.ts == T0 + hour * H1
    return s


def label_hours(way: Sequence[Waypoint], hours: Sequence[int]) -> dict[str, int]:
    return {w.label: h for w, h in zip(way, hours) if w.label}



def random_bars(seed: int, n: int, vol: float = 6.0, start: float = 5000.0) -> list[Bar]:
    """Random-walk 1h bars on the 1/64 grid with drifting regimes (property tests, mirror tests)."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0.0, vol / 3.0, size=n // 48 + 1), 48)[:n]
    closes = np.round((start + np.cumsum(rng.standard_t(4, size=n) * vol + drift)) / TICK) * TICK
    wick = np.round(np.abs(rng.normal(0.0, vol / 2.0, size=(n, 2))) / TICK) * TICK
    bars, o = [], float(closes[0])
    for k in range(n):
        c = float(closes[k])
        bars.append(Bar(T0 + k * H1, o, max(o, c) + float(wick[k, 0]), min(o, c) - float(wick[k, 1]), c, 1.0))
        o = c
    return bars
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/waves/test_synthetic.py tests/waves/test_no_lookahead.py tests/waves/test_mirror.py tests/waves/test_engine_perf.py -v -s`
Expected: PASS (38 passed). The run prints the recovery curve, e.g. `{0.0: 1.0, 0.3: 1.0, 1.0: 1.0, 2.0: 0.83, 3.0: 0.0}`, and the 80k-bar timings (about 1–2 s each).

Then run the whole wave suite: `uv run pytest tests/waves -q` → `125 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/synth.py tests/waves/test_synthetic.py tests/waves/test_no_lookahead.py tests/waves/test_mirror.py tests/waves/test_engine_perf.py
git commit -m "test(waves): synthetic Elliott paths, recovery curve, no-lookahead and mirror properties"
```


---

## Part B2 — Strategies

Tasks T11–T14 add `ewbot.strategy`, which turns each `WaveState` into an optional `Signal`:
- `choch.py` has the 1h trigger (breakout of the E-bar neighbourhood, spec 5.2; the code keeps the name `ChochDetector`).
- `base.py` has `Signal`, the R gate, the time stop and the `Strategy` protocol.
- `elliott.py` has strategy A.
- `fib_swing.py` has baseline C.
- `hybrid.py` has the A+C hybrid and `make_strategy`.

Every strategy is a pure function of `(bar, ws, choch)` and keeps no state between bars. Tests build `WaveState`/`Count`/`Pivot` objects by hand and use a fake CHoCH, so the strategy tests stay unit-level. Only the `ChochDetector` tests feed real bars, on hand-made 1h series.

### Decisions this part fixes (gaps in the contract/spec, binding for T11–T14)

1. **Trigger level (spec 5.2).** For a long, H1 is `max(high)` of the `choch_bars = 3` 1h bars immediately **before** the E bar (the 1h bar where `E_live` occurred, `E_ts`). The E bar itself and later bars are excluded. With fewer than 3 earlier bars the available ones are used; with none, the level is `None` and nothing triggers. When `E_live` moves to a new bar the level is recomputed from the new E bar. The trigger is `close_t > H1 and close_{t-1} <= H1`. Shorts are the mirror image (`min(low)`, close below). No 1h ZigZag is used. Why: the earlier rule (last 1h pivot of a K1 = 2 ZigZag) put the level a median 2.9–3.7 ATR4h from E on 2017–2023 BTC, at or beyond the 4h confirmation threshold (K4 × ATR4h, K4 = 2–4), so the 4h pivot usually confirmed first, the count's phase advanced and the setup expired untriggered (A: about 3 trades/year). The 3-bar level sits a median 1.4–1.9 ATR4h from E at every grid K4, below the 4h confirmation. This choice used trade mechanics only, no performance metric. "CHoCH" in names and comments below means this trigger.

   Every strategy passes `leg_start_ts = ws.leg_start.sub_ts` and `E_ts = ws.E_live_ts`.
2. **Trade direction.** `W3` (IMP W2) and `W5` (IMP W4) trade `+d`. `W5_REV` (IMP W5) and `C_END` (ABC C) trade `-d`. The in-progress leg always runs against the trade, so a strategy returns `None` unless `ws.leg_dir == -direction`. It does not depend on `Count.leg_dir`.
3. **H3 cap** is computed locally: if `|W3| < |W1|`, the cap is `P4 + d*|W3|`. Setup `W5` uses `P4 := E_live`, because wave 4 ends at E if the trade works.
4. **Targets are ordered near → far.** `TP1` is always the nearer target. Only `W5_REV` can need a swap: the spec's `E − 0.382·(E − S)` often lies beyond `P4`. Blended R is unchanged by the swap. This keeps "TP1 → stop to breakeven, time stop off" meaningful for T17.
5. **Sanity gates in `_finalize`** (shared by every setup):
   - The stop must be on the loss side of `entry_ref`.
   - `TP1` must be on the profit side of `entry_ref`. A target already behind the market would give fantasy limit fills in the PaperBroker.
   - Then comes the R gate: `blended_r(direction, entry_ref, stop, tp1, tp2, tp1_frac) >= r_gate`, inclusive.
6. **C_END prior swing** ("X 이전 lookback 구간의 최저 확정 low") is the most extreme pivot of kind `d` among the **4** confirmed 4h pivots before X. That is the same 4-pivot window as `g_start` in spec 4.4. If there is none, or it lies on the wrong side of X, there is no trade.
7. **Time-stop reference legs**, in 4h bars between pivot `i` indices:
   - W3/W5: W1 (`P1.i − S.i`)
   - W5_REV: the in-progress W5 (`ws.i4h − P4.i`)
   - C_END: wave A (`A_end.i − X.i`)
   - FIB: the swing being retraced (`anchor.i − base.i`)

   `time_stop_bars` rounds half-up, not with Python's banker's `round`.
8. **`blended_r`** returns `R_TP1` when `tp2 is None`, and `-inf` when risk is ≤ 0.
9. **Hybrid.** The states are checked in the order CONFIDENT → CONTRARY → UNCERTAIN. CONTRARY needs an alive top count: with no counts, `S_rev` carries no Elliott opinion, so the state is UNCERTAIN. This matters for `S_MIN = 0` in the walk-forward grid, where `0 <= -0` would otherwise block C whenever no count is alive. The emitted `reason` is prefixed with the state.
10. **`make_strategy`** is defined in `ewbot/strategy/hybrid.py` and re-exported from `ewbot.strategy`, so both `from ewbot.strategy import make_strategy` and `from ewbot.strategy.hybrid import make_strategy` work.
11. **Engine wiring (for T17).** Construct `ChochDetector(cfg.strategy.choch_bars)`. Call `choch.update(bar)` exactly once per closed 1h bar, in increasing `ts` order (it raises otherwise), before `strategy.on_bar`. `Signal.ts` is the trigger bar's `ts`, and `Signal.entry_ref` is its close.

### Task 11: 1h trigger detector (E-bar neighbourhood breakout)

**Files:**
- Create (only if T1 did not already create it): `src/ewbot/strategy/__init__.py`
- Create: `src/ewbot/strategy/choch.py`
- Test: `tests/strategy/test_choch.py`

**Interfaces:**
- Consumes: `ewbot.types.Bar`, `ewbot.types.H1`. No ZigZag: the trigger needs only the raw 1h history.
- Produces: `ChochDetector(n_bars: int = 3)` with `update(bar1h) -> None`, `level(direction, leg_start_ts, E_ts) -> float | None` and `triggered(direction, leg_start_ts, E_ts) -> bool`. `leg_start_ts` is accepted and ignored, so the strategies' call `choch.triggered(direction, ws.leg_start.sub_ts, ws.E_live_ts)` is the same as with the old pivot rule. It also produces the module-private pure helper `_choch_level(ts, highs, lows, direction, E_ts, n_bars)`, which is unit-tested directly. T17 builds it as `ChochDetector(cfg.strategy.choch_bars)` (`choch_bars = 3`, T1).

- [ ] **Step 1: Write the failing test**

```python
# tests/strategy/test_choch.py
from __future__ import annotations

import pytest

from ewbot.strategy.choch import ChochDetector, _choch_level
from ewbot.types import H1, Bar

T0 = 1_700_000_000 - (1_700_000_000 % H1)

# ---------------------------------------------------------------- pure level logic

TS = [T0 + k * H1 for k in range(20)]
HIGHS = [100.0 + k for k in range(20)]
LOWS = [90.0 + k for k in range(20)]


def test_level_is_max_high_of_n_bars_strictly_before_e_bar() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[15], 3) == 114.0  # max(HIGHS[12:15])


def test_level_ignores_the_e_bar_and_later_bars() -> None:
    highs = list(HIGHS)
    highs[15] = highs[16] = 10_000.0
    assert _choch_level(TS, highs, LOWS, 1, TS[15], 3) == 114.0


def test_level_short_side_is_min_low() -> None:
    assert _choch_level(TS, HIGHS, LOWS, -1, TS[15], 3) == 102.0  # min(LOWS[12:15])


def test_level_uses_fewer_bars_when_history_is_short() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[2], 3) == 101.0


def test_level_none_when_no_bar_precedes_e() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[0], 3) is None


def test_level_rejects_bad_direction() -> None:
    with pytest.raises(ValueError):
        _choch_level(TS, HIGHS, LOWS, 0, TS[15], 3)


# ---------------------------------------------------------------- detector on hand-made series


def build_series(segments: list[tuple[str, int]], start: float = 1000.0) -> list[Bar]:
    """flat: O=C=c, H=c+3, L=c-3 | up: O=L=c, C=c+5, H=c+6 | down: O=H=c, C=c-5, L=c-6"""
    bars: list[Bar] = []
    c = start
    for kind, n in segments:
        for _ in range(n):
            ts = T0 + len(bars) * H1
            if kind == "flat":
                bars.append(Bar(ts, c, c + 3, c - 3, c, 1.0))
            elif kind == "up":
                bars.append(Bar(ts, c, c + 6, c, c + 5, 1.0))
                c += 5
            elif kind == "down":
                bars.append(Bar(ts, c, c, c - 6, c - 5, 1.0))
                c -= 5
            else:
                raise ValueError(kind)
    return bars


def mirror(bars: list[Bar], m: float = 1000.0) -> list[Bar]:
    return [Bar(b.ts, 2 * m - b.open, 2 * m - b.low, 2 * m - b.high, 2 * m - b.close, b.volume) for b in bars]


# idx 0-19 flat | 20-39 up | 40-49 down | 50-53 up (lower high) | 54-75 down (L 959 @75 = E) | 76-105 up
LOWER_HIGH = [("flat", 20), ("up", 20), ("down", 10), ("up", 4), ("down", 22), ("up", 30)]
# idx 0-19 flat | 20-39 up | 40-69 straight down (L 949 @69 = E) | 70-99 up
V_SHAPE = [("flat", 20), ("up", 20), ("down", 30), ("up", 30)]


def run(bars: list[Bar], direction: int, leg_start_idx: int, e_idx: int) -> tuple[list[float | None], list[bool]]:
    det = ChochDetector(n_bars=3)
    levels: list[float | None] = []
    fired: list[bool] = []
    for k, b in enumerate(bars):
        det.update(b)
        if k >= e_idx:
            levels.append(det.level(direction, bars[leg_start_idx].ts, bars[e_idx].ts))
            fired.append(det.triggered(direction, bars[leg_start_idx].ts, bars[e_idx].ts))
        else:
            levels.append(None)
            fired.append(False)
    return levels, fired


def test_long_level_is_three_bar_high_before_e_and_triggers_once_on_cross() -> None:
    bars = build_series(LOWER_HIGH)
    assert bars[75].low == 959.0 and bars[72].high == 980.0
    levels, fired = run(bars, 1, leg_start_idx=39, e_idx=75)
    assert levels[75] == 980.0 and levels[105] == 980.0  # the lower high (1071) is not used
    assert bars[79].close == 980.0 and bars[80].close == 985.0
    assert [k for k, f in enumerate(fired) if f] == [80]  # 980 is not > 980; prev 980 <= 980 counts


def test_v_reversal_same_rule() -> None:
    bars = build_series(V_SHAPE)
    assert bars[69].low == 949.0
    levels, fired = run(bars, 1, leg_start_idx=39, e_idx=69)
    assert levels[69] == 970.0  # max(high[66..68]) = bar 66 open
    assert [k for k, f in enumerate(fired) if f] == [74]


def test_short_side_is_mirror_image() -> None:
    levels, fired = run(mirror(build_series(LOWER_HIGH)), -1, leg_start_idx=39, e_idx=75)
    assert levels[75] == 1020.0  # 2000 - 980
    assert [k for k, f in enumerate(fired) if f] == [80]
    vlevels, vfired = run(mirror(build_series(V_SHAPE)), -1, leg_start_idx=39, e_idx=69)
    assert vlevels[69] == 1030.0  # 2000 - 970
    assert [k for k, f in enumerate(vfired) if f] == [74]


def test_triggered_false_before_two_bars_and_rejects_out_of_order_bars() -> None:
    det = ChochDetector()
    b0 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    det.update(b0)
    assert det.triggered(1, T0 - H1, T0) is False
    with pytest.raises(ValueError):
        det.update(b0)


def test_n_bars_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ChochDetector(n_bars=0)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/strategy/test_choch.py -v`
Expected: FAIL. Collection error `ModuleNotFoundError: No module named 'ewbot.strategy'`, or `No module named 'ewbot.strategy.choch'` if T1 already created the package.

- [ ] **Step 3: Write minimal implementation**

Create `src/ewbot/strategy/__init__.py` with exactly this content, unless T1 already created it. T14 replaces it.
```python
# src/ewbot/strategy/__init__.py
"""Trading strategies (spec section 5)."""
```

Then create `src/ewbot/strategy/choch.py`:

```python
# src/ewbot/strategy/choch.py
"""1h trigger, spec 5.2: breakout of the E-bar neighbourhood ("CHoCH" in names and comments).

* ``level(+1, leg_start_ts, E_ts)`` = H1: max HIGH of the ``n_bars`` 1h bars immediately BEFORE the
  1h bar where E_live occurred (``E_ts``); the E bar itself and later bars are excluded. Fewer bars
  are used if the history is shorter; with no bar before E the level is None.
* ``level(-1, ...)`` = L1: the mirror image (min LOW).
* ``triggered()``: close_t beyond the level and close_{t-1} not beyond it, where t is the last
  bar fed to ``update()``.

``leg_start_ts`` is accepted for interface stability but unused. Only bars already fed are used,
so the detector cannot look ahead.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

from ewbot.types import Bar


def _choch_level(
    ts: Sequence[int],
    highs: Sequence[float],
    lows: Sequence[float],
    direction: int,
    E_ts: int,
    n_bars: int,
) -> float | None:
    """Pure core of ``ChochDetector.level``."""
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    e_idx = bisect_left(ts, E_ts)  # == number of bars strictly before the E bar
    lo = max(0, e_idx - n_bars)
    if lo >= e_idx:
        return None
    if direction == 1:
        return max(highs[lo:e_idx])
    return min(lows[lo:e_idx])


class ChochDetector:
    """Keeps the raw 1h history; call ``update`` once per closed 1h bar before ``triggered``."""

    def __init__(self, n_bars: int = 3) -> None:
        if n_bars < 1:
            raise ValueError("n_bars must be >= 1")
        self._n = n_bars
        self._ts: list[int] = []
        self._high: list[float] = []
        self._low: list[float] = []
        self._close: list[float] = []

    def update(self, bar1h: Bar) -> None:
        if self._ts and bar1h.ts <= self._ts[-1]:
            raise ValueError(f"1h bars must arrive in increasing ts order: {bar1h.ts} after {self._ts[-1]}")
        self._ts.append(bar1h.ts)
        self._high.append(bar1h.high)
        self._low.append(bar1h.low)
        self._close.append(bar1h.close)

    def level(self, direction: int, leg_start_ts: int, E_ts: int) -> float | None:
        return _choch_level(self._ts, self._high, self._low, direction, E_ts, self._n)

    def triggered(self, direction: int, leg_start_ts: int, E_ts: int) -> bool:
        if len(self._close) < 2:
            return False
        lvl = self.level(direction, leg_start_ts, E_ts)
        if lvl is None:
            return False
        c, c_prev = self._close[-1], self._close[-2]
        if direction == 1:
            return c > lvl and c_prev <= lvl
        return c < lvl and c_prev >= lvl
```

- [ ] **Step 4: Run test to verify it passes**
Run: `uv run pytest tests/strategy/test_choch.py -v`
Expected: PASS (11 tests).

If the series tests fail while the `_choch_level` tests pass, print `det.level(1, 0, bars[75].ts)` after feeding the whole `LOWER_HIGH` series. It must be `980.0`, the high of bar 72, which opens at 980. Any other value means the window is off by one: it must be the 3 bars strictly before the E bar, `[e_idx - 3, e_idx)` with `e_idx = bisect_left(ts, E_ts)`.

- [ ] **Step 5: Commit**
```bash
git add src/ewbot/strategy/__init__.py src/ewbot/strategy/choch.py tests/strategy/test_choch.py
git commit -m "feat(strategy): 1h E-bar neighbourhood breakout trigger (spec 5.2)"
```

### Task 12: Strategy base (Signal, R gate, time stop, Strategy protocol)

**Files:**
- Create: `src/ewbot/strategy/base.py`
- Test: `tests/strategy/test_base.py`

**Interfaces:**
- Consumes: `ewbot.config.StrategyParams` (`tp1_frac`, `r_gate`, `time_stop_mult`, `time_stop_min_4h`, `time_stop_max_4h`). It imports `Bar`, `WaveState` and `ChochDetector` under `TYPE_CHECKING` only, for the protocol signature.
- Produces:
  - `Signal`, a frozen dataclass whose fields are exactly those in the contract.
  - `blended_r(direction, entry, stop, tp1, tp2, tp1_frac) -> float`
  - `time_stop_bars(leg_bars_4h, p) -> int`
  - `Strategy`, a `runtime_checkable` Protocol with `name` and `on_bar`.
  - `_finalize(...) -> Signal | None`. It is package-private: it orders the targets, runs the sanity gates and the R gate, and appends `R=x.xx` to `reason`. T13 and T14 use it.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategy/test_base.py
from __future__ import annotations

import dataclasses
import math

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, Strategy, _finalize, blended_r, time_stop_bars


def test_blended_r_long_and_short_are_symmetric() -> None:
    assert blended_r(1, 100.0, 90.0, 110.0, 130.0, 0.5) == pytest.approx(2.0)
    assert blended_r(-1, 100.0, 110.0, 90.0, 70.0, 0.5) == pytest.approx(2.0)


def test_blended_r_weights_and_missing_tp2() -> None:
    assert blended_r(1, 100.0, 90.0, 110.0, 130.0, 0.25) == pytest.approx(2.5)
    assert blended_r(1, 100.0, 90.0, 110.0, None, 0.5) == pytest.approx(1.0)


def test_blended_r_wrong_side_stop_is_minus_inf() -> None:
    assert blended_r(1, 100.0, 100.0, 110.0, 130.0, 0.5) == -math.inf
    assert blended_r(-1, 100.0, 95.0, 90.0, 70.0, 0.5) == -math.inf
    with pytest.raises(ValueError):
        blended_r(0, 100.0, 90.0, 110.0, 130.0, 0.5)


@pytest.mark.parametrize(
    ("leg", "expected"),
    [(10, 15), (9, 14), (7, 12), (3, 12), (0, 12), (-4, 12), (40, 60), (50, 60), (39, 59)],
)
def test_time_stop_bars_default_clip(leg: int, expected: int) -> None:
    assert time_stop_bars(leg, StrategyParams()) == expected


def test_time_stop_bars_custom_params_round_half_up() -> None:
    p = StrategyParams(time_stop_mult=1.5, time_stop_min_4h=1, time_stop_max_4h=100)
    assert time_stop_bars(7, p) == 11  # 10.5 -> 11 (half up, not banker's 10)
    assert time_stop_bars(1, p) == 2  # 1.5 -> 2


def _fin(**kw: object) -> Signal | None:
    base: dict[str, object] = dict(
        ts=0, setup="W3", direction=1, entry_ref=100.0, stop=90.0, tp1=110.0, tp2=130.0,
        risk_mult=1.0, time_stop_4h=15, instance=("IMP", 1, 0, "W2"), lineage=("IMP", 1, 0),
        reason="x", sp=StrategyParams(),
    )
    base.update(kw)
    return _finalize(**base)  # type: ignore[arg-type]


def test_finalize_builds_signal_and_appends_r() -> None:
    sig = _fin()
    assert sig is not None
    assert (sig.stop, sig.tp1, sig.tp2, sig.entry_ref) == (90.0, 110.0, 130.0, 100.0)
    assert sig.reason == "x R=2.00"
    with pytest.raises(dataclasses.FrozenInstanceError):
        sig.stop = 1.0  # type: ignore[misc]


def test_finalize_orders_targets_near_to_far() -> None:
    sig = _fin(direction=-1, entry_ref=100.0, stop=110.0, tp1=60.0, tp2=80.0)
    assert sig is not None and (sig.tp1, sig.tp2) == (80.0, 60.0)


def test_finalize_rejects_bad_sides_and_applies_r_gate_inclusively() -> None:
    assert _fin(stop=101.0) is None  # stop above a long entry
    assert _fin(tp1=99.0, tp2=99.5) is None  # TP1 already behind entry
    # R exactly 1.5 passes (>=): R1 = 1.0, R2 = 2.0
    assert _fin(tp1=110.0, tp2=120.0) is not None
    assert _fin(tp1=110.0, tp2=119.0) is None  # R = 1.45


def test_strategy_protocol_is_runtime_checkable() -> None:
    class Dummy:
        name = "D"

        def on_bar(self, bar, ws, choch):  # type: ignore[no-untyped-def]
            return None

    assert isinstance(Dummy(), Strategy)
    assert not isinstance(object(), Strategy)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/strategy/test_base.py -v`
Expected: FAIL with collection error `ModuleNotFoundError: No module named 'ewbot.strategy.base'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ewbot/strategy/base.py
"""Strategy plumbing shared by every strategy: Signal, R gate, time stop, Strategy protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ewbot.config import StrategyParams

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState


@dataclass(frozen=True, slots=True)
class Signal:
    ts: int
    setup: str  # "W3","W5","W5_REV","C_END","FIB","RANDOM","EMA"
    direction: int
    entry_ref: float  # trigger bar close (R gate / sizing estimate)
    stop: float
    tp1: float
    tp2: float | None
    risk_mult: float  # 1.0 or setup3_risk_mult
    time_stop_4h: int | None  # 4h bars until time stop (None = no time stop)
    instance: tuple[Any, ...] | None  # cooldown key; Count.instance for Elliott
    lineage: tuple[Any, ...] | None  # lineage-kill key; None for non-Elliott
    reason: str


def blended_r(
    direction: int, entry: float, stop: float, tp1: float, tp2: float | None, tp1_frac: float
) -> float:
    """tp1_frac*R_TP1 + (1-tp1_frac)*R_TP2, R = (target-entry)/(entry-stop) signed by direction.

    tp2=None means the whole position is assumed to exit at tp1. A stop on the wrong side of
    entry (zero or negative risk) returns -inf so that every R gate rejects it.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    risk = direction * (entry - stop)
    if risk <= 0.0:
        return float("-inf")
    r1 = direction * (tp1 - entry) / risk
    if tp2 is None:
        return r1
    r2 = direction * (tp2 - entry) / risk
    return tp1_frac * r1 + (1.0 - tp1_frac) * r2


def time_stop_bars(leg_bars_4h: int, p: StrategyParams) -> int:
    """clip(round_half_up(time_stop_mult * leg), time_stop_min_4h, time_stop_max_4h)."""
    raw = math.floor(p.time_stop_mult * max(0, leg_bars_4h) + 0.5)
    return int(min(p.time_stop_max_4h, max(p.time_stop_min_4h, raw)))


def _finalize(
    *,
    ts: int,
    setup: str,
    direction: int,
    entry_ref: float,
    stop: float,
    tp1: float,
    tp2: float | None,
    risk_mult: float,
    time_stop_4h: int | None,
    instance: tuple[Any, ...] | None,
    lineage: tuple[Any, ...] | None,
    reason: str,
    sp: StrategyParams,
) -> Signal | None:
    """Common tail of every setup (strategy-package private).

    1. Order targets near -> far (TP1 is always the nearer one; only setup W5_REV can need a swap).
    2. Reject a stop that is not on the loss side of entry_ref, or a TP1 that is not on the profit side.
    3. R gate: blended_r(direction, entry_ref, stop, tp1, tp2, sp.tp1_frac) >= sp.r_gate.
    """
    if tp2 is not None and direction * (tp2 - tp1) < 0:
        tp1, tp2 = tp2, tp1
    if direction * (entry_ref - stop) <= 0:
        return None
    if direction * (tp1 - entry_ref) <= 0:
        return None
    r = blended_r(direction, entry_ref, stop, tp1, tp2, sp.tp1_frac)
    if r < sp.r_gate:
        return None
    return Signal(
        ts=ts,
        setup=setup,
        direction=direction,
        entry_ref=entry_ref,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        risk_mult=risk_mult,
        time_stop_4h=time_stop_4h,
        instance=instance,
        lineage=lineage,
        reason=f"{reason} R={r:.2f}",
    )


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        """Called every closed 1h bar AFTER WaveEngine and ChochDetector were updated.

        Must NOT check flat/breakers/blackout/cooldown (the engine does). MUST apply the q/S_rev
        gates, the entry zone, the CHoCH trigger and the R gate.
        """
```

- [ ] **Step 4: Run test to verify it passes**
Run: `uv run pytest tests/strategy/test_base.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**
```bash
git add src/ewbot/strategy/base.py tests/strategy/test_base.py
git commit -m "feat(strategy): Signal, blended R gate, time-stop bars, Strategy protocol"
```

### Task 13: Elliott setups (strategy A)

**Files:**
- Create: `src/ewbot/strategy/elliott.py`
- Create: `tests/strategy/conftest.py`. It holds the hand-built `WaveState`/`Count`/`Pivot` builders, `FakeChoch` and the price-mirror helpers, all exposed through the `sb` fixture. It works under any pytest import mode and is reused by T14.
- Test: `tests/strategy/test_elliott.py`

**Interfaces:**
- Consumes:
  - `ewbot.waves.engine.WaveState`: `ready`, `top`, `s_rev`, `E_live`, `E_live_ts`, `leg_dir`, `leg_start`, `pivots4h`, `atr4h`, `i4h`.
  - `ewbot.waves.enumerate.Count`: `pattern`, `d`, `phase`, `points`, `prices`, `q`, `instance`, `lineage`.
  - `ewbot.waves.zigzag.Pivot`: `i`, `sub_ts`, `price`, `kind`.
  - `ewbot.config.StrategyParams`: `setups_enabled`, `Q_MIN`, `S_MIN`, `buf_struct_atr`, `buf_ext_atr`, `setup3_risk_mult`, plus the R-gate and time-stop fields.
  - `ChochDetector.triggered`.
  - `base._finalize` and `base.time_stop_bars`.
- Produces: `ElliottStrategy(sp)` with `name = "A"` and `on_bar(bar, ws, choch) -> Signal | None`. `Signal.instance` is `top.instance` and `Signal.lineage` is `top.lineage`.

Setup table as implemented, bull case. Bear is the mirror image. `ATR` is `ws.atr4h`, `E` is `ws.E_live` at signal time, and every zone bound is inclusive.

| setup | top count | zone on E | dir | stop | TP1 / TP2 (nearer first) | time-stop leg | risk |
|---|---|---|---|---|---|---|---|
| W3 | IMP d W2 (S,P1) | r2=(P1−E)/(P1−S) ∈ [0.382,0.786] | d | S − d·0.25ATR | E+d·\|W1\| / E+d·1.618\|W1\| | P1.i−S.i | 1.0 |
| W5 | IMP d W4 (S,P1,P2,P3) | r4=(P3−E)/(P3−P2) ∈ [0.236,0.5], E beyond P1 | d | P1 − d·0.25ATR | P3 / E+d·max(\|W1\|, 0.618\|P3−S\|) capped at H3(P4:=E) | P1.i−S.i | 1.0 |
| W5_REV | IMP d W5 (S..P4) | d(E−P4) ≥ 0.618\|W1\|, E beyond P3, not beyond H3 cap | −d | E + d·0.5ATR, capped at H3 | E−0.382(E−S) and P4 | ws.i4h−P4.i | setup3_risk_mult |
| C_END | ABC d C (X,A_end,B_end) | d(E−B_end)/\|A\| ∈ [0.786,1.786], E beyond A_end, (X−E)/(X−swing) ≤ 0.786 | −d | E − (−d)·0.5ATR | E+0.618(X−E) / X | A_end.i−X.i | 1.0 |

The gates run in this order. Each one returns `None` on failure.
1. `ws.ready`, a top count, `ws.leg_start` and `ws.atr4h` are all present.
2. The phase maps to a setup that is in `setups_enabled`.
3. `ws.leg_dir == -direction`.
4. `top.q >= Q_MIN` and `ws.s_rev >= S_MIN`.
5. The zone holds. If it fails, CHoCH is not queried.
6. `choch.triggered(direction, ws.leg_start.sub_ts, ws.E_live_ts)`.
7. `_finalize`: target order, sanity checks, then the R gate on `entry_ref = bar.close`.

- [ ] **Step 1: Write the failing test**

First create the shared test builders in `tests/strategy/conftest.py`:

```python
# tests/strategy/conftest.py
"""Hand-built WaveState / Count / Pivot builders for strategy unit tests.

Exposed through the `sb` fixture so the helpers work under any pytest import mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.zigzag import Pivot

T0 = 1_600_000_000 - (1_600_000_000 % H4)  # 4h-aligned origin; 4h bar i opens at T0 + i*H4


def piv(i: int, price: float, kind: int) -> Pivot:
    """4h pivot at bar i; the extreme happened in the 2nd 1h bar of that 4h bar."""
    return Pivot(i=i, sub_ts=T0 + i * H4 + H1, price=float(price), kind=kind, confirmed_at=i + 2)


def count(pattern: str, d: int, phase: str, pts: Sequence[Pivot], q: float = 0.7) -> Count:
    return Count(pattern=pattern, d=d, phase=phase, points=tuple(pts), q=q)


def wave_state(
    *,
    top: Count | None,
    E: float,
    leg_dir: int,
    pivots: Sequence[Pivot],
    atr: float | None = 8.0,
    s_rev: float = 0.5,
    i4h: int | None = None,
    ready: bool = True,
) -> WaveState:
    leg_start = pivots[-1] if pivots else None
    if i4h is None:
        i4h = (leg_start.i if leg_start is not None else 0) + 6
    e_ts = T0 + (i4h - 1) * H4 + 2 * H1  # E_live printed in a 1h bar of the previous 4h bar
    now = T0 + (i4h + 1) * H4 + 2 * H1  # the 1h bar that just closed
    counts = (top,) if top is not None else ()
    return WaveState(
        ts=now,
        ready=ready,
        counts=counts,
        probs=(1.0,) * len(counts),
        top=top,
        q_top=top.q if top is not None else 0.0,
        s_rev=s_rev,
        lineages=frozenset(c.lineage for c in counts),
        E_live=float(E),
        E_live_ts=e_ts,
        leg_dir=leg_dir,
        leg_start=leg_start,
        pivots4h=tuple(pivots),
        new_pivot_4h=None,
        atr4h=atr,
        closed_4h=False,
        i4h=i4h,
    )


def bar_for(ws: WaveState, close: float) -> Bar:
    """The just-closed 1h trigger bar of `ws` with the given close."""
    return Bar(ts=ws.ts, open=close, high=close + 1.0, low=close - 1.0, close=float(close), volume=1.0)


class FakeChoch:
    """Stands in for ChochDetector: answers `fire` and records every query."""

    def __init__(self, fire: bool = True) -> None:
        self.fire = fire
        self.calls: list[tuple[int, int, int]] = []

    def triggered(self, direction: int, leg_start_ts: int, E_ts: int) -> bool:
        self.calls.append((direction, leg_start_ts, E_ts))
        return self.fire

    def level(self, direction: int, leg_start_ts: int, E_ts: int) -> float | None:
        return None


def mirror_pivot(p: Pivot, m: float) -> Pivot:
    return replace(p, price=2 * m - p.price, kind=-p.kind)


def mirror_count(c: Count, m: float) -> Count:
    return Count(pattern=c.pattern, d=-c.d, phase=c.phase, points=tuple(mirror_pivot(p, m) for p in c.points), q=c.q)


def mirror_ws(ws: WaveState, m: float) -> WaveState:
    counts = tuple(mirror_count(c, m) for c in ws.counts)
    return replace(
        ws,
        counts=counts,
        top=counts[0] if counts else None,
        lineages=frozenset(c.lineage for c in counts),
        E_live=2 * m - ws.E_live,
        leg_dir=-ws.leg_dir,
        leg_start=mirror_pivot(ws.leg_start, m) if ws.leg_start is not None else None,
        pivots4h=tuple(mirror_pivot(p, m) for p in ws.pivots4h),
        new_pivot_4h=mirror_pivot(ws.new_pivot_4h, m) if ws.new_pivot_4h is not None else None,
    )


def mirror_bar(b: Bar, m: float) -> Bar:
    return Bar(ts=b.ts, open=2 * m - b.open, high=2 * m - b.low, low=2 * m - b.high, close=2 * m - b.close, volume=b.volume)


@pytest.fixture
def sb() -> SimpleNamespace:
    return SimpleNamespace(
        T0=T0,
        piv=piv,
        count=count,
        ws=wave_state,
        bar=bar_for,
        FakeChoch=FakeChoch,
        mirror_ws=mirror_ws,
        mirror_bar=mirror_bar,
    )
```

Then create `tests/strategy/test_elliott.py`:

```python
# tests/strategy/test_elliott.py
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Strategy
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

STRAT = ElliottStrategy(StrategyParams())


# ---------------------------------------------------------------- bull scenarios (ATR4h = 8)


def scen_w3(sb: SimpleNamespace, E: float = 140.0, close: float = 150.0, q: float = 0.7, s_rev: float = 0.5) -> tuple[WaveState, Bar]:
    # IMP+ W2: S=100 (i10) -> P1=200 (i20); W2 running down, r2 = (200-E)/100
    s, p1 = sb.piv(10, 100, -1), sb.piv(20, 200, 1)
    top = sb.count("IMP", 1, "W2", [s, p1], q=q)
    ws = sb.ws(top=top, E=E, leg_dir=-1, pivots=[sb.piv(2, 120, 1), s, p1], s_rev=s_rev, i4h=26)
    return ws, sb.bar(ws, close)


def scen_w5(sb: SimpleNamespace, E: float = 258.0, close: float = 262.0) -> tuple[WaveState, Bar]:
    # IMP+ W4: S=100 P1=200 P2=110 P3=400; |W1|=100 |W3|=290 (no H3 cap); r4 = (400-E)/290
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 110, -1), sb.piv(30, 400, 1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W4", pts), E=E, leg_dir=-1, pivots=pts, i4h=36)
    return ws, sb.bar(ws, close)


def scen_w5_cap(sb: SimpleNamespace, E: float = 310.0, close: float = 315.0) -> tuple[WaveState, Bar]:
    # IMP+ W4 with |W3|=180 < |W1|=200 -> TP2 capped at E + 180
    pts = [sb.piv(0, 100, -1), sb.piv(10, 300, 1), sb.piv(16, 200, -1), sb.piv(24, 380, 1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W4", pts), E=E, leg_dir=-1, pivots=pts, i4h=30)
    return ws, sb.bar(ws, close)


def scen_w5_rev(sb: SimpleNamespace, E: float = 480.0, close: float = 470.0) -> tuple[WaveState, Bar]:
    # IMP+ W5: S=100 P1=200 P2=150 P3=400 P4=320 (i36); W5 running up; ws.i4h=50 -> W5 leg = 14 bars
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 150, -1), sb.piv(30, 400, 1), sb.piv(36, 320, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W5", pts), E=E, leg_dir=1, pivots=pts, i4h=50)
    return ws, sb.bar(ws, close)


def scen_w5_rev_cap(sb: SimpleNamespace, E: float = 508.0, close: float = 500.0) -> tuple[WaveState, Bar]:
    # IMP+ W5 with |W3|=180 < |W1|=200 -> H3 cap = P4 + 180 = 510
    pts = [sb.piv(0, 100, -1), sb.piv(10, 300, 1), sb.piv(16, 200, -1), sb.piv(24, 380, 1), sb.piv(30, 330, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W5", pts), E=E, leg_dir=1, pivots=pts, i4h=40)
    return ws, sb.bar(ws, close)


def scen_c_end(
    sb: SimpleNamespace,
    E: float = 220.0,
    close: float = 230.0,
    prior: list[tuple[int, float, int]] | None = None,
    b_end: float = 280.0,
) -> tuple[WaveState, Bar]:
    # ABC d=-1 (A down) phase C: X=300 (i20) A_end=240 (i26) B_end=280 (i32); C running down.
    # Prior swing low = lowest low among the 4 pivots before X = 100 -> (300-E)/200 <= 0.786
    prior = [(0, 100.0, -1), (6, 180.0, 1), (10, 150.0, -1)] if prior is None else prior
    x, a, b = sb.piv(20, 300, 1), sb.piv(26, 240, -1), sb.piv(32, b_end, 1)
    pivots = [sb.piv(i, p, k) for i, p, k in prior] + [x, a, b]
    ws = sb.ws(top=sb.count("ABC", -1, "C", [x, a, b]), E=E, leg_dir=-1, pivots=pivots, i4h=38)
    return ws, sb.bar(ws, close)


SCENARIOS: dict[str, Any] = {
    "W3": scen_w3,
    "W5": scen_w5,
    "W5_CAP": scen_w5_cap,
    "W5_REV": scen_w5_rev,
    "W5_REV_CAP": scen_w5_rev_cap,
    "C_END": scen_c_end,
}

EXPECTED: dict[str, dict[str, Any]] = {
    # stop = S - 0.25*8; TP1 = E + |W1|; TP2 = E + 1.618|W1|; time stop 1.5*W1(10 bars) = 15
    "W3": dict(setup="W3", direction=1, entry_ref=150.0, stop=98.0, tp1=240.0, tp2=301.8,
               time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 10, "W2"), lineage=("IMP", 1, 10)),
    # stop = P1 - 2; TP1 = P3; TP2 = max(258+100, 258+0.618*300=443.4)
    "W5": dict(setup="W5", direction=1, entry_ref=262.0, stop=198.0, tp1=400.0, tp2=443.4,
               time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 0, "W4"), lineage=("IMP", 1, 0)),
    # TP2 = min(cap 310+180=490, max(310+200, 310+0.618*280)=510) = 490
    "W5_CAP": dict(setup="W5", direction=1, entry_ref=315.0, stop=298.0, tp1=380.0, tp2=490.0,
                   time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 0, "W4"), lineage=("IMP", 1, 0)),
    # short: stop = E + 0.5*8 = 484; TP1 = 480 - 0.382*380 = 334.84; TP2 = P4; time stop 1.5*14 = 21
    "W5_REV": dict(setup="W5_REV", direction=-1, entry_ref=470.0, stop=484.0, tp1=334.84, tp2=320.0,
                   time_stop_4h=21, risk_mult=0.5, instance=("IMP", 1, 0, "W5"), lineage=("IMP", 1, 0)),
    # stop = min(cap 510, 508 + 4) = 510; TP1 = 508 - 0.382*408 = 352.144; time stop 1.5*10 = 15
    "W5_REV_CAP": dict(setup="W5_REV", direction=-1, entry_ref=500.0, stop=510.0, tp1=352.144, tp2=330.0,
                       time_stop_4h=15, risk_mult=0.5, instance=("IMP", 1, 0, "W5"), lineage=("IMP", 1, 0)),
    # long: stop = E - 4 = 216; TP1 = 220 + 0.618*80 = 269.44; TP2 = X; time stop 1.5*A(6) = 9 -> 12
    "C_END": dict(setup="C_END", direction=1, entry_ref=230.0, stop=216.0, tp1=269.44, tp2=300.0,
                  time_stop_4h=12, risk_mult=1.0, instance=("ABC", -1, 20, "C"), lineage=("ABC", -1, 20)),
}


def assert_signal(sig: Any, exp: dict[str, Any]) -> None:
    assert sig is not None
    for key, val in exp.items():
        got = getattr(sig, key)
        if isinstance(val, float):
            assert got == pytest.approx(val), key
        else:
            assert got == val, key


# ---------------------------------------------------------------- every setup fires


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_setup_fires_with_expected_levels(sb: SimpleNamespace, name: str) -> None:
    ws, bar = SCENARIOS[name](sb)
    choch = sb.FakeChoch()
    sig = STRAT.on_bar(bar, ws, choch)
    assert_signal(sig, EXPECTED[name])
    assert sig.ts == bar.ts
    assert sig.reason.startswith(EXPECTED[name]["setup"] + " ")
    assert choch.calls == [(EXPECTED[name]["direction"], ws.leg_start.sub_ts, ws.E_live_ts)]


def test_w5_rev_orders_targets_near_to_far(sb: SimpleNamespace) -> None:
    # E=450: spec TP1 = 450 - 0.382*350 = 316.3 lies BEYOND TP2 = P4 = 320 -> swapped
    ws, bar = scen_w5_rev(sb, E=450.0, close=440.0)
    sig = STRAT.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.stop == pytest.approx(454.0)
    assert (sig.tp1, sig.tp2) == (pytest.approx(320.0), pytest.approx(316.3))


# ---------------------------------------------------------------- bear = mirror image


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bear_mirror_of_each_setup(sb: SimpleNamespace, name: str) -> None:
    m = 1000.0
    ws, bar = SCENARIOS[name](sb)
    bull = STRAT.on_bar(bar, ws, sb.FakeChoch())
    choch = sb.FakeChoch()
    bear = STRAT.on_bar(sb.mirror_bar(bar, m), sb.mirror_ws(ws, m), choch)
    assert bull is not None and bear is not None
    assert bear.setup == bull.setup
    assert bear.direction == -bull.direction
    assert choch.calls[0][0] == -bull.direction
    for key in ("entry_ref", "stop", "tp1", "tp2"):
        assert getattr(bear, key) == pytest.approx(2 * m - getattr(bull, key)), key
    assert bear.time_stop_4h == bull.time_stop_4h
    assert bear.risk_mult == bull.risk_mult
    pattern, d, i0, phase = bull.instance
    assert bear.instance == (pattern, -d, i0, phase)
    assert bear.lineage == (pattern, -d, i0)


def test_bear_w3_explicit_numbers(sb: SimpleNamespace) -> None:
    # IMP- W2: S=400 -> P1=300, W2 bouncing to E=360 (r2=0.6); short on close 350
    s, p1 = sb.piv(10, 400, 1), sb.piv(20, 300, -1)
    ws = sb.ws(top=sb.count("IMP", -1, "W2", [s, p1]), E=360.0, leg_dir=1, pivots=[s, p1], i4h=26)
    sig = STRAT.on_bar(sb.bar(ws, 350.0), ws, sb.FakeChoch())
    assert_signal(sig, dict(setup="W3", direction=-1, stop=402.0, tp1=260.0, tp2=198.2, time_stop_4h=15, risk_mult=1.0))


# ---------------------------------------------------------------- gates (W3 scenario unless noted)


def test_q_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb, q=0.59)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w3(sb, q=0.6)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is not None


def test_s_rev_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb, s_rev=0.2)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w3(sb, s_rev=0.25)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is not None


@pytest.mark.parametrize(("E", "close"), [(170.0, 175.0), (115.0, 125.0)])  # r2 = 0.30 / 0.85
def test_w3_zone_gate_and_choch_not_consulted(sb: SimpleNamespace, E: float, close: float) -> None:
    ws, bar = scen_w3(sb, E=E, close=close)
    choch = sb.FakeChoch()
    assert STRAT.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_disabled_setup_is_ignored(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    strat = ElliottStrategy(StrategyParams(setups_enabled=("W5", "W5_REV", "C_END")))
    assert strat.on_bar(bar, ws, sb.FakeChoch()) is None


def test_r_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)  # blended R = 0.5*90/52 + 0.5*151.8/52 = 2.325
    assert ElliottStrategy(StrategyParams(r_gate=2.3)).on_bar(bar, ws, sb.FakeChoch()) is not None
    assert ElliottStrategy(StrategyParams(r_gate=2.4)).on_bar(bar, ws, sb.FakeChoch()) is None


def test_no_choch_no_signal(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch(fire=False)) is None


def test_not_ready_missing_top_atr_or_inconsistent_leg(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    choch = sb.FakeChoch()
    assert STRAT.on_bar(bar, replace(ws, ready=False), choch) is None
    assert STRAT.on_bar(bar, replace(ws, top=None, counts=(), probs=()), choch) is None
    assert STRAT.on_bar(bar, replace(ws, atr4h=None), choch) is None
    assert STRAT.on_bar(bar, replace(ws, leg_dir=1), choch) is None
    assert STRAT.on_bar(bar, replace(ws, leg_start=None), choch) is None


def test_phases_without_setup_never_fire(sb: SimpleNamespace) -> None:
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 150, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W3", pts), E=300.0, leg_dir=1, pivots=pts)
    assert STRAT.on_bar(sb.bar(ws, 290.0), ws, sb.FakeChoch()) is None
    ab = [sb.piv(0, 300, 1), sb.piv(10, 240, -1)]
    ws = sb.ws(top=sb.count("ABC", -1, "B", ab), E=270.0, leg_dir=1, pivots=ab)
    assert STRAT.on_bar(sb.bar(ws, 265.0), ws, sb.FakeChoch()) is None


def test_w5_zone_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen_w5(sb, E=380.0, close=385.0)  # r4 = 20/290 = 0.069 < 0.236
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_cap(sb, E=295.0, close=300.0)  # r4 = 0.472 in zone but E < P1 = 300
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_w5_rev_zone_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen_w5_rev_cap(sb, E=515.0, close=505.0)  # beyond H3 cap 510
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_rev_cap(sb, E=440.0, close=430.0)  # W5 = 110 < 0.618*200
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_rev(sb, E=395.0, close=385.0)  # W5 = 75 ok but E not beyond P3 = 400
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_c_end_zone_gates(sb: SimpleNamespace) -> None:
    for E, close in ((235.0, 238.0), (170.0, 180.0)):  # C/A = 0.75 and 1.833
        ws, bar = scen_c_end(sb, E=E, close=close)
        assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # prior swing low 230 -> (300-220)/(300-230) = 1.14 > 0.786
    ws, bar = scen_c_end(sb, prior=[(0, 230.0, -1), (6, 270.0, 1), (10, 250.0, -1)])
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # no confirmed pivot before X -> no prior swing -> no trade
    ws, bar = scen_c_end(sb, prior=[])
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # B_end=290: E=241 gives C/A = 49/60 = 0.817 in zone, but E has not passed A_end = 240
    ws, bar = scen_c_end(sb, E=241.0, close=245.0, b_end=290.0)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_c_end_prior_swing_lookback_is_four_pivots(sb: SimpleNamespace) -> None:
    # The low of 10 at i0 would allow the trade but lies 5 pivots before X -> ignored -> swing 230
    prior = [(0, 10.0, -1), (2, 260.0, 1), (6, 230.0, -1), (8, 270.0, 1), (10, 250.0, -1)]
    ws, bar = scen_c_end(sb, prior=prior)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_is_a_strategy() -> None:
    assert STRAT.name == "A"
    assert isinstance(STRAT, Strategy)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/strategy/test_elliott.py -v`
Expected: FAIL with collection error `ModuleNotFoundError: No module named 'ewbot.strategy.elliott'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ewbot/strategy/elliott.py
"""Strategy A: the four Elliott setups of spec 5.1, bull and bear symmetric.

Trade direction per top-count phase (d = Count.d; for ABC d is the direction of wave A):
  W3     = IMP W2 -> trade  d   (buy the end of wave 2)
  W5     = IMP W4 -> trade  d   (buy the end of wave 4)
  W5_REV = IMP W5 -> trade -d   (fade the end of wave 5, risk_mult = setup3_risk_mult)
  C_END  = ABC C  -> trade -d   (bull C_END = ABC d=-1 -> long)
The in-progress leg always runs against the trade, so ws.leg_dir must equal -direction and the
CHoCH is asked for `direction`. All zone checks use ws.E_live; targets are computed from
ws.E_live at signal time and never change afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, _finalize, time_stop_bars

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState
    from ewbot.waves.enumerate import Count
    from ewbot.waves.zigzag import Pivot

_SETUP_OF: dict[tuple[str, str], str] = {
    ("IMP", "W2"): "W3",
    ("IMP", "W4"): "W5",
    ("IMP", "W5"): "W5_REV",
    ("ABC", "C"): "C_END",
}
_WITH_TREND = frozenset({"W3", "W5"})

_W3_ZONE = (0.382, 0.786)  # r2 = |W2|/|W1|
_W3_TP1_W1 = 1.0
_W3_TP2_W1 = 1.618
_W5_ZONE = (0.236, 0.5)  # r4 = |W4|/|W3|
_W5_TP2_SP3 = 0.618  # TP2 alt = E + 0.618*(P3 - S)
_REV_MIN_W5_W1 = 0.618  # W5 >= 0.618*|W1|
_REV_TP1_RETR = 0.382  # TP1 = E - 0.382*(E - S)
_C_ZONE = (0.786, 1.786)  # |C|/|A|
_C_MAX_RETRACE = 0.786  # (X - E)/(X - prior swing extreme)
_C_TP1_RETR = 0.618  # TP1 = E + 0.618*(X - E)
_SWING_LOOKBACK = 4  # confirmed 4h pivots before X searched for the prior swing extreme


@dataclass(frozen=True, slots=True)
class _Plan:
    stop: float
    tp1: float
    tp2: float | None
    leg_bars_4h: int
    risk_mult: float
    detail: str


def _h3_cap(d: int, s: float, p1: float, p2: float, p3: float, p4: float) -> float | None:
    """Spec 4.3 H3 while wave 5 runs: if |W3| < |W1| then W5 may not exceed P4 + d*|W3|."""
    w1 = abs(p1 - s)
    w3 = abs(p3 - p2)
    return p4 + d * w3 if w3 < w1 else None


def _in(x: float, zone: tuple[float, float]) -> bool:
    return zone[0] <= x <= zone[1]


def _plan_w3(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    if w1 <= 0.0:
        return None
    r2 = (p1 - e) / (p1 - s)
    if not _in(r2, _W3_ZONE):
        return None
    return _Plan(
        stop=s - d * sp.buf_struct_atr * atr,
        tp1=e + d * _W3_TP1_W1 * w1,
        tp2=e + d * _W3_TP2_W1 * w1,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # W1
        risk_mult=1.0,
        detail=f"r2={r2:.3f}",
    )


def _plan_w5(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1, p2, p3 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    w3 = abs(p3 - p2)
    if w1 <= 0.0 or w3 <= 0.0:
        return None
    r4 = (p3 - e) / (p3 - p2)
    if not _in(r4, _W5_ZONE) or d * (e - p1) <= 0.0:
        return None
    tp2 = e + d * max(w1, _W5_TP2_SP3 * abs(p3 - s))
    cap = _h3_cap(d, s, p1, p2, p3, e)  # E becomes P4 if wave 4 ends here
    if cap is not None and d * (tp2 - cap) > 0.0:
        tp2 = cap
    return _Plan(
        stop=p1 - d * sp.buf_struct_atr * atr,
        tp1=p3,
        tp2=tp2,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # W1
        risk_mult=1.0,
        detail=f"r4={r4:.3f}",
    )


def _plan_w5_rev(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1, p2, p3, p4 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    if w1 <= 0.0:
        return None
    w5 = d * (e - p4)
    if w5 < _REV_MIN_W5_W1 * w1 or d * (e - p3) <= 0.0:
        return None
    cap = _h3_cap(d, s, p1, p2, p3, p4)
    if cap is not None and d * (e - cap) > 0.0:
        return None  # beyond the H3 bound: the count is dead
    stop = e + d * sp.buf_ext_atr * atr
    if cap is not None and d * (stop - cap) > 0.0:
        stop = cap
    return _Plan(
        stop=stop,
        tp1=e - _REV_TP1_RETR * (e - s),
        tp2=p4,
        leg_bars_4h=ws.i4h - c.points[4].i,  # W5 is still in progress
        risk_mult=sp.setup3_risk_mult,
        detail=f"w5/w1={w5 / w1:.3f}",
    )


def _prior_pivots(pivots: Sequence[Pivot], before_i: int, n: int) -> list[Pivot]:
    """Up to n confirmed pivots with index < before_i, scanning back from the newest."""
    out: list[Pivot] = []
    for p in reversed(pivots):
        if p.i >= before_i:
            continue
        out.append(p)
        if len(out) == n:
            break
    return out


def _plan_c_end(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    x, a_end, b_end = c.prices
    d, e = c.d, ws.E_live  # d = direction of wave A (and of the in-progress wave C)
    a = abs(a_end - x)
    c_len = d * (e - b_end)
    if a <= 0.0 or c_len <= 0.0:
        return None
    c_over_a = c_len / a
    if not _in(c_over_a, _C_ZONE) or d * (e - a_end) <= 0.0:
        return None
    swings = [p.price for p in _prior_pivots(ws.pivots4h, c.points[0].i, _SWING_LOOKBACK) if p.kind == d]
    if not swings:
        return None
    swing = max(swings, key=lambda v: d * v)  # lowest low before X for a bull C_END
    denom = x - swing
    if -d * denom <= 0.0:
        return None
    retrace = (x - e) / denom
    if retrace > _C_MAX_RETRACE:
        return None
    t = -d
    return _Plan(
        stop=e - t * sp.buf_ext_atr * atr,
        tp1=e + _C_TP1_RETR * (x - e),
        tp2=x,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # wave A
        risk_mult=1.0,
        detail=f"c/a={c_over_a:.3f} retr={retrace:.3f}",
    )


_PLANNERS: dict[str, Callable[[Count, WaveState, StrategyParams, float], _Plan | None]] = {
    "W3": _plan_w3,
    "W5": _plan_w5,
    "W5_REV": _plan_w5_rev,
    "C_END": _plan_c_end,
}


class ElliottStrategy:
    name = "A"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        sp = self.sp
        top = ws.top
        if not ws.ready or top is None or ws.leg_start is None or ws.atr4h is None:
            return None
        setup = _SETUP_OF.get((top.pattern, top.phase))
        if setup is None or setup not in sp.setups_enabled:
            return None
        direction = top.d if setup in _WITH_TREND else -top.d
        if ws.leg_dir != -direction:
            return None
        if top.q < sp.Q_MIN or ws.s_rev < sp.S_MIN:
            return None
        plan = _PLANNERS[setup](top, ws, sp, ws.atr4h)
        if plan is None:
            return None
        if not choch.triggered(direction, ws.leg_start.sub_ts, ws.E_live_ts):
            return None
        return _finalize(
            ts=bar.ts,
            setup=setup,
            direction=direction,
            entry_ref=bar.close,
            stop=plan.stop,
            tp1=plan.tp1,
            tp2=plan.tp2,
            risk_mult=plan.risk_mult,
            time_stop_4h=time_stop_bars(plan.leg_bars_4h, sp),
            instance=top.instance,
            lineage=top.lineage,
            reason=(
                f"{setup} {top.pattern}{top.d:+d} {top.phase} {plan.detail} "
                f"q={top.q:.3f} s_rev={ws.s_rev:.3f} E={ws.E_live:.2f}"
            ),
            sp=sp,
        )
```

- [ ] **Step 4: Run test to verify it passes**
Run: `uv run pytest tests/strategy/test_elliott.py -v`
Expected: PASS (28 tests). This covers the following:
- Each setup fires, plus the W5 TP2 H3-cap variant, the W5_REV stop H3-cap variant and the W5_REV target swap.
- The bear mirror of every setup.
- Every gate blocks on its own: q, S_rev, zone per setup, disabled setup, R gate, no CHoCH, not ready, no top, no ATR, inconsistent leg, phases with no setup, and the C_END lookback.

- [ ] **Step 5: Commit**
```bash
git add src/ewbot/strategy/elliott.py tests/strategy/conftest.py tests/strategy/test_elliott.py
git commit -m "feat(strategy): Elliott setups W3/W5/W5_REV/C_END, bull+bear symmetric (spec 5.1-5.3)"
```

### Task 14: Fib-swing baseline (C), Hybrid (A+C) and strategy factory

**Files:**
- Create: `src/ewbot/strategy/fib_swing.py`
- Create: `src/ewbot/strategy/hybrid.py`
- Modify: `src/ewbot/strategy/__init__.py`. Replace the whole file with the re-exports below.
- Test: `tests/strategy/test_fib_swing.py`, `tests/strategy/test_hybrid.py` (they reuse `tests/strategy/conftest.py` from T13)

**Interfaces:**
- Consumes: `WaveState` (`ready`, `top`, `s_rev`, `E_live`, `E_live_ts`, `leg_dir`, `leg_start`, `pivots4h`, `atr4h`), `Pivot`, `StrategyParams` (`mode`, `Q_MIN`, `S_MIN`, `buf_struct_atr`, R-gate and time-stop fields), `ChochDetector.triggered`, `base._finalize`, `base.time_stop_bars`, `ElliottStrategy`.
- Produces:
  - `FibSwingStrategy(sp)` with `name = "C"`. Its signals have `setup="FIB"`, `risk_mult=1.0`, `instance=None` and `lineage=None`. Stop `L − buf_struct_atr·ATR4h`, `TP1 = E + 1.0·(H − L)`, `TP2 = E + 1.618·(H − L)` (spec 5.5). This is setup ① (W3) geometry with S → L and P1 → H, so A versus C isolates the Elliott count filter. The earlier targets (`TP1 = H`, `TP2 = E + (H − L)`) give blended R ≤ 0.5·(1 + r)/(1 − r), which is below the 1.5 gate for every r < 0.5 before any entry distance, so C almost never traded (1–3 trades in 2017–2023).
  - `HybridStrategy(sp)` with `name = "HYBRID"`, `classify(ws) -> "CONFIDENT" | "CONTRARY" | "UNCERTAIN"` and `on_bar`.
  - `make_strategy(sp) -> Strategy`, chosen by `sp.mode` from `"A"`, `"C"` and `"HYBRID"`. Any other mode raises `ValueError`.
  - Package re-exports from `ewbot.strategy`.

#### Cycle A: Fib-swing baseline

- [ ] **Step 1: Write the failing test**

```python
# tests/strategy/test_fib_swing.py
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Strategy
from ewbot.strategy.fib_swing import FibSwingStrategy
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

FIB = FibSwingStrategy(StrategyParams())

UPTREND = [(0, 100.0, -1), (8, 200.0, 1), (14, 150.0, -1), (24, 300.0, 1)]  # HH 300>200, HL 150>100


def scen(
    sb: SimpleNamespace,
    pivots: list[tuple[int, float, int]] = UPTREND,
    E: float = 185.0,
    close: float = 192.0,
    leg_dir: int = -1,
) -> tuple[WaveState, Bar]:
    ws = sb.ws(top=None, E=E, leg_dir=leg_dir, pivots=[sb.piv(i, p, k) for i, p, k in pivots], s_rev=0.0, i4h=30)
    return ws, sb.bar(ws, close)


def test_long_in_uptrend_pullback(sb: SimpleNamespace) -> None:
    # H=300 (i24), L=150 (i14), E=185 -> r = 115/150 = 0.767; stop = 150 - 0.25*8;
    # TP1 = 185 + 1.0*150, TP2 = 185 + 1.618*150 (setup W3 geometry)
    ws, bar = scen(sb)
    choch = sb.FakeChoch()
    sig = FIB.on_bar(bar, ws, choch)
    assert sig is not None
    assert (sig.setup, sig.direction, sig.entry_ref) == ("FIB", 1, 192.0)
    assert sig.stop == pytest.approx(148.0)
    assert sig.tp1 == pytest.approx(335.0)
    assert sig.tp2 == pytest.approx(427.7)
    assert sig.time_stop_4h == 15  # 1.5 * (24 - 14)
    assert sig.risk_mult == 1.0
    assert sig.instance is None and sig.lineage is None
    assert choch.calls == [(1, ws.leg_start.sub_ts, ws.E_live_ts)]


def test_short_in_downtrend_rally_is_mirror(sb: SimpleNamespace) -> None:
    # highs 1300 -> 1250 (LH), lows 1200 -> 1100 (LL); E = 1215 (prices high enough that TP2 stays > 0)
    down = [(0, 1300.0, 1), (8, 1200.0, -1), (14, 1250.0, 1), (24, 1100.0, -1)]
    ws, bar = scen(sb, pivots=down, E=1215.0, close=1208.0, leg_dir=1)
    choch = sb.FakeChoch()
    sig = FIB.on_bar(bar, ws, choch)
    assert sig is not None
    assert (sig.setup, sig.direction) == ("FIB", -1)
    assert sig.stop == pytest.approx(1252.0)
    assert sig.tp1 == pytest.approx(1065.0)  # 1215 - 1.0*150
    assert sig.tp2 == pytest.approx(972.3)  # 1215 - 1.618*150
    assert sig.time_stop_4h == 15
    assert choch.calls[0][0] == -1


def test_generic_mirror_matches(sb: SimpleNamespace) -> None:
    m = 1000.0
    ws, bar = scen(sb)
    bull = FIB.on_bar(bar, ws, sb.FakeChoch())
    bear = FIB.on_bar(sb.mirror_bar(bar, m), sb.mirror_ws(ws, m), sb.FakeChoch())
    assert bull is not None and bear is not None
    assert bear.direction == -bull.direction
    for key in ("entry_ref", "stop", "tp1", "tp2"):
        assert getattr(bear, key) == pytest.approx(2 * m - getattr(bull, key))


def test_no_trend_no_signal(sb: SimpleNamespace) -> None:
    hh_ll = [(0, 100.0, -1), (8, 200.0, 1), (14, 90.0, -1), (24, 300.0, 1)]  # HH but LL
    ws, bar = scen(sb, pivots=hh_ll, E=150.0, close=160.0)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None
    too_few = [(8, 200.0, 1), (14, 150.0, -1), (24, 300.0, 1)]  # only one low
    ws, bar = scen(sb, pivots=too_few)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None


def test_leg_with_the_trend_no_signal(sb: SimpleNamespace) -> None:
    # still HH/HL (lows 150 -> 250) but the in-progress leg is rising
    ws, bar = scen(sb, pivots=UPTREND + [(30, 250.0, -1)], E=320.0, close=318.0, leg_dir=1)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None
    # same numbers as the firing long case, only the leg direction flipped -> blocked by leg_dir alone
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, replace(ws, leg_dir=1), sb.FakeChoch()) is None


@pytest.mark.parametrize(("E", "close"), [(280.0, 285.0), (140.0, 150.0)])  # r = 0.133 / 1.067
def test_zone_gate(sb: SimpleNamespace, E: float, close: float) -> None:
    ws, bar = scen(sb, E=E, close=close)
    choch = sb.FakeChoch()
    assert FIB.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_choch_and_r_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, ws, sb.FakeChoch(fire=False)) is None
    # blended R = 0.5*143/44 + 0.5*235.7/44 = 4.303
    assert FibSwingStrategy(StrategyParams(r_gate=4.30)).on_bar(bar, ws, sb.FakeChoch()) is not None
    assert FibSwingStrategy(StrategyParams(r_gate=4.31)).on_bar(bar, ws, sb.FakeChoch()) is None


def test_not_ready_or_no_atr(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, replace(ws, ready=False), sb.FakeChoch()) is None
    assert FIB.on_bar(bar, replace(ws, atr4h=None), sb.FakeChoch()) is None


def test_is_a_strategy() -> None:
    assert FIB.name == "C"
    assert isinstance(FIB, Strategy)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/strategy/test_fib_swing.py -v`
Expected: FAIL with collection error `ModuleNotFoundError: No module named 'ewbot.strategy.fib_swing'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ewbot/strategy/fib_swing.py
"""Strategy C: Fibonacci swing baseline (spec 5.5), long and short symmetric.

Trend from the last two confirmed 4h highs and lows: HH+HL = up (+1), LH+LL = down (-1), else none.
Up-trend with the in-progress leg falling: anchor H = last confirmed high, base L = the confirmed
low before it; long when r = (H - E)/(H - L) in [0.382, 0.786], E > L, the 1h trigger up and the R
gate pass. Stop = L - buf_struct_atr*ATR4h, TP1 = E + 1.0*(H - L), TP2 = E + 1.618*(H - L) (setup W3
geometry with S->L, P1->H, so A vs C isolates the Elliott count filter). Down-trend is the mirror image.
Time-stop reference leg = the swing L -> H (base.i -> anchor.i).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, _finalize, time_stop_bars

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState
    from ewbot.waves.zigzag import Pivot

_FIB_ZONE = (0.382, 0.786)
_FIB_TP1_SWING = 1.0  # TP1 = E + 1.0 * (H - L)   (same geometry as Elliott setup W3)
_FIB_TP2_SWING = 1.618  # TP2 = E + 1.618 * (H - L)


def _last_of_kind(pivots: Sequence[Pivot], kind: int, n: int, before_i: int | None = None) -> list[Pivot]:
    """Newest-first list of up to n pivots of `kind` (optionally only those with i < before_i)."""
    out: list[Pivot] = []
    for p in reversed(pivots):
        if before_i is not None and p.i >= before_i:
            continue
        if p.kind == kind:
            out.append(p)
            if len(out) == n:
                break
    return out


def _swing_trend(pivots: Sequence[Pivot]) -> int:
    highs = _last_of_kind(pivots, 1, 2)
    lows = _last_of_kind(pivots, -1, 2)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh = highs[0].price > highs[1].price
    hl = lows[0].price > lows[1].price
    lh = highs[0].price < highs[1].price
    ll = lows[0].price < lows[1].price
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


class FibSwingStrategy:
    name = "C"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        sp = self.sp
        if not ws.ready or ws.leg_start is None or ws.atr4h is None:
            return None
        t = _swing_trend(ws.pivots4h)
        if t == 0 or ws.leg_dir != -t:
            return None
        anchors = _last_of_kind(ws.pivots4h, t, 1)
        if not anchors:
            return None
        anchor = anchors[0]
        bases = _last_of_kind(ws.pivots4h, -t, 1, before_i=anchor.i)
        if not bases:
            return None
        base = bases[0]
        swing = anchor.price - base.price
        if t * swing <= 0.0:
            return None
        e = ws.E_live
        r = (anchor.price - e) / swing
        if not (_FIB_ZONE[0] <= r <= _FIB_ZONE[1]) or t * (e - base.price) <= 0.0:
            return None
        if not choch.triggered(t, ws.leg_start.sub_ts, ws.E_live_ts):
            return None
        return _finalize(
            ts=bar.ts,
            setup="FIB",
            direction=t,
            entry_ref=bar.close,
            stop=base.price - t * sp.buf_struct_atr * ws.atr4h,
            tp1=e + _FIB_TP1_SWING * swing,
            tp2=e + _FIB_TP2_SWING * swing,
            risk_mult=1.0,
            time_stop_4h=time_stop_bars(anchor.i - base.i, sp),
            instance=None,
            lineage=None,
            reason=f"FIB trend={t:+d} r={r:.3f} E={e:.2f}",
            sp=sp,
        )
```

- [ ] **Step 4: Run test to verify it passes**
Run: `uv run pytest tests/strategy/test_fib_swing.py -v`
Expected: PASS (10 tests)

#### Cycle B: Hybrid and factory

- [ ] **Step 5: Write the failing test**

```python
# tests/strategy/test_hybrid.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy import ElliottStrategy, FibSwingStrategy, HybridStrategy, Strategy, make_strategy
from ewbot.strategy.hybrid import CONFIDENT, CONTRARY, UNCERTAIN
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

HYB = HybridStrategy(StrategyParams())


def scen(sb: SimpleNamespace, q: float = 0.7, s_rev: float = 0.5, with_top: bool = True) -> tuple[WaveState, Bar]:
    """Both A and C would fire here.

    4h pivots 100(L) 200(H) 150(L) 300(H): up-trend for C (r = 0.767).
    Top count IMP+ W2 S=150 -> P1=300 with E=185: r2 = 0.767 for A.
    """
    pivots = [sb.piv(0, 100, -1), sb.piv(8, 200, 1), sb.piv(14, 150, -1), sb.piv(24, 300, 1)]
    top = sb.count("IMP", 1, "W2", pivots[2:], q=q) if with_top else None
    ws = sb.ws(top=top, E=185.0, leg_dir=-1, pivots=pivots, s_rev=s_rev, i4h=30)
    return ws, sb.bar(ws, 192.0)


@pytest.mark.parametrize(
    ("q", "s_rev", "with_top", "state"),
    [
        (0.7, 0.5, True, CONFIDENT),
        (0.7, 0.25, True, CONFIDENT),  # boundaries inclusive
        (0.5, 0.5, True, UNCERTAIN),  # q too low
        (0.7, 0.1, True, UNCERTAIN),  # S_rev between -S_MIN and S_MIN
        (0.7, -0.25, True, CONTRARY),
        (0.5, -0.6, True, CONTRARY),
        (0.0, 0.0, False, UNCERTAIN),  # no alive count -> no Elliott opinion
    ],
)
def test_classify(sb: SimpleNamespace, q: float, s_rev: float, with_top: bool, state: str) -> None:
    ws, _ = scen(sb, q=q, s_rev=s_rev, with_top=with_top)
    assert HYB.classify(ws) == state


def test_classify_with_zero_s_min_tie_goes_to_confident_then_contrary(sb: SimpleNamespace) -> None:
    hyb = HybridStrategy(StrategyParams(S_MIN=0.0))
    ws, _ = scen(sb, q=0.7, s_rev=0.0)
    assert hyb.classify(ws) == CONFIDENT
    ws, _ = scen(sb, q=0.5, s_rev=0.0)
    assert hyb.classify(ws) == CONTRARY
    ws, _ = scen(sb, s_rev=0.0, with_top=False)
    assert hyb.classify(ws) == UNCERTAIN


def test_confident_uses_elliott_only(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    sig = HYB.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.setup == "W3"
    assert sig.stop == pytest.approx(148.0)  # 150 - 0.25*8
    assert sig.tp1 == pytest.approx(335.0)  # 185 + 150
    assert sig.tp2 == pytest.approx(427.7)  # 185 + 1.618*150
    assert sig.time_stop_4h == 15
    assert sig.instance == ("IMP", 1, 14, "W2")
    assert sig.reason.startswith("CONFIDENT: W3 ")
    # A has no signal (W3 disabled) -> C is NOT consulted even though it would fire
    hyb = HybridStrategy(StrategyParams(setups_enabled=("W5", "W5_REV", "C_END")))
    assert hyb.on_bar(bar, ws, sb.FakeChoch()) is None


def test_uncertain_uses_fib_only(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb, q=0.5, s_rev=0.5)
    sig = HYB.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.setup == "FIB"
    assert (sig.stop, sig.tp1, sig.tp2) == (pytest.approx(148.0), pytest.approx(335.0), pytest.approx(427.7))  # same geometry as W3
    assert sig.instance is None and sig.lineage is None
    assert sig.reason.startswith("UNCERTAIN: FIB ")


def test_contrary_blocks_everything(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb, q=0.7, s_rev=-0.5)
    choch = sb.FakeChoch()
    assert HYB.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_make_strategy_by_mode() -> None:
    assert isinstance(make_strategy(StrategyParams(mode="A")), ElliottStrategy)
    assert isinstance(make_strategy(StrategyParams(mode="C")), FibSwingStrategy)
    assert isinstance(make_strategy(StrategyParams(mode="HYBRID")), HybridStrategy)
    for mode in ("A", "C", "HYBRID"):
        strat = make_strategy(StrategyParams(mode=mode))
        assert isinstance(strat, Strategy)
        assert strat.name == mode
    with pytest.raises(ValueError, match="unknown strategy mode"):
        make_strategy(StrategyParams(mode="B"))
```

- [ ] **Step 6: Run test to verify it fails**
Run: `uv run pytest tests/strategy/test_hybrid.py -v`
Expected: FAIL with collection error `ImportError: cannot import name 'ElliottStrategy' from 'ewbot.strategy'`

- [ ] **Step 7: Write minimal implementation**

Create `src/ewbot/strategy/hybrid.py`:

```python
# src/ewbot/strategy/hybrid.py
"""Hybrid A+C (spec 5.6) and the strategy factory.

Every 1h bar the Elliott state is classified (checked in this order):
  CONFIDENT  top exists, top.q >= Q_MIN and S_rev >= S_MIN -> only A (Elliott) signals
  CONTRARY   top exists and S_rev <= -S_MIN                  -> no signal at all (C blocked too)
  UNCERTAIN  everything else (incl. no alive count)          -> only C (Fib swing) signals
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, Strategy
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.strategy.fib_swing import FibSwingStrategy

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState

CONFIDENT = "CONFIDENT"
CONTRARY = "CONTRARY"
UNCERTAIN = "UNCERTAIN"


class HybridStrategy:
    name = "HYBRID"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp
        self.elliott = ElliottStrategy(sp)
        self.fib = FibSwingStrategy(sp)

    def classify(self, ws: WaveState) -> str:
        top = ws.top
        if top is not None and top.q >= self.sp.Q_MIN and ws.s_rev >= self.sp.S_MIN:
            return CONFIDENT
        if top is not None and ws.s_rev <= -self.sp.S_MIN:
            return CONTRARY
        return UNCERTAIN

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        state = self.classify(ws)
        if state == CONFIDENT:
            sig = self.elliott.on_bar(bar, ws, choch)
        elif state == UNCERTAIN:
            sig = self.fib.on_bar(bar, ws, choch)
        else:
            return None
        if sig is None:
            return None
        return replace(sig, reason=f"{state}: {sig.reason}")


def make_strategy(sp: StrategyParams) -> Strategy:
    if sp.mode == "A":
        return ElliottStrategy(sp)
    if sp.mode == "C":
        return FibSwingStrategy(sp)
    if sp.mode == "HYBRID":
        return HybridStrategy(sp)
    raise ValueError(f"unknown strategy mode {sp.mode!r} (expected 'A', 'C' or 'HYBRID')")
```

Replace the entire contents of `src/ewbot/strategy/__init__.py` with:

```python
# src/ewbot/strategy/__init__.py
"""Trading strategies (spec section 5)."""

from ewbot.strategy.base import Signal, Strategy, blended_r, time_stop_bars
from ewbot.strategy.choch import ChochDetector
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.strategy.fib_swing import FibSwingStrategy
from ewbot.strategy.hybrid import HybridStrategy, make_strategy

__all__ = [
    "ChochDetector",
    "ElliottStrategy",
    "FibSwingStrategy",
    "HybridStrategy",
    "Signal",
    "Strategy",
    "blended_r",
    "make_strategy",
    "time_stop_bars",
]
```

- [ ] **Step 8: Run tests to verify they pass**
Run: `uv run pytest tests/strategy -v`
Expected: PASS (78 tests: 11 choch, 17 base, 28 elliott, 10 fib_swing, 12 hybrid)

- [ ] **Step 9: Commit**
```bash
git add src/ewbot/strategy/fib_swing.py src/ewbot/strategy/hybrid.py src/ewbot/strategy/__init__.py tests/strategy/test_fib_swing.py tests/strategy/test_hybrid.py
git commit -m "feat(strategy): Fib-swing baseline C, hybrid A+C classifier, make_strategy (spec 5.5-5.6)"
```


---

## Part C1 — Execution, Risk & Engine Loop

Tasks T15–T17. Everything runs from the repository root. These tasks come after Parts A, B1 and B2
(T1–T14), so all imported modules already exist; the engine tests replace `WaveEngine` and
`ChochDetector` with small scripted stubs, so they do not depend on the wave engine's output.

What this part fixes for the rest of the plan:

- **Money and units.** All amounts are USD. One contract = `CostParams.contract_size` BTC (0.01).
  Fees are charged on every fill. TP fills are limit orders: they fill at the TP price with no
  slippage but still pay `fee_rate`. Funding is charged once per processed bar on the position that is
  still open at the close (mark = `bar.close`). This includes the futures-closed hour.
- **PaperBroker bar order** (spec 9.2). (0) While futures are closed (Fri 17–18 ET) nothing fills:
  pending market orders wait, and a stop the bar's range passed through is marked triggered. (1) The
  pending market exit fills, then the pending entry, at open ± slippage. (2) Gap: if the open is
  already at or past the stop, or the stop was triggered while closed, the stop fills at open ±
  slippage. (3) Stop touched intrabar → stop ± slippage, and it beats any TP in the same bar.
  Otherwise TP1 and then TP2 fill, each only on a strict cross; both can fill in the same bar.
  (4) Funding.
- **Engine bar order.** This is the contract's order. In the code each step is commented with its
  number. Before the loop, `run()` applies `fill_gaps` and then the spec 4.1 wick clip
  (`clip_wicks` at `wave.wick_clip_atr1h × ATR1h`); steps 3, 4 and 7 see the clipped bar, the broker
  and the equity marks see the raw bar.
  1. `process_bar`.
  2. Handle fills: TP1 moves the stop to breakeven from the next bar. A full exit records a `Trade`,
     calls `on_trade_closed`, and adds the instance to cooldown if the exit was a STOP.
  3. `wave.on_1h_close`. The 4h bar is built here from UTC buckets: a 1h bar with
     `ts % H4 == 3*H1` and 4 bars in its bucket completes it.
  4. `choch.update`.
  5. Manage the position: trailing stop, time stop, lineage kill.
  6. `breakers.on_equity`. TOTAL_HALT → exit at the next open, and no more entries.
  7. Release cooldowns, then evaluate entry or flip.
  8. On the last bar, close at `bar.close` with reason `END`.
  9. Record an `EquityPoint`.
- **Signals.** `strategy.on_bar` is called only when entries are allowed:
  - `bar.ts >= start_ts`;
  - no entry is pending;
  - `breakers.can_enter(bar.ts + H1)`;
  - neither `entry_blackout(bar.ts)` nor `entry_blackout(bar.ts + H1)`;
  - the bar is not the last one.

  It is called whether or not a position is open, because flips need it.
  - `EngineResult.signals` holds only the signals that were queued.
  - A rejection is logged in `events` as `(bar.ts, "SIGNAL_REJECTED:cooldown" | "SIGNAL_REJECTED:size")`
    or `(bar.ts, "SIGNAL_IGNORED:same_direction")`.
  - Breaker events are logged as `(ts, "DAILY_HALT" | "TOTAL_HALT" | "CONSEC_PAUSE")`.
  - The wick clip count is logged once per run as `(first bar ts, "WICKS_CLIPPED:<n>")`.
- **Equity series.** There is one `EquityPoint(bar.ts, equity at bar.close)` per 1h bar with
  `bar.ts >= start_ts`, or for every bar when `start_ts is None`. Warm-up bars are fed to the
  engines but not recorded. The last point is taken after the END close.
- **Import paths.** `Trade`, `EquityPoint` and `EngineResult` are defined in
  `ewbot/engine/position_mgr.py` and re-exported by `ewbot/engine/loop.py`. Both
  `from ewbot.engine.loop import Trade, EquityPoint, EngineResult, TradingEngine` and
  `from ewbot.engine.position_mgr import Trade` work.

Additive extensions to the contract. No public contract name is changed.

1. `PaperPosition.funding: float = 0.0` holds the funding accrued on that position, in USD, where
   >0 means paid. `Trade.funding` is read from it.
2. `PaperBroker.close_now(ts, price) -> Fill | None` closes the whole position immediately at
   `price` ± slippage plus fee. It is used for the `END` close.
3. `TradingEngine(cfg, strategy, *, wave_engine=None, choch=None)`. When an argument is `None`, a
   fresh real `WaveEngine(cfg.wave)` or `ChochDetector(cfg.strategy.choch_bars)` is built per
   `run()`.
4. `Trade.exit_reason == "TP1"` happens only for a one-contract trade, where TP1 closes everything
   and TP2 is unused.

---

### Task 15: Cost model and PaperBroker

**Files:**
- Create: `src/ewbot/execution/__init__.py`, `src/ewbot/execution/costs.py`, `src/ewbot/execution/paper.py`
- Test: `tests/execution/test_costs.py`, `tests/execution/test_paper_broker.py`

**Interfaces:**
- Consumes: `ewbot.config.CostParams`; `ewbot.types.{Bar, H1}`; `ewbot.data.candles.futures_closed`.
- Produces: `fee`, `slip`, `funding_per_hour`, `Fill`, `PaperPosition`, `PaperBroker`. They match the
  contract, plus extension 1 (`PaperPosition.funding`) and extension 2 (`PaperBroker.close_now`). Details fixed here:
  - `fee` is `fee_rate * price * contract_size` per contract, times `contracts`. When
    `apply_min_fee` is set, each contract pays at least `fee_min_per_contract`.
  - `slip(price, side)` = `price * (1 + side * slippage)`.
  - `funding_per_hour` = `direction * mark * contracts * contract_size * funding_apr_long / 8760`.
  - Every `Fill.side` is the order side: an ENTRY's side is the trade direction, and a reducing
    fill's side is `-direction`.
  - `queue_entry` raises `ValueError` if the direction is not ±1, if `contracts < 1`, or if
    `tp1_contracts` is not in `[1, contracts]`.

- [ ] **Step 1: Write the failing test for the cost model**

`tests/execution/test_costs.py`:

```python
from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.execution.costs import fee, funding_per_hour, slip

C = CostParams()  # fee 0.05%, slippage 0.05%, funding 10% APR, 0.01 BTC/contract, no min fee


def test_fee_is_rate_times_notional() -> None:
    # 10 contracts * 0.01 BTC * 50_000 = 5_000 USD notional; 0.05% -> 2.50
    assert fee(50_000.0, 10, C) == pytest.approx(2.5)


def test_fee_zero_contracts_is_zero() -> None:
    assert fee(50_000.0, 0, C) == 0.0


def test_fee_min_per_contract_applies_only_when_enabled() -> None:
    cheap = 1_000.0  # 0.0005 * 1_000 * 0.01 = 0.005 USD per contract < 0.15 minimum
    assert fee(cheap, 10, C) == pytest.approx(0.05)
    with_min = CostParams(apply_min_fee=True)
    assert fee(cheap, 10, with_min) == pytest.approx(1.5)
    # above the minimum the proportional fee wins: 0.0005 * 50_000 * 0.01 = 0.25 > 0.15
    assert fee(50_000.0, 10, with_min) == pytest.approx(2.5)


def test_slip_buy_pays_up_sell_receives_less() -> None:
    assert slip(50_000.0, +1, C) == pytest.approx(50_025.0)
    assert slip(50_000.0, -1, C) == pytest.approx(49_975.0)


def test_slip_rejects_bad_side() -> None:
    with pytest.raises(ValueError):
        slip(50_000.0, 0, C)


def test_funding_long_pays_short_receives() -> None:
    # 5_000 USD notional * 10% / 8760 h
    hourly = 5_000.0 * 0.10 / 8760.0
    assert funding_per_hour(50_000.0, 10, +1, C) == pytest.approx(hourly)
    assert funding_per_hour(50_000.0, 10, -1, C) == pytest.approx(-hourly)
    assert hourly == pytest.approx(0.0570776, rel=1e-6)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/execution/test_costs.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.execution'"

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/execution/__init__.py`:

```python
"""Order execution: cost model and the paper (simulated) broker."""
```

`src/ewbot/execution/costs.py`:

```python
"""Cost model for CFM nano BTC perp-style futures (spec 9.2): taker fee, slippage, hourly funding."""
from __future__ import annotations

from ewbot.config import CostParams

_HOURS_PER_YEAR = 8760.0


def fee(price: float, contracts: int, c: CostParams) -> float:
    """One-side fee in USD: per contract max(rate * notional, min) if apply_min_fee else rate * notional."""
    if contracts <= 0:
        return 0.0
    per_contract = c.fee_rate * price * c.contract_size
    if c.apply_min_fee:
        per_contract = max(per_contract, c.fee_min_per_contract)
    return per_contract * contracts


def slip(price: float, side: int, c: CostParams) -> float:
    """Execution price after slippage. side=+1 (buy) pays up, side=-1 (sell) receives less."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side}")
    return price * (1.0 + side * c.slippage)


def funding_per_hour(mark: float, contracts: int, direction: int, c: CostParams) -> float:
    """Funding cost for one hour in USD (>0 = pay). Longs pay funding_apr_long, shorts receive it."""
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    notional = mark * contracts * c.contract_size
    return direction * notional * c.funding_apr_long / _HOURS_PER_YEAR
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/execution/test_costs.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Write the failing PaperBroker test**

`tests/execution/test_paper_broker.py`:

```python
from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.types import H1, Bar

T0 = 1_704_067_200          # Mon 2024-01-01 00:00 UTC (futures open)
FRI_16ET = 1_704_488_400    # Fri 2024-01-05 21:00 UTC = 16:00 EST (open, but entry blackout)
FRI_17ET = 1_704_492_000    # Fri 2024-01-05 22:00 UTC = 17:00 EST (futures closed)
FRI_18ET = 1_704_495_600    # Fri 2024-01-05 23:00 UTC = 18:00 EST (re-open)

ZERO = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
HOURLY_FUNDING_5K = 5_000.0 * 0.10 / 8760.0  # 10 contracts @ 50_000


def bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=1.0)


def long_10_at_50k(costs: CostParams = ZERO, ts: int = T0) -> PaperBroker:
    b = PaperBroker(costs, 10_000.0)
    b.queue_entry(+1, 10, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=5)
    fills = b.process_bar(bar(ts, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert [f.kind for f in fills] == ["ENTRY"]
    return b


def test_entry_fills_at_next_open_with_slippage_and_fee() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.queue_entry(+1, 10, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=5)
    assert b.position is None  # nothing happens until the next bar is processed
    fills = b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert fills == [Fill(ts=T0, kind="ENTRY", side=+1, contracts=10, price=pytest.approx(50_025.0),
                          fee=pytest.approx(2.50125))]
    pos = b.position
    assert pos is not None
    assert (pos.direction, pos.contracts, pos.entry_ts, pos.tp1_contracts) == (1, 10, T0, 5)
    assert pos.entry_price == pytest.approx(50_025.0)
    # cash = 10_000 - entry fee - one hour of long funding (mark = close 50_000)
    assert b.cash == pytest.approx(10_000.0 - 2.50125 - HOURLY_FUNDING_5K)
    # equity marks the open position: (50_000 - 50_025) * 10 * 0.01 = -2.5
    assert b.equity(50_000.0) == pytest.approx(b.cash - 2.5)


def test_short_entry_sells_below_open() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.queue_entry(-1, 4, stop=51_000.0, tp1=49_000.0, tp2=None, tp1_contracts=2)
    fills = b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert fills[0].side == -1
    assert fills[0].price == pytest.approx(49_975.0)


def test_queue_entry_validates_arguments() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    with pytest.raises(ValueError):
        b.queue_entry(0, 10, 49_000.0, 51_000.0, None, 5)
    with pytest.raises(ValueError):
        b.queue_entry(1, 0, 49_000.0, 51_000.0, None, 0)
    with pytest.raises(ValueError):
        b.queue_entry(1, 10, 49_000.0, 51_000.0, None, 11)


def test_gap_through_stop_fills_at_open_not_at_stop() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 48_500.0, 48_700.0, 48_200.0, 48_600.0))
    assert [(f.kind, f.contracts, f.price) for f in fills] == [("STOP", 10, 48_500.0)]
    assert b.position is None
    assert b.cash == pytest.approx(10_000.0 - 150.0)  # (48_500 - 50_000) * 0.1


def test_gap_stop_fill_includes_slippage_and_fee() -> None:
    b = long_10_at_50k(CostParams(funding_apr_long=0.0))  # entry 50_025, fee 2.50125
    fills = b.process_bar(bar(T0 + H1, 48_500.0, 48_700.0, 48_200.0, 48_600.0))
    assert fills[0].price == pytest.approx(48_475.75)  # 48_500 * (1 - 0.0005)
    assert fills[0].fee == pytest.approx(2.4237875)    # 0.0005 * 48_475.75 * 0.1
    assert b.cash == pytest.approx(10_000.0 - 2.50125 + (48_475.75 - 50_025.0) * 0.1 - 2.4237875)


def test_intrabar_stop_fills_at_stop_minus_slippage() -> None:
    b = long_10_at_50k(CostParams(fee_rate=0.0, funding_apr_long=0.0))
    fills = b.process_bar(bar(T0 + H1, 49_500.0, 49_600.0, 48_950.0, 49_200.0))
    assert fills[0].kind == "STOP"
    assert fills[0].price == pytest.approx(48_975.5)  # 49_000 * (1 - 0.0005)


def test_stop_and_tp_in_same_bar_stop_wins() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 50_000.0, 51_500.0, 48_800.0, 50_000.0))
    assert [(f.kind, f.contracts, f.price) for f in fills] == [("STOP", 10, 49_000.0)]
    assert b.position is None
    assert b.cash == pytest.approx(9_900.0)


def test_tp_requires_strict_cross() -> None:
    b = long_10_at_50k()
    assert b.process_bar(bar(T0 + H1, 50_500.0, 51_000.0, 50_400.0, 50_900.0)) == []  # high == TP1: no fill
    fills = b.process_bar(bar(T0 + 2 * H1, 50_900.0, 51_000.5, 50_800.0, 50_950.0))
    assert [(f.kind, f.contracts, f.price, f.side) for f in fills] == [("TP1", 5, 51_000.0, -1)]


def test_short_tp_strict_cross_uses_low() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(-1, 10, stop=51_000.0, tp1=49_000.0, tp2=48_000.0, tp1_contracts=5)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert b.process_bar(bar(T0 + H1, 49_500.0, 49_600.0, 49_000.0, 49_100.0)) == []  # low == TP1
    fills = b.process_bar(bar(T0 + 2 * H1, 49_100.0, 49_200.0, 48_900.0, 49_000.0))
    assert [(f.kind, f.contracts, f.price, f.side) for f in fills] == [("TP1", 5, 49_000.0, +1)]
    assert b.cash == pytest.approx(10_050.0)


def test_partial_tp1_then_tp2() -> None:
    b = long_10_at_50k()
    f1 = b.process_bar(bar(T0 + H1, 50_500.0, 51_200.0, 50_400.0, 51_100.0))
    assert [(f.kind, f.contracts, f.price) for f in f1] == [("TP1", 5, 51_000.0)]
    pos = b.position
    assert pos is not None and pos.contracts == 5 and pos.tp1 is None
    f2 = b.process_bar(bar(T0 + 2 * H1, 51_100.0, 52_100.0, 51_000.0, 52_050.0))
    assert [(f.kind, f.contracts, f.price) for f in f2] == [("TP2", 5, 52_000.0)]
    assert b.position is None
    assert b.cash == pytest.approx(10_000.0 + 5 * 0.01 * 1_000.0 + 5 * 0.01 * 2_000.0)


def test_tp1_and_tp2_can_fill_in_the_same_bar() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 52_500.0, 50_400.0, 52_400.0))
    assert [(f.kind, f.contracts) for f in fills] == [("TP1", 5), ("TP2", 5)]
    assert b.position is None


def test_single_contract_tp1_closes_everything() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(+1, 1, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=1)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 52_500.0, 50_400.0, 52_400.0))
    assert [(f.kind, f.contracts) for f in fills] == [("TP1", 1)]
    assert b.position is None


def test_funding_sign_long_pays_short_receives() -> None:
    costs = CostParams(fee_rate=0.0, slippage=0.0)  # funding 10% APR
    long_b = PaperBroker(costs, 10_000.0)
    long_b.queue_entry(+1, 10, 49_000.0, 51_000.0, None, 5)
    long_b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert long_b.cash == pytest.approx(10_000.0 - HOURLY_FUNDING_5K)
    assert long_b.position is not None and long_b.position.funding == pytest.approx(HOURLY_FUNDING_5K)

    short_b = PaperBroker(costs, 10_000.0)
    short_b.queue_entry(-1, 10, 51_000.0, 49_000.0, None, 5)
    short_b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert short_b.cash == pytest.approx(10_000.0 + HOURLY_FUNDING_5K)
    assert short_b.position is not None and short_b.position.funding == pytest.approx(-HOURLY_FUNDING_5K)


def test_no_funding_when_flat() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert b.cash == 10_000.0


def test_pending_entry_deferred_while_futures_closed() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(+1, 10, 49_000.0, 51_000.0, 52_000.0, 5)
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_200.0, 49_800.0, 50_100.0)) == []
    assert b.position is None
    fills = b.process_bar(bar(FRI_18ET, 50_300.0, 50_400.0, 50_200.0, 50_350.0))
    assert [(f.kind, f.ts, f.price) for f in fills] == [("ENTRY", FRI_18ET, 50_300.0)]


def test_pending_exit_deferred_while_futures_closed() -> None:
    b = long_10_at_50k(ts=FRI_16ET)
    b.queue_exit()
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_200.0, 49_800.0, 50_100.0)) == []
    assert b.position is not None
    fills = b.process_bar(bar(FRI_18ET, 50_400.0, 50_500.0, 50_300.0, 50_450.0))
    assert [(f.kind, f.price) for f in fills] == [("EXIT", 50_400.0)]
    assert b.position is None


def test_stop_passed_while_closed_fills_at_reopen_price() -> None:
    b = long_10_at_50k(ts=FRI_16ET)
    # closed hour trades through the stop (49_000) and recovers: nothing can fill while closed
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_000.0, 48_500.0, 49_500.0)) == []
    assert b.position is not None
    # re-open above the stop: the triggered stop fills at the first tradable price (the open)
    fills = b.process_bar(bar(FRI_18ET, 49_600.0, 49_700.0, 49_400.0, 49_650.0))
    assert [(f.kind, f.price) for f in fills] == [("STOP", 49_600.0)]


def test_queue_exit_closes_at_next_open_with_slippage() -> None:
    b = long_10_at_50k(CostParams(fee_rate=0.0, funding_apr_long=0.0))  # entry 50_025
    b.queue_exit()
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 50_600.0, 50_400.0, 50_550.0))
    assert [(f.kind, f.side, f.contracts) for f in fills] == [("EXIT", -1, 10)]
    assert fills[0].price == pytest.approx(50_474.75)
    assert b.cash == pytest.approx(10_000.0 + (50_474.75 - 50_025.0) * 0.1)


def test_set_stop_effective_from_next_bar() -> None:
    b = long_10_at_50k()
    b.set_stop(49_800.0)
    assert b.process_bar(bar(T0 + H1, 50_000.0, 50_100.0, 49_850.0, 50_000.0)) == []
    fills = b.process_bar(bar(T0 + 2 * H1, 50_000.0, 50_100.0, 49_790.0, 49_900.0))
    assert [(f.kind, f.price) for f in fills] == [("STOP", 49_800.0)]


def test_exit_then_entry_in_same_bar_flips() -> None:
    b = long_10_at_50k()
    b.queue_exit()
    b.queue_entry(-1, 8, stop=51_000.0, tp1=49_000.0, tp2=48_000.0, tp1_contracts=4)
    fills = b.process_bar(bar(T0 + H1, 50_200.0, 50_300.0, 50_100.0, 50_250.0))
    assert [(f.kind, f.side, f.contracts, f.price) for f in fills] == [
        ("EXIT", -1, 10, 50_200.0),
        ("ENTRY", -1, 8, 50_200.0),
    ]
    pos = b.position
    assert pos is not None and pos.direction == -1 and pos.contracts == 8
    assert b.cash == pytest.approx(10_020.0)


def test_close_now_closes_whole_position() -> None:
    b = long_10_at_50k()
    fill = b.close_now(T0 + H1, 50_300.0)
    assert fill is not None and (fill.kind, fill.contracts, fill.price) == ("EXIT", 10, 50_300.0)
    assert b.position is None and b.cash == pytest.approx(10_030.0)
    assert b.close_now(T0 + H1, 50_300.0) is None
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/execution/test_paper_broker.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.execution.paper'"

- [ ] **Step 7: Write minimal implementation**

`src/ewbot/execution/paper.py`:

```python
"""PaperBroker: bar-by-bar fill simulator for one position (spec 9.2).

Order of events inside one 1h bar (process_bar):
  0. Futures closed (Fri 17-18 ET): nothing fills. Pending market orders stay pending; if the bar's
     range crossed the stop, the stop is marked as triggered and fills at the next tradable open.
  1. Pending market exit, then pending market entry, at open +/- slippage.
  2. Gap: open already at/beyond the stop (or stop triggered while closed) -> stop fills at open +/- slippage.
  3. Intrabar: stop touched -> stop at stop +/- slippage and nothing else (stop wins over TP in the same bar).
     Otherwise TP1 then TP2 (limit, no slippage), each only on a STRICT cross of the level.
  4. Funding for the hour on the position still open at the close (mark = bar.close).
"""
from __future__ import annotations

from dataclasses import dataclass

from ewbot.config import CostParams
from ewbot.data.candles import futures_closed
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.types import Bar


@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    kind: str  # "ENTRY","STOP","TP1","TP2","EXIT"
    side: int  # +1 buy, -1 sell
    contracts: int
    price: float
    fee: float


@dataclass(slots=True)
class PaperPosition:
    direction: int
    contracts: int  # contracts still open
    entry_price: float
    entry_ts: int
    stop: float
    tp1: float | None  # None once TP1 has filled
    tp2: float | None
    tp1_contracts: int
    funding: float = 0.0  # extension: funding accrued on this position (USD, >0 = paid)


@dataclass(slots=True)
class _PendingEntry:
    direction: int
    contracts: int
    stop: float
    tp1: float
    tp2: float | None
    tp1_contracts: int


class PaperBroker:
    def __init__(self, costs: CostParams, equity: float) -> None:
        self.costs = costs
        self.cash = float(equity)
        self.position: PaperPosition | None = None
        self._pending_entry: _PendingEntry | None = None
        self._pending_exit = False
        self._stop_triggered_while_closed = False

    # ------------------------------------------------------------------ queries
    def equity(self, mark: float) -> float:
        pos = self.position
        if pos is None:
            return self.cash
        upnl = pos.direction * (mark - pos.entry_price) * pos.contracts * self.costs.contract_size
        return self.cash + upnl

    # ------------------------------------------------------------------ orders
    def queue_entry(
        self,
        direction: int,
        contracts: int,
        stop: float,
        tp1: float,
        tp2: float | None,
        tp1_contracts: int,
    ) -> None:
        """Market entry at the NEXT processed bar's open. It only fills while flat: to flip,
        call queue_exit() first (the exit is always processed before the entry)."""
        if direction not in (1, -1):
            raise ValueError(f"direction must be +1 or -1, got {direction}")
        if contracts < 1:
            raise ValueError("contracts must be >= 1")
        if not 1 <= tp1_contracts <= contracts:
            raise ValueError("tp1_contracts must be in [1, contracts]")
        self._pending_entry = _PendingEntry(direction, contracts, stop, tp1, tp2, tp1_contracts)

    def queue_exit(self) -> None:
        """Market close of the whole position at the NEXT processed bar's open."""
        self._pending_exit = True

    def set_stop(self, price: float) -> None:
        """Move the stop; the engine calls this after process_bar, so it is effective from the next bar."""
        if self.position is not None:
            self.position.stop = price
        elif self._pending_entry is not None:
            self._pending_entry.stop = price

    def close_now(self, ts: int, price: float) -> Fill | None:
        """Extension (used for END): market-close the whole position immediately at price +/- slippage."""
        self._pending_exit = False
        pos = self.position
        if pos is None:
            return None
        return self._close(ts, "EXIT", slip(price, -pos.direction, self.costs), pos.contracts)

    # ------------------------------------------------------------------ simulation
    def process_bar(self, bar: Bar) -> list[Fill]:
        fills: list[Fill] = []
        if futures_closed(bar.ts):
            pos = self.position
            if pos is not None and self._stop_touched(pos, bar):
                self._stop_triggered_while_closed = True
            self._charge_funding(bar)
            return fills

        # (1) pending market orders at the open
        if self._pending_exit:
            self._pending_exit = False
            if self.position is not None:
                pos = self.position
                price = slip(bar.open, -pos.direction, self.costs)
                fills.append(self._close(bar.ts, "EXIT", price, pos.contracts))
        if self._pending_entry is not None and self.position is None:
            pe = self._pending_entry
            self._pending_entry = None
            price = slip(bar.open, pe.direction, self.costs)
            f = fee(price, pe.contracts, self.costs)
            self.cash -= f
            self.position = PaperPosition(
                direction=pe.direction,
                contracts=pe.contracts,
                entry_price=price,
                entry_ts=bar.ts,
                stop=pe.stop,
                tp1=pe.tp1,
                tp2=pe.tp2,
                tp1_contracts=pe.tp1_contracts,
            )
            fills.append(Fill(bar.ts, "ENTRY", pe.direction, pe.contracts, price, f))

        pos = self.position
        if pos is not None:
            d = pos.direction
            if self._stop_triggered_while_closed or d * (bar.open - pos.stop) <= 0:
                # (2) gap through the stop (or stop passed during the futures close): fill at the open
                fills.append(self._close(bar.ts, "STOP", slip(bar.open, -d, self.costs), pos.contracts))
            elif self._stop_touched(pos, bar):
                # (3a) stop has priority over any TP in the same bar
                fills.append(self._close(bar.ts, "STOP", slip(pos.stop, -d, self.costs), pos.contracts))
            else:
                # (3b) limit TPs need a strict cross
                fav = bar.high if d == 1 else bar.low
                if pos.tp1 is not None and d * (fav - pos.tp1) > 0:
                    tp1 = pos.tp1
                    pos.tp1 = None
                    fills.append(self._close(bar.ts, "TP1", tp1, min(pos.tp1_contracts, pos.contracts)))
                if self.position is not None and pos.tp1 is None and pos.tp2 is not None:
                    if d * (fav - pos.tp2) > 0:
                        fills.append(self._close(bar.ts, "TP2", pos.tp2, pos.contracts))

        # (4) funding for the hour
        self._charge_funding(bar)
        return fills

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _stop_touched(pos: PaperPosition, bar: Bar) -> bool:
        return bar.low <= pos.stop if pos.direction == 1 else bar.high >= pos.stop

    def _close(self, ts: int, kind: str, price: float, qty: int) -> Fill:
        pos = self.position
        assert pos is not None and 1 <= qty <= pos.contracts
        realized = pos.direction * (price - pos.entry_price) * qty * self.costs.contract_size
        f = fee(price, qty, self.costs)
        self.cash += realized - f
        pos.contracts -= qty
        if pos.contracts == 0:
            self.position = None
            self._stop_triggered_while_closed = False
        return Fill(ts, kind, -pos.direction, qty, price, f)

    def _charge_funding(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return
        cost = funding_per_hour(bar.close, pos.contracts, pos.direction, self.costs)
        self.cash -= cost
        pos.funding += cost
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/execution -v`
Expected: PASS — 27 passed (6 in `test_costs.py`, 21 in `test_paper_broker.py`).

- [ ] **Step 9: Commit**

```bash
git add src/ewbot/execution/__init__.py src/ewbot/execution/costs.py src/ewbot/execution/paper.py tests/execution/test_costs.py tests/execution/test_paper_broker.py
git commit -m "feat: cost model and PaperBroker fill simulator (next-open fills, gap stops, strict TP, funding, futures close)"
```

---

### Task 16: Position sizing and circuit breakers

**Files:**
- Create: `src/ewbot/risk/__init__.py`, `src/ewbot/risk/sizing.py`, `src/ewbot/risk/breakers.py`
- Test: `tests/risk/test_sizing.py`, `tests/risk/test_breakers.py`

**Interfaces:**
- Consumes: `ewbot.config.{CostParams, RiskProfile, BreakerParams, CONSERVATIVE, AGGRESSIVE}`.
- Produces: `size_contracts`, `CircuitBreakers`, exactly as in the contract. Details fixed here:
  - **Stop distance.** `|entry - stop| + slippage * (entry + stop)`. This is the exact per-BTC loss
    when both the entry fill and the stop fill slip against the trade. Fees are not included (spec 6.2).
  - **Leverage cap.** Quantity is capped by `equity * max_leverage / entry`, then floored to whole
    contracts with a `1e-9` guard so that `0.6/0.01` gives 60.
  - **Liquidation check.** `liq = entry * (1 - direction / lev_eff)`, where
    `lev_eff = notional / equity`. The trade is rejected if the liquidation price is at or inside the
    stop. With maintenance ≈ 0 the liquidation distance equals `distance / risk_pct`, so this check
    can only fire when `risk_pct * risk_mult >= 1`. It is a safety net, and the tests use a
    deliberately extreme profile.
  - **Breaker clock.** `ts` is an observation instant. The engine passes the bar close
    (`bar.ts + H1`), so the observation at exactly 00:00 New York starts the new day.
  - **Daily reference.** It is the first equity observed on that New York calendar day. The daily
    halt lasts until the date changes: `can_enter` compares dates itself, so the halt ends even if
    `on_equity` is not called.
  - **TOTAL_HALT** latches forever.
  - **Switch.** With `BreakerParams.enabled = False` every method is a no-op: `on_equity` and
    `on_trade_closed` return `[]` and change no state, so `can_enter` is always `True` and `halted`
    stays `False`. Only the walk-forward grid uses this (T21); everything else runs with breakers on.
  - **CONSEC_PAUSE.** A pnl `>= 0` resets the loss streak. A pause blocks entries for
    `consec_pause_hours` from the closing trade's `ts`, and the streak restarts from 0.

- [ ] **Step 1: Write the failing sizing test**

`tests/risk/test_sizing.py`:

```python
from __future__ import annotations

from ewbot.config import AGGRESSIVE, CONSERVATIVE, CostParams, RiskProfile
from ewbot.risk.sizing import size_contracts

C = CostParams()                     # slippage 0.05%, 0.01 BTC per contract
NO_SLIP = CostParams(slippage=0.0)


def test_risk_one_percent_long_example() -> None:
    # risk$ = 10_000 * 1% = 100; distance = 1_000 + 0.0005 * (50_000 + 49_000) = 1_049.5
    # qty = 100 / 1_049.5 = 0.09528 BTC = 9.53 contracts -> 9
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 9


def test_risk_one_percent_short_example() -> None:
    # distance = 1_000 + 0.0005 * (50_000 + 51_000) = 1_050.5 -> 0.09519 BTC -> 9 contracts
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, -1, CONSERVATIVE, 1.0, C) == 9


def test_without_slippage_risk_is_exact() -> None:
    # 100 / 1_000 = 0.1 BTC = 10 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, NO_SLIP) == 10


def test_risk_mult_half() -> None:
    # 50 / 1_049.5 = 0.04764 BTC -> 4 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 0.5, C) == 4


def test_leverage_cap_limits_notional() -> None:
    # tight stop: 100 / 100 = 1 BTC = 50_000 notional > 3x * 10_000 -> capped to 0.6 BTC = 60 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_900.0, +1, CONSERVATIVE, 1.0, NO_SLIP) == 60
    # aggressive: 3% risk -> 3 BTC, capped at 10x -> 2 BTC = 200 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_900.0, +1, AGGRESSIVE, 1.0, NO_SLIP) == 200


def test_below_one_contract_skips() -> None:
    # 10 / 1_049.5 = 0.0095 BTC = 0.95 contracts -> 0
    assert size_contracts(1_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 0


def test_stop_on_wrong_side_or_bad_inputs_skip() -> None:
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, -1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 50_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(0.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, 0, CONSERVATIVE, 1.0, C) == 0


def test_liquidation_inside_stop_rejects() -> None:
    # 200% "risk" with 100x cap: qty = 20_000 / 1_000 = 20 BTC (cap 20 BTC) -> lev 100x
    # liq = 50_000 * (1 - 1/100) = 49_500, which is above the 49_000 stop -> reject
    degenerate = RiskProfile("degenerate", 2.0, 100.0)
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, degenerate, 1.0, NO_SLIP) == 0
    # short mirror: liq = 50_500 < stop 51_000 -> reject
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, -1, degenerate, 1.0, NO_SLIP) == 0


def test_liquidation_beyond_stop_accepts() -> None:
    # 50% risk: qty = 5 BTC = 500 contracts, lev 25x, liq = 48_000 < stop 49_000 -> accept
    wide = RiskProfile("wide", 0.5, 100.0)
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, wide, 1.0, NO_SLIP) == 500
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/risk/test_sizing.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.risk'"

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/risk/__init__.py`:

```python
"""Risk management: position sizing and circuit breakers."""
```

`src/ewbot/risk/sizing.py`:

```python
"""Position sizing (spec 6.2)."""
from __future__ import annotations

import math

from ewbot.config import CostParams, RiskProfile

_EPS = 1e-9  # guards floor() against 59.99999999 -> 59


def size_contracts(
    equity: float,
    entry: float,
    stop: float,
    direction: int,
    risk: RiskProfile,
    risk_mult: float,
    c: CostParams,
) -> int:
    """Whole contracts to trade; 0 means skip the signal.

    risk$     = equity * risk_pct * risk_mult
    distance  = |entry - stop| + slippage * (entry + stop)   (entry and stop fill both slip against us)
    qty (BTC) = risk$ / distance, capped so notional <= equity * max_leverage (stop never widened)
    contracts = floor(qty / contract_size); < 1 -> 0
    liquidation (isolated margin, maintenance ~ 0): liq = entry * (1 - direction / lev_eff);
    reject if liq is at/inside the stop.
    """
    if direction not in (1, -1):
        return 0
    if equity <= 0.0 or entry <= 0.0 or stop <= 0.0 or risk_mult <= 0.0:
        return 0
    raw = direction * (entry - stop)
    if raw <= 0.0:
        return 0  # stop on the wrong side of the entry
    distance = raw + c.slippage * (entry + stop)
    qty = equity * risk.risk_pct * risk_mult / distance
    qty = min(qty, equity * risk.max_leverage / entry)
    contracts = math.floor(qty / c.contract_size + _EPS)
    if contracts < 1:
        return 0
    lev_eff = contracts * c.contract_size * entry / equity
    liq = entry * (1.0 - direction / lev_eff)
    if direction * (liq - stop) >= 0.0:
        return 0  # liquidation would hit before (or at) the stop
    return contracts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/risk/test_sizing.py -v`
Expected: PASS — 9 passed.

- [ ] **Step 5: Write the failing breaker test**

`tests/risk/test_breakers.py`:

```python
from __future__ import annotations

from ewbot.config import BreakerParams
from ewbot.risk.breakers import CircuitBreakers

P = BreakerParams()  # daily 3%, total 15%, 5 losses -> 24h
H = 3600

NY_MIDNIGHT_MAR10 = 1_710_046_800   # 2024-03-10 00:00 EST (05:00 UTC); DST starts 02:00 that day
NY_MIDNIGHT_MAR11 = 1_710_129_600   # 2024-03-11 00:00 EDT (04:00 UTC)
NY_MIDNIGHT_NOV03 = 1_730_606_400   # 2024-11-03 00:00 EDT (04:00 UTC); DST ends 02:00 that day
NY_MIDNIGHT_NOV04 = 1_730_696_400   # 2024-11-04 00:00 EST (05:00 UTC)


def test_daily_halt_triggers_at_three_percent_and_keeps_others_open() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    assert cb.on_equity(NY_MIDNIGHT_MAR10, 10_000.0) == []
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 3 * H, 9_710.0) == []       # -2.9%
    assert cb.can_enter(NY_MIDNIGHT_MAR10 + 3 * H)
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 10 * H, 9_690.0) == ["DAILY_HALT"]  # -3.1%
    assert not cb.can_enter(NY_MIDNIGHT_MAR10 + 10 * H)
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 11 * H, 9_600.0) == []      # emitted once per day
    assert not cb.halted


def test_daily_halt_resets_at_ny_midnight_across_spring_dst() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_MAR10, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_MAR10 + 10 * H, 9_690.0)
    # 23 wall-clock hours later it is 2024-03-10 23:00 EDT (03:00 UTC): still the halted day
    assert not cb.can_enter(NY_MIDNIGHT_MAR11 - H)
    # 2024-03-11 00:00 EDT = 04:00 UTC (a fixed UTC-5 offset would wrongly say 23:00)
    assert cb.can_enter(NY_MIDNIGHT_MAR11)
    # the first observation of the new day becomes the new reference: no halt at 9_690
    assert cb.on_equity(NY_MIDNIGHT_MAR11, 9_690.0) == []
    assert cb.can_enter(NY_MIDNIGHT_MAR11 + H)


def test_daily_halt_resets_at_ny_midnight_across_fall_dst() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_NOV03, 10_000.0)
    assert cb.on_equity(NY_MIDNIGHT_NOV03 + 20 * H, 9_650.0) == ["DAILY_HALT"]
    # 2024-11-03 is 25 hours long: 04:59:59 UTC on Nov 4 is still 23:59:59 EST on Nov 3
    assert not cb.can_enter(NY_MIDNIGHT_NOV04 - 1)
    assert cb.can_enter(NY_MIDNIGHT_NOV04)


def test_total_drawdown_latches() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = NY_MIDNIGHT_MAR10
    assert cb.on_equity(t, 12_000.0) == []                  # new peak 12_000
    assert cb.on_equity(t + 30 * H, 10_250.0) == []         # -14.6% from peak (new NY day)
    assert "TOTAL_HALT" in cb.on_equity(t + 31 * H, 10_150.0)  # -15.4% from peak
    assert cb.halted
    assert not cb.can_enter(t + 32 * H)
    assert cb.on_equity(t + 80 * H, 13_000.0) == []         # recovery does not un-latch
    assert cb.halted and not cb.can_enter(t + 80 * H)


def test_five_consecutive_losses_pause_24h() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = 1_704_067_200
    for k in range(4):
        assert cb.on_trade_closed(t + k * H, -10.0) == []
    assert cb.on_trade_closed(t + 4 * H, -10.0) == ["CONSEC_PAUSE"]
    assert not cb.can_enter(t + 4 * H)
    assert not cb.can_enter(t + 28 * H - 1)
    assert cb.can_enter(t + 28 * H)
    # streak restarted: one more loss does not pause again
    assert cb.on_trade_closed(t + 29 * H, -10.0) == []
    assert cb.can_enter(t + 29 * H)


def test_win_resets_loss_streak() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = 1_704_067_200
    for k in range(4):
        cb.on_trade_closed(t + k * H, -10.0)
    assert cb.on_trade_closed(t + 4 * H, 5.0) == []
    assert cb.on_trade_closed(t + 5 * H, -10.0) == []
    assert cb.can_enter(t + 5 * H)


def test_disabled_breakers_are_a_no_op() -> None:
    cb = CircuitBreakers(BreakerParams(enabled=False), 10_000.0)
    t = NY_MIDNIGHT_MAR10
    assert cb.on_equity(t, 10_000.0) == []
    assert cb.on_equity(t + H, 5_000.0) == []  # -50%: neither DAILY_HALT nor TOTAL_HALT
    for k in range(10):
        assert cb.on_trade_closed(t + (2 + k) * H, -10.0) == []  # no CONSEC_PAUSE
    assert not cb.halted
    assert cb.can_enter(t + 12 * H)
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/risk/test_breakers.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.risk.breakers'"

- [ ] **Step 7: Write minimal implementation**

`src/ewbot/risk/breakers.py`:

```python
"""Circuit breakers (spec 6.3).

- DAILY_HALT: equity <= (first equity observed on this America/New_York calendar day) * (1 - daily_loss).
  Blocks new entries until the next NY midnight; open positions are kept.
- TOTAL_HALT: equity <= running peak * (1 - total_dd). Latched forever in backtests (the engine flattens).
- CONSEC_PAUSE: max_consec_losses losing trades in a row -> no new entries for consec_pause_hours; streak resets.
- enabled=False (walk-forward grid runs only): every method is a no-op; can_enter is always True.

`ts` arguments are observation instants (epoch s UTC). The engine passes the bar CLOSE instant
(bar.ts + H1) so that the observation at exactly 00:00 New York opens the new day.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams

_NY = ZoneInfo("America/New_York")


def _ny_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=_NY).date()


class CircuitBreakers:
    def __init__(self, p: BreakerParams, equity0: float) -> None:
        self.p = p
        self._peak = float(equity0)
        self._day: date | None = None
        self._day_ref = float(equity0)
        self._daily_halted = False
        self._halted = False
        self._consec = 0
        self._pause_until: int | None = None

    def on_equity(self, ts: int, equity: float) -> list[str]:
        if not self.p.enabled:
            return []
        events: list[str] = []
        day = _ny_date(ts)
        if day != self._day:
            self._day = day
            self._day_ref = equity
            self._daily_halted = False
        self._peak = max(self._peak, equity)
        if not self._daily_halted and equity <= self._day_ref * (1.0 - self.p.daily_loss):
            self._daily_halted = True
            events.append("DAILY_HALT")
        if not self._halted and equity <= self._peak * (1.0 - self.p.total_dd):
            self._halted = True
            events.append("TOTAL_HALT")
        return events

    def on_trade_closed(self, ts: int, pnl: float) -> list[str]:
        if not self.p.enabled:
            return []
        if pnl >= 0.0:
            self._consec = 0
            return []
        self._consec += 1
        if self._consec >= self.p.max_consec_losses:
            self._consec = 0
            self._pause_until = ts + self.p.consec_pause_hours * 3600
            return ["CONSEC_PAUSE"]
        return []

    def can_enter(self, ts: int) -> bool:
        if self._halted:
            return False
        if self._daily_halted and _ny_date(ts) == self._day:
            return False
        if self._pause_until is not None and ts < self._pause_until:
            return False
        return True

    @property
    def halted(self) -> bool:
        return self._halted
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/risk -v`
Expected: PASS — 16 passed (9 in `test_sizing.py`, 7 in `test_breakers.py`).

- [ ] **Step 9: Commit**

```bash
git add src/ewbot/risk/__init__.py src/ewbot/risk/sizing.py src/ewbot/risk/breakers.py tests/risk/test_sizing.py tests/risk/test_breakers.py
git commit -m "feat: risk sizing (slippage-aware, leverage cap, liquidation check) and NY-day circuit breakers with enable switch"
```

---

### Task 17: Position manager and TradingEngine loop

**Files:**
- Create: `src/ewbot/engine/__init__.py`, `src/ewbot/engine/position_mgr.py`, `src/ewbot/engine/loop.py`
- Test: `tests/engine/test_position_mgr.py`, `tests/engine/test_engine_loop.py`

**Interfaces:**
- Consumes:
  - `ewbot.types.{Bar, H1, H4}`, `ewbot.config.BotConfig`.
  - `ewbot.data.candles.{fill_gaps, clip_wicks, to_bars, entry_blackout}`, `ewbot.indicators.atr_series`.
    `fill_gaps` is idempotent (T3) and is always applied. Then 1h wicks are clipped (spec 4.1):
    `clip_wicks(df, atr_series(high, low, close, wave.atr_len), wave.wick_clip_atr1h)`. The clipped bars
    feed the 4h aggregation, `WaveEngine`, `ChochDetector` and `strategy.on_bar`; the broker, equity
    marks and the END close use the raw bars. The count is logged once as
    `(first bar ts, "WICKS_CLIPPED:<n>")` (also when `n == 0`).
  - `ewbot.waves.engine.{WaveEngine, WaveState}`, `ewbot.waves.enumerate.Count`,
    `ewbot.waves.zigzag.Pivot`, `ewbot.waves.patterns.N_POINTS` (tests only).
  - `ewbot.strategy.choch.ChochDetector`, `ewbot.strategy.base.{Signal, Strategy}`.
  - T15 `PaperBroker`, `Fill`, `PaperPosition`; T16 `size_contracts`, `CircuitBreakers`.
- Produces: `Trade`, `EquityPoint`, `EngineResult`, `TradingEngine`, exactly as in the contract, plus
  extensions 3 and 4. Management rules fixed here (spec 5.3, 5.4, 6.3):
  - **Entry.** A signal at bar t fills at the open of bar t+1. Its size comes from
    `size_contracts(broker.equity(bar.close), sig.entry_ref, sig.stop, ...)`.
    - TP1 takes `floor(n * tp1_frac)` contracts, at least 1.
    - With one contract, TP1 takes that contract and TP2 is unused.
    - Stop and targets stay as given by the signal.
  - **TP1.** After TP1 the stop moves to `max(stop, entry_fill)` for a long (`min` for a short),
    effective from the next bar. The time stop is disarmed.
  - **Trailing.** A newly confirmed 4h pivot on the *protective* side (`kind == -direction`: pivot
    lows for longs, highs for shorts) gives `pivot ∓ buf_struct_atr * atr4h`. The stop is tightened
    only, effective from the next bar.
    - Reason: a pivot of `kind == direction` is confirmed only after the close has already retraced
      `K4·ATR` from it, so a stop at `pivot - 0.25·ATR` would sit above the market.
  - **Time stop.** Every 4h close observed while the position is open and TP1 has not filled adds 1,
    including the close of the entry bar's own bucket. At `>= sig.time_stop_4h` the engine queues an
    exit (reason `TIME`).
  - **Lineage kill.** If `sig.lineage` is set, `ws.ready`, and `sig.lineage not in ws.lineages`, the
    engine queues an exit (reason `LINEAGE`).
  - **Exit precedence.** Only the first queued exit reason counts, in the order TIME, LINEAGE,
    BREAKER, then FLIP.
  - **Flip.** An opposite-direction signal that passes cooldown and sizing queues an exit (reason
    `FLIP`) and the new entry, both filled at the next open. A same-direction signal while in a
    position is ignored. If an exit is already queued, the position counts as flat.
  - **Cooldown.** It is added when a trade with `sig.instance` exits by `STOP`, including a
    breakeven or trailing stop. On every bar, an instance is released when `ws.top.lineage` equals
    the instance's lineage and `ws.top.phase` differs from the instance's phase.
  - **END.** On the last bar (`ts < end_ts`) any open position is closed at `bar.close` ± slippage
    plus fee. A queued entry is dropped.
  - **Trade fields.**
    - `exit_price_avg` is the contract-weighted average of the exit fills.
    - `pnl = gross − fees − funding`.
    - `r_multiple = pnl / (|entry_fill − stop0| · contracts · contract_size)`.
    - `snapshot` is taken at signal time: `{"ts", "setup", "direction", "counts": [top_k of
      ws.counts as {"pattern","d","phase","points": [(sub_ts, price), ...],"q","p"}],
      "q_top", "s_rev", "E_live", "reason"}`.

- [ ] **Step 1: Write the failing position-manager test**

`tests/engine/test_position_mgr.py`:

```python
from __future__ import annotations

import pytest

from ewbot.engine.position_mgr import (
    EngineResult,
    EquityPoint,
    Trade,
    _make_snapshot,
    _OpenTrade,
    _tighten,
    _tp1_split,
    _trail_candidate,
)
from ewbot.execution.paper import Fill, PaperPosition
from ewbot.strategy.base import Signal
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.zigzag import Pivot

T0 = 1_704_067_200


def make_signal(**kw: object) -> Signal:
    base: dict[str, object] = dict(
        ts=T0, setup="W3", direction=1, entry_ref=50_000.0, stop=49_000.0, tp1=51_000.0, tp2=52_000.0,
        risk_mult=1.0, time_stop_4h=12, instance=("IMP", 1, 7, "W2"), lineage=("IMP", 1, 7), reason="unit",
    )
    base.update(kw)
    return Signal(**base)  # type: ignore[arg-type]


def make_count(i0: int, phase: str = "W2", q: float = 0.7) -> Count:
    pts = (
        Pivot(i=i0, sub_ts=T0 + i0 * 3600, price=48_000.0 + i0, kind=-1, confirmed_at=i0 + 1),
        Pivot(i=i0 + 1, sub_ts=T0 + (i0 + 1) * 3600, price=51_000.0 + i0, kind=1, confirmed_at=i0 + 2),
    )
    return Count(pattern="IMP", d=1, phase=phase, points=pts, q=q)


def make_ws(counts: tuple[Count, ...], probs: tuple[float, ...]) -> WaveState:
    return WaveState(
        ts=T0, ready=True, counts=counts, probs=probs, top=counts[0] if counts else None,
        q_top=counts[0].q if counts else 0.0, s_rev=0.4, lineages=frozenset(c.lineage for c in counts),
        E_live=49_500.0, E_live_ts=T0, leg_dir=-1, leg_start=None, pivots4h=(), new_pivot_4h=None,
        atr4h=400.0, closed_4h=False, i4h=10,
    )


def test_result_types_are_frozen_value_objects() -> None:
    ep = EquityPoint(ts=T0, equity=10_000.0)
    res = EngineResult(trades=(), equity=(ep,), signals=(), events=((T0, "DAILY_HALT"),))
    assert res.equity[0].equity == 10_000.0
    with pytest.raises(AttributeError):
        ep.equity = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("contracts", "frac", "expected"),
    [(10, 0.5, 5), (9, 0.5, 4), (2, 0.5, 1), (1, 0.5, 1), (2, 0.3, 1), (4, 1.0, 4)],
)
def test_tp1_split(contracts: int, frac: float, expected: int) -> None:
    assert _tp1_split(contracts, frac) == expected


def test_tighten_only_moves_in_favour() -> None:
    assert _tighten(1, 49_000.0, 49_700.0) == 49_700.0
    assert _tighten(1, 49_000.0, 48_500.0) == 49_000.0
    assert _tighten(-1, 51_000.0, 50_300.0) == 50_300.0
    assert _tighten(-1, 51_000.0, 51_500.0) == 51_000.0


def test_trail_candidate_uses_protective_pivot_side() -> None:
    low = Pivot(i=5, sub_ts=T0, price=49_800.0, kind=-1, confirmed_at=6)
    high = Pivot(i=6, sub_ts=T0, price=50_200.0, kind=1, confirmed_at=7)
    assert _trail_candidate(1, low, 400.0, 0.25) == pytest.approx(49_700.0)
    assert _trail_candidate(1, high, 400.0, 0.25) is None
    assert _trail_candidate(-1, high, 400.0, 0.25) == pytest.approx(50_300.0)
    assert _trail_candidate(-1, low, 400.0, 0.25) is None
    assert _trail_candidate(1, low, None, 0.25) is None
    assert _trail_candidate(1, None, 400.0, 0.25) is None


def test_snapshot_keeps_top_k_counts_with_sub_ts_points() -> None:
    counts = tuple(make_count(i0) for i0 in (1, 3, 5, 7, 9, 11))
    probs = (0.5, 0.2, 0.1, 0.1, 0.05, 0.05)
    snap = _make_snapshot(make_ws(counts, probs), make_signal(), top_k=5)
    assert len(snap["counts"]) == 5
    assert snap["counts"][0] == {
        "pattern": "IMP", "d": 1, "phase": "W2",
        "points": [(T0 + 3600, 48_001.0), (T0 + 7200, 51_001.0)],
        "q": 0.7, "p": 0.5,
    }
    assert [c["p"] for c in snap["counts"]] == [0.5, 0.2, 0.1, 0.1, 0.05]
    assert snap["q_top"] == 0.7 and snap["s_rev"] == 0.4 and snap["E_live"] == 49_500.0
    assert snap["reason"] == "unit" and snap["setup"] == "W3" and snap["ts"] == T0


def test_open_trade_accounting_partial_exit() -> None:
    sig = make_signal()
    pos = PaperPosition(direction=1, contracts=10, entry_price=50_000.0, entry_ts=T0, stop=49_000.0,
                        tp1=51_000.0, tp2=52_000.0, tp1_contracts=5, funding=3.0)
    ot = _OpenTrade(sig=sig, snapshot={"k": 1}, contract_size=0.01, entry_ts=T0, entry_price=50_000.0,
                    contracts=10, pos=pos, fees=2.0)
    assert not ot.add_exit_fill(Fill(T0 + 3600, "TP1", -1, 5, 51_000.0, 1.0))
    assert ot.tp1_done
    assert ot.add_exit_fill(Fill(T0 + 7200, "STOP", -1, 5, 50_000.0, 1.0))
    tr = ot.to_trade(T0 + 7200, "STOP")
    assert isinstance(tr, Trade)
    # gross 5 * 0.01 * 1_000 = 50; fees 2 + 1 + 1 = 4; funding 3 -> pnl 43; risk 1_000 * 10 * 0.01 = 100
    assert tr.pnl == pytest.approx(43.0)
    assert tr.fees == pytest.approx(4.0) and tr.funding == pytest.approx(3.0)
    assert tr.r_multiple == pytest.approx(0.43)
    assert tr.exit_price_avg == pytest.approx(50_500.0)
    assert (tr.entry_ts, tr.exit_ts, tr.contracts) == (T0, T0 + 7200, 10)
    assert (tr.stop0, tr.tp1, tr.tp2) == (49_000.0, 51_000.0, 52_000.0)
    assert tr.snapshot == {"k": 1} and tr.exit_reason == "STOP"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_position_mgr.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.engine'"

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/engine/__init__.py`:

```python
"""Trading engine: per-bar event loop and position management."""
```

`src/ewbot/engine/position_mgr.py`:

```python
"""Engine result types and per-trade bookkeeping / position-management helpers (spec 5.4)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ewbot.execution.paper import Fill, PaperPosition
from ewbot.strategy.base import Signal
from ewbot.waves.engine import WaveState
from ewbot.waves.zigzag import Pivot


@dataclass(frozen=True, slots=True)
class Trade:
    entry_ts: int
    exit_ts: int
    setup: str
    direction: int
    contracts: int
    entry_price: float
    exit_price_avg: float
    stop0: float
    tp1: float
    tp2: float | None
    pnl: float
    fees: float
    funding: float
    r_multiple: float
    exit_reason: str  # "STOP","TP2","TIME","LINEAGE","FLIP","BREAKER","END" (+ "TP1" for single-contract trades)
    snapshot: dict


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: int
    equity: float


@dataclass(frozen=True, slots=True)
class EngineResult:
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    signals: tuple[Signal, ...]
    events: tuple[tuple[int, str], ...]


def _tp1_split(contracts: int, tp1_frac: float) -> int:
    """Contracts closed at TP1. One contract: TP1 closes it (TP2 unused). Otherwise at least 1."""
    if contracts <= 1:
        return contracts
    return min(contracts, max(1, math.floor(contracts * tp1_frac + 1e-9)))


def _tighten(direction: int, current: float, candidate: float) -> float:
    """Stops only move in the trade's favour: max for longs, min for shorts."""
    return max(current, candidate) if direction == 1 else min(current, candidate)


def _trail_candidate(direction: int, pivot: Pivot | None, atr4h: float | None, buf_atr: float) -> float | None:
    """Structural trailing level from a newly confirmed 4h pivot on the protective side
    (pivot low for longs, pivot high for shorts): pivot -/+ buf_atr * ATR4h."""
    if pivot is None or atr4h is None or pivot.kind != -direction:
        return None
    return pivot.price - direction * buf_atr * atr4h


def _make_snapshot(ws: WaveState, sig: Signal, top_k: int) -> dict[str, Any]:
    counts: list[dict[str, Any]] = []
    for idx, c in enumerate(ws.counts[:top_k]):
        counts.append(
            {
                "pattern": c.pattern,
                "d": c.d,
                "phase": c.phase,
                "points": [(p.sub_ts, p.price) for p in c.points],
                "q": c.q,
                "p": ws.probs[idx] if idx < len(ws.probs) else None,
            }
        )
    return {
        "ts": sig.ts,
        "setup": sig.setup,
        "direction": sig.direction,
        "counts": counts,
        "q_top": ws.q_top,
        "s_rev": ws.s_rev,
        "E_live": ws.E_live,
        "reason": sig.reason,
    }


@dataclass(slots=True)
class _PendingEntry:
    sig: Signal
    snapshot: dict[str, Any]


@dataclass(slots=True)
class _OpenTrade:
    sig: Signal
    snapshot: dict[str, Any]
    contract_size: float
    entry_ts: int
    entry_price: float
    contracts: int
    pos: PaperPosition | None  # live broker object (accrues funding); None if closed inside the entry bar
    fees: float = 0.0
    gross: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    tp1_done: bool = False
    n4h: int = 0  # 4h closes seen since entry (incl. the entry bar's own bucket close)
    exit_reason: str | None = None  # reason for a queued market exit

    @property
    def direction(self) -> int:
        return self.sig.direction

    def add_exit_fill(self, f: Fill) -> bool:
        """Book a reducing fill; returns True when the position is fully closed."""
        self.fees += f.fee
        self.gross += self.direction * (f.price - self.entry_price) * f.contracts * self.contract_size
        self.exit_qty += f.contracts
        self.exit_value += f.price * f.contracts
        if f.kind == "TP1":
            self.tp1_done = True
        return self.exit_qty >= self.contracts

    def to_trade(self, exit_ts: int, reason: str) -> Trade:
        funding = self.pos.funding if self.pos is not None else 0.0
        pnl = self.gross - self.fees - funding
        risk_usd = abs(self.entry_price - self.sig.stop) * self.contracts * self.contract_size
        return Trade(
            entry_ts=self.entry_ts,
            exit_ts=exit_ts,
            setup=self.sig.setup,
            direction=self.direction,
            contracts=self.contracts,
            entry_price=self.entry_price,
            exit_price_avg=self.exit_value / self.exit_qty if self.exit_qty else self.entry_price,
            stop0=self.sig.stop,
            tp1=self.sig.tp1,
            tp2=self.sig.tp2,
            pnl=pnl,
            fees=self.fees,
            funding=funding,
            r_multiple=pnl / risk_usd if risk_usd > 0.0 else 0.0,
            exit_reason=reason,
            snapshot=self.snapshot,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_position_mgr.py -v`
Expected: PASS — 11 passed (the `_tp1_split` parametrization counts 6).

- [ ] **Step 5: Write the failing engine-loop test**

The engine tests use the real `PaperBroker`, sizing, breakers and the Part A candle helpers. They use
a scripted `StubWave`, a no-op choch and a `FakeStrategy` that returns planned signals by timestamp.
With zero costs, a 1% risk, `entry_ref` 50_000 and stop 49_000, every trade is exactly 10 contracts,
so 1 USD of price movement is 0.1 USD of P&L.

`tests/engine/test_engine_loop.py`:

```python
from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from ewbot.config import BotConfig, BreakerParams, CostParams
from ewbot.engine.loop import EngineResult, TradingEngine
from ewbot.strategy.base import Signal
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.patterns import N_POINTS
from ewbot.waves.zigzag import Pivot

T0 = 1_704_067_200    # Mon 2024-01-01 00:00 UTC (4h aligned)
FRI = 1_704_412_800   # Fri 2024-01-05 00:00 UTC; bar 20 = 15:00 EST, 21 = 16:00, 22 = 17:00 (closed)
FLAT = (50_000.0, 50_100.0, 49_900.0, 50_000.0)
ZERO = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
CFG = BotConfig(costs=ZERO)  # conservative 1% risk: entry_ref 50_000 / stop 49_000 -> 10 contracts


def ts(i: int, t0: int = T0) -> int:
    return t0 + i * H1


def frame(n: int, rows: dict[int, tuple[float, float, float, float]] | None = None, t0: int = T0) -> pd.DataFrame:
    rows = rows or {}
    data = [rows.get(i, FLAT) for i in range(n)]
    return pd.DataFrame(
        {
            "ts": pd.Series([t0 + i * H1 for i in range(n)], dtype="int64"),
            "open": [r[0] for r in data],
            "high": [r[1] for r in data],
            "low": [r[2] for r in data],
            "close": [r[3] for r in data],
            "volume": [1.0] * n,
        }
    )


def sig(at: int, d: int = 1, **kw: Any) -> Signal:
    if d == 1:
        base: dict[str, Any] = dict(entry_ref=50_000.0, stop=49_000.0, tp1=51_000.0, tp2=52_000.0)
    else:
        base = dict(entry_ref=50_000.0, stop=51_000.0, tp1=49_000.0, tp2=48_000.0)
    base.update(dict(ts=at, setup="W3", direction=d, risk_mult=1.0, time_stop_4h=None,
                     instance=None, lineage=None, reason="fake"))
    base.update(kw)
    return Signal(**base)


def count(i0: int, phase: str = "W2", q: float = 0.72) -> Count:
    """Bull IMP count whose lineage is ("IMP", 1, i0); alternating low/high points."""
    pts = tuple(
        Pivot(i=i0 + k, sub_ts=T0 + (k + 1) * 3_600, price=48_000.0 if k % 2 == 0 else 51_000.0,
              kind=-1 if k % 2 == 0 else 1, confirmed_at=i0 + k + 1)
        for k in range(N_POINTS[phase])
    )
    return Count(pattern="IMP", d=1, phase=phase, points=pts, q=q)


class StubWave:
    """Scripted WaveEngine: default WaveState fields, overridden per 1h ts."""

    def __init__(self, default: dict[str, Any] | None = None, at: dict[int, dict[str, Any]] | None = None) -> None:
        self.default = default or {}
        self.at = at or {}
        self.calls: list[tuple[int, Bar | None, tuple[Bar, ...] | None]] = []

    def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Any) -> WaveState:
        self.calls.append((bar1h.ts, bar4h, tuple(sub_bars) if sub_bars is not None else None))
        kw: dict[str, Any] = dict(
            ts=bar1h.ts, ready=True, counts=(), probs=(), top=None, q_top=0.0, s_rev=0.0,
            lineages=frozenset(), E_live=bar1h.close, E_live_ts=bar1h.ts, leg_dir=1, leg_start=None,
            pivots4h=(), new_pivot_4h=None, atr4h=400.0, closed_4h=bar4h is not None, i4h=0,
        )
        kw.update(self.default)
        kw.update(self.at.get(bar1h.ts, {}))
        return WaveState(**kw)


class NullChoch:
    def update(self, bar1h: Bar) -> None:
        return None


class FakeStrategy:
    name = "FAKE"

    def __init__(self, plan: dict[int, Signal]) -> None:
        self.plan = plan
        self.calls: list[int] = []

    def on_bar(self, bar: Bar, ws: WaveState, choch: Any) -> Signal | None:
        self.calls.append(bar.ts)
        return self.plan.get(bar.ts)


def run(
    df: pd.DataFrame,
    plan: dict[int, Signal],
    wave: StubWave | None = None,
    cfg: BotConfig = CFG,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> tuple[EngineResult, FakeStrategy, StubWave]:
    strat = FakeStrategy(plan)
    w = wave or StubWave()
    eng = TradingEngine(cfg, strat, wave_engine=w, choch=NullChoch())  # type: ignore[arg-type]
    return eng.run(df, start_ts=start_ts, end_ts=end_ts), strat, w


def test_entry_at_next_open_end_close_snapshot_and_equity() -> None:
    c1, c2 = count(7), count(9, q=0.6)
    decision = dict(counts=(c1, c2), probs=(0.7, 0.3), top=c1, q_top=0.72, s_rev=0.4, E_live=49_950.0)
    wave = StubWave(at={ts(2): decision})
    s = sig(ts(2))
    df = frame(12, {3: (50_050.0, 50_100.0, 49_900.0, 50_000.0), 11: (50_000.0, 50_250.0, 49_900.0, 50_200.0)})
    res, _, _ = run(df, {ts(2): s}, wave)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert (t.entry_ts, t.exit_ts, t.direction, t.contracts, t.exit_reason) == (ts(3), ts(11), 1, 10, "END")
    assert t.entry_price == 50_050.0 and t.exit_price_avg == 50_200.0
    assert (t.stop0, t.tp1, t.tp2, t.setup) == (49_000.0, 51_000.0, 52_000.0, "W3")
    assert t.pnl == pytest.approx(15.0)                     # (50_200 - 50_050) * 10 * 0.01
    assert t.r_multiple == pytest.approx(15.0 / 105.0)      # risk = (50_050 - 49_000) * 0.1
    assert t.snapshot["counts"][0] == {
        "pattern": "IMP", "d": 1, "phase": "W2",
        "points": [(T0 + 3_600, 48_000.0), (T0 + 7_200, 51_000.0)], "q": 0.72, "p": 0.7,
    }
    assert len(t.snapshot["counts"]) == 2
    snap = t.snapshot
    assert (snap["q_top"], snap["s_rev"], snap["E_live"], snap["reason"]) == (0.72, 0.4, 49_950.0, "fake")
    assert res.signals == (s,)
    assert [p.ts for p in res.equity] == [ts(i) for i in range(12)]
    assert res.equity[2].equity == 10_000.0
    assert res.equity[3].equity == pytest.approx(9_995.0)   # marked at close 50_000
    assert res.equity[-1].equity == pytest.approx(10_015.0)


def test_end_ts_truncates_and_closes_at_last_bar() -> None:
    res, _, _ = run(frame(12), {ts(1): sig(ts(1))}, end_ts=ts(8))
    assert [(t.exit_ts, t.exit_reason) for t in res.trades] == [(ts(7), "END")]
    assert len(res.equity) == 8


def test_4h_bars_are_built_from_utc_buckets() -> None:
    start = T0 + H1  # data starts mid-bucket: the first bucket is incomplete
    rows = {
        3: (100.0, 110.0, 95.0, 105.0),
        4: (105.0, 130.0, 100.0, 120.0),
        5: (120.0, 125.0, 90.0, 92.0),
        6: (92.0, 99.0, 91.0, 98.0),
    }
    _, _, wave = run(frame(9, rows, t0=start), {})
    with_4h = [(t, b4, sub) for t, b4, sub in wave.calls if b4 is not None]
    assert len(with_4h) == 1
    t, b4, sub = with_4h[0]
    assert t == T0 + 7 * H1
    assert b4 == Bar(ts=T0 + 4 * H1, open=100.0, high=130.0, low=90.0, close=98.0, volume=4.0, synthetic=False)
    assert sub is not None and [b.ts for b in sub] == [T0 + k * H1 for k in (4, 5, 6, 7)]
    assert [c[0] for c in wave.calls] == [start + k * H1 for k in range(9)]


def test_missing_bar_is_gap_filled_before_bucketing() -> None:
    df = frame(8).drop(index=5).reset_index(drop=True)
    _, _, wave = run(df, {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(8)]
    b4 = [c[1] for c in wave.calls if c[1] is not None]
    assert len(b4) == 2 and b4[1] is not None and b4[1].synthetic


class RecChoch:
    """No-op CHoCH that records the 1h bars it is fed."""

    def __init__(self) -> None:
        self.bars: list[Bar] = []

    def update(self, bar1h: Bar) -> None:
        self.bars.append(bar1h)


def test_wicks_beyond_6_atr1h_are_clipped_for_waves_but_not_for_fills() -> None:
    # FLAT bars have TR = 200, so ATR1h(14) = 200 from bar 13 on and the cap is body + 6 * 200.
    # Bar 30 prints a 60_000 wick: the wave engine, the 4h bar (bucket 28..31) and the CHoCH see
    # 51_200, while the broker still fills TP1 55_000 and TP2 59_000 on the raw wick.
    df = frame(40, {30: (50_000.0, 60_000.0, 49_900.0, 50_000.0)})
    s = sig(ts(25), tp1=55_000.0, tp2=59_000.0)
    strat, wave, choch = FakeStrategy({ts(25): s}), StubWave(), RecChoch()
    res = TradingEngine(CFG, strat, wave_engine=wave, choch=choch).run(df)  # type: ignore[arg-type]
    assert (ts(0), "WICKS_CLIPPED:1") in res.events
    t31, b4, sub = wave.calls[31]
    assert t31 == ts(31) and b4 is not None and sub is not None
    assert b4.high == 51_200.0 and sub[2].ts == ts(30) and sub[2].high == 51_200.0
    assert choch.bars[30].high == 51_200.0 and choch.bars[30].close == 50_000.0
    assert [b.high for b in choch.bars[:30]] == [50_100.0] * 30
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(26), ts(30), "TP2")
    assert t.pnl == pytest.approx(5 * 0.01 * 5_000.0 + 5 * 0.01 * 9_000.0)  # 250 + 450
    res0, _, _ = run(frame(20), {})
    assert (ts(0), "WICKS_CLIPPED:0") in res0.events


def test_tp1_moves_stop_to_breakeven_from_next_bar() -> None:
    rows = {
        3: (50_000.0, 51_100.0, 49_950.0, 51_000.0),  # TP1 5 @ 51_000; low below BE but BE not active yet
        4: (50_500.0, 50_600.0, 49_990.0, 50_100.0),  # BE stop 50_000 hit
    }
    res, _, _ = run(frame(10, rows), {ts(1): sig(ts(1))})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(2), ts(4), "STOP")
    assert t.exit_price_avg == pytest.approx(50_500.0)
    assert t.pnl == pytest.approx(50.0)


def test_trailing_stop_on_new_4h_pivot_long() -> None:
    wave = StubWave(at={
        ts(3): dict(new_pivot_4h=Pivot(i=1, sub_ts=ts(2), price=49_800.0, kind=-1, confirmed_at=0), atr4h=400.0),
        ts(7): dict(new_pivot_4h=Pivot(i=2, sub_ts=ts(5), price=50_600.0, kind=1, confirmed_at=1), atr4h=400.0),
    })
    rows = {
        8: (50_500.0, 50_550.0, 50_450.0, 50_500.0),  # would stop out if the HIGH pivot had been used
        9: (50_000.0, 50_050.0, 49_650.0, 49_800.0),  # trailed stop 49_800 - 0.25*400 = 49_700 hit
    }
    res, _, _ = run(frame(12, rows), {ts(1): sig(ts(1))}, wave)
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason) == (ts(9), "STOP")
    assert t.exit_price_avg == pytest.approx(49_700.0)
    assert t.pnl == pytest.approx(-30.0)


def test_trailing_stop_short_never_loosens() -> None:
    wave = StubWave(at={
        ts(3): dict(new_pivot_4h=Pivot(i=1, sub_ts=ts(2), price=50_200.0, kind=1, confirmed_at=0), atr4h=400.0),
        ts(7): dict(new_pivot_4h=Pivot(i=2, sub_ts=ts(6), price=51_500.0, kind=1, confirmed_at=1), atr4h=400.0),
    })
    rows = {8: (50_000.0, 50_350.0, 49_950.0, 50_100.0)}  # stop 50_300 (not loosened to 51_600)
    res, _, _ = run(frame(12, rows), {ts(1): sig(ts(1), d=-1)}, wave)
    [t] = res.trades
    assert (t.direction, t.exit_ts, t.exit_reason) == (-1, ts(8), "STOP")
    assert t.exit_price_avg == pytest.approx(50_300.0)
    assert t.pnl == pytest.approx(-30.0)


def test_time_stop_counts_4h_closes_since_entry() -> None:
    # entry fills at bar 4 (ts % H4 == 0); 4h closes at bars 7 and 11 -> exit at bar 12 open
    rows = {12: (50_300.0, 50_400.0, 50_200.0, 50_300.0)}
    res, _, _ = run(frame(16, rows), {ts(3): sig(ts(3), time_stop_4h=2)})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(4), ts(12), "TIME")
    assert t.exit_price_avg == 50_300.0
    assert t.pnl == pytest.approx(30.0)


def test_tp1_disarms_time_stop() -> None:
    rows: dict[int, tuple[float, float, float, float]] = {5: (50_500.0, 51_100.0, 50_400.0, 50_900.0)}
    rows.update({i: (50_900.0, 51_000.0, 50_800.0, 50_900.0) for i in range(6, 16)})
    res, _, _ = run(frame(16, rows), {ts(3): sig(ts(3), time_stop_4h=2)})
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason) == (ts(15), "END")
    assert t.pnl == pytest.approx(95.0)  # 5 * 0.01 * 1_000 + 5 * 0.01 * 900


def test_lineage_kill_exits_at_next_open() -> None:
    lin = ("IMP", 1, 7)
    wave = StubWave(default=dict(lineages=frozenset({lin})), at={ts(5): dict(lineages=frozenset())})
    rows = {6: (50_100.0, 50_200.0, 50_000.0, 50_100.0)}
    res, _, _ = run(frame(10, rows), {ts(1): sig(ts(1), lineage=lin)}, wave)
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason, t.exit_price_avg) == (ts(6), "LINEAGE", 50_100.0)
    assert t.pnl == pytest.approx(10.0)


def test_flip_on_opposite_signal_and_ignore_same_direction() -> None:
    long1, long2, short = sig(ts(1)), sig(ts(3)), sig(ts(5), d=-1, setup="W5_REV")
    rows = {6: (50_200.0, 50_300.0, 50_100.0, 50_200.0), 9: (50_100.0, 50_200.0, 50_000.0, 50_100.0)}
    res, _, _ = run(frame(10, rows), {ts(1): long1, ts(3): long2, ts(5): short})
    assert [(t.direction, t.entry_ts, t.exit_ts, t.exit_reason) for t in res.trades] == [
        (1, ts(2), ts(6), "FLIP"),
        (-1, ts(6), ts(9), "END"),
    ]
    assert res.trades[0].pnl == pytest.approx(20.0)
    assert res.trades[1].pnl == pytest.approx(10.0)
    assert res.trades[1].setup == "W5_REV"
    assert (ts(3), "SIGNAL_IGNORED:same_direction") in res.events
    assert res.signals == (long1, short)


def test_cooldown_after_stop_until_phase_changes() -> None:
    inst = ("IMP", 1, 7, "W2")
    wave = StubWave(at={
        ts(4): dict(top=count(9, phase="W3")),   # other lineage: no release
        ts(5): dict(top=count(7, phase="W2")),   # same lineage, same phase: no release
        ts(6): dict(top=count(7, phase="W3")),   # same lineage, phase changed: release
    })
    rows = {3: (49_500.0, 49_600.0, 48_900.0, 49_000.0)}  # stop at 49_000
    plan = {ts(1): sig(ts(1), instance=inst), ts(5): sig(ts(5), instance=inst), ts(7): sig(ts(7), instance=inst)}
    res, _, _ = run(frame(12, rows), plan, wave)
    assert [(t.entry_ts, t.exit_reason) for t in res.trades] == [(ts(2), "STOP"), (ts(8), "END")]
    assert res.trades[0].pnl == pytest.approx(-100.0)
    assert res.trades[1].contracts == 9  # 1% of 9_900 / 1_000 = 0.099 BTC
    assert (ts(5), "SIGNAL_REJECTED:cooldown") in res.events


def test_entry_blackout_and_futures_close_block_new_entries() -> None:
    plan = {ts(k, FRI): sig(ts(k, FRI)) for k in (20, 21, 22, 24)}
    res, strat, _ = run(frame(30, t0=FRI), plan)
    for k in (20, 21, 22):   # fill bar in blackout / decision bar in blackout / futures closed
        assert ts(k, FRI) not in strat.calls
    assert ts(24, FRI) in strat.calls
    assert [(t.entry_ts, t.exit_reason) for t in res.trades] == [(ts(25, FRI), "END")]


def test_start_ts_gates_entries_and_equity_points() -> None:
    plan = {ts(2): sig(ts(2)), ts(6): sig(ts(6))}
    res, strat, wave = run(frame(10), plan, start_ts=ts(5))
    assert ts(2) not in strat.calls
    assert len(wave.calls) == 10  # warm-up bars are still fed to the wave engine
    assert [t.entry_ts for t in res.trades] == [ts(7)]
    assert [p.ts for p in res.equity] == [ts(i) for i in range(5, 10)]


def test_total_halt_flattens_and_blocks_entries() -> None:
    cfg = BotConfig(costs=ZERO, breakers=BreakerParams(total_dd=0.005))
    rows = {
        3: (50_000.0, 50_000.0, 49_350.0, 49_400.0),  # equity 9_940 <= 10_000 * 0.995
        4: (49_450.0, 49_500.0, 49_400.0, 49_450.0),
    }
    res, strat, _ = run(frame(10, rows), {ts(1): sig(ts(1)), ts(6): sig(ts(6))}, cfg=cfg)
    assert (ts(3), "TOTAL_HALT") in res.events
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason, t.exit_price_avg) == (ts(4), "BREAKER", 49_450.0)
    assert t.pnl == pytest.approx(-55.0)
    assert ts(6) not in strat.calls
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_engine_loop.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'ewbot.engine.loop'"

- [ ] **Step 7: Write minimal implementation**

`src/ewbot/engine/loop.py`:

```python
"""TradingEngine: the single event loop shared by backtest and (later) paper/live modes (spec 3, 5.3, 5.4, 6.3).

Before the loop (spec 4.1): fill_gaps, then clip 1h wicks beyond wick_clip_atr1h x ATR1h (ATR of the
previous bar). The CLIPPED bars feed the 4h aggregation, the WaveEngine, the CHoCH detector and the
strategy; the PaperBroker, equity marks and the END close use the RAW bars (real traded prices).
The number of clipped bars is logged once as (first bar ts, "WICKS_CLIPPED:<n>").

Per closed 1h bar:
  1. broker.process_bar(bar)                      fills of orders queued at the previous close
  2. handle fills                                 ENTRY -> open trade; TP1 -> stop to breakeven;
                                                  full exit -> Trade, breakers.on_trade_closed, cooldown on STOP
  3. wave.on_1h_close(cbar, bar4h, sub_bars)      bar4h built here from UTC 4h buckets of clipped bars
  4. choch.update(cbar)
  5. position management                          trailing on new 4h pivot, time stop, lineage kill
  6. breakers.on_equity(close instant, equity)    TOTAL_HALT -> flatten at next open, no more entries
  7. cooldown release; entry / flip evaluation    (bar.ts >= start_ts, no pending entry, can_enter,
                                                   no blackout on this bar or the fill bar, not the last bar)
  8. last bar: close any open position at bar.close (reason END)
  9. EquityPoint(bar.ts, equity marked at bar.close) for bar.ts >= start_ts
"""
from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from ewbot.config import BotConfig
from ewbot.data.candles import clip_wicks, entry_blackout, fill_gaps, to_bars
from ewbot.engine.position_mgr import (
    EngineResult,
    EquityPoint,
    Trade,
    _make_snapshot,
    _OpenTrade,
    _PendingEntry,
    _tighten,
    _tp1_split,
    _trail_candidate,
)
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.indicators import atr_series
from ewbot.risk.breakers import CircuitBreakers
from ewbot.risk.sizing import size_contracts
from ewbot.strategy.base import Signal, Strategy
from ewbot.strategy.choch import ChochDetector
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState

__all__ = ["EngineResult", "EquityPoint", "Trade", "TradingEngine"]

_CLOSING_KIND_REASON = {"STOP": "STOP", "TP1": "TP1", "TP2": "TP2"}


def _agg_4h(sub: Sequence[Bar]) -> Bar:
    return Bar(
        ts=sub[0].ts,
        open=sub[0].open,
        high=max(b.high for b in sub),
        low=min(b.low for b in sub),
        close=sub[-1].close,
        volume=sum(b.volume for b in sub),
        synthetic=any(b.synthetic for b in sub),
    )


class TradingEngine:
    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        *,
        wave_engine: WaveEngine | None = None,
        choch: ChochDetector | None = None,
    ) -> None:
        """wave_engine / choch: optional injected instances (tests). When None, fresh real ones are
        built for every run(). Injected instances are stateful: use a new TradingEngine per run."""
        self.cfg = cfg
        self.strategy = strategy
        self._wave_in = wave_engine
        self._choch_in = choch

    def run(self, df1h: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult:
        df = fill_gaps(df1h)  # idempotent (T3); guarantees a continuous hourly series for bucketing
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        df = df.reset_index(drop=True)
        if df.empty:
            return EngineResult((), (), (), ())
        atr1h = atr_series(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), self.cfg.wave.atr_len
        )
        clipped, n_clipped = clip_wicks(df, atr1h, self.cfg.wave.wick_clip_atr1h)  # spec 4.1
        wave = self._wave_in if self._wave_in is not None else WaveEngine(self.cfg.wave)
        choch = (
            self._choch_in
            if self._choch_in is not None
            else ChochDetector(self.cfg.strategy.choch_bars)
        )
        run = _Run(self.cfg, self.strategy, wave, choch, start_ts)
        return run.execute(to_bars(df), to_bars(clipped), n_clipped)


class _Run:
    """State of one engine run (kept out of TradingEngine so run() has no hidden carry-over)."""

    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        wave: WaveEngine,
        choch: ChochDetector,
        start_ts: int | None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.wave = wave
        self.choch = choch
        self.start_ts = start_ts
        self.broker = PaperBroker(cfg.costs, cfg.initial_equity)
        self.breakers = CircuitBreakers(cfg.breakers, cfg.initial_equity)
        self.trades: list[Trade] = []
        self.equity: list[EquityPoint] = []
        self.signals: list[Signal] = []
        self.events: list[tuple[int, str]] = []
        self.open: _OpenTrade | None = None
        self.pending: _PendingEntry | None = None
        self.cooldown: set[tuple] = set()

    # ------------------------------------------------------------------ main loop
    def execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult:
        """bars: raw 1h bars (broker, marks, END close); clipped: the same bars after clip_wicks
        (4h aggregation, wave engine, CHoCH, strategy). Same length and ts."""
        self.events.append((bars[0].ts, f"WICKS_CLIPPED:{n_clipped}"))
        bucket: list[Bar] = []
        last = len(bars) - 1
        for idx, (bar, cbar) in enumerate(zip(bars, clipped)):
            if bucket and bucket[0].ts // H4 != cbar.ts // H4:
                bucket = []
            bucket.append(cbar)
            bar4h: Bar | None = None
            sub: tuple[Bar, ...] | None = None
            if cbar.ts % H4 == 3 * H1:
                if len(bucket) == 4:
                    sub = tuple(bucket)
                    bar4h = _agg_4h(sub)
                bucket = []

            for f in self.broker.process_bar(bar):  # 1-2 (raw prices)
                self._on_fill(f)
            ws = self.wave.on_1h_close(cbar, bar4h, sub)  # 3
            self.choch.update(cbar)  # 4
            self._manage(ws, closed_4h=bar4h is not None)  # 5

            t_close = bar.ts + H1  # 6
            for ev in self.breakers.on_equity(t_close, self.broker.equity(bar.close)):
                self.events.append((bar.ts, ev))
            if self.breakers.halted:
                self._request_exit("BREAKER")

            self._release_cooldown(ws)  # 7
            if idx < last and self._may_enter(bar, t_close):
                sig = self.strategy.on_bar(cbar, ws, self.choch)
                if sig is not None:
                    self._consider(sig, bar, ws)

            if idx == last:  # 8
                self._close_end(bar)
            if self.start_ts is None or bar.ts >= self.start_ts:  # 9
                self.equity.append(EquityPoint(bar.ts, self.broker.equity(bar.close)))

        return EngineResult(tuple(self.trades), tuple(self.equity), tuple(self.signals), tuple(self.events))

    # ------------------------------------------------------------------ fills
    def _on_fill(self, f: Fill) -> None:
        if f.kind == "ENTRY":
            pe = self.pending
            assert pe is not None, "ENTRY fill without a pending signal"
            self.pending = None
            self.open = _OpenTrade(
                sig=pe.sig,
                snapshot=pe.snapshot,
                contract_size=self.cfg.costs.contract_size,
                entry_ts=f.ts,
                entry_price=f.price,
                contracts=f.contracts,
                pos=self.broker.position,  # None if it was already closed inside the entry bar
                fees=f.fee,
            )
            return
        ot = self.open
        assert ot is not None, f"{f.kind} fill without an open trade"
        if ot.add_exit_fill(f):
            reason = _CLOSING_KIND_REASON.get(f.kind) or ot.exit_reason or "END"
            self._finish(ot, f.ts, reason)
        elif f.kind == "TP1":
            pos = self.broker.position
            if pos is not None:
                self.broker.set_stop(_tighten(ot.direction, pos.stop, ot.entry_price))

    def _finish(self, ot: _OpenTrade, exit_ts: int, reason: str) -> None:
        trade = ot.to_trade(exit_ts, reason)
        self.trades.append(trade)
        self.open = None
        for ev in self.breakers.on_trade_closed(exit_ts, trade.pnl):
            self.events.append((exit_ts, ev))
        if reason == "STOP" and ot.sig.instance is not None:
            self.cooldown.add(tuple(ot.sig.instance))

    # ------------------------------------------------------------------ management
    def _manage(self, ws: WaveState, closed_4h: bool) -> None:
        ot = self.open
        pos = self.broker.position
        if ot is None or pos is None:
            return
        d = ot.direction
        cand = _trail_candidate(d, ws.new_pivot_4h, ws.atr4h, self.cfg.strategy.buf_struct_atr)
        if cand is not None:
            new_stop = _tighten(d, pos.stop, cand)
            if new_stop != pos.stop:
                self.broker.set_stop(new_stop)
        if closed_4h and not ot.tp1_done:
            ot.n4h += 1
            if ot.sig.time_stop_4h is not None and ot.n4h >= ot.sig.time_stop_4h:
                self._request_exit("TIME")
        lineage = ot.sig.lineage
        if lineage is not None and ws.ready and tuple(lineage) not in ws.lineages:
            self._request_exit("LINEAGE")

    def _request_exit(self, reason: str) -> None:
        ot = self.open
        if ot is None or self.broker.position is None or ot.exit_reason is not None:
            return
        ot.exit_reason = reason
        self.broker.queue_exit()

    def _release_cooldown(self, ws: WaveState) -> None:
        top = ws.top
        if top is None or not self.cooldown:
            return
        lineage = tuple(top.lineage)
        self.cooldown = {i for i in self.cooldown if not (tuple(i[:3]) == lineage and i[3] != top.phase)}

    # ------------------------------------------------------------------ entries
    def _may_enter(self, bar: Bar, t_close: int) -> bool:
        if self.start_ts is not None and bar.ts < self.start_ts:
            return False
        if self.pending is not None:
            return False
        if not self.breakers.can_enter(t_close):
            return False
        return not (entry_blackout(bar.ts) or entry_blackout(bar.ts + H1))

    def _consider(self, sig: Signal, bar: Bar, ws: WaveState) -> None:
        held = 0
        if self.open is not None and self.broker.position is not None and self.open.exit_reason is None:
            held = self.open.direction
        if sig.direction == held:
            self.events.append((bar.ts, "SIGNAL_IGNORED:same_direction"))
            return
        if sig.instance is not None and tuple(sig.instance) in self.cooldown:
            self.events.append((bar.ts, "SIGNAL_REJECTED:cooldown"))
            return
        n = size_contracts(
            self.broker.equity(bar.close),
            sig.entry_ref,
            sig.stop,
            sig.direction,
            self.cfg.risk,
            sig.risk_mult,
            self.cfg.costs,
        )
        if n == 0:
            self.events.append((bar.ts, "SIGNAL_REJECTED:size"))
            return
        if held == -sig.direction:
            self._request_exit("FLIP")
        tp1_n = _tp1_split(n, self.cfg.strategy.tp1_frac)
        self.broker.queue_entry(sig.direction, n, sig.stop, sig.tp1, sig.tp2, tp1_n)
        self.pending = _PendingEntry(sig, _make_snapshot(ws, sig, self.cfg.wave.top_k))
        self.signals.append(sig)

    # ------------------------------------------------------------------ end of data
    def _close_end(self, bar: Bar) -> None:
        self.pending = None
        ot = self.open
        if ot is None or self.broker.position is None:
            return
        ot.exit_reason = "END"
        f = self.broker.close_now(bar.ts, bar.close)
        if f is not None:
            self._on_fill(f)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/engine -v`
Expected: PASS — 27 passed (11 in `test_position_mgr.py`, 16 in `test_engine_loop.py`).

Then run the whole suite to check that nothing upstream broke: `uv run pytest -q`. Expected: all
tests pass. Part C1 alone contributes 70 tests.

Accounting sanity check (optional, about 1 s). Over a random run, the sum of trade P&L must equal
the final equity minus the initial equity, to float precision:

```bash
uv run python -c "
import numpy as np, pandas as pd
from ewbot.config import BotConfig, BreakerParams
from ewbot.engine.loop import TradingEngine
from ewbot.strategy.base import Signal
n, t0 = 20000, 1483228800
rng = np.random.default_rng(0)
px = 30000 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
op = np.r_[px[0], px[:-1]]
df = pd.DataFrame({'ts': t0 + np.arange(n) * 3600, 'open': op, 'high': np.maximum(op, px) * 1.003,
                   'low': np.minimum(op, px) * 0.997, 'close': px, 'volume': 1.0})
class Rand:
    name = 'R'
    def on_bar(self, bar, ws, choch):
        if rng.random() < 0.02:
            d = int(rng.choice([-1, 1])); r = rng.uniform(0.005, 0.04); c = bar.close
            return Signal(bar.ts, 'RANDOM', d, c, c * (1 - r * d), c * (1 + 1.5 * r * d), c * (1 + 3 * r * d),
                          1.0, 20, None, None, 'sanity')
        return None
cfg = BotConfig(breakers=BreakerParams(daily_loss=1.0, total_dd=0.99, max_consec_losses=10**9))
res = TradingEngine(cfg, Rand()).run(df, start_ts=t0 + 1000 * 3600)
gap = sum(t.pnl for t in res.trades) - (res.equity[-1].equity - cfg.initial_equity)
print(len(res.trades), abs(gap) < 1e-6)
"
```

Expected: a trade count in the hundreds, followed by `True`.

- [ ] **Step 9: Commit**

```bash
git add src/ewbot/engine/__init__.py src/ewbot/engine/position_mgr.py src/ewbot/engine/loop.py tests/engine/test_position_mgr.py tests/engine/test_engine_loop.py
git commit -m "feat: TradingEngine loop with wick clipping, breakeven, structural trailing, time stop, lineage kill, flip, cooldown and END close"
```


---

## Part C2 — Backtest, Validation & Reporting

Tasks **T18–T23**. Everything here lives in `src/ewbot/backtest/`, `src/ewbot/cli.py`, `tests/backtest/` and `scripts/`. Parts A, B and C1 are assumed to be in place (the unit tests below use small synthetic `Trade` / `EquityPoint` / `EngineResult` objects and a fake `TradingEngine`, so they do not depend on the wave engine; only T23 runs the real engine).

### How this part reads the shared contract (resolved ambiguities)

- `execution.costs.slip(price, side, c)` returns the **adjusted fill price** (buy pays up, sell receives less); `fee(price, contracts, c)` returns USD for `contracts` contracts of `c.contract_size` BTC with `apply_min_fee=False`. Benchmark tests hand-compute with `price*(1±slippage)` and `fee_rate * price * contracts * contract_size`.
- Equity points are one per 1h bar, `ts` = bar open. Benchmarks follow the same convention and fill at the **next bar open**.
- `compute_metrics` windows are `[start_ts, end_ts)`: equity points inside the window **plus the last point before it as base** (so the first in-window return is measured against the true previous value and consecutive walk-forward folds join without a gap). Trade stats use trades whose `entry_ts` is in the window. `calmar = cagr / max_dd`, and `0.0` when `max_dd == 0` (nothing to divide by).
- `buy_hold_equity` / `ema_cross_equity` are the contract names; `buy_hold()` is an extra public helper that also returns the single trade (used for exposure and fee reporting). Buy&hold is a **spot** hold (fractional BTC, no leverage, no funding): one entry fee + slippage at the next open, one exit fee + slippage at the last close. EMA is a 1x futures position and pays/receives funding like the strategies. EMA/BH trades have no stop, so their `r_multiple` is defined as `pnl / (1% of equity at entry)`; `avg_r` of BH/EMA is therefore **not comparable** with Elliott `avg_r`.
- `RandomEntryStrategy` pre-draws its whole plan at construction (times, directions, reference trade) from `seed`; the stop distance is stored as a **fraction of price** (so a 2017 stop of 5% stays 5% in 2021), TP multiples in R, and the time stop comes from `time_stop_bars(reference holding time in 4h bars)`. Times in the blackout (`entry_blackout(t)` or `entry_blackout(t+H1)`) are never eligible, matching the engine's own gate. The engine still decides whether a planned signal is usable (flat / breakers / cooldown), so realised random trades can be slightly fewer than `n_signals`.
- `compare()` gains an optional trailing `workers: int = 1` (parallel A/C/HYBRID and RANDOM runs). `n_signals` for RANDOM = number of **A trades** in the window (A's actual entries), geometry sampled from those trades, seeds `cfg.seed + k`.
- `Trade.snapshot` is only specified as "top-5 counts (pattern,d,phase,points[(ts,price)],q), q_top, s_rev, E_live, reason". The report looks for the counts list under `"counts"` (also accepts `"top5"` / `"top_counts"`), items as dicts with `pattern, d, phase, q, points`; `q_top` is read by `reliability_by_q`. If Part C1 names the key differently, change the single tuple in `report._snapshot_view`.
- Walk-forward runs each of the 60 combos **once** over `[2017-01-01, 2024-01-01)` and slices that continuous run into train/test windows (compounding therefore carries across folds; this is the approximation the contract asks for). The first ~33 days of 2017 are engine warm-up (200 4h bars), so the first training window is slightly shorter than 24 months of trading. Bars at/after `WF_END` (2024-01-01) are dropped before anything runs, and `HOLDOUT_START` is derived from `WF_END`, so the boundary has a single source.
- Circuit breakers are **disabled** in those grid runs: `run_grid` applies `breakers.enabled = False` itself. Breakers are a risk overlay validated in the holdout and in paper mode. In one continuous 2017–2023 run a TOTAL_HALT latched in an early year would zero the trades of every later fold and disqualify the combo there for reasons unrelated to that fold. The holdout, `compare` and plain backtests keep breakers on.
- A combo is eligible in a fold only with `>= MIN_TRAIN_TRADES = 12` trades in the 24-month training window (spec 9.3). This floor only keeps a handful of trades from producing a degenerate Calmar; it is not a statistical-power criterion. On 2017–2023 the strategy makes about 8–13 trades a year (K4 4.0 → 2.0), so a floor of 30 left most folds with no eligible combo.
- The holdout is run through `compare()` so that A / HYBRID can be judged against C, buy&hold and the random control on the same window (spec 9.6); only `[2024-01-01, last bar]` is reported. The lock file is written **before** the run, lives at `<project root>/holdout.lock` (never the CWD) and is bound to the full resolved config (every `BotConfig` field + grid + holdout start + `random_runs`). Off-grid params, disabled breakers, a non-conservative risk profile and `random_runs < 1` are refused before anything is written, and without a random-control distribution the automatic go-live criteria are not met.
- Go-live trade count (spec 9.6 criterion 3): **50 out-of-sample trades with the frozen parameters**, counted over the holdout plus later paper trading (Plan 2). At the measured frequency the holdout alone yields roughly 21–35 trades, so the holdout report states each candidate's `n_trades` and, below 50, prints `<name>: 표본 외 거래 N/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족`. These lines are counts only, never performance claims. Plan 1 does not implement the paper accumulation.
- `pyproject.toml` must expose the console script `ewbot = "ewbot.cli:main"` (T22 checks/adds it).

---

### Task 18: Metrics and bootstrap confidence intervals

**Files:**
- Create: `src/ewbot/backtest/__init__.py` (empty), `src/ewbot/backtest/metrics.py`
- Create: `tests/backtest/conftest.py`
- Test: `tests/backtest/test_metrics.py`

**Interfaces:**
- Consumes: `ewbot.engine.loop.EquityPoint, Trade`.
- Produces: `Metrics`, `compute_metrics(equity, trades, start_ts=None, end_ts=None)`, `bootstrap_ci(trades, stat="avg_r"|"win_rate"|"avg_pnl", n=2000, seed=0)`, plus helpers used later: `trade_stats(trades)` (n, win_rate, avg_r, pnl, bootstrap CI of avg_r), `reliability_by_q(trades, edges)` (spec 9.5 hit rate per `snapshot["q_top"]` bin), `HOURS_PER_YEAR = 8760`.

Hand-computed expectations used in the tests: equity `100 → 110 → 99` has `max_dd = (110-99)/110 = 0.1` and `total_return = -0.01`; equity `100, 110, 121, 108.9` has hourly returns `+0.1, +0.1, -0.1`, so mean `0.1/3`, sample std `sqrt(0.0266667/2)`, downside RMS `sqrt(0.01/3)`.

- [ ] **Step 1: Write the failing test**

`tests/backtest/conftest.py` (shared factories; no `tests/backtest/__init__.py` is needed under `--import-mode=importlib`):

```python
from __future__ import annotations

import pytest

from ewbot.engine.loop import EquityPoint, Trade


def make_trade(
    entry_ts: int = 0,
    exit_ts: int = 3600,
    pnl: float = 10.0,
    r: float = 1.0,
    setup: str = "W3",
    direction: int = 1,
    snapshot: dict | None = None,
    entry: float = 100.0,
    stop0: float = 95.0,
    tp1: float = 105.0,
    tp2: float | None = 110.0,
    reason: str = "STOP",
) -> Trade:
    return Trade(
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        setup=setup,
        direction=direction,
        contracts=1,
        entry_price=entry,
        exit_price_avg=entry + pnl,
        stop0=stop0,
        tp1=tp1,
        tp2=tp2,
        pnl=pnl,
        fees=0.0,
        funding=0.0,
        r_multiple=r,
        exit_reason=reason,
        snapshot=snapshot if snapshot is not None else {},
    )


def make_equity(values, t0: int = 0, step: int = 3600) -> list[EquityPoint]:
    return [EquityPoint(ts=t0 + i * step, equity=float(v)) for i, v in enumerate(values)]


@pytest.fixture
def mk_trade():
    return make_trade


@pytest.fixture
def mk_equity():
    return make_equity
```

`tests/backtest/test_metrics.py`:

```python
from __future__ import annotations

import math

import pytest

from ewbot.backtest.metrics import (
    Metrics,
    bootstrap_ci,
    compute_metrics,
    reliability_by_q,
    trade_stats,
)

ANN = math.sqrt(8760)


def test_max_dd_hand_computed(mk_equity):
    m = compute_metrics(mk_equity([100, 110, 99]), [])
    assert isinstance(m, Metrics)
    assert m.max_dd == pytest.approx(0.1)          # peak 110 -> trough 99
    assert m.total_return == pytest.approx(-0.01)  # 99/100 - 1
    assert m.sharpe == pytest.approx(0.0, abs=1e-9)    # returns +10% / -10%: mean 0
    assert m.sortino == pytest.approx(0.0, abs=1e-9)
    assert m.calmar == pytest.approx(m.cagr / 0.1)
    assert m.cagr == pytest.approx(-1.0, abs=1e-9)  # -1% over 2 hours, annualised
    assert m.n_trades == 0 and m.win_rate == 0.0 and m.exposure == 0.0


def test_sharpe_and_sortino_hand_computed(mk_equity):
    # returns: +0.1, +0.1, -0.1 -> mean 0.1/3, sample var = 0.0266667/2, downside ms = 0.01/3
    m = compute_metrics(mk_equity([100, 110, 121, 108.9]), [])
    mean = 0.1 / 3
    assert m.sharpe == pytest.approx(mean / math.sqrt(0.02666666666666667 / 2) * ANN, rel=1e-6)
    assert m.sortino == pytest.approx(mean / math.sqrt(0.01 / 3) * ANN, rel=1e-6)
    assert m.max_dd == pytest.approx(0.1)


def test_window_uses_prior_point_as_base(mk_equity):
    eq = mk_equity([100, 110, 121, 108.9])
    m = compute_metrics(eq, [], start_ts=2 * 3600)  # points 121, 108.9 + base 110
    assert m.total_return == pytest.approx(108.9 / 110 - 1)
    m2 = compute_metrics(eq, [], start_ts=0, end_ts=2 * 3600)  # points 100, 110
    assert m2.total_return == pytest.approx(0.1)
    assert m2.max_dd == 0.0 and m2.calmar == 0.0


def test_trade_stats_and_exposure(mk_equity, mk_trade):
    eq = mk_equity([100, 101, 102, 103])  # ts 0, 3600, 7200, 10800 -> span 3 hours
    trades = [
        mk_trade(entry_ts=3600, exit_ts=7200, pnl=5.0, r=2.0),
        mk_trade(entry_ts=7200, exit_ts=10800, pnl=-2.0, r=-1.0),
        mk_trade(entry_ts=10800, exit_ts=14400, pnl=3.0, r=1.0),
    ]
    m = compute_metrics(eq, trades)
    assert m.n_trades == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.avg_r == pytest.approx(2 / 3)
    assert m.exposure == pytest.approx(2 / 3)  # held 1h + 1h out of 3h (3rd trade is clipped away)
    only_first = compute_metrics(eq, trades, start_ts=0, end_ts=7200)
    assert only_first.n_trades == 1 and only_first.win_rate == 1.0


def test_degenerate_inputs_do_not_crash(mk_equity):
    assert compute_metrics([], []).total_return == 0.0
    flat = compute_metrics(mk_equity([100, 100, 100]), [])
    assert flat.sharpe == 0.0 and flat.max_dd == 0.0 and flat.calmar == 0.0
    dead = compute_metrics(mk_equity([100, 50, 0]), [])
    assert dead.cagr == -1.0 and dead.max_dd == pytest.approx(1.0)


def test_bootstrap_ci_deterministic_and_sane(mk_trade):
    trades = [mk_trade(r=r, pnl=r) for r in (2.0, -1.0, 1.0, 3.0, -1.0, 0.5, 2.5, -1.0)]
    a = bootstrap_ci(trades, "avg_r", n=500, seed=3)
    b = bootstrap_ci(trades, "avg_r", n=500, seed=3)
    c = bootstrap_ci(trades, "avg_r", n=500, seed=4)
    assert a == b
    assert a != c
    mean = sum(t.r_multiple for t in trades) / len(trades)
    assert a[0] <= mean <= a[1]
    const = bootstrap_ci([mk_trade(r=2.0) for _ in range(5)], "avg_r", n=100, seed=0)
    assert const == (2.0, 2.0)
    assert bootstrap_ci([], "avg_r") == (0.0, 0.0)
    wr = bootstrap_ci([mk_trade(pnl=1.0), mk_trade(pnl=-1.0)] * 10, "win_rate", n=200, seed=0)
    assert 0.0 <= wr[0] <= 0.5 <= wr[1] <= 1.0
    with pytest.raises(ValueError):
        bootstrap_ci(trades, "bogus")


def test_trade_stats_and_reliability(mk_trade):
    ts = trade_stats([mk_trade(pnl=4.0, r=2.0), mk_trade(pnl=-2.0, r=-1.0)])
    assert ts["n"] == 2 and ts["win_rate"] == 0.5 and ts["avg_r"] == 0.5 and ts["pnl"] == 2.0
    assert ts["ci_lo"] <= 0.5 <= ts["ci_hi"]
    assert trade_stats([])["n"] == 0

    trades = [
        mk_trade(pnl=1.0, r=1.0, snapshot={"q_top": 0.65}),
        mk_trade(pnl=-1.0, r=-1.0, snapshot={"q_top": 0.66}),
        mk_trade(pnl=2.0, r=2.0, snapshot={"q_top": 0.75}),
        mk_trade(pnl=-1.0, r=-1.0, snapshot={"q_top": 0.55}),
        mk_trade(pnl=1.0, r=1.0, snapshot={}),  # no q_top -> skipped
    ]
    rows = {(r["q_lo"], r["q_hi"]): r for r in reliability_by_q(trades)}
    assert rows[(0.6, 0.7)]["n"] == 2 and rows[(0.6, 0.7)]["hit_rate"] == 0.5
    assert rows[(0.7, 0.8)]["n"] == 1 and rows[(0.7, 0.8)]["hit_rate"] == 1.0
    assert rows[(0.5, 0.6)]["n"] == 1 and rows[(0.5, 0.6)]["hit_rate"] == 0.0
    assert rows[(0.8, 1.01)]["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_metrics.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.metrics'`.

- [ ] **Step 3: Write minimal implementation**

```bash
mkdir -p src/ewbot/backtest && touch src/ewbot/backtest/__init__.py
```

`src/ewbot/backtest/metrics.py`:

```python
"""Backtest performance metrics (hourly equity, annualised with 8760) and bootstrap CIs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ewbot.engine.loop import EquityPoint, Trade

HOURS_PER_YEAR = 8760
_EPS = 1e-15


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return: float
    cagr: float
    max_dd: float
    sharpe: float
    sortino: float
    calmar: float
    win_rate: float
    avg_r: float
    n_trades: int
    exposure: float


def _in_window(ts: int, start_ts: int | None, end_ts: int | None) -> bool:
    return (start_ts is None or ts >= start_ts) and (end_ts is None or ts < end_ts)


def _window_equity(
    equity: Sequence[EquityPoint], start_ts: int | None, end_ts: int | None
) -> tuple[np.ndarray, np.ndarray]:
    """Equity points with start_ts <= ts < end_ts, plus the last point BEFORE start_ts as
    the base, so the first in-window return is measured from the true prior value."""
    n = len(equity)
    ts = np.fromiter((p.ts for p in equity), dtype=np.int64, count=n)
    eq = np.fromiter((p.equity for p in equity), dtype=np.float64, count=n)
    keep = np.ones(n, dtype=bool)
    if start_ts is not None:
        keep &= ts >= start_ts
    if end_ts is not None:
        keep &= ts < end_ts
    idx = np.flatnonzero(keep)
    if idx.size == 0:
        return ts[:0], eq[:0]
    first, last = int(idx[0]), int(idx[-1])
    if first > 0:
        first -= 1
    return ts[first : last + 1], eq[first : last + 1]


def compute_metrics(
    equity: Sequence[EquityPoint],
    trades: Sequence[Trade],
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> Metrics:
    """Hourly-return metrics over [start_ts, end_ts). Trade stats use trades whose
    entry_ts is in the window. calmar = cagr / max_dd, or 0.0 when max_dd == 0."""
    win = [t for t in trades if _in_window(t.entry_ts, start_ts, end_ts)]
    n_tr = len(win)
    win_rate = sum(1 for t in win if t.pnl > 0) / n_tr if n_tr else 0.0
    avg_r = float(np.mean([t.r_multiple for t in win])) if n_tr else 0.0

    ts, eq = _window_equity(equity, start_ts, end_ts)
    if eq.size < 2 or eq[0] <= 0:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, win_rate, avg_r, n_tr, 0.0)

    prev = eq[:-1]
    safe_prev = np.where(prev > 0, prev, 1.0)
    rets = np.where(prev > 0, eq[1:] / safe_prev - 1.0, 0.0)

    total_return = float(eq[-1] / eq[0] - 1.0)
    years = rets.size / HOURS_PER_YEAR
    if eq[-1] <= 0:
        cagr = -1.0
    else:
        cagr = math.expm1(min(700.0, math.log(eq[-1] / eq[0]) / years))

    peak = np.maximum.accumulate(eq)
    safe_peak = np.where(peak > 0, peak, 1.0)
    max_dd = float(np.max(np.where(peak > 0, (peak - eq) / safe_peak, 0.0)))

    ann = math.sqrt(HOURS_PER_YEAR)
    mean = float(rets.mean())
    std = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
    sharpe = mean / std * ann if std > _EPS else 0.0
    downside = float(math.sqrt(np.mean(np.minimum(rets, 0.0) ** 2)))
    sortino = mean / downside * ann if downside > _EPS else 0.0
    calmar = cagr / max_dd if max_dd > 1e-12 else 0.0

    span = float(ts[-1] - ts[0])
    held = 0.0
    for t in trades:
        lo, hi = max(t.entry_ts, int(ts[0])), min(t.exit_ts, int(ts[-1]))
        if hi > lo:
            held += hi - lo
    exposure = min(1.0, held / span) if span > 0 else 0.0

    return Metrics(
        total_return=total_return,
        cagr=float(cagr),
        max_dd=max_dd,
        sharpe=float(sharpe),
        sortino=float(sortino),
        calmar=float(calmar),
        win_rate=win_rate,
        avg_r=avg_r,
        n_trades=n_tr,
        exposure=float(exposure),
    )


def _trade_values(trades: Sequence[Trade], stat: str) -> np.ndarray:
    if stat == "avg_r":
        return np.array([t.r_multiple for t in trades], dtype=np.float64)
    if stat == "win_rate":
        return np.array([1.0 if t.pnl > 0 else 0.0 for t in trades], dtype=np.float64)
    if stat == "avg_pnl":
        return np.array([t.pnl for t in trades], dtype=np.float64)
    raise ValueError(f"unknown bootstrap stat {stat!r} (use avg_r | win_rate | avg_pnl)")


def bootstrap_ci(
    trades: Sequence[Trade], stat: str = "avg_r", n: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% percentile bootstrap CI of the per-trade mean of `stat` (resampling trades)."""
    vals = _trade_values(trades, stat)
    m = vals.size
    if m == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    means = np.empty(n, dtype=np.float64)
    chunk = max(1, 4_000_000 // m)
    for lo in range(0, n, chunk):
        hi = min(n, lo + chunk)
        idx = rng.integers(0, m, size=(hi - lo, m))
        means[lo:hi] = vals[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def trade_stats(trades: Sequence[Trade], seed: int = 0) -> dict[str, float | int]:
    """Trade-level summary used by the per-setup / long-short breakdown tables."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_r": 0.0, "pnl": 0.0, "ci_lo": 0.0, "ci_hi": 0.0}
    lo, hi = bootstrap_ci(trades, "avg_r", n=1000, seed=seed)
    return {
        "n": n,
        "win_rate": sum(1 for t in trades if t.pnl > 0) / n,
        "avg_r": float(np.mean([t.r_multiple for t in trades])),
        "pnl": float(sum(t.pnl for t in trades)),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def reliability_by_q(
    trades: Sequence[Trade], edges: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 1.01)
) -> list[dict[str, float | int]]:
    """Hit rate (pnl > 0) per bin of the top-count fit q at signal time (spec 9.5).
    Trades without snapshot['q_top'] are skipped."""
    rows: list[dict[str, float | int]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [t for t in trades if lo <= float(t.snapshot.get("q_top", -1.0)) < hi]
        n = len(sel)
        rows.append(
            {
                "q_lo": lo,
                "q_hi": hi,
                "n": n,
                "hit_rate": sum(1 for t in sel if t.pnl > 0) / n if n else 0.0,
                "avg_r": float(np.mean([t.r_multiple for t in sel])) if n else 0.0,
            }
        )
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_metrics.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**
```bash
git add src/ewbot/backtest/__init__.py src/ewbot/backtest/metrics.py tests/backtest/conftest.py tests/backtest/test_metrics.py
git commit -m "feat(backtest): performance metrics, bootstrap CI, q reliability"
```

---

### Task 19: Benchmarks (buy&hold, EMA 50/200, random entry)

**Files:**
- Create: `src/ewbot/backtest/benchmarks.py`
- Test: `tests/backtest/test_benchmarks.py`

**Interfaces:**
- Consumes: `ewbot.config.CostParams, StrategyParams`; `ewbot.data.candles.aggregate_4h, entry_blackout, slice_ts`; `ewbot.execution.costs.fee, slip, funding_per_hour`; `ewbot.indicators.ema_series`; `ewbot.strategy.base.Signal, time_stop_bars`; `ewbot.engine.loop.EquityPoint, Trade`; `ewbot.types.Bar, H1, H4`. Test only: T18 `ewbot.backtest.metrics.compute_metrics`.
- Produces: `buy_hold_equity(df1h, start_ts, end_ts, costs, equity0) -> list[EquityPoint]`, `buy_hold(...) -> (equity, trades)`, `ema_cross_equity(df1h, start_ts, end_ts, costs, equity0, fast=50, slow=200) -> (equity, trades)`, `RandomEntryStrategy(sp, reference_trades, n_signals, span, seed)` implementing `Strategy.on_bar(bar, ws, choch)` (ignores `ws`/`choch`; extra attributes `seed`, `planned_times`).

Design:
- **Buy&hold is a SPOT hold** (the benchmark an investor actually has): fractional BTC, no leverage, **no funding**. It decides at the close of the first bar in the window and buys at the next open (+slippage, one fee on the notional, the whole equity spent including that fee), then sells everything at the last close (−slippage, one fee, reason `END`). Hence `final / equity0 = (close_last / open_second) · ((1−s)/(1+s)) · ((1−f)/(1+f))`, the raw price multiple minus two fees and two slippages, and its first equity point is `equity0`, so `compute_metrics` sees both fees. `Trade.contracts` is informational (`floor(qty / contract_size)`); the exact holding is `snapshot["qty_btc"]`.
- **EMA cross** uses a 1x-notional futures simulator `_TargetSim` that follows a per-bar target direction: fills at the bar **open** with slippage and fee, funding charged per held hour at the close mark, integer contracts of `contract_size` BTC sized on realised equity, forced exit at the last bar close (`END`). Target = sign(EMA_fast − EMA_slow) of the last **closed** 4h bar, flat until `slow` 4h bars exist.

Hand-computed expectations in the tests (fee 0.1%, slippage 0.1%, funding 50% APR which buy&hold must ignore, equity 10,000, opens 90/100/104/108, closes 100/105/110/120): buy fill `100 · 1.001 = 100.1` at bar 1, `qty = 10000 / (100.1 · 1.001) = 99.8003` BTC, exit fill `120 · 0.999 = 119.88`, final equity `qty · 119.88 · 0.999 = 10000 · 1.2 · (0.999/1.001)² = 11952.10`. EMA test series (one price per 4h bar, fast=2, slow=4): `[100,90,80,70,60,60,100,100,100,60]` gives flat until bar 4 closes (hour 20), short hours 20–27, long from hour 28 to the end.

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_benchmarks.py`:

```python
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from ewbot.backtest.benchmarks import (
    RandomEntryStrategy,
    buy_hold,
    buy_hold_equity,
    ema_cross_equity,
)
from ewbot.backtest.metrics import compute_metrics
from ewbot.config import CostParams, StrategyParams
from ewbot.strategy.base import time_stop_bars
from ewbot.types import Bar

T0 = 1_577_836_800  # 2020-01-01 00:00 UTC (Wednesday), divisible by 14400
H = 3600


def make_df(opens, closes=None, t0=T0) -> pd.DataFrame:
    closes = closes if closes is not None else opens
    rows = [
        {
            "ts": t0 + i * H,
            "open": float(o),
            "high": float(max(o, c)),
            "low": float(min(o, c)),
            "close": float(c),
            "volume": 1.0,
            "synthetic": False,
        }
        for i, (o, c) in enumerate(zip(opens, closes))
    ]
    return pd.DataFrame(rows)


def test_buy_hold_is_spot_two_fees_no_funding():
    # Funding is deliberately huge: a SPOT hold must ignore it.
    costs = CostParams(fee_rate=0.001, slippage=0.001, funding_apr_long=0.5)
    df = make_df(opens=[90, 100, 104, 108], closes=[100, 105, 110, 120])
    eq = buy_hold_equity(df, T0, T0 + 4 * H, costs, 10_000.0)
    assert [p.ts for p in eq] == [T0 + k * H for k in range(4)]
    # decide at bar 0's close, buy at bar 1's open: 100 * 1.001 = 100.1; spend 10,000 incl. the fee
    qty = 10_000.0 / (100.1 * 1.001)  # 99.8003 BTC
    assert eq[0].equity == 10_000.0
    assert eq[1].equity == pytest.approx(qty * 105.0)
    assert eq[2].equity == pytest.approx(qty * 110.0)
    # sell at the last close: 120 * 0.999 = 119.88, minus one fee
    assert eq[3].equity == pytest.approx(qty * 119.88 * 0.999)
    # == raw price multiple 120/100 minus two fees and two slippages
    assert eq[3].equity == pytest.approx(10_000.0 * 1.2 * (0.999 / 1.001) ** 2)
    m = compute_metrics(eq, [], T0, T0 + 4 * H)
    assert m.total_return == pytest.approx(1.2 * (0.999 / 1.001) ** 2 - 1.0)
    _, trades = buy_hold(df, T0, T0 + 4 * H, costs, 10_000.0)
    [t] = trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason, t.direction, t.setup) == (T0 + H, T0 + 3 * H, "END", 1, "BH")
    assert t.entry_price == pytest.approx(100.1) and t.exit_price_avg == pytest.approx(119.88)
    assert t.funding == 0.0
    assert t.fees == pytest.approx(0.001 * qty * 100.1 + 0.001 * qty * 119.88)
    assert t.pnl == pytest.approx(eq[3].equity - 10_000.0)
    assert t.snapshot["qty_btc"] == pytest.approx(qty) and t.contracts == 9980
    no_funding = buy_hold_equity(df, T0, T0 + 4 * H, dataclasses.replace(costs, funding_apr_long=0.0), 10_000.0)
    assert [p.equity for p in no_funding] == [p.equity for p in eq]


def test_buy_hold_single_bar_window_stays_in_cash():
    df = make_df([100, 100, 100])
    eq, trades = buy_hold(df, T0 + 2 * H, T0 + 3 * H, CostParams(), 10_000.0)
    assert [(p.ts, p.equity) for p in eq] == [(T0 + 2 * H, 10_000.0)] and trades == []


def test_buy_hold_empty_window():
    df = make_df([100, 100])
    assert buy_hold_equity(df, T0 + 10 * H, T0 + 20 * H, CostParams(), 10_000.0) == []


def _ema_df():
    # one price per 4h bar: down-trend, then a jump, then a collapse (fast=2, slow=4)
    p4 = [100, 90, 80, 70, 60, 60, 100, 100, 100, 60]
    opens = [p for p in p4 for _ in range(4)]
    return make_df(opens)


def test_ema_cross_tiny_series():
    costs = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
    df = _ema_df()
    end = T0 + 40 * H
    eq, trades = ema_cross_equity(df, T0, end, costs, 10_000.0, fast=2, slow=4)
    assert len(eq) == 40
    # flat until 4h bar index 4 has closed (hour 20); short through hour 27; long from hour 28
    assert eq[19].equity == 10_000.0
    assert [t.direction for t in trades] == [-1, 1]
    short, long_ = trades
    assert short.entry_ts == T0 + 20 * H and short.exit_ts == T0 + 28 * H and short.exit_reason == "FLIP"
    assert long_.entry_ts == T0 + 28 * H and long_.exit_reason == "END"
    n_short = int(10_000 // (60 * 0.01))
    assert short.contracts == n_short
    assert short.pnl == pytest.approx(-n_short * 0.01 * (100 - 60))
    cash = 10_000 + short.pnl
    n_long = int(cash // (100 * 0.01))
    assert long_.contracts == n_long
    assert long_.pnl == pytest.approx(n_long * 0.01 * (60 - 100))
    assert eq[-1].equity == pytest.approx(cash + long_.pnl)
    assert short.setup == "EMA"


def test_random_entry_deterministic_and_geometry(mk_trade):
    sp = StrategyParams()
    refs = [mk_trade(entry_ts=T0, exit_ts=T0 + 8 * H, entry=100.0, stop0=95.0, tp1=105.0, tp2=110.0)]
    span = (T0, T0 + 48 * H)
    a = RandomEntryStrategy(sp, refs, 5, span, seed=7)
    b = RandomEntryStrategy(sp, refs, 5, span, seed=7)
    c = RandomEntryStrategy(sp, refs, 5, span, seed=8)
    assert len(a.planned_times) == 5
    assert a.planned_times == b.planned_times
    assert a.planned_times != c.planned_times
    assert all(T0 <= t < T0 + 48 * H and t % H == 0 for t in a.planned_times)
    assert a.name == "RANDOM"

    dirs = set()
    for ts in a.planned_times:
        bar = Bar(ts, 100.0, 101.0, 99.0, 100.0, 1.0)
        sig = a.on_bar(bar, None, None)
        assert sig is not None and sig.setup == "RANDOM" and sig.ts == ts
        d = sig.direction
        dirs.add(d)
        assert sig.entry_ref == 100.0
        assert sig.stop == pytest.approx(100.0 - d * 5.0)
        assert sig.tp1 == pytest.approx(100.0 + d * 5.0)   # 1R
        assert sig.tp2 == pytest.approx(100.0 + d * 10.0)  # 2R
        assert sig.time_stop_4h == time_stop_bars(2, sp)   # 8h hold = 2 four-hour bars
        assert sig.instance is None and sig.lineage is None
    assert dirs <= {1, -1}
    assert a.on_bar(Bar(T0 + 100 * H, 100, 101, 99, 100, 1.0), None, None) is None


def test_random_entry_skips_blackout_and_handles_no_reference(mk_trade):
    sp = StrategyParams()
    # Fri 2020-01-03: the NY Friday 16:00-18:00 blackout is 21:00-23:00 UTC (winter, UTC-5).
    # A signal at 20:00 UTC would fill in the 21:00 bar, so 20:00, 21:00 and 22:00 are all excluded.
    fri = T0 + 2 * 86400
    r = RandomEntryStrategy(sp, [mk_trade()], 24, (fri, fri + 86400), seed=1)
    assert len(r.planned_times) == 21
    assert not set(r.planned_times) & {fri + 20 * H, fri + 21 * H, fri + 22 * H}
    empty = RandomEntryStrategy(sp, [], 10, (T0, T0 + 48 * H), seed=1)
    assert empty.planned_times == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_benchmarks.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.benchmarks'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/backtest/benchmarks.py`:

```python
"""Benchmarks under the same cost model: buy&hold, EMA 50/200 (4h) long/short, random entry."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Sequence

import numpy as np
import pandas as pd

from ewbot.config import CostParams, StrategyParams
from ewbot.data.candles import aggregate_4h, entry_blackout, slice_ts
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.indicators import ema_series
from ewbot.strategy.base import Signal, time_stop_bars
from ewbot.types import H1, H4, Bar

if TYPE_CHECKING:  # pragma: no cover
    from ewbot.strategy.choch import ChochDetector
    from ewbot.waves.engine import WaveState

_R_PCT_OF_EQUITY = 0.01  # benchmarks have no stop: 1R is defined as 1% of equity at entry


class _TargetSim:
    """1x-notional futures simulator that follows a target direction (+1/-1/0).

    Fills at the bar OPEN with slippage + fee, funding charged per held hour at the bar
    close mark. Integer contracts of `contract_size` BTC, sized on realised equity.
    """

    def __init__(self, costs: CostParams, equity0: float, setup: str) -> None:
        self.c = costs
        self.setup = setup
        self.cash = float(equity0)
        self.dir = 0
        self.n = 0
        self.entry_px = 0.0
        self.entry_ts = 0
        self.entry_fee = 0.0
        self.entry_eq = 0.0
        self.funding = 0.0
        self.trades: list[Trade] = []

    def open(self, ts: int, raw_px: float, direction: int) -> None:
        px = slip(raw_px, direction, self.c)
        n = int(self.cash // (px * self.c.contract_size))
        if n <= 0:
            return
        f = fee(px, n, self.c)
        self.entry_eq = self.cash
        self.cash -= f
        self.dir, self.n, self.entry_px, self.entry_ts = direction, n, px, ts
        self.entry_fee, self.funding = f, 0.0

    def close(self, ts: int, raw_px: float, reason: str) -> None:
        px = slip(raw_px, -self.dir, self.c)
        f = fee(px, self.n, self.c)
        gross = self.dir * self.n * self.c.contract_size * (px - self.entry_px)
        self.cash += gross - f
        pnl = gross - self.entry_fee - f - self.funding
        self.trades.append(
            Trade(
                entry_ts=self.entry_ts,
                exit_ts=ts,
                setup=self.setup,
                direction=self.dir,
                contracts=self.n,
                entry_price=self.entry_px,
                exit_price_avg=px,
                stop0=0.0,
                tp1=0.0,
                tp2=None,
                pnl=pnl,
                fees=self.entry_fee + f,
                funding=self.funding,
                r_multiple=pnl / (_R_PCT_OF_EQUITY * self.entry_eq),
                exit_reason=reason,
                snapshot={},
            )
        )
        self.dir, self.n = 0, 0

    def hold_hour(self, mark: float) -> None:
        if self.dir:
            cost = funding_per_hour(mark, self.n, self.dir, self.c)
            self.cash -= cost
            self.funding += cost

    def mark(self, close: float) -> float:
        if not self.dir:
            return self.cash
        return self.cash + self.dir * self.n * self.c.contract_size * (close - self.entry_px)


def _simulate(
    ts: np.ndarray,
    op: np.ndarray,
    cl: np.ndarray,
    desired: np.ndarray,
    costs: CostParams,
    equity0: float,
    setup: str,
) -> tuple[list[EquityPoint], list[Trade]]:
    sim = _TargetSim(costs, equity0, setup)
    equity: list[EquityPoint] = []
    last = len(ts) - 1
    for i in range(len(ts)):
        t = int(ts[i])
        want = int(desired[i])
        if want != sim.dir:
            if sim.dir != 0:
                sim.close(t, float(op[i]), "FLIP" if want != 0 else "EXIT")
            if want != 0:
                sim.open(t, float(op[i]), want)
        if sim.dir:
            sim.hold_hour(float(cl[i]))
        if i == last and sim.dir:
            sim.close(t + H1, float(cl[i]), "END")
        equity.append(EquityPoint(ts=t, equity=sim.mark(float(cl[i]))))
    return equity, sim.trades


def _window_arrays(df1h: pd.DataFrame, start_ts: int, end_ts: int):
    w = slice_ts(df1h, start_ts, end_ts)
    return (
        w["ts"].to_numpy(dtype=np.int64),
        w["open"].to_numpy(dtype=np.float64),
        w["close"].to_numpy(dtype=np.float64),
    )


def buy_hold(
    df1h: pd.DataFrame, start_ts: int, end_ts: int, costs: CostParams, equity0: float
) -> tuple[list[EquityPoint], list[Trade]]:
    """SPOT buy & hold: fractional BTC, no leverage, NO funding.

    Like every strategy it decides at the close of the first bar in the window and buys at the
    NEXT open (+slippage, one fee on the notional, the whole equity spent including that fee),
    holds, and sells everything at the last close (-slippage, one fee), reason END. So
    final / equity0 = (close_last / open_second) * ((1 - s) / (1 + s)) * ((1 - f) / (1 + f)):
    the raw price multiple minus two fees and two slippages. Equity: one point per 1h bar marked
    at the close (the first point is equity0, the last is after the exit). Returns
    (equity, [the single trade]); no trade when the window has fewer than 2 bars.
    """
    ts, op, cl = _window_arrays(df1h, start_ts, end_ts)
    if ts.size == 0:
        return [], []
    equity = [EquityPoint(ts=int(ts[0]), equity=float(equity0))]
    if ts.size < 2:
        return equity, []
    px_in = slip(float(op[1]), 1, costs)
    qty = equity0 / (px_in * (1.0 + costs.fee_rate))  # BTC; notional + entry fee == equity0
    fee_in = costs.fee_rate * qty * px_in
    for i in range(1, ts.size - 1):
        equity.append(EquityPoint(ts=int(ts[i]), equity=qty * float(cl[i])))
    px_out = slip(float(cl[-1]), -1, costs)
    fee_out = costs.fee_rate * qty * px_out
    final = qty * px_out - fee_out
    equity.append(EquityPoint(ts=int(ts[-1]), equity=final))
    pnl = final - equity0
    trade = Trade(
        entry_ts=int(ts[1]),
        exit_ts=int(ts[-1]),
        setup="BH",
        direction=1,
        contracts=int(qty / costs.contract_size),  # informational; the holding is qty_btc
        entry_price=px_in,
        exit_price_avg=px_out,
        stop0=0.0,
        tp1=0.0,
        tp2=None,
        pnl=pnl,
        fees=fee_in + fee_out,
        funding=0.0,
        r_multiple=pnl / (_R_PCT_OF_EQUITY * equity0),
        exit_reason="END",
        snapshot={"spot": True, "qty_btc": qty},
    )
    return equity, [trade]


def buy_hold_equity(
    df1h: pd.DataFrame, start_ts: int, end_ts: int, costs: CostParams, equity0: float
) -> list[EquityPoint]:
    return buy_hold(df1h, start_ts, end_ts, costs, equity0)[0]


def ema_cross_equity(
    df1h: pd.DataFrame,
    start_ts: int,
    end_ts: int,
    costs: CostParams,
    equity0: float,
    fast: int = 50,
    slow: int = 200,
) -> tuple[list[EquityPoint], list[Trade]]:
    """Always-in-the-market 4h EMA(fast)/EMA(slow) cross, long when fast>slow else short.

    Direction is read from the last CLOSED 4h bar and executed at the next 1h open. EMAs are
    computed on all history in `df1h` (warm-up), flat until `slow` 4h bars exist.
    """
    ts, op, cl = _window_arrays(df1h, start_ts, end_ts)
    if ts.size == 0:
        return [], []
    d4 = aggregate_4h(df1h)
    if len(d4) == 0:
        return _simulate(ts, op, cl, np.zeros(ts.size, dtype=np.int64), costs, equity0, "EMA")
    close4 = d4["close"].to_numpy(dtype=np.float64)
    ts4 = d4["ts"].to_numpy(dtype=np.int64)
    ef, es = ema_series(close4, fast), ema_series(close4, slow)
    dirs = np.where(ef > es, 1, -1).astype(np.int64)
    dirs[:slow] = 0
    closed = np.searchsorted(ts4 + H4, ts, side="right") - 1  # last 4h bar closed at/before bar open
    desired = np.where(closed >= 0, dirs[np.clip(closed, 0, None)], 0)
    return _simulate(ts, op, cl, desired, costs, equity0, "EMA")


@dataclass(frozen=True, slots=True)
class _RefShape:
    stop_frac: float      # stop distance / entry price
    tp1_r: float          # |tp1 - entry| / stop distance
    tp2_r: float | None
    leg_4h: int           # holding time in 4h bars -> time stop via time_stop_bars


def _reference_shape(t: Trade) -> _RefShape | None:
    dist = abs(t.entry_price - t.stop0)
    if t.entry_price <= 0 or dist <= 0:
        return None
    return _RefShape(
        stop_frac=dist / t.entry_price,
        tp1_r=abs(t.tp1 - t.entry_price) / dist,
        tp2_r=None if t.tp2 is None else abs(t.tp2 - t.entry_price) / dist,
        leg_4h=max(1, int(round((t.exit_ts - t.entry_ts) / H4))),
    )


@lru_cache(maxsize=8)
def _eligible_hours(start_ts: int, end_ts: int) -> np.ndarray:
    first = (start_ts + H1 - 1) // H1 * H1
    return np.array(
        [t for t in range(first, end_ts, H1) if not entry_blackout(t) and not entry_blackout(t + H1)],
        dtype=np.int64,
    )


class RandomEntryStrategy:
    """Spec 5.7 control: `n_signals` entries at uniformly random eligible 1h bars in `span`,
    direction 50/50, stop distance (as a fraction of price), TP multiples (in R) and time stop
    drawn from the reference trades. Ignores the wave state and CHoCH. Plan is fixed at
    construction from `seed`; the engine decides whether a signal can be used (flat, breakers).
    """

    name = "RANDOM"

    def __init__(
        self,
        sp: StrategyParams,
        reference_trades: Sequence[Trade],
        n_signals: int,
        span: tuple[int, int],
        seed: int,
    ) -> None:
        self.sp = sp
        self.seed = seed
        refs = [r for r in (_reference_shape(t) for t in reference_trades) if r is not None]
        eligible = _eligible_hours(int(span[0]), int(span[1]))
        k = min(int(n_signals), int(eligible.size)) if refs else 0
        plan: dict[int, tuple[int, _RefShape]] = {}
        if k > 0:
            rng = np.random.default_rng(seed)
            times = np.sort(rng.choice(eligible, size=k, replace=False))
            dirs = rng.choice(np.array([1, -1]), size=k)
            picks = rng.integers(0, len(refs), size=k)
            for t, d, p in zip(times, dirs, picks):
                plan[int(t)] = (int(d), refs[int(p)])
        self._plan = plan

    @property
    def planned_times(self) -> tuple[int, ...]:
        return tuple(sorted(self._plan))

    def on_bar(self, bar: Bar, ws: "WaveState", choch: "ChochDetector") -> Signal | None:
        item = self._plan.get(bar.ts)
        if item is None:
            return None
        d, ref = item
        dist = ref.stop_frac * bar.close
        return Signal(
            ts=bar.ts,
            setup="RANDOM",
            direction=d,
            entry_ref=bar.close,
            stop=bar.close - d * dist,
            tp1=bar.close + d * ref.tp1_r * dist,
            tp2=None if ref.tp2_r is None else bar.close + d * ref.tp2_r * dist,
            risk_mult=1.0,
            time_stop_4h=time_stop_bars(ref.leg_4h, self.sp),
            instance=None,
            lineage=None,
            reason="random entry",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_benchmarks.py -v`
Expected: 6 passed. (If `test_buy_hold_is_spot_two_fees_no_funding` is off by a few cents, Part C1's `slip` deviates from the contract semantics above; fix `costs.py`, not this test.)

- [ ] **Step 5: Commit**
```bash
git add src/ewbot/backtest/benchmarks.py tests/backtest/test_benchmarks.py
git commit -m "feat(backtest): buy&hold, EMA 50/200 and random-entry benchmarks"
```

---

### Task 20: Backtest runner and strategy comparison

**Files:**
- Create: `src/ewbot/backtest/runner.py`
- Test: `tests/backtest/test_runner.py`, `tests/backtest/test_integration_synth.py` (real stack, no stubs)

**Interfaces:**
- Consumes: `ewbot.engine.loop.TradingEngine, EngineResult, EquityPoint, Trade`; `ewbot.strategy.hybrid.make_strategy`; `ewbot.config.with_overrides`; T18 `compute_metrics, bootstrap_ci, trade_stats`; T19 `buy_hold, ema_cross_equity, RandomEntryStrategy`. Integration test only: `tests.synth.random_bars` (T10), `ewbot.data.candles.fill_gaps`, `ewbot.config.SETUPS`.
- Produces: `run_backtest(cfg, df1h, start_ts=None, end_ts=None) -> EngineResult`; `compare(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1) -> dict`; `breakdowns(res, start_ts, end_ts)` (per setup / long-short / per calendar year); `utc_ts("YYYY-MM-DD")`; `equity_pairs(equity)`.

`compare()` return shape (consumed by the report and the holdout):
`{"period": {"start","end"}, "comparison": {A,C,HYBRID,BH,EMA -> Metrics as dict}, "random": {"runs","n_signals","dist": {metric -> mean/p05/p50/p95}, "beaten_by": {A|C|HYBRID -> {sharpe, calmar, total_return -> share of random runs beaten}}}, "equity": {name -> [(ts, equity)]}, "ci": {name -> {"avg_r": (lo, hi), "win_rate": (lo, hi)}}, "breakdown": {A|C|HYBRID -> {"per_setup","long_short","per_year"}}, "trades": {name -> [Trade]}}`.

A, C and HYBRID use the **same cfg** except `strategy.mode`. Parallelism uses a `multiprocessing.Pool` whose initializer stores `(cfg, df, start, end)` once per worker (spawn-safe on macOS); `workers <= 1` runs in-process. The unit test swaps `TradingEngine` / `make_strategy` for fakes, so it needs no wave engine. The second test file, `test_integration_synth.py`, is the cross-part integration check: two years of synthetic 1h bars from `tests/synth.random_bars` (T10) go through the real candles → wave engine → CHoCH → strategies → sizing/breakers → PaperBroker → metrics/benchmarks stack for A, C, HYBRID and `compare(random_runs=3)`, plus an end-to-end price-mirror check (about 7 s).

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_runner.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest import runner
from ewbot.backtest.benchmarks import RandomEntryStrategy
from ewbot.backtest.runner import breakdowns, compare, utc_ts
from ewbot.config import BotConfig
from ewbot.engine.loop import EngineResult, EquityPoint

T0 = utc_ts("2020-01-01")
H = 3600
N = 1000


def _df() -> pd.DataFrame:
    px = [100.0 + 10.0 * ((i // 50) % 4) - 0.01 * i for i in range(N)]  # saw-tooth, positive
    return pd.DataFrame(
        {
            "ts": [T0 + i * H for i in range(N)],
            "open": px,
            "high": [p + 1 for p in px],
            "low": [p - 1 for p in px],
            "close": px,
            "volume": [1.0] * N,
            "synthetic": [False] * N,
        }
    )


class FakeEngine:
    """Replaces TradingEngine: returns canned results per strategy name, records RANDOM plans."""

    random_strategies: list = []
    trade_factory = None

    def __init__(self, cfg, strategy):
        self.strategy = strategy

    def run(self, df1h, start_ts=None, end_ts=None):
        name = self.strategy.name
        mk = FakeEngine.trade_factory
        if name == "A":
            trades = (
                mk(entry_ts=T0 + 100 * H, exit_ts=T0 + 110 * H, pnl=300.0, r=2.0, setup="W3"),
                mk(entry_ts=T0 + 200 * H, exit_ts=T0 + 210 * H, pnl=-100.0, r=-1.0, setup="W5", direction=-1),
                mk(entry_ts=T0 + 300 * H, exit_ts=T0 + 310 * H, pnl=200.0, r=1.5, setup="W3"),
            )
            vals = [10_000 + 2 * i for i in range(N)]
        elif name == "RANDOM":
            FakeEngine.random_strategies.append(self.strategy)
            trades = ()
            k = self.strategy.seed + 1
            vals = [10_000 + k * (-1) ** (i // 24) * (i % 24) for i in range(N)]
        else:
            trades = ()
            vals = [10_000 + i for i in range(N)]
        eq = tuple(EquityPoint(ts=T0 + i * H, equity=float(v)) for i, v in enumerate(vals))
        return EngineResult(trades=trades, equity=eq, signals=(), events=())


@pytest.fixture
def patched(monkeypatch, mk_trade):
    from types import SimpleNamespace

    FakeEngine.random_strategies = []
    FakeEngine.trade_factory = staticmethod(mk_trade)
    monkeypatch.setattr(runner, "TradingEngine", FakeEngine)
    monkeypatch.setattr(runner, "make_strategy", lambda sp: SimpleNamespace(name=sp.mode))
    return FakeEngine


def test_utc_ts():
    assert utc_ts("2017-01-01") == 1_483_228_800
    assert utc_ts("2024-01-01") == 1_704_067_200


def test_run_backtest_uses_mode_strategy(patched):
    from ewbot.config import with_overrides

    cfg = with_overrides(BotConfig(), **{"strategy.mode": "C"})
    res = runner.run_backtest(cfg, _df(), T0, T0 + N * H)
    assert len(res.equity) == N and res.trades == ()


def test_compare_structure_and_random(patched):
    df = _df()
    cmp = compare(BotConfig(), df, T0, T0 + N * H, random_runs=3, workers=1)
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    assert set(cmp["equity"]) == set(cmp["comparison"])
    a = cmp["comparison"]["A"]
    assert a["n_trades"] == 3 and a["win_rate"] == pytest.approx(2 / 3)
    assert cmp["comparison"]["BH"]["exposure"] == 1.0
    assert cmp["comparison"]["BH"]["n_trades"] == 1
    assert cmp["ci"]["A"]["avg_r"][0] <= (2.0 - 1.0 + 1.5) / 3 <= cmp["ci"]["A"]["avg_r"][1]
    assert len(cmp["equity"]["BH"]) == N

    rnd = cmp["random"]
    assert rnd["runs"] == 3 and rnd["n_signals"] == 3
    d = rnd["dist"]["sharpe"]
    assert d["p05"] <= d["p50"] <= d["p95"]
    assert set(rnd["beaten_by"]) == {"A", "C", "HYBRID"}
    assert all(0.0 <= v <= 1.0 for v in rnd["beaten_by"]["A"].values())
    # RANDOM strategies: seeds cfg.seed + k, same count as A's trades, 3 planned entries each
    seeds = sorted(s.seed for s in patched.random_strategies)
    assert seeds == [0, 1, 2]
    assert all(isinstance(s, RandomEntryStrategy) and len(s.planned_times) == 3 for s in patched.random_strategies)

    bd = cmp["breakdown"]["A"]
    assert set(bd["per_setup"]) == {"W3", "W5"} and bd["per_setup"]["W3"]["n"] == 2
    assert bd["long_short"]["long"]["n"] == 2 and bd["long_short"]["short"]["n"] == 1
    assert list(bd["per_year"]) == ["2020"]


def test_compare_without_random_runs(patched):
    cmp = compare(BotConfig(), _df(), T0, T0 + N * H, random_runs=0)
    assert cmp["random"]["runs"] == 0 and cmp["random"]["dist"] == {}


def test_breakdowns_per_year_boundaries(mk_trade):
    t1 = mk_trade(entry_ts=utc_ts("2020-12-31"), exit_ts=utc_ts("2020-12-31") + H, pnl=1.0)
    t2 = mk_trade(entry_ts=utc_ts("2021-01-02"), exit_ts=utc_ts("2021-01-02") + H, pnl=-1.0)
    eq = tuple(
        EquityPoint(ts=utc_ts("2020-12-30") + i * 86400, equity=10_000.0 + i) for i in range(6)
    )
    res = EngineResult(trades=(t1, t2), equity=eq, signals=(), events=())
    bd = breakdowns(res, utc_ts("2020-12-30"), utc_ts("2021-01-05"))
    assert list(bd["per_year"]) == ["2020", "2021"]
    assert bd["per_year"]["2020"]["n_trades"] == 1 and bd["per_year"]["2021"]["n_trades"] == 1
    assert bd["per_year"]["2021"]["total_return"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_runner.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.runner'`.

- [ ] **Step 3: Write minimal implementation**

`src/ewbot/backtest/runner.py`:

```python
"""Backtest runner and the A / C / HYBRID / BH / EMA / RANDOM comparison."""

from __future__ import annotations

import dataclasses
import multiprocessing as mp
from datetime import UTC, datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.benchmarks import RandomEntryStrategy, buy_hold, ema_cross_equity
from ewbot.backtest.metrics import Metrics, bootstrap_ci, compute_metrics, trade_stats
from ewbot.config import BotConfig, with_overrides
from ewbot.engine.loop import EngineResult, EquityPoint, TradingEngine, Trade
from ewbot.strategy.hybrid import make_strategy

MODES = ("A", "C", "HYBRID")
_RANDOM_METRICS = ("total_return", "max_dd", "sharpe", "calmar", "avg_r", "n_trades")


def utc_ts(date: str) -> int:
    """'YYYY-MM-DD' (UTC midnight) -> epoch seconds."""
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def equity_pairs(equity: Sequence[EquityPoint]) -> list[tuple[int, float]]:
    return [(p.ts, p.equity) for p in equity]


def run_backtest(
    cfg: BotConfig, df1h: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None
) -> EngineResult:
    """One TradingEngine run with the strategy selected by cfg.strategy.mode."""
    engine = TradingEngine(cfg, make_strategy(cfg.strategy))
    return engine.run(df1h, start_ts, end_ts)


# ----------------------------------------------------------------------------- breakdowns


def breakdowns(res: EngineResult, start_ts: int, end_ts: int) -> dict[str, Any]:
    """Per-setup, long/short and per-calendar-year breakdowns of one engine result."""
    trades = [t for t in res.trades if start_ts <= t.entry_ts < end_ts]
    per_setup = {
        s: trade_stats([t for t in trades if t.setup == s]) for s in sorted({t.setup for t in trades})
    }
    long_short = {
        "long": trade_stats([t for t in trades if t.direction > 0]),
        "short": trade_stats([t for t in trades if t.direction < 0]),
    }
    per_year: dict[str, dict[str, float | int]] = {}
    y0 = datetime.fromtimestamp(start_ts, UTC).year
    y1 = datetime.fromtimestamp(end_ts - 1, UTC).year
    for y in range(y0, y1 + 1):
        lo = max(start_ts, utc_ts(f"{y}-01-01"))
        hi = min(end_ts, utc_ts(f"{y + 1}-01-01"))
        per_year[str(y)] = dataclasses.asdict(compute_metrics(res.equity, res.trades, lo, hi))
    return {"per_setup": per_setup, "long_short": long_short, "per_year": per_year}


# ----------------------------------------------------------------------------- task execution

# Per-process state set by the pool initializer (spawn-safe; each worker gets its own copy).
_STATE: dict[str, Any] = {}


def _init_worker(cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int) -> None:
    _STATE.update(cfg=cfg, df=df1h, start=start_ts, end=end_ts)


def _task(spec: tuple) -> EngineResult:
    cfg: BotConfig = _STATE["cfg"]
    df, start, end = _STATE["df"], _STATE["start"], _STATE["end"]
    if spec[0] == "mode":
        return run_backtest(with_overrides(cfg, **{"strategy.mode": spec[1]}), df, start, end)
    _, seed, refs, n_signals = spec
    strat = RandomEntryStrategy(cfg.strategy, refs, n_signals, (start, end), seed)
    res = TradingEngine(cfg, strat).run(df, start, end)
    return dataclasses.replace(res, signals=())  # keep the pickle back to the parent small


def _map_tasks(
    specs: list[tuple], workers: int, cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int
) -> list[EngineResult]:
    initargs = (cfg, df1h, start_ts, end_ts)
    if workers <= 1 or len(specs) <= 1:
        _init_worker(*initargs)
        return [_task(s) for s in specs]
    with mp.Pool(min(workers, len(specs)), initializer=_init_worker, initargs=initargs) as pool:
        return pool.map(_task, specs, chunksize=1)


# ----------------------------------------------------------------------------- compare


def _dist(values: Sequence[float]) -> dict[str, float]:
    a = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(a.mean()),
        "p05": float(np.percentile(a, 5)),
        "p50": float(np.percentile(a, 50)),
        "p95": float(np.percentile(a, 95)),
    }


def compare(
    cfg: BotConfig,
    df1h: pd.DataFrame,
    start_ts: int,
    end_ts: int,
    random_runs: int = 100,
    workers: int = 1,
) -> dict[str, Any]:
    """Run A, C, HYBRID (same cfg, only strategy.mode differs), buy&hold, EMA 50/200 and
    `random_runs` RandomEntryStrategy runs over [start_ts, end_ts) with identical costs.

    Returns {"period", "comparison": {name: Metrics dict}, "random": {...}, "equity": {name:
    [(ts, equity)]}, "ci": {name: {...}}, "breakdown": {name: {...}}, "trades": {name: [Trade]}}.
    """
    results = dict(zip(MODES, _map_tasks([("mode", m) for m in MODES], workers, cfg, df1h, start_ts, end_ts)))

    equity: dict[str, list[tuple[int, float]]] = {}
    trades: dict[str, list[Trade]] = {}
    comparison: dict[str, dict[str, float | int]] = {}
    ci: dict[str, dict[str, tuple[float, float]]] = {}
    breakdown: dict[str, dict[str, Any]] = {}
    metrics: dict[str, Metrics] = {}

    def add(name: str, eq: Sequence[EquityPoint], trs: Sequence[Trade], res: EngineResult | None) -> None:
        m = compute_metrics(eq, trs, start_ts, end_ts)
        metrics[name] = m
        comparison[name] = dataclasses.asdict(m)
        equity[name] = equity_pairs(eq)
        trades[name] = list(trs)
        win = [t for t in trs if start_ts <= t.entry_ts < end_ts]
        ci[name] = {
            "avg_r": bootstrap_ci(win, "avg_r", seed=cfg.seed),
            "win_rate": bootstrap_ci(win, "win_rate", seed=cfg.seed),
        }
        if res is not None:
            breakdown[name] = breakdowns(res, start_ts, end_ts)

    for mode in MODES:
        add(mode, results[mode].equity, results[mode].trades, results[mode])

    bh_eq, bh_trades = buy_hold(df1h, start_ts, end_ts, cfg.costs, cfg.initial_equity)
    add("BH", bh_eq, bh_trades, None)
    comparison["BH"]["exposure"] = 1.0  # always invested; mark-to-market trade is one END exit
    ema_eq, ema_trades = ema_cross_equity(df1h, start_ts, end_ts, cfg.costs, cfg.initial_equity)
    add("EMA", ema_eq, ema_trades, None)

    # Random control: same number of entries as A actually made, geometry from A's trades.
    a_trades = [t for t in results["A"].trades if start_ts <= t.entry_ts < end_ts]
    n_signals = len(a_trades)
    rand_metrics: list[Metrics] = []
    if random_runs > 0 and n_signals > 0:
        specs = [("random", cfg.seed + k, a_trades, n_signals) for k in range(random_runs)]
        for r in _map_tasks(specs, workers, cfg, df1h, start_ts, end_ts):
            rand_metrics.append(compute_metrics(r.equity, r.trades, start_ts, end_ts))
    random: dict[str, Any] = {"runs": len(rand_metrics), "n_signals": n_signals, "dist": {}, "beaten_by": {}}
    if rand_metrics:
        for key in _RANDOM_METRICS:
            random["dist"][key] = _dist([getattr(m, key) for m in rand_metrics])
        for name in MODES:
            random["beaten_by"][name] = {
                key: float(np.mean([getattr(m, key) < getattr(metrics[name], key) for m in rand_metrics]))
                for key in ("sharpe", "calmar", "total_return")
            }

    return {
        "period": {"start": start_ts, "end": end_ts},
        "comparison": comparison,
        "random": random,
        "equity": equity,
        "ci": ci,
        "breakdown": breakdown,
        "trades": trades,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_runner.py -v`
Expected: 5 passed.

- [ ] **Step 5: Write the end-to-end integration test on synthetic data**

It checks what only the assembled parts can show: every mode runs on the real engine, the trades' P&L sums to the equity change, positions never overlap, the Elliott gates are visible in the decision snapshots, `compare()` reproduces the single runs exactly, buy&hold is the spot price multiple minus two fees and two slippages, and a price mirror flips every trade's direction through the whole stack (spec 10 symmetry, end to end).

`tests/backtest/test_integration_synth.py`:

```python
"""End-to-end integration on synthetic data: the REAL stack, no stubs.

candles (fill_gaps, wick clip) -> WaveEngine -> ChochDetector -> strategies A / C / HYBRID ->
sizing + breakers -> PaperBroker -> Trade / EquityPoint -> metrics, benchmarks, random control.
Two years of 1h bars from tests/synth.py (seed 0 stays between ~7.4k and ~24.8k).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ewbot.backtest.runner import compare, run_backtest
from ewbot.config import SETUPS, BotConfig, BreakerParams, CostParams, with_overrides
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.synth import random_bars

N_BARS = 2 * 8760  # two years of 1h bars
WARMUP_H = 40 * 24  # > 200 4h bars of wave-engine warm-up before entries are allowed
MODES = ("A", "C", "HYBRID")
SETUPS_OF = {"A": set(SETUPS), "C": {"FIB"}, "HYBRID": set(SETUPS) | {"FIB"}}
EXIT_REASONS = {"STOP", "TP1", "TP2", "TIME", "LINEAGE", "FLIP", "BREAKER", "END"}


def synth_frame() -> pd.DataFrame:
    bars = random_bars(seed=0, n=N_BARS, vol=30.0, start=20_000.0)
    df = pd.DataFrame(
        {
            "ts": np.array([b.ts for b in bars], dtype=np.int64),
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    return df.drop(index=[3_000, 3_001, 9_000]).reset_index(drop=True)  # 3 missing hours


@pytest.fixture(scope="module")
def synth():
    df = fill_gaps(synth_frame())  # what the CLI does before every run
    start = int(df["ts"].iloc[0]) + WARMUP_H * H1
    end = int(df["ts"].iloc[-1]) + H1
    cfg = BotConfig()
    runs = {m: run_backtest(with_overrides(cfg, **{"strategy.mode": m}), df, start, end) for m in MODES}
    return df, start, end, cfg, runs


@pytest.fixture(scope="module")
def cmp(synth):
    df, start, end, cfg, _ = synth
    return compare(cfg, df, start, end, random_runs=3, workers=1)


def test_synthetic_frame_is_a_valid_positive_price_path(synth):
    df = synth[0]
    assert len(df) == N_BARS and int(df["synthetic"].sum()) == 3
    assert float(df["low"].min()) > 1_000.0


@pytest.mark.parametrize("mode", MODES)
def test_mode_runs_end_to_end_and_books_every_dollar(synth, mode):
    df, start, end, cfg, runs = synth
    res = runs[mode]
    assert [p.ts for p in res.equity] == list(range(start, end, H1))
    eq = np.array([p.equity for p in res.equity])
    assert np.isfinite(eq).all() and (eq > 0).all()
    # trades sum to the equity change (fees and funding are inside Trade.pnl)
    assert sum(t.pnl for t in res.trades) == pytest.approx(eq[-1] - cfg.initial_equity, abs=1e-6)
    assert res.events[0][0] == int(df["ts"].iloc[0]) and res.events[0][1].startswith("WICKS_CLIPPED:")
    assert len(res.signals) >= len(res.trades)
    assert all(s.ts >= start for s in res.signals)
    prev_exit = start
    for t in res.trades:
        assert start < t.entry_ts <= t.exit_ts < end
        assert t.entry_ts >= prev_exit  # one position at a time (a flip exits and enters on the same open)
        prev_exit = t.exit_ts
        assert t.setup in SETUPS_OF[mode] and t.exit_reason in EXIT_REASONS
        assert t.contracts >= 1 and math.isfinite(t.r_multiple)
        assert t.snapshot["setup"] == t.setup
        if t.setup in SETUPS:  # Elliott gates (spec 5.3) as recorded at signal time
            assert t.snapshot["counts"]
            assert t.snapshot["q_top"] >= cfg.strategy.Q_MIN and t.snapshot["s_rev"] >= cfg.strategy.S_MIN


def test_elliott_strategy_actually_trades(synth):
    runs = synth[4]
    assert len(runs["A"].trades) >= 3  # seed 0: about ten; 0 would mean a wiring/gate problem


def test_compare_on_the_real_stack(synth, cmp):
    df, start, end, cfg, runs = synth
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    for name, m in cmp["comparison"].items():
        assert all(math.isfinite(float(v)) for v in m.values()), name
        assert 0.0 <= m["max_dd"] <= 1.0
    for mode in MODES:  # compare() drives the same engine: identical trades and equity
        assert cmp["trades"][mode] == list(runs[mode].trades)
        assert cmp["equity"][mode] == [(p.ts, p.equity) for p in runs[mode].equity]
    assert cmp["random"]["runs"] == 3 and cmp["random"]["n_signals"] == len(runs["A"].trades)
    assert set(cmp["random"]["beaten_by"]) == set(MODES)
    # spot buy&hold: raw price multiple minus two fees and two slippages, no funding
    c = cfg.costs
    w = df[(df["ts"] >= start) & (df["ts"] < end)]
    multiple = float(w["close"].iloc[-1]) / float(w["open"].iloc[1])
    expect = multiple * ((1 - c.slippage) / (1 + c.slippage)) * ((1 - c.fee_rate) / (1 + c.fee_rate)) - 1
    assert cmp["comparison"]["BH"]["total_return"] == pytest.approx(expect, rel=1e-9)
    [bh] = cmp["trades"]["BH"]
    assert bh.funding == 0.0 and bh.exit_reason == "END"
    assert cmp["comparison"]["EMA"]["n_trades"] >= 1


@pytest.mark.parametrize("mode", MODES)
def test_price_mirror_flips_every_trade_through_the_whole_stack(synth, mode):
    """Spec 10 symmetry end to end: p' = 2 * p_ref - p gives the same trades at the same times with
    the opposite direction. Costs are zeroed (funding is the one asymmetric cost) and breakers are off
    (they act on % equity, which differs once the leverage cap binds at different price levels)."""
    df, start, end, _, _ = synth
    p_ref = 20_000.0  # on the 1/64 price grid of tests/synth, so mirrored prices are exact
    mirrored = df.assign(
        open=2 * p_ref - df["open"],
        high=2 * p_ref - df["low"],
        low=2 * p_ref - df["high"],
        close=2 * p_ref - df["close"],
    )
    cfg = with_overrides(
        BotConfig(
            costs=CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0),
            breakers=BreakerParams(daily_loss=1.0, total_dd=1.0, max_consec_losses=10**6),
        ),
        **{"strategy.mode": mode},
    )
    a = run_backtest(cfg, df, start, end)
    b = run_backtest(cfg, mirrored, start, end)

    def key(t, sign):
        return (t.entry_ts, t.exit_ts, t.setup, sign * t.direction, t.exit_reason)

    assert [key(t, 1) for t in a.trades] == [key(t, -1) for t in b.trades]
    assert [(s.ts, s.setup, s.direction) for s in a.signals] == [(s.ts, s.setup, -s.direction) for s in b.signals]
    if mode == "A":
        assert a.trades
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_integration_synth.py -v`
Expected: 9 passed (about 7 s). A failure here is a wiring bug between parts (gates, sign conventions, field semantics, accounting), not a test problem.

- [ ] **Step 7: Commit**
```bash
git add src/ewbot/backtest/runner.py tests/backtest/test_runner.py tests/backtest/test_integration_synth.py
git commit -m "feat(backtest): runner, A/C/HYBRID/BH/EMA/random comparison with breakdowns, synthetic end-to-end test"
```

---

### Task 21: Walk-forward, plateau selection and one-shot holdout

**Files:**
- Create: `src/ewbot/backtest/walkforward.py`, `src/ewbot/backtest/holdout.py`
- Test: `tests/backtest/test_walkforward.py`, `tests/backtest/test_holdout.py`

**Interfaces:**
- Consumes: T18 `compute_metrics`; T20 `run_backtest, compare, utc_ts`; `ewbot.config.with_overrides, CONSERVATIVE` and T1 `BreakerParams.enabled`; T16 `CircuitBreakers` (a no-op when disabled); `ewbot.engine.loop.EngineResult, EquityPoint`.
- Produces (walkforward): `GRID` (60 combos, key order `wave.K4, strategy.Q_MIN, strategy.S_MIN`), `WF_START = "2017-01-01"`, `WF_END = "2024-01-01"` (the single source of the walk-forward / holdout boundary), `MIN_TRAIN_TRADES = 12`, `MIN_ELIGIBLE_NEIGHBOUR_FRAC = 0.5`, `grid_combos()`, `config_for(cfg, combo)`, `run_grid(cfg, df1h, start_ts, end_ts, workers) -> {combo: EngineResult}` (drops bars at/after `end_ts`, always runs with `breakers.enabled = False`), `folds(...)`, `pick_plateau(calmar, n_trades, min_trades=MIN_TRAIN_TRADES)`, `select_plateau(results, train_start, train_end, min_trades=MIN_TRAIN_TRADES)`, `oos_returns`, `evaluate_folds(results, fold_list, equity0, min_trades=MIN_TRAIN_TRADES)` (returns `chosen_final`, `chosen_final_from_fold`, `chosen_final_fallback`), `walk_forward(cfg, df1h, workers) -> dict` (drops bars at/after `WF_END` first).
- Produces (holdout): `HOLDOUT_START = utc_ts(WF_END)`, `GO_LIVE_MIN_TRADES = 50`, `PROJECT_ROOT`, `DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout.lock"`, `LOCK_VERSION = 2`, `HoldoutError`, `params_digest` (informational), `holdout_config(cfg, params)`, `lock_digest(cfg, params, random_runs=100)`, `trade_count_lines(comp, required=GO_LIVE_MIN_TRADES) -> list[str]`, `evaluate_criteria` (also returns `n_trades` per candidate, the `trade_count` lines and `random_control_evaluated`), `run_holdout(cfg, df1h, params, lock_path=None, confirm=False, *, random_runs=100, workers=1) -> {params, sha256, lock_path, window, compare, criteria}`.

Rules implemented:
- **Folds** (24-month train, 6-month test, rolling 6 months, first test 2019-01-01, last test end `WF_END` = 2024-01-01) give exactly 10 folds; boundaries are listed literally in `test_folds_exact_boundaries` (fold 0: train `2017-01-01 .. 2019-01-01`, test `2019-01-01 .. 2019-07-01`; fold 9: train `2021-07-01 .. 2023-07-01`, test `2023-07-01 .. 2024-01-01`).
- **Plateau**: score of a combo = mean Calmar over every combo whose grid index differs by at most 1 on each axis (itself included), counting only eligible combos (`>= min_trades` trades in the training window, finite Calmar). A combo is **selectable** only if it is eligible and at least half (`MIN_ELIGIBLE_NEIGHBOUR_FRAC`) of its in-grid neighbours, itself excluded, are eligible too (a corner of the 4x5x3 grid has 7 neighbours and needs 4; an interior cell has 26 and needs 13), so an eligible cell surrounded by ineligible ones cannot win a "plateau" made of itself. Highest score wins; ties (compared at 12 significant digits) go to the higher own Calmar, then the smallest grid index; no selectable combo raises `ValueError`. The test grid is 4x5x3 with a plateau block of Calmar 2.0, an isolated corner spike of 5.0 (neighbourhood mean `14/8 = 1.75`) and everything else 1.0: naive argmax picks the spike, the plateau rule picks `(3.0, 0.6, 0.0)`; with the spike's 7 neighbours ineligible the pick is still `(3.0, 0.6, 0.0)`.
- **Stitching**: for each fold the chosen combo's single-run equity is sliced to the test window, hourly returns are compounded onto a running equity that starts at `initial_equity`; a fold with no selectable combo stays flat (`chosen = None`). `chosen_final` is the pick of the last fold that picked anything; `chosen_final_from_fold` names that fold and `chosen_final_fallback` is `True` when it is not the last fold (the most recent training window had no selectable combo), so the CLI can refuse to pass it on silently (T22).
- **2024 cut (defence in depth)**: `HOLDOUT_START` is derived from `WF_END`. `walk_forward` drops every bar at/after `WF_END` before anything runs and `run_grid` drops bars at/after its `end_ts` (= `WF_END` when called from `walk_forward`); the engine also cuts at `end_ts` itself. `test_walk_forward_never_sees_bars_at_or_after_wf_end` feeds a frame whose post-2024 bars are poisoned (x10 and NaN) to a deliberately leaky fake engine and requires the result to equal the walk-forward of the clean truncated frame.
- **Holdout validation** (before anything is written, `HoldoutError`): `confirm=True`; params must be a `GRID` combo; `breakers.enabled` must be `True`; the risk profile must equal `CONSERVATIVE` (name and values); `random_runs >= 1`; `df1h` must contain bars at/after `HOLDOUT_START`.
- **Holdout lock**: default path `DEFAULT_LOCK_PATH` = `<project root>/holdout.lock`, resolved from the package location (`Path(__file__).resolve().parents[3]`, src layout, editable install), never the CWD; if no `pyproject.toml` is found there, `lock_path` must be passed. The lock is bound to `lock_digest(cfg, params, random_runs)` = sha256 of the sorted JSON of every field of `holdout_config(cfg, params)` (the fully resolved `BotConfig` of the run; `strategy.mode` is pinned to `"A"` because `compare()` runs A, C and HYBRID itself), the grid values, `HOLDOUT_START` and `random_runs`. It is JSON `{version, keys, params, sha256, params_sha256, config, grid, holdout_start, random_runs, first_run, runs}`, written atomically **before** the run (a crash cannot be used to retune) and again after it. `first_run` records the first completed run (`start_ts`, `end_ts` = last bar + 1h, `last_bar_ts`, headline metrics of A / HYBRID / C / BH / EMA, the RANDOM summary, criteria and `n_trades`) and is never overwritten; `runs` lists every attempt. Re-running the identical configuration is allowed (e.g. later, with more data); any other difference (params, any config field, `random_runs`) raises `HoldoutError` naming the changed keys, without touching the lock. An unreadable lock is refused.
- **Random control mandatory**: if the random distribution is empty (`random_runs` produced nothing, or A made no trades to replay) no candidate wins, `random_control_evaluated` is `False` and `automatic_criteria_met` is `False`.
- **Training floor**: `MIN_TRAIN_TRADES = 12` trades per 24-month training window is the default of `pick_plateau`, `select_plateau` and `evaluate_folds` (spec 9.3). It is a degeneracy guard for the Calmar objective, not a power criterion. On 2017–2023 the strategy makes about 8–13 trades a year (K4 4.0 → 2.0); at 12, between 24 and 60 of the 60 combos are eligible in every fold, while a floor of 30 leaves most folds with none. (The neighbour rule can only shrink the selectable set; it has not been re-measured on real data.)
- **Breakers off in grid runs**: `run_grid` applies `with_overrides(cfg, **{"breakers.enabled": False})` before running anything, so the pool workers and the in-process path both get it and the caller's config is unchanged. The reason: a TOTAL_HALT latched early in the single 2017–2023 run would zero every later fold. The holdout (`compare`) and plain backtests keep breakers on.
- **Go-live trade count**: `GO_LIVE_MIN_TRADES = 50` out-of-sample trades with frozen params, counted over the holdout plus later paper trading (Plan 2, not implemented here). `trade_count_lines` gives one line per candidate (A, HYBRID) with its holdout count. Below 50 the line is `<name>: 표본 외 거래 N/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족`, otherwise `<name>: 표본 외 거래 N/50 — 거래 수 기준 충족`. The lines are counts only; the CLI prints them and puts them at the top of the holdout report (T22).

- [ ] **Step 1: Write the failing test (walk-forward)**

`tests/backtest/test_walkforward.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest import walkforward
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import (
    GRID,
    MIN_TRAIN_TRADES,
    WF_END,
    config_for,
    evaluate_folds,
    folds,
    grid_combos,
    pick_plateau,
    select_plateau,
)
from ewbot.config import BotConfig
from ewbot.engine.loop import EngineResult, EquityPoint

H = 3600


def _idx(combo):
    return tuple(axis.index(v) for axis, v in zip(GRID.values(), combo))


def _neighbours(combo):
    """In-grid neighbours of `combo` (Chebyshev index distance 1, itself excluded)."""
    ic = _idx(combo)
    return [o for o in grid_combos() if o != combo and max(abs(a - b) for a, b in zip(ic, _idx(o))) <= 1]


def test_grid_has_60_combos_in_key_order():
    combos = grid_combos()
    assert len(combos) == 60 and len(set(combos)) == 60
    assert combos[0] == (2.0, 0.5, 0.0) and combos[-1] == (4.0, 0.7, 0.5)
    cfg = config_for(BotConfig(), (2.5, 0.55, 0.5))
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == (2.5, 0.55, 0.5)


def test_folds_exact_boundaries():
    fl = folds()
    assert len(fl) == 10
    # (train_start, train_end, test_start, test_end)
    assert fl[0] == (1_483_228_800, 1_546_300_800, 1_546_300_800, 1_561_939_200)  # 2017-01-01 | 2019-01-01 .. 2019-07-01
    assert fl[1] == (1_498_867_200, 1_561_939_200, 1_561_939_200, 1_577_836_800)  # 2017-07-01 | 2019-07-01 .. 2020-01-01
    assert fl[-1] == (1_625_097_600, 1_688_169_600, 1_688_169_600, 1_704_067_200)  # 2021-07-01 | 2023-07-01 .. 2024-01-01
    assert fl[-1][3] == utc_ts(WF_END)  # the last test window ends where the holdout starts
    for a, b in zip(fl, fl[1:]):
        assert b[2] == a[3]            # test windows are contiguous
        assert b[0] > a[0]
    assert all(f[1] == f[2] for f in fl)  # training ends where the test starts (no gap, no overlap)
    assert folds(train_months=12, test_months=12, first_test="2020-01-01", last_test_end="2022-01-01") == [
        (1_546_300_800, 1_577_836_800, 1_577_836_800, 1_609_459_200),
        (1_577_836_800, 1_609_459_200, 1_609_459_200, 1_640_995_200),
    ]


def _score_grid(plateau=2.0, base=1.0):
    k, q, s = GRID["wave.K4"], GRID["strategy.Q_MIN"], GRID["strategy.S_MIN"]
    calmar = {}
    for i, kv in enumerate(k):
        for j, qv in enumerate(q):
            for l, sv in enumerate(s):
                calmar[(kv, qv, sv)] = plateau if (i in (1, 2, 3) and j in (1, 2, 3)) else base
    spike = (k[0], q[4], s[2])  # isolated corner cell
    calmar[spike] = 5.0
    return calmar, spike


def test_pick_plateau_prefers_plateau_over_isolated_spike():
    calmar, spike = _score_grid()
    ntr = {c: 100 for c in calmar}
    assert max(calmar, key=calmar.get) == spike           # naive argmax would pick the spike
    # spike neighbourhood mean = 14/8 = 1.75 < 2.0 (interior of the plateau)
    assert pick_plateau(calmar, ntr) == (3.0, 0.6, 0.0)   # smallest grid index among the tied plateau centres


def test_pick_plateau_excludes_combos_below_min_trades_everywhere():
    calmar, _ = _score_grid()
    hot = (4.0, 0.65, 0.25)  # plateau-edge cell that would inflate its neighbours' mean to ~5.6
    calmar[hot] = 100.0
    ntr = {c: 100 for c in calmar}
    ntr[hot] = 5
    assert pick_plateau(calmar, ntr, min_trades=30) == (3.0, 0.6, 0.0)
    ntr[hot] = 100  # once it counts, it dominates its neighbourhood and the pick moves
    assert pick_plateau(calmar, ntr, min_trades=30) != (3.0, 0.6, 0.0)


def test_pick_plateau_raises_when_nothing_qualifies():
    calmar, _ = _score_grid()
    with pytest.raises(ValueError):
        pick_plateau(calmar, {c: 1 for c in calmar}, min_trades=30)


def test_pick_plateau_ignores_an_eligible_spike_whose_neighbours_are_ineligible():
    calmar, spike = _score_grid()
    ntr = {c: 100 for c in calmar}
    for o in _neighbours(spike):
        ntr[o] = 0  # every neighbour of the spike fails the trade floor
    # without the neighbour rule the spike's "neighbourhood mean" is its own Calmar 5.0 and it wins
    assert pick_plateau(calmar, ntr) == (3.0, 0.6, 0.0)


def test_pick_plateau_needs_half_of_the_in_grid_neighbourhood_eligible():
    calmar, spike = _score_grid()
    nb = sorted(_neighbours(spike))
    assert len(nb) == 7  # corner of the 4 x 5 x 3 grid
    ntr = {c: 0 for c in calmar}
    ntr[spike] = 100
    for o in nb[:4]:
        ntr[o] = 100  # 4/7 eligible: the spike qualifies; its neighbours (4/11, 4/17, 4/26) do not
    assert pick_plateau(calmar, ntr) == spike
    ntr[nb[3]] = 0  # 3/7 < half: nothing is selectable
    with pytest.raises(ValueError, match="neighbour"):
        pick_plateau(calmar, ntr)


def test_min_train_trades_default_is_12():
    calmar, _ = _score_grid()
    assert MIN_TRAIN_TRADES == 12
    assert pick_plateau(calmar, {c: 12 for c in calmar}) == (3.0, 0.6, 0.0)  # default floor
    with pytest.raises(ValueError):
        pick_plateau(calmar, {c: 11 for c in calmar})


def test_run_grid_disables_breakers_and_cuts_at_end_without_touching_the_input(monkeypatch):
    seen = []

    def fake_run_backtest(cfg, df1h, start_ts, end_ts):
        seen.append((cfg, int(df1h["ts"].max()), end_ts))
        return EngineResult(trades=(), equity=(), signals=(), events=())

    monkeypatch.setattr(walkforward, "run_backtest", fake_run_backtest)
    base = BotConfig()
    df = pd.DataFrame({"ts": [0, H, 2 * H, 3 * H], "close": 1.0})
    out = walkforward.run_grid(base, df, 0, 2 * H, workers=1)
    assert list(out) == grid_combos() and len(seen) == 60
    assert all(c.breakers.enabled is False for c, _, _ in seen)
    assert {(c.wave.K4, c.strategy.Q_MIN, c.strategy.S_MIN) for c, _, _ in seen} == set(grid_combos())
    assert all(last == H and end == 2 * H for _, last, end in seen)  # nothing at/after end_ts reaches a run
    assert base.breakers.enabled is True and len(df) == 4  # the caller's config and frame are untouched


def _result(value_fn, n_points, trade_hours, mk_trade):
    eq = tuple(EquityPoint(ts=i * H, equity=float(value_fn(i))) for i in range(n_points))
    trades = tuple(mk_trade(entry_ts=h * H, exit_ts=(h + 1) * H, pnl=1.0) for h in trade_hours)
    return EngineResult(trades=trades, equity=eq, signals=(), events=())


A_CENTRE = (3.0, 0.6, 0.0)  # smallest grid index whose whole neighbourhood runs result A


@pytest.fixture
def grid_results(mk_trade):
    """All 60 combos: result A on K4 in {2.5, 3.0, 4.0} x Q_MIN in {0.55, 0.6, 0.65} (every S_MIN),
    result B elsewhere. A has the higher Calmar on every window used below."""
    a = _result(lambda i: 100 + i - (5 if 40 <= i < 45 else 0), 200, (10, 20, 30, 120), mk_trade)
    b = _result(lambda i: 100 + 0.5 * i - (10 if 40 <= i < 45 else 0), 200, (10, 20, 30), mk_trade)

    def in_block(c):
        i, j, _ = _idx(c)
        return i >= 1 and 1 <= j <= 3

    return {c: (a if in_block(c) else b) for c in grid_combos()}


def test_select_plateau_uses_train_window_and_min_trades(grid_results):
    assert select_plateau(grid_results, 0, 100 * H, min_trades=3) == A_CENTRE
    with pytest.raises(ValueError):
        select_plateau(grid_results, 0, 100 * H, min_trades=4)   # only 3 trades in the window
    with pytest.raises(ValueError):
        select_plateau(grid_results, 150 * H, 190 * H, min_trades=1)  # no trades in that window


def test_evaluate_folds_stitches_oos_returns(grid_results):
    fold_list = [
        (0, 100 * H, 100 * H, 150 * H),         # fold 0: A block wins on train; OOS = hours 100..149
        (100 * H, 150 * H, 150 * H, 200 * H),   # fold 1: only 1 trade in train -> nothing eligible -> flat
    ]
    out = evaluate_folds(grid_results, fold_list, equity0=1000.0, min_trades=3)
    f0, f1 = out["folds"]
    assert f0["chosen"] == A_CENTRE and f0["oos"] is not None
    assert f1["chosen"] is None and f1["oos"] is None
    st = out["stitched_equity"]
    assert len(st) == 1 + 100                                  # base point + 50 + 50 flat hours
    assert st[0] == (100 * H - H, 1000.0)
    # A: equity(99) = 199, equity(149) = 249 -> stitched value after fold 0 = 1000 * 249/199
    assert st[50][1] == pytest.approx(1000.0 * 249.0 / 199.0)
    assert st[-1][1] == pytest.approx(st[50][1])               # fold 1 is flat
    assert out["stitched_metrics"]["n_trades"] == 1            # A's trade at hour 120 only
    assert [o.entry_ts for o in out["oos_trades"]] == [120 * H]
    # the last fold chose nothing: chosen_final is fold 0's pick and is flagged as a fallback
    assert out["chosen_final"] == A_CENTRE
    assert out["chosen_final_from_fold"] == 0 and out["chosen_final_fallback"] is True


def test_chosen_final_from_the_last_fold_is_not_a_fallback(grid_results):
    fold_list = [(0, 100 * H, 100 * H, 150 * H), (0, 150 * H, 150 * H, 200 * H)]  # both trainable
    out = evaluate_folds(grid_results, fold_list, equity0=1000.0, min_trades=3)
    assert out["folds"][1]["chosen"] is not None
    assert out["chosen_final"] == out["folds"][1]["chosen"]
    assert out["chosen_final_from_fold"] == 1 and out["chosen_final_fallback"] is False
    none = evaluate_folds(grid_results, [(100 * H, 150 * H, 150 * H, 200 * H)], equity0=1000.0, min_trades=3)
    assert none["chosen_final"] is None and none["chosen_final_from_fold"] is None
    assert none["chosen_final_fallback"] is False


def test_walk_forward_never_sees_bars_at_or_after_wf_end(monkeypatch, mk_trade):
    """A frame with poisoned bars after WF_END (NaN / x10) must give exactly the walk-forward of the
    clean truncated frame, even with a 'leaky' engine that reads every bar it is handed."""
    wf_end = utc_ts(WF_END)
    ts = list(range(utc_ts("2016-12-01"), utc_ts("2024-07-01"), 24 * H))  # daily bars are enough here
    px = [100.0 + (i % 50) for i in range(len(ts))]
    full = pd.DataFrame({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0})
    post = full["ts"] >= wf_end
    full.loc[post, ["open", "high", "low", "close"]] *= 10.0
    full.loc[post & (full.index % 2 == 0), "close"] = float("nan")
    clean = full[~post].reset_index(drop=True)
    assert post.sum() > 100 and clean["close"].notna().all()

    seen_bt, seen_grid = [], []

    def leaky_run_backtest(cfg, df1h, start_ts, end_ts):
        seen_bt.append((int(df1h["ts"].max()), end_ts))
        leak = float(df1h["close"].sum())  # any bar at/after WF_END changes (or NaNs) every equity value
        slope = cfg.wave.K4 + cfg.strategy.Q_MIN + cfg.strategy.S_MIN
        eq = tuple(
            EquityPoint(ts=int(t), equity=10_000.0 + slope * i + (i % 7) * leak * 1e-5)
            for i, t in enumerate(df1h["ts"])
        )
        trades = tuple(mk_trade(entry_ts=int(t), exit_ts=int(t) + H, pnl=1.0) for t in df1h["ts"].iloc[::20])
        return EngineResult(trades=trades, equity=eq, signals=(), events=())

    real_run_grid = walkforward.run_grid

    def spy_run_grid(cfg, df1h, start_ts, end_ts, workers):
        seen_grid.append((start_ts, end_ts, int(df1h["ts"].max())))
        return real_run_grid(cfg, df1h, start_ts, end_ts, workers)

    monkeypatch.setattr(walkforward, "run_backtest", leaky_run_backtest)
    monkeypatch.setattr(walkforward, "run_grid", spy_run_grid)
    out_full = walkforward.walk_forward(BotConfig(), full, workers=1)
    out_clean = walkforward.walk_forward(BotConfig(), clean, workers=1)

    assert any(f["chosen"] is not None for f in out_clean["folds"])  # the comparison is not vacuous
    assert out_clean["stitched_metrics"]["max_dd"] > 0
    assert out_full == out_clean
    assert [g[:2] for g in seen_grid] == [(utc_ts("2017-01-01"), wf_end)] * 2  # run_grid gets end_ts == WF_END
    assert all(last < wf_end for _, _, last in seen_grid)
    assert len(seen_bt) == 120 and all(last < wf_end and end == wf_end for last, end in seen_bt)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_walkforward.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.walkforward'`.

- [ ] **Step 3: Write minimal implementation (walk-forward)**

`src/ewbot/backtest/walkforward.py`:

```python
"""Walk-forward validation: one run per grid combo, plateau selection per fold, stitched OOS."""

from __future__ import annotations

import dataclasses
import itertools
import math
import multiprocessing as mp
import sys
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.runner import run_backtest, utc_ts
from ewbot.config import BotConfig, with_overrides
from ewbot.engine.loop import EngineResult, EquityPoint
from ewbot.types import H1

GRID: dict[str, tuple[float, ...]] = {
    "wave.K4": (2.0, 2.5, 3.0, 4.0),
    "strategy.Q_MIN": (0.5, 0.55, 0.6, 0.65, 0.7),
    "strategy.S_MIN": (0.0, 0.25, 0.5),
}
WF_START = "2017-01-01"
# Single source of the walk-forward / holdout boundary: the holdout (holdout.HOLDOUT_START) starts
# here, and walk_forward() drops every bar at/after it before anything runs.
WF_END = "2024-01-01"
# spec 9.3: a combo needs this many trades in a 24-month training window. A degeneracy floor for
# the Calmar objective, not a power criterion (2017-2023: about 8-13 trades/year).
MIN_TRAIN_TRADES = 12
# A combo is selectable only if at least this fraction of its in-grid neighbours (itself excluded)
# is eligible too, so a lone eligible cell cannot "win" a plateau made of itself.
MIN_ELIGIBLE_NEIGHBOUR_FRAC = 0.5

Combo = tuple[float, ...]


def grid_combos() -> list[Combo]:
    """The 4 x 5 x 3 = 60 combos in GRID key order (K4, Q_MIN, S_MIN)."""
    return list(itertools.product(*GRID.values()))


def config_for(cfg: BotConfig, combo: Combo) -> BotConfig:
    return with_overrides(cfg, **dict(zip(GRID.keys(), combo)))


# ----------------------------------------------------------------------------- grid run

_STATE: dict[str, Any] = {}  # per-process, set by the pool initializer


def _init_worker(cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int) -> None:
    _STATE.update(cfg=cfg, df=df1h, start=start_ts, end=end_ts)


def _run_combo(combo: Combo) -> tuple[Combo, EngineResult]:
    res = run_backtest(config_for(_STATE["cfg"], combo), _STATE["df"], _STATE["start"], _STATE["end"])
    return combo, dataclasses.replace(res, signals=())  # signals are not needed downstream


def run_grid(
    cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int, workers: int
) -> dict[Combo, EngineResult]:
    """Run every combo ONCE over [start_ts, end_ts); folds later slice these single runs.

    Bars at/after end_ts are dropped before any run (the engine never sees them).
    Circuit breakers are DISABLED here (breakers.enabled = False): they are a risk overlay validated
    in the holdout and in paper mode, and a TOTAL_HALT latched early in this single continuous run
    would zero the trades of every later fold. The caller's cfg and frame are not modified."""
    cfg = with_overrides(cfg, **{"breakers.enabled": False})
    df1h = df1h[df1h["ts"] < end_ts].reset_index(drop=True)
    combos = grid_combos()
    out: dict[Combo, EngineResult] = {}
    initargs = (cfg, df1h, start_ts, end_ts)
    if workers <= 1:
        _init_worker(*initargs)
        for n, c in enumerate(combos, 1):
            out[c] = _run_combo(c)[1]
            print(f"[grid] {n}/{len(combos)} {c}", file=sys.stderr, flush=True)
    else:
        with mp.Pool(min(workers, len(combos)), initializer=_init_worker, initargs=initargs) as pool:
            for n, (c, res) in enumerate(pool.imap_unordered(_run_combo, combos), 1):
                out[c] = res
                print(f"[grid] {n}/{len(combos)} {c}", file=sys.stderr, flush=True)
    return {c: out[c] for c in combos}


# ----------------------------------------------------------------------------- folds


def _add_months(ts: int, months: int) -> int:
    dt = datetime.fromtimestamp(ts, UTC)
    if dt.day != 1 or dt.hour or dt.minute or dt.second:
        raise ValueError("fold boundaries must be first-of-month midnight UTC")
    y, m = divmod(dt.year * 12 + (dt.month - 1) + months, 12)
    return int(datetime(y, m + 1, 1, tzinfo=UTC).timestamp())


def folds(
    train_months: int = 24,
    test_months: int = 6,
    first_test: str = "2019-01-01",
    last_test_end: str = WF_END,
) -> list[tuple[int, int, int, int]]:
    """[(train_start, train_end, test_start, test_end)] as epoch seconds, rolling by test_months.
    Defaults give the 10 folds of spec 9.3 (test windows 2019H1 .. 2023H2)."""
    out: list[tuple[int, int, int, int]] = []
    test_start, limit = utc_ts(first_test), utc_ts(last_test_end)
    while True:
        test_end = _add_months(test_start, test_months)
        if test_end > limit:
            return out
        out.append((_add_months(test_start, -train_months), test_start, test_start, test_end))
        test_start = test_end


# ----------------------------------------------------------------------------- plateau


def _grid_index(combo: Combo) -> tuple[int, ...]:
    return tuple(axis.index(v) for axis, v in zip(GRID.values(), combo))


def _n_grid_neighbours(index: tuple[int, ...]) -> int:
    """Number of grid cells at Chebyshev index distance exactly 1 from `index` (edges clipped)."""
    n = 1
    for i, axis in zip(index, GRID.values()):
        n *= min(i + 1, len(axis) - 1) - max(i - 1, 0) + 1
    return n - 1


def _tie_key(x: float) -> float:
    """x rounded to 12 significant digits: equal means of differently sized sets compare equal."""
    return float(f"{x:.12g}")


def pick_plateau(
    calmar: Mapping[Combo, float], n_trades: Mapping[Combo, int], min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    """Score(c) = mean Calmar over c's grid neighbours (Chebyshev index distance <= 1 on every
    axis, c included), counting only eligible combos (>= min_trades trades, finite Calmar). A combo
    is selectable only if it is eligible and at least MIN_ELIGIBLE_NEIGHBOUR_FRAC of its in-grid
    neighbours (itself excluded) are eligible. Highest score wins; ties go to the higher own Calmar,
    then to the smallest grid index. Raises ValueError if no combo is selectable."""
    valid = {
        c: float(v) for c, v in calmar.items() if n_trades.get(c, 0) >= min_trades and math.isfinite(v)
    }
    if not valid:
        raise ValueError(f"no grid combo has >= {min_trades} trades in the training window")
    idx = {c: _grid_index(c) for c in valid}
    best: tuple[tuple[float, float], Combo] | None = None
    for c in sorted(valid, key=lambda k: idx[k]):
        near = [o for o in valid if max(abs(a - b) for a, b in zip(idx[c], idx[o])) <= 1]
        if len(near) - 1 < MIN_ELIGIBLE_NEIGHBOUR_FRAC * _n_grid_neighbours(idx[c]):
            continue  # an (almost) isolated eligible cell is a spike, not a plateau
        key = (_tie_key(float(np.mean([valid[o] for o in near]))), _tie_key(valid[c]))
        if best is None or key > best[0]:
            best = (key, c)
    if best is None:
        raise ValueError(
            f"no grid combo with >= {min_trades} trades has at least {MIN_ELIGIBLE_NEIGHBOUR_FRAC:.0%} "
            "of its grid neighbours eligible"
        )
    return best[1]


def _train_scores(
    results: Mapping[Combo, EngineResult], train_start: int, train_end: int
) -> tuple[dict[Combo, float], dict[Combo, int]]:
    calmar: dict[Combo, float] = {}
    ntr: dict[Combo, int] = {}
    for c, res in results.items():
        m = compute_metrics(res.equity, res.trades, train_start, train_end)
        calmar[c], ntr[c] = m.calmar, m.n_trades
    return calmar, ntr


def select_plateau(
    results: Mapping[Combo, EngineResult], train_start: int, train_end: int, min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    calmar, ntr = _train_scores(results, train_start, train_end)
    return pick_plateau(calmar, ntr, min_trades)


# ----------------------------------------------------------------------------- stitching


def oos_returns(res: EngineResult, ts0: int, ts1: int) -> list[tuple[int, float]]:
    """Hourly returns of the equity points with ts0 <= ts < ts1 (the first one is measured
    against the last point before ts0, so consecutive folds join without a gap)."""
    out: list[tuple[int, float]] = []
    eq = res.equity
    for i in range(1, len(eq)):
        t = eq[i].ts
        if t < ts0:
            continue
        if t >= ts1:
            break
        prev = eq[i - 1].equity
        out.append((t, eq[i].equity / prev - 1.0 if prev > 0 else 0.0))
    return out


def evaluate_folds(
    results: Mapping[Combo, EngineResult],
    fold_list: Sequence[tuple[int, int, int, int]],
    equity0: float,
    min_trades: int = MIN_TRAIN_TRADES,
) -> dict[str, Any]:
    """Pick the plateau combo on each training window, then stitch each fold's OOS slice of
    that combo's (single-run) equity returns into one compounded equity curve. A fold with no
    selectable combo stays flat (chosen = None).

    chosen_final is the pick of the LAST fold that picked anything; chosen_final_from_fold is that
    fold's index and chosen_final_fallback is True when it is not the last fold (the most recent
    training window had no selectable combo - treat that pick with suspicion)."""
    rows: list[dict[str, Any]] = []
    stitched: list[EquityPoint] = []
    oos_trades = []
    cur = float(equity0)
    if fold_list:
        stitched.append(EquityPoint(ts=fold_list[0][2] - H1, equity=cur))
    chosen_final: Combo | None = None
    chosen_final_from_fold: int | None = None
    for k, (tr0, tr1, te0, te1) in enumerate(fold_list):
        calmar, ntr = _train_scores(results, tr0, tr1)
        try:
            chosen: Combo | None = pick_plateau(calmar, ntr, min_trades)
        except ValueError:
            chosen = None
        row: dict[str, Any] = {
            "train": (tr0, tr1),
            "test": (te0, te1),
            "chosen": chosen,
            "train_calmar": calmar[chosen] if chosen is not None else None,
            "oos": None,
        }
        if chosen is None:
            stitched.extend(EquityPoint(ts=t, equity=cur) for t in range(te0, te1, H1))
        else:
            res = results[chosen]
            for t, r in oos_returns(res, te0, te1):
                cur *= 1.0 + r
                stitched.append(EquityPoint(ts=t, equity=cur))
            oos_trades.extend(t for t in res.trades if te0 <= t.entry_ts < te1)
            row["oos"] = dataclasses.asdict(compute_metrics(res.equity, res.trades, te0, te1))
            chosen_final, chosen_final_from_fold = chosen, k
        rows.append(row)
    fallback = chosen_final_from_fold is not None and chosen_final_from_fold != len(fold_list) - 1
    return {
        "keys": list(GRID),
        "folds": rows,
        "stitched_equity": [(p.ts, p.equity) for p in stitched],
        "stitched_metrics": dataclasses.asdict(compute_metrics(stitched, oos_trades)),
        "oos_trades": oos_trades,
        "chosen_final": chosen_final,
        "chosen_final_from_fold": chosen_final_from_fold,
        "chosen_final_fallback": fallback,
    }


def walk_forward(cfg: BotConfig, df1h: pd.DataFrame, workers: int) -> dict[str, Any]:
    """Full walk-forward over [WF_START, WF_END) = [2017-01-01, 2024-01-01). Every bar at/after
    WF_END is dropped here, before anything runs, so nothing from the holdout can leak in.
    Grid runs have circuit breakers disabled (see run_grid); folds need MIN_TRAIN_TRADES trades."""
    start_ts, end_ts = utc_ts(WF_START), utc_ts(WF_END)
    df1h = df1h[df1h["ts"] < end_ts].reset_index(drop=True)
    results = run_grid(cfg, df1h, start_ts, end_ts, workers)
    out = evaluate_folds(results, folds(), cfg.initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start_ts, end_ts) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": c, "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades} for c, m in full.items()
    ]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_walkforward.py -v`
Expected: 13 passed.

- [ ] **Step 5: Write the failing test (holdout)**

`tests/backtest/test_holdout.py`:

```python
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from ewbot.backtest import holdout
from ewbot.backtest.holdout import (
    HOLDOUT_START,
    HoldoutError,
    evaluate_criteria,
    params_digest,
    run_holdout,
    trade_count_lines,
)
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, WF_END, config_for
from ewbot.config import BotConfig, with_overrides

H = 3600
PARAMS = (3.0, 0.6, 0.25)
_MODULE_DEFAULT_LOCK = getattr(holdout, "DEFAULT_LOCK_PATH", None)  # read before any test patches it


def _m(sharpe, calmar, dd=0.1, n=60):
    return {"sharpe": sharpe, "calmar": calmar, "max_dd": dd, "n_trades": n}


def _fake_compare_factory(calls):
    def fake(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1):
        calls.append((cfg, start_ts, end_ts, random_runs))
        return {
            "comparison": {
                "A": _m(1.0, 2.0),
                "C": _m(0.5, 1.0),
                "HYBRID": _m(0.4, 0.5),
                "BH": _m(0.6, 1.5),
                "EMA": _m(0.2, 0.3),
            },
            "random": {
                "runs": random_runs,
                "n_signals": 60,
                "dist": {"sharpe": {"mean": 0.1}, "calmar": {"mean": 0.2}},
            },
        }

    return fake


@pytest.fixture(autouse=True)
def _never_the_repo_lock(tmp_path, monkeypatch):
    """No test may write <repo>/holdout.lock, even one that forgets lock_path."""
    monkeypatch.setattr(holdout, "DEFAULT_LOCK_PATH", tmp_path / "default" / "holdout.lock", raising=False)
    monkeypatch.chdir(tmp_path)  # and nothing CWD-relative lands in the repo either


@pytest.fixture
def df():
    ts = [HOLDOUT_START - 3 * H + i * H for i in range(10)]
    return pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})


def test_holdout_starts_exactly_where_the_walk_forward_ends():
    assert HOLDOUT_START == utc_ts(WF_END) == 1_704_067_200


def test_requires_confirm_and_writes_nothing(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="confirm"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=False)
    assert not lock.exists() and calls == []


def test_lock_created_rerun_same_ok_different_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "sub" / "holdout.lock"
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=7)
    data = json.loads(lock.read_text())
    assert data["params"] == [3.0, 0.6, 0.25]
    assert data["sha256"] == holdout.lock_digest(BotConfig(), PARAMS, random_runs=7)
    assert len(data["sha256"]) == 64 and len(data["runs"]) == 1
    cfg_used, start, end, runs = calls[0]
    assert start == HOLDOUT_START == 1_704_067_200
    assert end == int(df["ts"].iloc[-1]) + H and runs == 7
    assert (cfg_used.wave.K4, cfg_used.strategy.Q_MIN, cfg_used.strategy.S_MIN) == PARAMS
    assert out["window"] == (start, end) and out["sha256"] == data["sha256"]
    assert out["lock_path"] == str(lock)

    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=7)  # identical: allowed
    assert len(json.loads(lock.read_text())["runs"]) == 2

    before = lock.read_text()
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(BotConfig(), df, (2.5, 0.6, 0.25), lock_path=lock, confirm=True, random_runs=7)
    assert len(calls) == 2  # the refused call never ran anything
    assert lock.read_text() == before  # lock untouched


@pytest.mark.parametrize(
    "change",
    [
        {"costs.slippage": 0.001},
        {"costs.fee_rate": 0.0006},
        {"costs.funding_apr_long": 0.2},
        {"seed": 1},
        {"initial_equity": 20_000.0},
        {"wave.atr_len": 21},
        {"wave.sigma": 0.2},
        {"strategy.r_gate": 2.0},
        {"strategy.setups_enabled": ("W3",)},
        {"breakers.daily_loss": 0.05},
    ],
    ids=lambda ch: next(iter(ch)),
)
def test_lock_binds_the_full_config(tmp_path, df, monkeypatch, change):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    before = lock.read_text()
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(with_overrides(BotConfig(), **change), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert lock.read_text() == before and len(calls) == 1


def test_lock_binds_the_random_control_size(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=6)
    assert len(calls) == 1


def test_strategy_mode_is_pinned_and_not_part_of_the_binding(tmp_path, df, monkeypatch):
    """compare() runs A, C and HYBRID itself, so cfg.strategy.mode has no effect on the holdout."""
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    hybrid = with_overrides(BotConfig(), **{"strategy.mode": "HYBRID"})
    run_holdout(hybrid, df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert [c[0].strategy.mode for c in calls] == ["A", "A"] and calls[0][0] == calls[1][0]


def test_lock_digest_covers_config_grid_and_random_runs():
    base = holdout.lock_digest(BotConfig(), PARAMS)
    assert len(base) == 64
    assert base == holdout.lock_digest(BotConfig(), (3, 0.6, 0.25))  # int vs float K4
    assert base != holdout.lock_digest(BotConfig(), (3.0, 0.6, 0.5))
    assert base != holdout.lock_digest(with_overrides(BotConfig(), **{"costs.slippage": 0.0006}), PARAMS)
    assert base != holdout.lock_digest(BotConfig(), PARAMS, random_runs=50)
    assert base == holdout.lock_digest(with_overrides(BotConfig(), **{"strategy.mode": "C"}), PARAMS)


def test_lock_records_config_window_and_first_run_headline(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert data["config"] == json.loads(json.dumps(dataclasses.asdict(config_for(BotConfig(), PARAMS))))
    assert data["grid"] == {k: list(v) for k, v in GRID.items()} and data["keys"] == list(GRID)
    assert data["holdout_start"] == HOLDOUT_START and data["random_runs"] == 5
    assert data["params_sha256"] == params_digest(PARAMS)
    last_bar = int(df["ts"].iloc[-1])
    first = data["first_run"]
    assert (first["start_ts"], first["end_ts"], first["last_bar_ts"]) == (HOLDOUT_START, last_bar + H, last_bar)
    head = first["headline"]
    assert {"A", "HYBRID", "C", "BH", "RANDOM"} <= set(head)
    assert head["A"] == {"sharpe": 1.0, "calmar": 2.0, "max_dd": 0.1, "n_trades": 60}
    assert head["RANDOM"]["runs"] == 5 and head["RANDOM"]["sharpe"]["mean"] == 0.1
    assert first["criteria"]["automatic_criteria_met"] is True and first["criteria"]["n_trades"]["A"] == 60
    assert data["runs"][0]["completed"] is not None and data["runs"][0]["last_bar_ts"] == last_bar

    more = pd.concat([df, df.tail(1).assign(ts=last_bar + H)], ignore_index=True)  # one more bar later on
    run_holdout(BotConfig(), more, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data2 = json.loads(lock.read_text())
    assert data2["first_run"] == first  # the first run's record is never overwritten
    assert len(data2["runs"]) == 2 and data2["runs"][1]["last_bar_ts"] == last_bar + H


def test_crash_during_the_run_keeps_the_lock(tmp_path, df, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(holdout, "compare", boom)
    lock = tmp_path / "holdout.lock"
    with pytest.raises(RuntimeError, match="crashed"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert data["first_run"] is None and data["runs"][0]["completed"] is None
    with pytest.raises(HoldoutError, match="refusing"):  # a crash cannot be used to retune
        run_holdout(BotConfig(), df, (2.5, 0.6, 0.25), lock_path=lock, confirm=True, random_runs=5)
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert len(data["runs"]) == 2 and data["first_run"]["headline"]["A"]["n_trades"] == 60


def test_unreadable_lock_is_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    lock.write_text("{not json", encoding="utf-8")
    with pytest.raises(HoldoutError, match="unreadable"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert calls == [] and lock.read_text() == "{not json"


@pytest.mark.parametrize(
    "cfg, params, runs, msg",
    [
        (BotConfig(), (3.0, 0.6, 0.3), 5, "grid"),
        (BotConfig(), (3.0, 0.6), 5, "grid"),
        (with_overrides(BotConfig(), **{"breakers.enabled": False}), PARAMS, 5, "breakers"),
        (with_overrides(BotConfig(), risk="aggressive"), PARAMS, 5, "conservative"),
        (with_overrides(BotConfig(), **{"risk.risk_pct": 0.02}), PARAMS, 5, "conservative"),
        (with_overrides(BotConfig(), risk={"risk_pct": 0.01, "max_leverage": 3.0}), PARAMS, 5, "conservative"),
        (BotConfig(), PARAMS, 0, "random"),
    ],
    ids=["off-grid", "short-params", "breakers-off", "aggressive", "conservative-1pct-changed", "custom", "no-random"],
)
def test_invalid_holdout_setups_are_refused_before_any_write(tmp_path, df, monkeypatch, cfg, params, runs, msg):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match=msg):
        run_holdout(cfg, df, params, lock_path=lock, confirm=True, random_runs=runs)
    assert not lock.exists() and calls == []


def test_no_data_in_holdout_does_not_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    old = pd.DataFrame({"ts": [HOLDOUT_START - 10 * H], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="no data"):
        run_holdout(BotConfig(), old, PARAMS, lock_path=lock, confirm=True)
    assert not lock.exists()


def test_default_lock_path_is_the_project_root_not_the_cwd():
    repo_root = Path(__file__).resolve().parents[2]  # tests/backtest/test_holdout.py -> repo root
    assert holdout.PROJECT_ROOT == repo_root and (repo_root / "pyproject.toml").is_file()
    assert (repo_root / "src" / "ewbot" / "backtest" / "holdout.py").is_file()
    assert _MODULE_DEFAULT_LOCK == repo_root / "holdout.lock"


def test_run_holdout_without_lock_path_uses_the_default_whatever_the_cwd(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    target = tmp_path / "root" / "holdout.lock"
    monkeypatch.setattr(holdout, "DEFAULT_LOCK_PATH", target)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    out = run_holdout(BotConfig(), df, PARAMS, confirm=True, random_runs=5)
    assert target.exists() and out["lock_path"] == str(target)
    assert not (cwd / "holdout.lock").exists()


def test_default_lock_path_needs_a_recognisable_project_root(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    monkeypatch.setattr(holdout, "PROJECT_ROOT", tmp_path / "site-packages")  # e.g. a non-editable install
    with pytest.raises(HoldoutError, match="lock_path"):
        run_holdout(BotConfig(), df, PARAMS, confirm=True, random_runs=5)
    assert calls == []


def test_params_digest_is_stable_and_sensitive():
    assert params_digest((3.0, 0.6, 0.25)) == params_digest([3, 0.6, 0.25])
    assert params_digest((3.0, 0.6, 0.25)) != params_digest((3.0, 0.6, 0.5))


def test_criteria(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=tmp_path / "l.lock", confirm=True)
    c = out["criteria"]
    assert c["winners"] == ["A"] and c["passing"] == ["A"] and c["automatic_criteria_met"] is True
    assert c["random_control_evaluated"] is True
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    cmp["comparison"]["A"]["max_dd"] = 0.2
    assert evaluate_criteria(cmp)["passing"] == [] and evaluate_criteria(cmp)["max_dd_ok"] is False
    cmp["comparison"]["A"].update(max_dd=0.1, n_trades=49)
    assert evaluate_criteria(cmp)["n_trades_ok"] is False
    cmp["comparison"]["A"].update(n_trades=60, sharpe=0.05)  # below random-control mean
    assert evaluate_criteria(cmp)["winners"] == []


@pytest.mark.parametrize(
    "random",
    [{"runs": 0, "n_signals": 0, "dist": {}, "beaten_by": {}}, {"runs": 0, "n_signals": 5, "dist": {}}, {}],
    ids=["no-runs", "no-dist", "missing"],
)
def test_random_control_is_mandatory(random):
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    assert evaluate_criteria(cmp)["automatic_criteria_met"] is True  # A beats C, BH and the random mean
    cmp["random"] = random  # e.g. random_runs=0, or A made no trades so there was nothing to replay
    crit = evaluate_criteria(cmp)
    assert crit["random_control_evaluated"] is False
    assert crit["winners"] == [] and crit["passing"] == [] and crit["automatic_criteria_met"] is False


def test_trade_count_lines_state_out_of_sample_progress():
    comp = {"A": _m(1.0, 2.0, n=27), "HYBRID": _m(0.4, 0.5, n=50), "C": _m(0.5, 1.0, n=3)}
    assert trade_count_lines(comp) == [
        "A: 표본 외 거래 27/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족",
        "HYBRID: 표본 외 거래 50/50 — 거래 수 기준 충족",
    ]
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    cmp["comparison"]["A"]["n_trades"] = 27
    crit = evaluate_criteria(cmp)
    assert crit["n_trades"] == {"A": 27, "HYBRID": 60}
    assert crit["trade_count"][0] == "A: 표본 외 거래 27/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족"
    assert crit["n_trades_ok"] is False and crit["automatic_criteria_met"] is False
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_holdout.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.holdout'`.

- [ ] **Step 7: Write minimal implementation (holdout)**

`src/ewbot/backtest/holdout.py`:

```python
"""One-shot holdout [WF_END, end) behind a lock file bound to the full run configuration."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.runner import compare, utc_ts
from ewbot.backtest.walkforward import GRID, WF_END, config_for, grid_combos
from ewbot.config import CONSERVATIVE, BotConfig, with_overrides
from ewbot.types import H1

HOLDOUT_START = utc_ts(WF_END)  # single source: the holdout starts exactly where the walk-forward ends
GO_LIVE_MIN_TRADES = 50  # spec 9.6 criterion 3: out-of-sample trades with frozen params (holdout + paper)
# src/ewbot/backtest/holdout.py -> parents[3] is the repository root (src layout, editable install).
# The default lock lives there, never in the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout.lock"
LOCK_VERSION = 2
_CANDIDATES = ("A", "HYBRID")
_HEADLINE_STRATEGIES = ("A", "HYBRID", "C", "BH", "EMA")
_HEADLINE_KEYS = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "n_trades")


class HoldoutError(RuntimeError):
    """The holdout may not be run: not confirmed, invalid setup, or locked to a different configuration."""


def params_digest(params: Sequence[float]) -> str:
    """sha256 of the grid params alone. Informational (stored as `params_sha256`); the lock is bound
    to `lock_digest`, which covers the whole configuration."""
    payload = json.dumps({"keys": list(GRID), "params": [float(p) for p in params]}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def holdout_config(cfg: BotConfig, params: Sequence[float]) -> BotConfig:
    """The fully resolved config of a holdout run: `cfg` with the grid params applied and
    strategy.mode pinned to "A" (compare() runs A, C and HYBRID itself, so the field has no effect)."""
    return with_overrides(config_for(cfg, tuple(float(p) for p in params)), **{"strategy.mode": "A"})


def lock_digest(cfg: BotConfig, params: Sequence[float], random_runs: int = 100) -> str:
    """sha256 of the sorted JSON of everything that determines the holdout result: every field of
    holdout_config(cfg, params), the grid values, the holdout start and the random-control size."""
    payload = {
        "config": dataclasses.asdict(holdout_config(cfg, params)),
        "grid": {k: list(v) for k, v in GRID.items()},
        "holdout_start": HOLDOUT_START,
        "random_runs": int(random_runs),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def trade_count_lines(comp: dict[str, Any], required: int = GO_LIVE_MIN_TRADES) -> list[str]:
    """One line per go-live candidate in `comp`: its out-of-sample trade count against the spec 9.6
    minimum. Counts only, no performance statement. Below the minimum the holdout alone cannot meet
    criterion 3 and trades must keep accumulating in paper mode (Plan 2)."""
    lines: list[str] = []
    for name in _CANDIDATES:
        if name not in comp:
            continue
        n = int(comp[name]["n_trades"])
        if n < required:
            lines.append(f"{name}: 표본 외 거래 {n}/{required} — paper 모드로 누적 필요; 실거래 전환 기준 미충족")
        else:
            lines.append(f"{name}: 표본 외 거래 {n}/{required} — 거래 수 기준 충족")
    return lines


def evaluate_criteria(cmp: dict[str, Any]) -> dict[str, Any]:
    """Automatic part of the spec 9.6 go-live gate on the holdout window (criterion 4, 4 weeks of
    paper, is manual).

    A candidate (A or HYBRID) must beat C, BH and the random-control mean on BOTH Sharpe and
    Calmar; among those, one must also have max_dd <= 15% and >= GO_LIVE_MIN_TRADES trades.
    The random control is mandatory: without a random distribution (no runs, or A made no trades
    to replay) no candidate wins and `random_control_evaluated` is False.
    Criterion 3 counts all out-of-sample trades with frozen params (holdout + later paper);
    `n_trades` / `trade_count` state the holdout's count per candidate."""
    comp = cmp["comparison"]
    rnd = (cmp.get("random") or {}).get("dist") or {}
    random_ok = all(isinstance(rnd.get(k), dict) and "mean" in rnd[k] for k in ("sharpe", "calmar"))

    def beats(name: str) -> bool:
        if not random_ok:
            return False
        a = comp[name]
        for other in ("C", "BH"):
            if a["sharpe"] <= comp[other]["sharpe"] or a["calmar"] <= comp[other]["calmar"]:
                return False
        return a["sharpe"] > rnd["sharpe"]["mean"] and a["calmar"] > rnd["calmar"]["mean"]

    winners = [n for n in _CANDIDATES if n in comp and beats(n)]
    passing = [n for n in winners if comp[n]["max_dd"] <= 0.15 and comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES]
    return {
        "winners": winners,
        "max_dd_ok": any(comp[n]["max_dd"] <= 0.15 for n in winners),
        "n_trades_ok": any(comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES for n in winners),
        "n_trades": {n: int(comp[n]["n_trades"]) for n in _CANDIDATES if n in comp},
        "trade_count": trade_count_lines(comp),
        "random_control_evaluated": random_ok,
        "passing": passing,
        "paper_4_weeks": "manual check required",
        "automatic_criteria_met": bool(passing) and random_ok,
    }


# ----------------------------------------------------------------------------- lock file


def _validate(cfg: BotConfig, params: Sequence[float], random_runs: int) -> tuple[float, ...]:
    """Refuse a holdout setup that is not the one the walk-forward / go-live gate is about."""
    combo = tuple(float(p) for p in params)
    if combo not in grid_combos():
        raise HoldoutError(f"params {list(combo)} are not a combo of the walk-forward grid {list(GRID)} = {GRID}")
    if not cfg.breakers.enabled:
        raise HoldoutError("circuit breakers must be on in the holdout (breakers.enabled = True)")
    if cfg.risk != CONSERVATIVE:
        raise HoldoutError(f"the holdout runs the conservative risk profile only ({CONSERVATIVE}); got {cfg.risk}")
    if int(random_runs) < 1:
        raise HoldoutError("random_runs must be >= 1: beating the random-entry control is a go-live criterion")
    return combo


def _default_lock_path() -> Path:
    if not (PROJECT_ROOT / "pyproject.toml").is_file():
        raise HoldoutError(
            f"cannot locate the project root (no pyproject.toml in {PROJECT_ROOT}); pass lock_path explicitly"
        )
    return DEFAULT_LOCK_PATH


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutError(f"{path} exists but is unreadable ({exc}); refusing to run - inspect it by hand") from exc
    if not isinstance(lock, dict):
        raise HoldoutError(f"{path} exists but is not a holdout lock; refusing to run - inspect it by hand")
    return lock


def _write_lock(path: Path, lock: dict[str, Any]) -> None:
    """Atomic replace, so a crash mid-write cannot leave a truncated lock behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _changed_keys(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted keys whose values differ between two JSON-like mappings (for the refusal message)."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [d for k in sorted(set(old) | set(new)) for d in _changed_keys(old.get(k), new.get(k), f"{prefix}{k}.")]
    return [] if old == new else [prefix.rstrip(".") or "<root>"]


def _headline(cmp: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
    comp = cmp["comparison"]
    head: dict[str, Any] = {
        n: {k: comp[n][k] for k in _HEADLINE_KEYS if k in comp[n]} for n in _HEADLINE_STRATEGIES if n in comp
    }
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    head["RANDOM"] = {
        "runs": rnd.get("runs"),
        "n_signals": rnd.get("n_signals"),
        **{k: dist[k] for k in ("sharpe", "calmar", "total_return") if k in dist},
    }
    keep = ("winners", "passing", "n_trades", "random_control_evaluated", "automatic_criteria_met")
    return {"headline": head, "criteria": {k: criteria[k] for k in keep}}


# ----------------------------------------------------------------------------- run


def run_holdout(
    cfg: BotConfig,
    df1h: pd.DataFrame,
    params: Sequence[float],
    lock_path: Path | str | None = None,
    confirm: bool = False,
    *,
    random_runs: int = 100,
    workers: int = 1,
) -> dict[str, Any]:
    """Run A / C / HYBRID / BH / EMA / RANDOM on [HOLDOUT_START, last bar] with `params`
    (K4, Q_MIN, S_MIN), once per configuration.

    Checked before anything is written: confirm=True; params are a GRID combo; circuit breakers on;
    the conservative risk profile; random_runs >= 1; data at/after HOLDOUT_START.
    The lock (default DEFAULT_LOCK_PATH = <project root>/holdout.lock, independent of the CWD) is
    bound to lock_digest(cfg, params, random_runs) - every config field, the grid, the holdout start
    and the random-control size - and is written BEFORE the run, so a crash cannot be used to retune.
    Re-running the identical configuration is allowed (e.g. later, with more data); anything else is
    refused. The first completed run's window and headline metrics are recorded in the lock and
    never overwritten."""
    if not confirm:
        raise HoldoutError("the holdout is single-use: pass confirm=True (CLI: --confirm) to run it")
    combo = _validate(cfg, params, random_runs)
    ts = df1h["ts"]
    if ts.empty or int(ts.iloc[-1]) < HOLDOUT_START:
        raise HoldoutError(f"no data at or after {WF_END} in df1h")
    last_bar = int(ts.iloc[-1])
    end_ts = last_bar + H1
    lock_path = _default_lock_path() if lock_path is None else Path(lock_path)
    run_cfg = holdout_config(cfg, combo)
    digest = lock_digest(cfg, combo, random_runs)
    config_json = json.loads(json.dumps(dataclasses.asdict(run_cfg)))

    run: dict[str, Any] = {"started": _now(), "end_ts": end_ts, "last_bar_ts": last_bar, "completed": None}
    if lock_path.exists():
        lock = _read_lock(lock_path)
        if lock.get("sha256") != digest:
            changed = _changed_keys(
                {"params": lock.get("params"), "config": lock.get("config"), "random_runs": lock.get("random_runs")},
                {"params": list(combo), "config": config_json, "random_runs": int(random_runs)},
            )
            raise HoldoutError(
                f"{lock_path} is locked to params {lock.get('params')} (sha256 {str(lock.get('sha256'))[:12]}); "
                f"refusing to run a different configuration (params {list(combo)}; changed: {changed or 'unknown'})"
            )
        lock.setdefault("runs", []).append(run)
    else:
        lock = {
            "version": LOCK_VERSION,
            "keys": list(GRID),
            "params": list(combo),
            "sha256": digest,
            "params_sha256": params_digest(combo),
            "config": config_json,
            "grid": {k: list(v) for k, v in GRID.items()},
            "holdout_start": HOLDOUT_START,
            "random_runs": int(random_runs),
            "first_run": None,
            "runs": [run],
        }
    _write_lock(lock_path, lock)

    cmp = compare(run_cfg, df1h, HOLDOUT_START, end_ts, random_runs=random_runs, workers=workers)
    criteria = evaluate_criteria(cmp)

    run["completed"] = _now()
    if lock.get("first_run") is None:
        lock["first_run"] = {
            "started": run["started"],
            "completed": run["completed"],
            "start_ts": HOLDOUT_START,
            "end_ts": end_ts,
            "last_bar_ts": last_bar,
            **_headline(cmp, criteria),
        }
    _write_lock(lock_path, lock)
    return {
        "params": list(combo),
        "sha256": digest,
        "lock_path": str(lock_path),
        "window": (HOLDOUT_START, end_ts),
        "compare": cmp,
        "criteria": criteria,
    }
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_walkforward.py tests/backtest/test_holdout.py -v`
Expected: 49 passed (13 walk-forward, 36 holdout incl. parametrized cases). No `holdout.lock` is created in the repository (the holdout tests point `DEFAULT_LOCK_PATH` and the CWD at `tmp_path`).

- [ ] **Step 9: Commit**
```bash
git add src/ewbot/backtest/walkforward.py src/ewbot/backtest/holdout.py tests/backtest/test_walkforward.py tests/backtest/test_holdout.py
git commit -m "feat(backtest): 60-combo walk-forward (breakers off, >=12 train trades, 2024 cut) with plateau selection, one-shot holdout locked to the full config, mandatory random control and go-live trade count"
```

---

### Task 22: HTML report and CLI

**Files:**
- Create: `src/ewbot/backtest/report.py`, `src/ewbot/cli.py`
- Modify: `pyproject.toml` (console script, only if missing)
- Test: `tests/backtest/test_report.py`, `tests/backtest/test_cli.py`

**Interfaces:**
- Consumes: T18–T21 (`compute_metrics`, `reliability_by_q`, `compare`, `run_backtest`, `breakdowns`, `walk_forward` (its `chosen_final`, `chosen_final_from_fold`, `chosen_final_fallback`), `run_holdout` (its `criteria["trade_count"]` lines, `lock_path`, `sha256`; `lock_path=None` means `holdout.DEFAULT_LOCK_PATH` = `<project root>/holdout.lock`), `GRID`, `MIN_TRAIN_TRADES`), `ewbot.data.candles.load_1h, fill_gaps, check_integrity, aggregate_4h`, `ewbot.data.coinbase_history.update_cache`, `ewbot.config.load_config, with_overrides, AGGRESSIVE, CONSERVATIVE`. CLI end-to-end test only: `tests.synth.random_bars` (T10).
- Produces: `write_report(path, title, sections) -> Path` (+ `to_jsonable`), `ewbot.cli.main(argv) -> int` with subcommands `fetch | backtest | compare | walkforward | holdout`.

Report (`write_report`): one self-contained HTML file, plotly via `<script src="https://cdn.plot.ly/plotly-2.35.2.min.js">`, light/dark via CSS variables. Recognised `sections` keys: `notes`, `equity` (equity chart, log axis when all values are positive, plus drawdown chart; series are down-sampled to about 4000 points, drawdown keeps each block's trough), `comparison` (+ `random` row with mean and [p5, p95], `ci` column), `per_setup`, `long_short`, `per_year`, `reliability`, `trades` (collapsible table per strategy; each trade with a snapshot has a collapsible chart that, on first open, draws the **top count's points as a labelled polyline** (S,1,2,... or X,A,B) over the **4h close series** from `entry - window` to `exit + window`, window = `snapshot_window_bars` (default 60) 4h bars, with entry marker and stop/TP lines), `candles4h`; any other key is rendered as a table (list of flat dicts) or JSON. Titles and all cell text are HTML-escaped, JSON embedded in the page escapes `</`.

CLI options: `--config`, `--profile {conservative,aggressive}`, `--mode {A,C,HYBRID}`, `--start/--end YYYY-MM-DD` (UTC, end exclusive), `--workers`, `--out`, `--confirm`, plus `--product`, `--cache-dir`, `--random-runs` (compare, holdout), `--params` / `--params-from` / `--lock` (holdout). `--lock` defaults to `None`, so `run_holdout` uses `<project root>/holdout.lock` whatever the CWD. `holdout` without `--confirm` returns exit code 2 **before loading any data**; a `HoldoutError` (lock bound to a different config, off-grid params, aggressive profile, breakers off, `--random-runs 0`) is printed as `refusing: ...` with exit code 2. `walkforward` writes `<out>.json` containing `chosen_final`, `chosen_final_from_fold` and `chosen_final_fallback` and warns on stderr when `chosen_final` is a fallback from an earlier fold; `holdout --params-from` reads `chosen_final` and refuses a fallback one (pass `--params` explicitly to accept it).

- [ ] **Step 1: Write the failing test (report)**

`tests/backtest/test_report.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_report.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.backtest.report'`.

- [ ] **Step 3: Write minimal implementation (report)**

`src/ewbot/backtest/report.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_report.py -v`
Expected: 3 passed.

- [ ] **Step 5: Write the failing test (CLI)**

`tests/backtest/test_cli.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import ewbot
from ewbot.cli import build_parser, main
from tests.synth import random_bars

SRC = str(Path(ewbot.__file__).resolve().parents[1])  # works with or without an editable install


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-m", "ewbot.cli", *args], capture_output=True, text=True, timeout=120, env=env
    )


def test_help_lists_all_subcommands():
    r = _run("--help")
    assert r.returncode == 0
    for name in ("fetch", "backtest", "compare", "walkforward", "holdout"):
        assert name in r.stdout


def test_subcommand_help_flags():
    r = _run("compare", "--help")
    assert r.returncode == 0
    for flag in ("--config", "--profile", "--mode", "--start", "--end", "--workers", "--out", "--random-runs"):
        assert flag in r.stdout
    r = _run("holdout", "--help")
    assert r.returncode == 0 and "--confirm" in r.stdout and "--params" in r.stdout and "--lock" in r.stdout


def test_parser_defaults_and_types():
    ns = build_parser().parse_args(["compare", "--start", "2019-01-01", "--end", "2024-01-01", "--workers", "3"])
    assert (ns.cmd, ns.start, ns.end, ns.workers, ns.random_runs) == ("compare", "2019-01-01", "2024-01-01", 3, 100)
    ho = build_parser().parse_args(["holdout", "--params", "3.0,0.6,0.25"])
    assert ho.confirm is False and ho.lock is None  # run_holdout then uses <project root>/holdout.lock


def test_holdout_without_confirm_is_refused_before_touching_data(capsys, tmp_path):
    rc = main(["holdout", "--params", "3.0,0.6,0.25", "--cache-dir", str(tmp_path)])
    assert rc == 2
    assert "--confirm" in capsys.readouterr().err
    assert not (tmp_path / "BTC-USD_1h.parquet").exists()


def test_missing_cache_gives_clear_error(tmp_path):
    r = _run("backtest", "--cache-dir", str(tmp_path))
    assert r.returncode != 0 and "ewbot fetch" in r.stderr


def test_params_from_refuses_a_fallback_chosen_final(tmp_path):
    wf = tmp_path / "walkforward.json"
    wf.write_text(
        json.dumps({"chosen_final": [3.0, 0.6, 0.25], "chosen_final_from_fold": 7, "chosen_final_fallback": True}),
        encoding="utf-8",
    )
    lock = tmp_path / "holdout.lock"
    argv = ["holdout", "--params-from", str(wf), "--confirm", "--cache-dir", str(tmp_path), "--lock", str(lock)]
    with pytest.raises(SystemExit, match="fold 7"):
        main(argv)
    assert not lock.exists()  # refused before any data or lock is touched


def test_compare_end_to_end_on_synthetic_cache_writes_report(tmp_path, capsys):
    """Real stack through the CLI: parquet cache -> load_1h/fill_gaps -> compare -> HTML report."""
    bars = random_bars(seed=0, n=120 * 24, vol=30.0, start=20_000.0)  # 2023-11-15 .. 2024-03-14
    df = pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    df.drop(index=[1_500]).to_parquet(cache / "BTC-USD_1h.parquet", index=False)  # one missing hour
    out = tmp_path / "compare.html"
    argv = ["compare", "--cache-dir", str(cache), "--start", "2023-12-26", "--random-runs", "2", "--workers", "1"]
    assert main([*argv, "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "1 missing" in captured.err and f"report: {out}" in captured.out
    for name in ("A", "C", "HYBRID", "BH", "EMA"):
        assert any(line.startswith(name) for line in captured.out.splitlines()), name
    html = out.read_text(encoding="utf-8")
    for heading in ("<h2>Equity", "<h2>Drawdown", "<h2>Comparison", "<h2>Trades - A"):
        assert heading in html
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_cli.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ewbot.cli'`.

- [ ] **Step 7: Write minimal implementation (CLI)**

`src/ewbot/cli.py`:

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.holdout import HoldoutError, run_holdout
from ewbot.backtest.metrics import compute_metrics, reliability_by_q
from ewbot.backtest.report import to_jsonable, write_report
from ewbot.backtest.runner import breakdowns, compare, equity_pairs, run_backtest, utc_ts
from ewbot.backtest.walkforward import GRID, MIN_TRAIN_TRADES, walk_forward
from ewbot.config import AGGRESSIVE, CONSERVATIVE, BotConfig, load_config, with_overrides
from ewbot.data.candles import aggregate_4h, check_integrity, fill_gaps, load_1h
from ewbot.data.coinbase_history import update_cache
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
    common.add_argument("--end", default=None, help="UTC date YYYY-MM-DD, exclusive (default: after the last bar)")
    common.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parallel processes")
    common.add_argument("--out", type=Path, default=None, help="output HTML path (default: reports/<command>.html)")
    common.add_argument("--product", default="BTC-USD")
    common.add_argument("--cache-dir", type=Path, default=Path("data/cache"))

    sub.add_parser("fetch", parents=[common], help="download/update the Coinbase 1h candle cache")
    sub.add_parser("backtest", parents=[common], help="one backtest of --mode over --start/--end")
    p_cmp = sub.add_parser("compare", parents=[common], help="A, C, HYBRID vs buy&hold, EMA 50/200 and random entries")
    p_cmp.add_argument("--random-runs", type=int, default=100)
    sub.add_parser("walkforward", parents=[common], help="60-combo walk-forward on 2017-01..2023-12")
    p_ho = sub.add_parser("holdout", parents=[common], help="ONE-SHOT evaluation on 2024-01-01..end")
    p_ho.add_argument("--random-runs", type=int, default=100)
    p_ho.add_argument("--params", default=None, help="K4,Q_MIN,S_MIN e.g. 3.0,0.6,0.25")
    p_ho.add_argument("--params-from", type=Path, default=None, help="reports/walkforward.json (uses chosen_final)")
    p_ho.add_argument("--lock", type=Path, default=None, help="lock file (default: <project root>/holdout.lock)")
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")
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


def _load_df(args: argparse.Namespace) -> pd.DataFrame:
    path = Path(args.cache_dir) / f"{args.product}_1h.parquet"
    if not path.exists():
        raise SystemExit(f"candle cache not found: {path} - run `ewbot fetch` first")
    raw = load_1h(path)
    rep = check_integrity(raw)
    print(f"[data] {rep.n} bars, {len(rep.missing_ts)} missing, {len(rep.duplicate_ts)} duplicate", file=sys.stderr)
    return fill_gaps(raw)


def _range(args: argparse.Namespace, df: pd.DataFrame) -> tuple[int, int]:
    start = utc_ts(args.start) if args.start else int(df["ts"].iloc[0])
    end = utc_ts(args.end) if args.end else int(df["ts"].iloc[-1]) + H1
    if end <= start:
        raise SystemExit("--end must be after --start")
    return start, end


def _out(args: argparse.Namespace) -> Path:
    return args.out if args.out is not None else Path("reports") / f"{args.cmd}.html"


def _candles4h(df: pd.DataFrame) -> dict[str, list]:
    d4 = aggregate_4h(df)
    return {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}


def _print_comparison(comp: dict[str, dict[str, Any]]) -> None:
    cols = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")
    print(f"{'':8}" + "".join(f"{c:>13}" for c in cols))
    for name, m in comp.items():
        print(f"{name:8}" + "".join(f"{m[c]:>13.3f}" if c != "n_trades" else f"{m[c]:>13d}" for c in cols))


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


def _parse_params(args: argparse.Namespace) -> tuple[float, ...]:
    if args.params:
        vals = tuple(float(x) for x in args.params.split(","))
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
    else:
        raise SystemExit("holdout needs --params K4,Q_MIN,S_MIN or --params-from reports/walkforward.json")
    if len(vals) != len(GRID):
        raise SystemExit(f"expected {len(GRID)} params for {list(GRID)}, got {len(vals)}")
    return vals


def cmd_holdout(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the holdout is single-use. Re-run with --confirm once you are done tuning.", file=sys.stderr)
        return 2
    params = _parse_params(args)
    cfg, df = _load_cfg(args), _load_df(args)
    try:
        out = run_holdout(cfg, df, params, lock_path=args.lock, confirm=True, random_runs=args.random_runs, workers=args.workers)
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
    return 0


_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
```

Make sure the console script exists (Part A may already have added it):

```bash
grep -n 'ewbot = "ewbot.cli:main"' pyproject.toml || echo MISSING
```

If `MISSING`, add this table to `pyproject.toml` and re-sync:

```toml
[project.scripts]
ewbot = "ewbot.cli:main"
```

```bash
uv sync
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_report.py tests/backtest/test_cli.py -v && uv run ewbot --help`
Expected: 10 passed (the last CLI test runs `ewbot compare` end to end on a synthetic parquet cache and checks the HTML report, about 2 s); the help text lists `fetch, backtest, compare, walkforward, holdout`.

- [ ] **Step 9: Commit**
```bash
git add src/ewbot/backtest/report.py src/ewbot/cli.py tests/backtest/test_report.py tests/backtest/test_cli.py pyproject.toml
git commit -m "feat(backtest): HTML report with per-trade snapshot charts and ewbot CLI"
```

---

### Task 23: End-to-end smoke on real data and performance check

**Files:**
- Create: `scripts/time_commands.py`, `tests/backtest/test_e2e_real.py`
- Create (by running the script): `docs/backtest-runtime.md`
- Modify: `.gitignore` (add `reports/`)

**Interfaces:**
- Consumes: the whole CLI (T22), the real wave engine and strategies (Parts B1/B2/C1), the Coinbase cache (Part A).
- Produces: a verified real-data cache, a timing log (`docs/backtest-runtime.md`), and a first comparison report. **Do not run `ewbot holdout` in this task** (it is single-use, and parameters have not been selected yet).

`scripts/time_commands.py` runs a command, appends `| when | label | seconds | budget | PASS/FAIL | exit | machine |` to `docs/backtest-runtime.md`, and exits non-zero when a `--budget` is exceeded. The e2e test is skipped until the real cache exists; once it does, it runs `compare()` on Q1 2019 with a 3-month warm-up slice and checks that every metric is finite and buy&hold is a single spot trade whose return equals the raw price multiple minus two fees and two slippages.

- [ ] **Step 1: Write the failing test and the timing helper**

`tests/backtest/test_e2e_real.py`:

```python
"""End-to-end smoke on the REAL Coinbase cache (skipped until `ewbot fetch` has been run)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from ewbot.backtest.runner import compare, utc_ts
from ewbot.config import BotConfig
from ewbot.data.candles import fill_gaps, load_1h

CACHE = Path("data/cache/BTC-USD_1h.parquet")

pytestmark = pytest.mark.skipif(not CACHE.exists(), reason="no real data: run `uv run ewbot fetch`")


def test_compare_runs_on_three_months_of_real_data():
    start, end = utc_ts("2019-01-01"), utc_ts("2019-04-01")
    df = fill_gaps(load_1h(CACHE))
    df = df[(df["ts"] >= utc_ts("2018-10-01")) & (df["ts"] < end)].reset_index(drop=True)  # 3 months warm-up
    assert len(df) > 24 * 150
    cmp = compare(BotConfig(), df, start, end, random_runs=2, workers=1)
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    for name, m in cmp["comparison"].items():
        assert all(math.isfinite(float(v)) for v in m.values()), name
        assert 0.0 <= m["max_dd"] <= 1.0
    bh = cmp["comparison"]["BH"]
    assert bh["n_trades"] == 1 and bh["exposure"] == 1.0
    assert -0.3 < bh["total_return"] < 0.6  # BTC went ~3.7k -> ~4.1k over Q1 2019 (about +10%)
    # spot hold (T19): raw price multiple minus two fees and two slippages, no funding
    c = BotConfig().costs
    w = df[(df["ts"] >= start) & (df["ts"] < end)]
    multiple = float(w["close"].iloc[-1]) / float(w["open"].iloc[1])
    spot = multiple * ((1 - c.slippage) / (1 + c.slippage)) * ((1 - c.fee_rate) / (1 + c.fee_rate)) - 1
    assert bh["total_return"] == pytest.approx(spot, rel=1e-9)
    assert cmp["random"]["runs"] in (0, 2)
```

`scripts/time_commands.py`:

```python
"""Run a command, time it, append a line to docs/backtest-runtime.md, optionally enforce a budget.

Usage: uv run python scripts/time_commands.py "<label>" [--budget SECONDS] -- <command...>
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

LOG = Path("docs/backtest-runtime.md")
HEADER = (
    "# Backtest runtime log\n\n"
    "| when (UTC) | label | seconds | budget | result | exit | machine |\n"
    "|---|---|---|---|---|---|---|\n"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label")
    ap.add_argument("--budget", type=float, default=None, help="seconds; mark FAIL when exceeded")
    argv = sys.argv[1:]
    if "--" not in argv:
        ap.error("missing `-- <command...>`")
    split = argv.index("--")
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1 :]
    if not cmd:
        ap.error("missing command after --")

    t0 = time.perf_counter()
    rc = subprocess.run(cmd).returncode
    secs = time.perf_counter() - t0

    verdict = "n/a" if args.budget is None else ("PASS" if secs <= args.budget and rc == 0 else "FAIL")
    machine = f"{platform.machine()} {os.cpu_count()} cpu {platform.system()}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not LOG.exists():
        LOG.write_text(HEADER, encoding="utf-8")
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    budget = "-" if args.budget is None else f"{args.budget:g}"
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"| {when} | {args.label} | {secs:.1f} | {budget} | {verdict} | {rc} | {machine} |\n")
    print(f"[time] {args.label}: {secs:.1f}s ({verdict})", file=sys.stderr)
    return rc if rc != 0 else (1 if verdict == "FAIL" else 0)


if __name__ == "__main__":
    sys.exit(main())
```

```bash
grep -qx 'reports/' .gitignore 2>/dev/null || echo 'reports/' >> .gitignore
```

- [ ] **Step 2: Run test to verify it fails (skips)**

Run: `uv run pytest tests/backtest/test_e2e_real.py -v`
Expected: `1 skipped` with reason `no real data: run uv run ewbot fetch`. That is the intended "not yet green" state; the test turns into a real pass/fail after Step 3.

- [ ] **Step 3: Fetch the real data**

```bash
uv run python scripts/time_commands.py "fetch BTC-USD 1h" -- uv run ewbot fetch
```

Expected output (about 1-4 minutes; ~300 candles per request, 0.15 s sleep):
```
cache: data/cache/BTC-USD_1h.parquet
bars: ~85000  first_ts: 1483228800  last_ts: <within the last hours>
missing: <small, 0 to a few dozen>  duplicate: 0  nonpositive: 0
```
`first_ts` must be `1483228800` (2017-01-01 00:00 UTC), `bars` roughly `8760 * years since 2017` (about 85,000 in autumn 2026), `duplicate` and `nonpositive` must be 0. A few missing hours are normal for Coinbase history; they are gap-filled with `synthetic=True` bars at load time. If `missing` is in the hundreds, stop and inspect `check_integrity` output before trusting any result.

- [ ] **Step 4: Run the end-to-end test**

Run: `uv run pytest tests/backtest/test_e2e_real.py -v`
Expected: 1 passed (a few seconds: five engine runs plus two random runs over roughly 6 months of bars). A failure here is a real integration bug between the parts (exception from the engine, NaN metric, key mismatch in `Trade.snapshot`), not a test problem; fix it at the source before continuing.

- [ ] **Step 5: Performance budget for one full backtest (2017-2023)**

Budget: **one backtest of mode A over 2017-01-01 .. 2024-01-01 takes less than 180 seconds** on the development Mac (this is what makes the 60-combo walk-forward feasible).

```bash
uv run python scripts/time_commands.py "backtest A 2017-2023" --budget 180 -- \
  uv run ewbot backtest --mode A --start 2017-01-01 --end 2024-01-01 --out reports/a_2017_2023.html
```

Expected output: a one-row metrics table for `A` (`total_return, cagr, max_dd, sharpe, calmar, win_rate, avg_r, n_trades`), then `backtest A: <N> trades in <S>s`, `report: reports/a_2017_2023.html`, and `[time] backtest A 2017-2023: <S>s (PASS)`. Sanity: `N` should be in the tens to a few hundreds (a handful of setups per year over 7 years); `N = 0` means a gate or wiring problem (check `q_top`/`s_rev` in the engine's `signals`, the `entry_blackout` handling, and that `fill_gaps` was applied), not "no edge".

If the line says `FAIL` (more than 180 s), profile before changing anything:

```bash
uv run python -m cProfile -s cumtime -m ewbot.cli backtest --mode A --start 2017-01-01 --end 2019-01-01 --out reports/prof.html 2>&1 | head -60
```

Read the top of the `cumtime` column. **First suspect: the incremental-enumeration cache in `src/ewbot/waves/engine.py`.** `enumerate_counts` must run only when a 4h bar closes AND a new 4h pivot was confirmed (otherwise cached counts are re-scored with the new `E_live` only); if `enumerate_counts` / `fit_q` show up with a call count close to the number of 4h bars (or, worse, 1h bars) instead of the number of confirmed pivots, the cache is not being hit (typically a cache key that includes `E` or the bar index). Next suspects, in order: per-bar `df.iloc` / `to_bars` conversions in `TradingEngine.run` (convert once, iterate a list), building the snapshot dict on every bar instead of only when a signal is accepted, and `EquityPoint` / dataclass creation overhead. Fix the top item, re-run the timing command, repeat until `PASS`. Record the profile finding as a one-line comment in the commit message.

- [ ] **Step 6: Smoke comparison, then the full comparison (2019-2023)**

Smoke first (5 random runs) to see the whole pipeline work end to end:

```bash
uv run python scripts/time_commands.py "compare 2019-2023 random=5" -- \
  uv run ewbot compare --start 2019-01-01 --end 2024-01-01 --workers 4 --random-runs 5 --out reports/compare_smoke.html
```

Then the real one (100 random runs, as specified):

```bash
uv run python scripts/time_commands.py "compare 2019-2023 random=100" -- \
  uv run ewbot compare --start 2019-01-01 --end 2024-01-01 --workers 6 --random-runs 100 --out reports/compare.html
```

Expected stdout for both: a six-line table with rows `A, C, HYBRID, BH, EMA` (columns `total_return cagr max_dd sharpe calmar win_rate avg_r n_trades`), then `random control: <runs> runs, <n> entries each; compare took <S>s` (`<n>` equals the number of A trades in 2019-2023), then `report: reports/compare.html`. Runtime model: the compare performs `3 + random_runs` engine runs, each feeding all bars from 2017 to 2024, so wall time is about `T_single * (1 + random_runs / workers)` where `T_single` is the Step 5 time; with `T_single = 120 s`, 6 workers and 100 runs expect about 35 minutes. If that is too slow for a first look, use `--random-runs 30` and note it in the log, but run the full 100 before drawing conclusions about A versus the random control.

- [ ] **Step 7: Open and inspect the report**

```bash
open reports/compare.html
```

Checklist (all must hold; the page needs internet access for the plotly CDN):
1. **Equity** chart shows five lines (A, C, HYBRID, BH, EMA) starting at 10,000 on 2019-01-01 on a log axis; **Drawdown** chart below shows the same five names, all at or below 0%.
2. **Comparison** table has rows `A, C, HYBRID, BH, EMA` plus a sixth row `RANDOM x100 (mean [p5, p95])`, with an `Avg R 95% CI` column for the five strategies, and a muted line `Share of random runs beaten by each strategy` underneath.
3. **Buy&hold sanity band** (not a target): BH is a **spot** hold (fractional BTC, no leverage, no funding; T19). It buys at the open of the window's second bar (2019-01-01 01:00 UTC, about 3,692) and sells at the last close (2023-12-31 23:00 UTC, about 42,288), a raw multiple of about 11.45x, and pays only two fees and two slippages: `BH total_return = multiple · ((1−s)/(1+s)) · ((1−f)/(1+f)) − 1 ≈ 11.45 · 0.998 − 1` ≈ **+1,040%** (measured on the Coinbase cache: +1,043.1%), and `max_dd` ≈ **77%** (69,000 in 2021-11 down to about 15,500 in 2022-11). A BH return more than a few tenths of a percent below the raw multiple means funding or extra fees are being charged; a return above the raw multiple means the entry is not filled at the next open. `BH n_trades = 1`, `exposure = 100%`.
4. **EMA** row: `n_trades` in the tens (a 4h 50/200 cross flips a few times per year); `exposure` is high (always in the market once warmed up).
5. **Per setup** table lists only setups that actually traded (some of `W3, W5, W5_REV, C_END`; `FIB` appears under `C`, mixed setups under `HYBRID`), each with trades, win rate, avg R and an `Avg R 95% CI`. **Long / short** and **Per year** (2019 .. 2023) tables exist for A, C and HYBRID.
6. **Trades - A** is collapsed under `show table`; open it, open one row's `chart`: a line of 4h closes with a dotted labelled polyline (S,1,2,...) for the top count, a triangle at the entry and dashed horizontal lines for stop/TP1/TP2. No console errors in the browser.
7. Nothing in the page says `NaN`, `Infinity` or `undefined`.

If an item fails, fix the cause (report code in T22, metric definitions in T18, simulator in T19) before recording numbers.

- [ ] **Step 8: Record the runtime and commit**

`docs/backtest-runtime.md` was appended automatically by `time_commands.py` (rows for fetch, the single A backtest, the smoke compare and the full compare). Append the derived walk-forward estimate (60 combos x the Step 5 time, divided by the worker count):

```bash
uv run python - <<'PYEOF'
import os
from pathlib import Path

log = Path("docs/backtest-runtime.md")
rows = [ln.split("|") for ln in log.read_text().splitlines() if "backtest A 2017-2023" in ln]
t_single = float(rows[-1][3])
workers = max(1, (os.cpu_count() or 2) - 1)
minutes = 60 * t_single / workers / 60
with log.open("a") as fh:
    fh.write(f"\nWalk-forward estimate: 60 combos x {t_single:.0f}s / {workers} workers = about {minutes:.0f} minutes (plus a few seconds of stitching).\n")
print(f"estimated walk-forward: {minutes:.0f} min")
PYEOF
```

Do **not** run `ewbot walkforward` or `ewbot holdout` as part of Plan 1's smoke test: the walk-forward is run deliberately once the strategy code is frozen, and `holdout` exactly once after that.


```bash
git add scripts/time_commands.py tests/backtest/test_e2e_real.py docs/backtest-runtime.md .gitignore
git commit -m "test(backtest): real-data e2e smoke, timing helper and runtime log"
```


---

