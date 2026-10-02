from __future__ import annotations

import hashlib
import platform

import numpy as np
import pandas as pd
import pyarrow

from ewbot.paper.environment import environment_digest, runtime_environment


def test_runtime_environment_records_versions_lock_hash_and_runner_image(tmp_path):
    (tmp_path / "uv.lock").write_bytes(b"version = 1\n")
    env = runtime_environment(tmp_path, {"ImageOS": "ubuntu24", "ImageVersion": "20261001.1.0", "OTHER": "x"})
    assert env == {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "pyarrow": pyarrow.__version__,
        "uv_lock_sha256": hashlib.sha256(b"version = 1\n").hexdigest(),
        "image_os": "ubuntu24",
        "image_version": "20261001.1.0",
    }


def test_runtime_environment_off_actions_and_without_a_lock_file(tmp_path):
    env = runtime_environment(tmp_path, {})
    assert env["image_os"] is None and env["image_version"] is None and env["uv_lock_sha256"] is None
    assert runtime_environment()["uv_lock_sha256"] is not None  # the repository's own uv.lock


def test_environment_digest_is_order_independent_and_sensitive_to_every_compared_value():
    a = {"python": "3.12.7", "numpy": "2.1.0", "image_os": None}
    assert environment_digest(a) == environment_digest(dict(reversed(list(a.items()))))
    assert len(environment_digest(a)) == 64
    for key, value in (("python", "3.12.8"), ("numpy", "2.1.1"), ("image_os", "ubuntu24")):
        assert environment_digest({**a, key: value}) != environment_digest(a), key


def test_environment_digest_ignores_the_runner_image_version_but_not_the_image_os():
    a = {"python": "3.12.13", "numpy": "2.1.0", "image_os": "ubuntu24", "image_version": "20261001.1.0"}
    assert environment_digest({**a, "image_version": "20261008.1.0"}) == environment_digest(a)
    assert environment_digest({k: v for k, v in a.items() if k != "image_version"}) == environment_digest(a)
    assert environment_digest({**a, "numpy": "2.1.1"}) != environment_digest(a)
    assert environment_digest({**a, "image_os": "ubuntu22"}) != environment_digest(a)
