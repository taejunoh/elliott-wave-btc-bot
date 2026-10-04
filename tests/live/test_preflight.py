from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from ewbot.live.exchange import KeyPermissions, Unavailable
from ewbot.live.preflight import (
    BASELINE_FINGERPRINT,
    LOCK_FILE_SHA256,
    LOCK_SHA256,
    PreflightError,
    check_fingerprint,
    check_key,
    check_lock,
    check_product,
    require,
    run_preflight,
)
from ewbot.live.settings import SettingsError, load_settings, parse_expiry
from ewbot.live.sim import BIP_SPEC
from ewbot.paper.fingerprint import tree_files
from tests.live.helpers import Clock, sim
from tests.paper.helpers import make_lock, synth_frame


@pytest.fixture(scope="module")
def lock_file(tmp_path_factory) -> tuple[Path, dict]:
    lock = make_lock(synth_frame())
    path = tmp_path_factory.mktemp("lock") / "holdout.lock"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path, lock


def file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_working_tree_fingerprint_is_the_pinned_baseline():
    c = check_fingerprint()
    assert c.ok and BASELINE_FINGERPRINT[:12] in c.detail and "25 files" in c.detail


def test_a_changed_or_missing_strategy_file_is_refused():
    files = tree_files()
    changed = dict(files, **{"src/ewbot/risk/sizing.py": files["src/ewbot/risk/sizing.py"] + b"\n"})
    assert not check_fingerprint(changed).ok
    assert not check_fingerprint({}).ok  # e.g. a non-editable install: src/ewbot not found


def test_lock_checks(lock_file, tmp_path):
    path, lock = lock_file
    ok, parsed = check_lock(path, expected=lock["sha256"], expected_file=file_sha(path))
    assert ok.ok and parsed["params"] == list(lock["params"])
    wrong, none = check_lock(path, expected="0" * 64, expected_file=file_sha(path))
    assert not wrong.ok and none is None and "committed" in wrong.detail
    bad = dict(lock, params=[9.0, 9.0, 9.0])
    (tmp_path / "bad.lock").write_text(json.dumps(bad), encoding="utf-8")
    c, _ = check_lock(tmp_path / "bad.lock", expected=lock["sha256"], expected_file=file_sha(tmp_path / "bad.lock"))
    assert not c.ok and "grid combo" in c.detail and "lock_digest" in c.detail
    assert not check_lock(tmp_path / "missing.lock")[0].ok


def test_the_lock_file_itself_is_pinned_not_only_its_digest_field(lock_file, tmp_path):
    path, lock = lock_file
    assert len(LOCK_FILE_SHA256) == 64 and LOCK_FILE_SHA256 != LOCK_SHA256
    # same sha256 field, same params and digest inputs, but another byte somewhere else in the file
    edited = dict(lock, runs=[{"note": "edited after the holdout"}])
    (tmp_path / "edited.lock").write_text(json.dumps(edited), encoding="utf-8")
    c, parsed = check_lock(tmp_path / "edited.lock", expected=lock["sha256"], expected_file=file_sha(path))
    assert not c.ok and parsed is None and "file sha256" in c.detail
    assert check_lock(tmp_path / "edited.lock", expected=lock["sha256"],
                      expected_file=file_sha(tmp_path / "edited.lock"))[0].ok
    # the default pins the committed file: a synthetic lock is refused by it
    d, none = check_lock(path, expected=lock["sha256"])
    assert not d.ok and none is None and LOCK_FILE_SHA256[:12] in d.detail


def test_key_permissions():
    assert check_key(KeyPermissions(True, False, False), "SHADOW").ok
    assert not check_key(KeyPermissions(True, False, False), "CANARY").ok
    assert check_key(KeyPermissions(True, True, False), "FULL").ok
    c = check_key(KeyPermissions(True, True, True), "SHADOW")
    assert not c.ok and "TRANSFER" in c.detail
    assert not check_key(KeyPermissions(False, True, False), "SHADOW").ok


def test_product_spec():
    assert check_product(BIP_SPEC, "BIP-20DEC30-CDE").ok
    assert not check_product(dataclasses.replace(BIP_SPEC, contract_size=0.1), "BIP-20DEC30-CDE").ok
    assert not check_product(dataclasses.replace(BIP_SPEC, price_increment=1.0), "BIP-20DEC30-CDE").ok
    assert not check_product(BIP_SPEC, "BIT-31OCT26-CDE").ok


def test_run_preflight_online_offline_and_exchange_errors(lock_file):
    path, lock = lock_file
    ex = sim(Clock())
    checks, parsed = run_preflight(ex, "CANARY", path, expected_lock=lock["sha256"], expected_lock_file=file_sha(path))
    assert [c.name for c in checks] == ["fingerprint", "lock", "key", "product"] and all(c.ok for c in checks)
    require(checks)
    offline, _ = run_preflight(None, "SHADOW", path, expected_lock=lock["sha256"], expected_lock_file=file_sha(path))
    assert [c.name for c in offline] == ["fingerprint", "lock"]
    ex.fail_next("key_permissions", Unavailable("503"))
    failing, _ = run_preflight(ex, "SHADOW", path, expected_lock=lock["sha256"], expected_lock_file=file_sha(path))
    with pytest.raises(PreflightError, match="key_permissions failed"):
        require(failing)
    transfer = sim(Clock(), permissions=KeyPermissions(True, True, True))
    with pytest.raises(PreflightError, match="TRANSFER"):
        require(run_preflight(transfer, "SHADOW", path, expected_lock=lock["sha256"], expected_lock_file=file_sha(path))[0])


def test_settings_read_secrets_only_from_private_files(tmp_path):
    secrets = tmp_path / "live.env"
    secrets.write_text("# ewbot\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID='42'\nEWBOT_GITHUB_TOKEN=\"ghp_x\"\n",
                       encoding="utf-8")
    key = tmp_path / "cdp_api_key.json"
    key.write_text('{"name": "organizations/o/apiKeys/k", "privateKey": "-----BEGIN EC PRIVATE KEY-----"}')
    secrets.chmod(0o600)
    key.chmod(0o600)
    env = {"EWBOT_SECRETS_FILE": str(secrets), "EWBOT_COINBASE_KEY_FILE": str(key), "EWBOT_LIVE_DIR": str(tmp_path),
           "TELEGRAM_CHAT_ID": "7", "EWBOT_PAPER_REPO": "me/repo", "EWBOT_LIVE_EDIT_STOPS": "1",
           "EWBOT_EXPIRY": "coinbase=2027-10-01, github=2027-09-30", "EWBOT_HEALTHCHECK_URL": "https://hc-ping.com/u-1"}
    s = load_settings(env)
    assert (s.telegram_token, s.telegram_chat, s.github_token) == ("123:abc", "42", "ghp_x")  # the file wins
    assert s.healthcheck_url == "https://hc-ping.com/u-1"
    assert not any(secret in repr(s) for secret in ("123:abc", "ghp_x", "hc-ping.com/u-1", str(key)))
    assert s.key_file == key and s.journal_path == tmp_path / "journal.sqlite" and s.cache_dir == tmp_path / "cache"
    assert s.paper_repo == "me/repo" and s.edit_stops and s.futures_id == "BIP-20DEC30-CDE"
    assert s.expiry == {"coinbase": "2027-10-01", "github": "2027-09-30"}
    secrets.chmod(0o644)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    secrets.chmod(0o600)
    key.chmod(0o640)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    defaults = load_settings({})
    assert defaults.data_dir == Path("/data") and defaults.key_file is None and not defaults.edit_stops
    with pytest.raises(SettingsError):
        parse_expiry("coinbase:2027-10-01")


def test_telegram_commands_come_only_from_a_private_chat(tmp_path):
    secrets = tmp_path / "live.env"
    env = {"EWBOT_SECRETS_FILE": str(secrets)}
    for chat in ("-1001234567890", "-42", "@ewbot_alerts", "0", "42a"):  # a channel or group id, a name
        secrets.write_text(f"TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8")
        secrets.chmod(0o600)
        with pytest.raises(SettingsError, match="private chat"):
            load_settings(env)
    secrets.write_text("TELEGRAM_CHAT_ID=0042\n", encoding="utf-8")
    assert load_settings(env).telegram_chat == "42"  # as Telegram writes the chat id
