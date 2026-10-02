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
