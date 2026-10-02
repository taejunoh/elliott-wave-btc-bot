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
