from __future__ import annotations

import pytest

from tests.paperb.helpers import build_passed_lock


@pytest.fixture(scope="session")
def lock_text(tmp_path_factory) -> str:
    """holdout-b.lock after one real synthetic holdout run (computed once per session)."""
    return build_passed_lock(tmp_path_factory.mktemp("paperb-lock"))
