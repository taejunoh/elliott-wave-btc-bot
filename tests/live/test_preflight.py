from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import pickle
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.live.exchange import FUTURES_PRODUCT, KeyPermissions, Unavailable
from ewbot.live.journal import KNOWN_STAGES
from ewbot.live.preflight import (
    BASELINE_FINGERPRINT,
    LOCK_FILE_SHA256,
    LOCK_SHA256,
    PRODUCT_EXPIRY,
    Check,
    PreflightError,
    check_alerts,
    check_fingerprint,
    check_key,
    check_lock,
    check_product,
    require,
    run_preflight,
)
from ewbot.live.settings import LiveSettings, Secret, SettingsError, load_settings, parse_expiry, read_env_file
from ewbot.live.sim import BIP_SPEC
from ewbot.live.stage import STAGES
from ewbot.paper.fingerprint import tree_files
from tests.live.helpers import Clock, sim
from tests.paper.helpers import make_lock, synth_frame


NOW = datetime(2026, 10, 4, tzinfo=UTC).timestamp()


@pytest.fixture(scope="module")
def lock_file(tmp_path_factory) -> tuple[Path, dict]:
    lock = make_lock(synth_frame())
    path = tmp_path_factory.mktemp("lock") / "holdout.lock"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path, lock


def file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_lock(path: Path, lock: dict) -> Path:
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


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
    bad = write_lock(tmp_path / "bad.lock", dict(lock, params=[9.0, 9.0, 9.0]))
    c, _ = check_lock(bad, expected=lock["sha256"], expected_file=file_sha(bad))
    assert not c.ok and "grid combo" in c.detail and "lock_digest" in c.detail
    # the digest does not cover the lock's own holdout_start: it is checked against HOLDOUT_START by itself
    late = write_lock(tmp_path / "late.lock", dict(lock, holdout_start=lock["holdout_start"] + 3600))
    c, none = check_lock(late, expected=lock["sha256"], expected_file=file_sha(late))
    assert not c.ok and none is None and c.detail == f"holdout_start {lock['holdout_start'] + 3600} != {HOLDOUT_START}"
    assert not check_lock(tmp_path / "missing.lock")[0].ok


def test_the_lock_file_itself_is_pinned_and_checked_first(lock_file, tmp_path):
    path, lock = lock_file
    assert len(LOCK_FILE_SHA256) == 64 and LOCK_FILE_SHA256 != LOCK_SHA256
    # same sha256 field, same params and digest inputs, but another byte somewhere else in the file
    edited = write_lock(tmp_path / "edited.lock", dict(lock, runs=[{"note": "edited after the holdout"}]))
    c, parsed = check_lock(edited, expected=lock["sha256"], expected_file=file_sha(path))
    assert not c.ok and parsed is None and c.detail == f"file sha256 {file_sha(edited)[:12]} != committed {file_sha(path)[:12]}"
    assert check_lock(edited, expected=lock["sha256"], expected_file=file_sha(edited))[0].ok
    # the same content with CRLF line ends, a BOM or re-serialised is another file: refused by the pin, not parsed
    pretty = tmp_path / "pretty.lock"
    pretty.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    raw = pretty.read_bytes()
    for name, data in (("crlf", raw.replace(b"\n", b"\r\n")), ("bom", b"\xef\xbb\xbf" + raw),
                       ("compact", json.dumps(lock).encode())):
        copy = tmp_path / f"{name}.lock"
        copy.write_bytes(data)
        c, parsed = check_lock(copy, expected=lock["sha256"], expected_file=file_sha(pretty))
        assert not c.ok and parsed is None and c.detail.startswith("file sha256 ") and "!= committed" in c.detail, name
    # the default pins the committed file: a synthetic lock is refused by it
    d, none = check_lock(path, expected=lock["sha256"])
    assert not d.ok and none is None and LOCK_FILE_SHA256[:12] in d.detail


def test_the_committed_lock_passes_the_production_pins():
    """The one test that reads <repo>/holdout.lock (read-only, never written): a wrong pin or a check that refuses
    the real lock fails here, not first in deploy.sh on the VPS."""
    assert hashlib.sha256(DEFAULT_LOCK_PATH.read_bytes()).hexdigest() == LOCK_FILE_SHA256
    c, lock = check_lock()
    assert c.ok and lock is not None and lock["sha256"] == LOCK_SHA256 and lock["params"] == [2.0, 0.7, 0.5]
    assert c.detail == "file sha256 6530175541c1, sha256 d1be04baa06c, params [2.0, 0.7, 0.5]"


def test_a_malformed_lock_is_a_failing_check_never_an_exception(tmp_path):
    good = '"params": [2.0, 0.7, 0.5], "random_runs": 100, "code_sha256": "x", "holdout_start": 1704067200, "sha256": "x"'
    cases = {
        "random_runs Infinity": good.replace('"random_runs": 100', '"random_runs": Infinity'),
        "a NaN param": good.replace("[2.0,", "[NaN,"),
        "params a dict": good.replace("[2.0, 0.7, 0.5]", '{"k4": 2.0}'),
        "params a string": good.replace("[2.0, 0.7, 0.5]", '"2.0"'),
        "params booleans": good.replace("[2.0, 0.7, 0.5]", "[true, false, true]"),
        "a param too large for a float": good.replace("[2.0,", "[" + "9" * 400 + ","),
        "random_runs a float": good.replace('"random_runs": 100', '"random_runs": 1e300'),
        "random_runs a boolean": good.replace('"random_runs": 100', '"random_runs": true'),
        "code_sha256 a number": good.replace('"code_sha256": "x"', '"code_sha256": 7'),
        "holdout_start a string": good.replace("1704067200", '"1704067200"'),
        "no sha256": good.replace(', "sha256": "x"', ""),
    }
    blobs = {name: ("{" + body + "}").encode() for name, body in cases.items()}
    blobs.update({"a list": b"[1, 2]", "a string": b'"x"', "not JSON": b"{", "not UTF-8": b"\xff\xfe\x00",
                  "nested too deep": b"[" * 100_000})
    for i, (name, data) in enumerate(blobs.items()):
        p = tmp_path / f"m{i}.lock"
        p.write_bytes(data)
        c, lock = check_lock(p, expected="0" * 64, expected_file=hashlib.sha256(data).hexdigest())
        assert not c.ok and lock is None and c.name == "lock", name
        assert c.detail.startswith(f"{p} malformed") or c.detail.startswith(f"{p} unreadable"), (name, c.detail)
    d = tmp_path / "dir.lock"
    d.mkdir()
    assert check_lock(d)[0].detail == f"{d} unreadable: IsADirectoryError"


def test_key_permissions():
    assert check_key(KeyPermissions(True, False, False), "SHADOW").ok
    assert not check_key(KeyPermissions(True, False, False), "CANARY").ok
    assert check_key(KeyPermissions(True, True, False), "FULL").ok
    c = check_key(KeyPermissions(True, True, True), "SHADOW")
    assert not c.ok and "TRANSFER" in c.detail
    assert not check_key(KeyPermissions(False, True, False), "SHADOW").ok
    for stage in ("canary", "", "LIVE", "Shadow"):  # never read as SHADOW (view only) or as a real-money stage
        c = check_key(KeyPermissions(True, True, False), stage)
        assert not c.ok and f"unknown stage {stage!r}" in c.detail
    assert KNOWN_STAGES == STAGES  # the journal refuses any other stage too (it sits below live.stage)


def test_product_spec():
    assert check_product(BIP_SPEC, "BIP-20DEC30-CDE", now=NOW).ok
    assert not check_product(dataclasses.replace(BIP_SPEC, contract_size=0.1), "BIP-20DEC30-CDE", now=NOW).ok
    assert not check_product(dataclasses.replace(BIP_SPEC, price_increment=1.0), "BIP-20DEC30-CDE", now=NOW).ok
    assert not check_product(BIP_SPEC, "BIT-31OCT26-CDE", now=NOW).ok


def test_the_contract_expiry_is_checked():
    # recorded 2026-10-03: contract_expiry 2089-12-30T16:00:00Z (type EXPIRING), while the id says 20 Dec 2030
    assert BIP_SPEC.expiry == int(datetime(2089, 12, 30, 16, tzinfo=UTC).timestamp())
    assert FUTURES_PRODUCT == "BIP-20DEC30-CDE" and PRODUCT_EXPIRY == "2030-12-20"
    c = check_product(BIP_SPEC, now=NOW)
    assert c.ok and "expires 2030-12-20 at the earliest" in c.detail
    c = check_product(dataclasses.replace(BIP_SPEC, expiry=None), now=NOW)
    assert not c.ok and "no contract expiry" in c.detail
    early = int(datetime(2027, 3, 26, 16, tzinfo=UTC).timestamp())  # e.g. a contract that expires before its name says
    c = check_product(dataclasses.replace(BIP_SPEC, expiry=early), now=NOW)
    assert not c.ok and "contract expiry 2027-03-26 before 2030-12-20" in c.detail
    # less than 90 days before the expiry (the id's date: the earlier one) the runtime refuses to start
    last = datetime(2030, 12, 20, tzinfo=UTC).timestamp() - 90 * 86_400
    assert check_product(BIP_SPEC, now=last).ok
    c = check_product(BIP_SPEC, now=last + 1)
    assert not c.ok and "less than 90 days away" in c.detail


def test_run_preflight_online_offline_and_exchange_errors(lock_file):
    path, lock = lock_file
    pins = {"expected_lock": lock["sha256"], "expected_lock_file": file_sha(path), "now": NOW, "settings": None}
    ex = sim(Clock())
    checks, parsed = run_preflight(ex, "CANARY", path, **pins)
    assert [c.name for c in checks] == ["fingerprint", "lock", "key", "product"] and all(c.ok for c in checks)
    require(checks)
    offline, _ = run_preflight(None, "SHADOW", path, **pins)
    assert [c.name for c in offline] == ["fingerprint", "lock"]
    ex.fail_next("key_permissions", Unavailable("503"))
    failing, _ = run_preflight(ex, "SHADOW", path, **pins)
    with pytest.raises(PreflightError, match="key_permissions failed"):
        require(failing)
    transfer = sim(Clock(), permissions=KeyPermissions(True, True, True))
    with pytest.raises(PreflightError, match="TRANSFER"):
        require(run_preflight(transfer, "SHADOW", path, **pins)[0])


def test_the_products_are_fixed(lock_file):
    path, lock = lock_file
    pins = {"expected_lock": lock["sha256"], "expected_lock_file": file_sha(path), "now": NOW, "settings": None}
    # an exchange set up for another future (same specs) or another signal product is refused
    for ex in (sim(Clock(), product=dataclasses.replace(BIP_SPEC, product_id="BIT-30OCT26-CDE")),
               sim(Clock(), spot_id="ETH-USD")):
        checks, _ = run_preflight(ex, "SHADOW", path, **pins)
        bad = [c for c in checks if not c.ok]
        assert [c.name for c in bad] == ["product"] and "both are fixed" in bad[0].detail
    # and no setting names another one
    assert not any(f.name in ("futures_id", "spot_id") for f in dataclasses.fields(LiveSettings))
    for name in ("EWBOT_FUTURES_PRODUCT", "EWBOT_SPOT_PRODUCT"):
        with pytest.raises(SettingsError, match=f"{name} is not a setting"):
            load_settings({name: "BIT-30OCT26-CDE"})


def private_file(path: Path, content: str | bytes, mode: int = 0o600) -> Path:
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_settings_read_secrets_only_from_private_files(tmp_path):
    secrets = private_file(tmp_path / "live.env", "# ewbot\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID='42'\n"
                           "EWBOT_GITHUB_TOKEN=\"ghp_x\"\nEWBOT_HEALTHCHECK_URL=https://hc-ping.com/u-1\n")
    key = private_file(tmp_path / "cdp_api_key.json",
                       '{"name": "organizations/o/apiKeys/k", "privateKey": "-----BEGIN EC PRIVATE KEY-----"}')
    env = {"EWBOT_SECRETS_FILE": str(secrets), "EWBOT_COINBASE_KEY_FILE": str(key), "EWBOT_LIVE_DIR": str(tmp_path),
           "TELEGRAM_CHAT_ID": "7", "EWBOT_PAPER_REPO": "me/repo", "EWBOT_LIVE_EDIT_STOPS": "1",
           "EWBOT_EXPIRY": "coinbase=2027-10-01, github=2027-09-30"}
    s = load_settings(env)
    assert (s.telegram_token, s.telegram_chat, s.github_token) == (Secret("123:abc"), "42", Secret("ghp_x"))  # the file wins
    assert s.healthcheck_url is not None and s.healthcheck_url.reveal() == "https://hc-ping.com/u-1"
    assert not any(secret in repr(s) for secret in ("123:abc", "ghp_x", "hc-ping.com/u-1", str(key)))
    assert s.key_file == key and s.journal_path == tmp_path / "journal.sqlite" and s.cache_dir == tmp_path / "cache"
    assert s.paper_repo == "me/repo" and s.edit_stops
    assert s.expiry == (("coinbase", "2027-10-01"), ("github", "2027-09-30"))
    secrets.chmod(0o644)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    secrets.chmod(0o600)
    key.chmod(0o640)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    defaults = load_settings({})
    assert defaults.data_dir == Path("/data") and defaults.key_file is None and not defaults.edit_stops
    assert defaults.telegram_token is None and defaults.expiry == ()
    assert load_settings({"TELEGRAM_CHAT_ID": " 7 "}).telegram_chat == "7"  # not a secret: the environment may hold it
    with pytest.raises(SettingsError):
        parse_expiry("coinbase:2027-10-01")
    with pytest.raises(SettingsError, match="twice"):
        parse_expiry("coinbase=2027-10-01,coinbase=2027-10-02")


def test_a_secret_in_the_environment_is_refused_by_name_never_by_value(tmp_path):
    secrets = private_file(tmp_path / "live.env", "TELEGRAM_BOT_TOKEN=123:abc\n")
    for name in ("TELEGRAM_BOT_TOKEN", "EWBOT_GITHUB_TOKEN", "EWBOT_HEALTHCHECK_URL"):
        for env in ({name: "v4lue-1"}, {name: "v4lue-1", "EWBOT_SECRETS_FILE": str(secrets)}, {name: ""}):
            with pytest.raises(SettingsError, match=f"{name} is set in the environment") as err:
                load_settings(env)
            assert "v4lue" not in str(err.value)


def test_the_secrets_file_format(tmp_path):
    good = private_file(tmp_path / "good.env",
                        "\ufeff# ewbot live (a BOM is ignored)\n"
                        "export TELEGRAM_BOT_TOKEN=123:abc\n"
                        "  TELEGRAM_CHAT_ID = '42'  \n"
                        "\n"
                        "   # a comment line\n"
                        "EWBOT_GITHUB_TOKEN=\"ghp_x\"\r\n"
                        "EWBOT_HEALTHCHECK_URL=https://hc-ping.com/u-1#x\n")
    assert read_env_file(good) == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42", "EWBOT_GITHUB_TOKEN": "ghp_x",
                                   "EWBOT_HEALTHCHECK_URL": "https://hc-ping.com/u-1#x"}
    assert read_env_file(private_file(tmp_path / "empty.env", "TELEGRAM_BOT_TOKEN=\n")) == {"TELEGRAM_BOT_TOKEN": ""}
    refused = {
        "TELEGRAM_TOKEN=SECRET\n": ":1: unknown key TELEGRAM_TOKEN",
        "# x\nTELEGRAM_BOT_TOKEN=a\nTELEGRAM_BOT_TOKEN=SECRET\n": ":3: TELEGRAM_BOT_TOKEN again (first on line 2)",
        "TELEGRAM_BOT_TOKEN=SECRET # the bot\n": ":1: the value of TELEGRAM_BOT_TOKEN has a space or a quote",
        "TELEGRAM_BOT_TOKEN=\"SECRET'\n": ":1: the value of TELEGRAM_BOT_TOKEN has a space or a quote",
        "EWBOT_GITHUB_TOKEN=SECRET\"\n": ":1: the value of EWBOT_GITHUB_TOKEN has a space or a quote",
        "TELEGRAM_BOT_TOKEN SECRET\n": ":1 is not KEY=VALUE",
        "123:SECRET=x\n": ":1 is not KEY=VALUE",  # a key that is not a name is never echoed
        "export=SECRET\n": ":1 is not KEY=VALUE",
    }
    for i, (text, message) in enumerate(refused.items()):
        path = private_file(tmp_path / f"bad{i}.env", text)
        with pytest.raises(SettingsError, match=re.escape(f"{path}{message}")) as err:
            read_env_file(path)
        assert "SECRET" not in str(err.value)


def test_secrets_files_are_judged_on_the_opened_file(tmp_path, monkeypatch):
    target = private_file(tmp_path / "target.env", "TELEGRAM_CHAT_ID=42\n")
    (tmp_path / "link.env").symlink_to(target)
    assert load_settings({"EWBOT_SECRETS_FILE": str(tmp_path / "link.env")}).telegram_chat == "42"  # the target is judged
    loose = private_file(tmp_path / "loose.env", "TELEGRAM_CHAT_ID=42\n", mode=0o644)
    (tmp_path / "loose-link.env").symlink_to(loose)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings({"EWBOT_SECRETS_FILE": str(tmp_path / "loose-link.env")})
    folder = tmp_path / "folder.env"
    folder.mkdir(mode=0o700)
    fifo = tmp_path / "fifo.env"
    os.mkfifo(fifo, 0o600)  # never blocks: opened without waiting for a writer
    for path in (folder, fifo):
        for name in ("EWBOT_SECRETS_FILE", "EWBOT_COINBASE_KEY_FILE"):
            with pytest.raises(SettingsError, match=re.escape(f"{path} is not a regular file")):
                load_settings({name: str(path)})
    latin = private_file(tmp_path / "latin.env", b"TELEGRAM_BOT_TOKEN=\xe9SECRET\n")
    with pytest.raises(SettingsError, match="is not UTF-8 text") as err:
        load_settings({"EWBOT_SECRETS_FILE": str(latin)})
    assert "SECRET" not in str(err.value) and "xe9" not in str(err.value)
    for path, kind in ((tmp_path / "missing.env", "FileNotFoundError"),
                       (private_file(tmp_path / "none.env", "", mode=0o000), "PermissionError")):
        with pytest.raises(SettingsError, match=f"is not readable: {kind}"):
            load_settings({"EWBOT_SECRETS_FILE": str(path)})
    monkeypatch.setattr(os, "geteuid", lambda: target.stat().st_uid + 1)  # a file of another user: refused
    with pytest.raises(SettingsError, match="must belong to uid"):
        load_settings({"EWBOT_SECRETS_FILE": str(target)})


def test_settings_values_are_checked(tmp_path):
    for value in ("", "  ", "data", "./data"):
        with pytest.raises(SettingsError, match="EWBOT_LIVE_DIR must be an absolute path"):
            load_settings({"EWBOT_LIVE_DIR": value})
    assert load_settings({"EWBOT_LIVE_DIR": " /srv/ewbot "}).data_dir == Path("/srv/ewbot")
    for value in ("true", "yes", "2", "", "on"):
        with pytest.raises(SettingsError, match="EWBOT_LIVE_EDIT_STOPS must be 0 or 1"):
            load_settings({"EWBOT_LIVE_EDIT_STOPS": value})
    assert load_settings({"EWBOT_LIVE_EDIT_STOPS": " 1 "}).edit_stops
    assert not load_settings({"EWBOT_LIVE_EDIT_STOPS": "0"}).edit_stops
    for repo in ("not a repo", "owner", "owner/repo/x", "/repo", "owner/", "owner/..", "https://github.com/o/r"):
        with pytest.raises(SettingsError, match="EWBOT_PAPER_REPO .* is not owner/repo"):
            load_settings({"EWBOT_PAPER_REPO": repo})
    s = load_settings({"EWBOT_PAPER_REPO": " taejunoh/elliott-wave-btc-bot "})
    assert s.paper_repo == "taejunoh/elliott-wave-btc-bot" and load_settings({"EWBOT_PAPER_REPO": ""}).paper_repo is None
    for i, url in enumerate(("ftp://hc-ping.com/SECRET", "http://hc-ping.com/SECRET", "hc-ping.com/SECRET",
                             "https:///SECRET")):
        path = private_file(tmp_path / f"hc{i}.env", f"EWBOT_HEALTHCHECK_URL={url}\n")
        with pytest.raises(SettingsError, match="EWBOT_HEALTHCHECK_URL must be an https:// URL") as err:
            load_settings({"EWBOT_SECRETS_FILE": str(path)})
        assert "SECRET" not in str(err.value)


def test_settings_are_hashable_and_no_dump_shows_a_secret(tmp_path):
    path = private_file(tmp_path / "live.env", "TELEGRAM_BOT_TOKEN=123:SECRET\nTELEGRAM_CHAT_ID=42\n"
                        "EWBOT_GITHUB_TOKEN=ghp_SECRET\nEWBOT_HEALTHCHECK_URL=https://hc-ping.com/SECRET\n")
    env = {"EWBOT_SECRETS_FILE": str(path), "EWBOT_EXPIRY": "coinbase=2027-10-01"}
    s = load_settings(env)
    assert s == load_settings(env) and hash(s) == hash(load_settings(env)) and len({s, load_settings(env)}) == 1
    for dump in (repr(s), str(s), str(dataclasses.asdict(s)), str(dataclasses.astuple(s)), f"{s.telegram_token}",
                 repr(copy.deepcopy(s)), str(vars(s))):
        assert "SECRET" not in dump, dump
    assert repr(s.telegram_token) == "Secret('<redacted>')"
    assert (s.telegram_token.reveal(), s.github_token.reveal()) == ("123:SECRET", "ghp_SECRET")  # only where used
    assert copy.deepcopy(s).telegram_token.reveal() == "123:SECRET"
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.expiry = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        s.telegram_token._value = "x"  # type: ignore[union-attr]
    assert Secret("a") != "a" and Secret("a") == Secret("a")


def test_canary_and_full_need_telegram_and_the_heartbeat(lock_file):
    full = LiveSettings(telegram_token=Secret("123:abc"), telegram_chat="42", healthcheck_url=Secret("https://hc-ping.com/u"))
    for stage in STAGES:
        assert check_alerts(full, stage) == Check("alerts", True, "Telegram alerts and commands, heartbeat")
    c = check_alerts(LiveSettings(), "SHADOW")  # SHADOW may run without them, with a warning
    assert c.ok and c.detail.startswith("WARNING: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_HEALTHCHECK_URL not set")
    for stage in ("CANARY", "FULL"):
        for name, field in (("TELEGRAM_BOT_TOKEN", "telegram_token"), ("TELEGRAM_CHAT_ID", "telegram_chat"),
                            ("EWBOT_HEALTHCHECK_URL", "healthcheck_url")):
            c = check_alerts(dataclasses.replace(full, **{field: None}), stage)
            assert not c.ok and c.detail.startswith(f"{stage} needs {name}")
    assert not check_alerts(full, "canary").ok
    path, lock = lock_file
    pins = {"expected_lock": lock["sha256"], "expected_lock_file": file_sha(path), "now": NOW}
    checks, _ = run_preflight(None, "CANARY", path, settings=LiveSettings(), **pins)
    assert [(c.name, c.ok) for c in checks] == [("fingerprint", True), ("lock", True), ("alerts", False)]
    checks, _ = run_preflight(sim(Clock()), "FULL", path, settings=full, **pins)
    assert [c.name for c in checks] == ["fingerprint", "lock", "alerts", "key", "product"] and all(c.ok for c in checks)


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


# ---------------------------------------------------------------------------- Final fix
def test_run_preflight_needs_the_settings_and_marks_outage_failures(lock_file):
    """Final fix (Task 11 N4): every caller passes its settings - the alerts check is never skipped by omission. A key or
    product read that failed for an outage (429, 5xx, timeout) is marked `retry`: the start is tried again soon (Task 13),
    a refusal for a configuration reason is not."""
    path, lock = lock_file
    pins = {"expected_lock": lock["sha256"], "expected_lock_file": file_sha(path), "now": NOW}
    with pytest.raises(TypeError, match="settings"):
        run_preflight(None, "SHADOW", path, **pins)  # type: ignore[call-arg]
    ex = sim(Clock())
    ex.fail_next("key_permissions", Unavailable("503"))
    ex.fail_next("product", Unavailable("503"))
    checks, _ = run_preflight(ex, "SHADOW", path, settings=LiveSettings(), **pins)
    assert [(c.name, c.ok, c.retry) for c in checks] == [
        ("fingerprint", True, False), ("lock", True, False), ("alerts", True, False), ("key", False, True),
        ("product", False, True)]
    transfer = sim(Clock(), permissions=KeyPermissions(True, True, True))
    checks, _ = run_preflight(transfer, "SHADOW", path, settings=LiveSettings(), **pins)
    assert [(c.name, c.retry) for c in checks if not c.ok] == [("key", False)]


def test_a_secret_is_never_empty_never_pickled_and_copied_as_itself():
    """Final fix (Task 11 N2): pickle would write the value out in clear; copy and deepcopy (dataclasses.asdict) keep
    the one immutable Secret; an empty one is refused (an unset secret is None, so `if s.telegram_token` is honest)."""
    s = Secret("123:SECRET")
    for dump in (lambda: pickle.dumps(s), lambda: pickle.dumps(LiveSettings(telegram_token=s))):
        with pytest.raises(TypeError, match="never pickled"):
            dump()
    assert copy.copy(s) is s and copy.deepcopy(s) is s
    assert dataclasses.asdict(LiveSettings(telegram_token=s))["telegram_token"] is s
    with pytest.raises(ValueError, match="never empty"):
        Secret("")


def test_a_health_check_url_urlsplit_cannot_read_is_a_settings_error(tmp_path):
    """Final fix (Task 11 N1): urlsplit raises ValueError for e.g. an unclosed IPv6 bracket - a refusal by name, never a
    traceback that would carry the URL."""
    for i, url in enumerate(("https://[::1/SECRET", "https://[SECRET]/x")):
        path = private_file(tmp_path / f"v{i}.env", f"EWBOT_HEALTHCHECK_URL={url}\n")
        with pytest.raises(SettingsError, match="EWBOT_HEALTHCHECK_URL must be an https:// URL") as err:
            load_settings({"EWBOT_SECRETS_FILE": str(path)})
        assert "SECRET" not in str(err.value) and err.value.__cause__ is None
