"""Strategy-B parameters (Plan 4 spec 4-5): the 32-combo grid, the fixed values and the run config.

Separate from ewbot.config (strategy A): A's RiskProfile / BreakerParams / CostParams types are only
imported, never changed. Combos are tuples in GRID_B key order: (zz_k, (z_lo, z_hi), tp1_r, ext, remainder)."""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import Any, Sequence

from ewbot.config import BreakerParams, CostParams, RiskProfile

M15 = 900  # one 15-minute bar, seconds

GRID_B: dict[str, tuple[Any, ...]] = {
    "zz_k": (2.0, 3.0),
    "zone": ((0.382, 0.618), (0.5, 0.786)),
    "tp1_r": (1.0, 1.5),
    "ext": (1.272, 1.618),
    "remainder": ("breakeven", "original"),
}
REMAINDER_MODES = ("breakeven", "original")

Combo = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class StratBParams:
    """Grid values (defaults = the first grid combo) and the fixed values of spec 5, which are not
    in the grid. confluence_lookback_hours is the plan's resolution of spec 4.2 ("swing highs or lows
    before L"): only the 1h pivots of the 30 days before L count."""

    zz_k: float = 2.0
    zone: tuple[float, float] = (0.382, 0.618)
    tp1_r: float = 1.0
    ext: float = 1.272
    remainder: str = "breakeven"
    rsi_len: int = 14
    pivot_w: int = 3
    confluence_atr: float = 0.25
    stop_buf_atr: float = 0.2
    max_stop_atr1h: float = 2.5
    confirm_hours: int = 72
    time_stop_hours: int = 48
    atr_len: int = 14
    confluence_lookback_hours: int = 720

    def __post_init__(self) -> None:
        if self.remainder not in REMAINDER_MODES:
            raise ValueError(f"remainder must be one of {REMAINDER_MODES}, got {self.remainder!r}")
        if not 0.0 < self.zone[0] < self.zone[1] < 1.0:
            raise ValueError(f"zone must satisfy 0 < z_lo < z_hi < 1, got {self.zone}")


RISK_B = RiskProfile("stratb", 0.005, 3.0)  # spec 4.4: 0.5% of equity per trade, leverage cap 3x


@dataclass(frozen=True, slots=True)
class BConfig:
    """Everything a strategy-B run depends on. Breakers, costs and the initial equity are A's values
    (spec 4.4, 6.2): daily -3%, total -15%, 5 losses -> 24h pause; fee 0.05%, slippage 0.05%, funding
    10% APR; 10,000 USD; contracts of 0.01 BTC."""

    params: StratBParams = StratBParams()
    risk: RiskProfile = RISK_B
    breakers: BreakerParams = BreakerParams()
    costs: CostParams = CostParams()
    initial_equity: float = 10_000.0
    tp1_frac: float = 0.5


def grid_combos() -> list[Combo]:
    """The 2 x 2 x 2 x 2 x 2 = 32 combos in GRID_B key order."""
    return list(itertools.product(*GRID_B.values()))


def normalize_combo(values: Sequence[Any]) -> Combo:
    """A combo from JSON or the CLI (the zone may be a list) -> the exact grid tuple. ValueError when the
    values are not one of the 32 combos."""
    if len(values) != len(GRID_B):
        raise ValueError(f"a combo has {len(GRID_B)} values {list(GRID_B)}, got {list(values)}")
    zz, zone, tp1, ext, rem = values
    try:
        combo = (float(zz), (float(zone[0]), float(zone[1])), float(tp1), float(ext), str(rem))
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"malformed combo {list(values)}: {exc}") from exc
    if combo not in grid_combos():
        raise ValueError(f"{list(values)} is not a combo of the strategy-B grid {GRID_B}")
    return combo


def combo_to_json(combo: Sequence[Any]) -> list[Any]:
    c = normalize_combo(combo)
    return [c[0], [c[1][0], c[1][1]], c[2], c[3], c[4]]


def params_for(combo: Sequence[Any]) -> StratBParams:
    return StratBParams(**dict(zip(GRID_B, normalize_combo(combo))))


def combo_of(p: StratBParams) -> Combo:
    return tuple(getattr(p, k) for k in GRID_B)


def config_for(combo: Sequence[Any], *, breakers_enabled: bool = True) -> BConfig:
    """The run config of a grid combo. Walk-forward grid runs pass breakers_enabled=False (as A's)."""
    return BConfig(params=params_for(combo), breakers=dataclasses.replace(BreakerParams(), enabled=breakers_enabled))


def scale_costs(c: CostParams, mult: float) -> CostParams:
    """Fees (rate and per-contract minimum) and slippage times `mult`; funding unchanged (spec 6.5 item 4)."""
    return dataclasses.replace(
        c, fee_rate=c.fee_rate * mult, fee_min_per_contract=c.fee_min_per_contract * mult, slippage=c.slippage * mult
    )
