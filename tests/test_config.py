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
