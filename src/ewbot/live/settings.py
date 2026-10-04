"""Settings of `ewbot live` (Plan 3 spec 3.4).

Non-secret values come from the environment (deploy/live/docker-compose.yml): EWBOT_LIVE_DIR (an absolute path,
default /data), EWBOT_COINBASE_KEY_FILE, EWBOT_SECRETS_FILE, EWBOT_PAPER_REPO (owner/repo), EWBOT_LIVE_EDIT_STOPS
(0 or 1), EWBOT_EXPIRY and TELEGRAM_CHAT_ID (the secrets file's value wins). The futures product and the signal
product are fixed (exchange.FUTURES_PRODUCT, SPOT_PRODUCT; the preflight refuses an exchange set up for any
other): EWBOT_FUTURES_PRODUCT or EWBOT_SPOT_PRODUCT in the environment is refused.

Secrets live only in two files on the VPS that only their owner may read (a regular file of this process's user,
mode 600 or stricter, judged on the opened file; a symlink is followed and its target judged), mounted read-only
into the container and never stored in the repository, the image or a log:
  EWBOT_COINBASE_KEY_FILE  the CDP API key JSON as downloaded ({"name": ..., "privateKey": ...})
  EWBOT_SECRETS_FILE       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_GITHUB_TOKEN, EWBOT_HEALTHCHECK_URL
TELEGRAM_BOT_TOKEN, EWBOT_GITHUB_TOKEN or EWBOT_HEALTHCHECK_URL set in the environment is refused (an environment
is visible in `docker inspect` and /proc): they are read from the secrets file only.

The secrets file (read_env_file): UTF-8 (a leading BOM is ignored); blank lines and lines whose first non-blank
character is # are skipped; every other line is KEY=VALUE, optionally preceded by `export `, with KEY one of the
four keys above, each at most once (spaces around KEY and VALUE are ignored). A VALUE may be wrapped in one
matching pair of quotes and never contains a space or a quote, so a `#` inside it is part of it and a trailing
comment (`KEY=value  # note`) is refused - put a comment on a line of its own. An empty VALUE is unset. An error
names the file, the line and the key, never a value.

TELEGRAM_CHAT_ID must be a positive number: the user's private chat with the bot (its id is the user's own id),
the only chat Telegram commands are taken from (live/control.py); a group or channel id (negative) or an @name
is refused. EWBOT_HEALTHCHECK_URL must be an https:// URL (one urlsplit cannot read is refused the same way). The
settings hold every secret as a `Secret`: never empty (an unset secret is None), its repr and str are redacted (so
is `dataclasses.asdict` of the settings: a copy is the same Secret), it is never pickled (that would write the value
out in clear), `.reveal()` gives the value where it is used; a LiveSettings repr shows neither a secret nor the key
file's path, and settings are hashable (EWBOT_EXPIRY is kept as sorted (name, day) pairs)."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, NoReturn
from urllib.parse import urlsplit

from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT

SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "EWBOT_GITHUB_TOKEN", "EWBOT_HEALTHCHECK_URL")  # never from the environment
FILE_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EWBOT_GITHUB_TOKEN", "EWBOT_HEALTHCHECK_URL")
FIXED_PRODUCTS = ("EWBOT_FUTURES_PRODUCT", "EWBOT_SPOT_PRODUCT")
MAX_SECRET_FILE_BYTES = 1 << 20
_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z")


class SettingsError(RuntimeError):
    """A setting or a secrets file is missing, malformed or readable by others."""


class Secret:
    """A secret value: repr and str show `<redacted>`; `.reveal()` returns it where it is used. Never empty: an unset
    secret is None, so `if s.telegram_token` means it is set."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        text = str(value)
        if not text:
            raise ValueError("a Secret is never empty (an unset secret is None)")
        object.__setattr__(self, "_value", text)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('<redacted>')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("a Secret is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a Secret is immutable")

    def __copy__(self) -> Secret:  # immutable: copy and deepcopy (dataclasses.asdict) give the same Secret
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret:
        return self

    def __reduce__(self) -> NoReturn:  # pickle (and anything built on it) would write the value out in clear
        raise TypeError("a Secret is never pickled")


@dataclass(frozen=True)
class LiveSettings:
    data_dir: Path = Path("/data")
    key_file: Path | None = field(default=None, repr=False)  # where the CDP key is: never in a repr
    telegram_token: Secret | None = None
    telegram_chat: str | None = None
    healthcheck_url: Secret | None = None
    github_token: Secret | None = None
    paper_repo: str | None = None
    edit_stops: bool = False
    expiry: tuple[tuple[str, str], ...] = ()  # (name, ISO day) sorted by name: hashable, immutable

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def read_private(path: Path) -> bytes:
    """The bytes of a secrets file, judged on the opened file itself (one open: no race between a check by path and
    the read): a regular file of this process's user with no permission for its group or others. A FIFO or a
    device is refused without blocking. Errors name the path and an exception type only."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0))
    except OSError as exc:
        raise SettingsError(f"{path} is not readable: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SettingsError(f"{path} is not a regular file")
        if st.st_uid != os.geteuid():
            raise SettingsError(f"{path} must belong to uid {os.geteuid()} (this process), belongs to uid {st.st_uid}")
        if st.st_mode & 0o077:
            raise SettingsError(f"{path} must be mode 600 (chmod 600), is {oct(st.st_mode & 0o777)}")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 65_536):
            size += len(chunk)
            if size > MAX_SECRET_FILE_BYTES:
                raise SettingsError(f"{path} is larger than {MAX_SECRET_FILE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise SettingsError(f"{path} is not readable: {type(exc).__name__}") from None
    finally:
        os.close(fd)


def check_private(path: Path) -> None:
    """The file is a secrets file only its owner can access (read_private): the CDP key file, read by the SDK."""
    read_private(path)


def read_env_file(path: Path) -> dict[str, str]:
    """The secrets file as {key: value} (read_private; the format is in the module docstring)."""
    try:
        text = read_private(path).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SettingsError(f"{path} is not UTF-8 text") from None
    out: dict[str, str] = {}
    first: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise SettingsError(f"{path}:{n} is not KEY=VALUE (KEY one of {', '.join(FILE_KEYS)})")
        if key not in FILE_KEYS:
            raise SettingsError(f"{path}:{n}: unknown key {key} (the secrets file holds {', '.join(FILE_KEYS)})")
        if key in first:
            raise SettingsError(f"{path}:{n}: {key} again (first on line {first[key]})")
        first[key] = n
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if any(c.isspace() or c in "'\"" for c in value):
            raise SettingsError(f"{path}:{n}: the value of {key} has a space or a quote (a comment goes on a line "
                                "of its own)")
        out[key] = value
    return out


def parse_expiry(text: str) -> dict[str, str]:
    """'coinbase=2027-10-01,github=2027-09-30' -> {name: ISO date}."""
    out: dict[str, str] = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        name, sep, day = item.partition("=")
        try:
            iso = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD") from None
        if not sep or not name.strip():
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD")
        if name.strip() in out:
            raise SettingsError(f"EWBOT_EXPIRY names {name.strip()!r} twice")
        out[name.strip()] = iso
    return out


def private_chat(value: str | None) -> str | None:
    """TELEGRAM_CHAT_ID as the canonical positive number of a private chat (None when unset)."""
    if value is None:
        return None
    if not (value.isascii() and value.isdigit() and int(value) > 0):
        raise SettingsError("TELEGRAM_CHAT_ID must be the positive id of your private chat with the bot (your Telegram "
                            "user id): commands are taken only from a private chat - not a group or channel id "
                            "(negative) or an @name")
    return str(int(value))


def _live_dir(value: str | None) -> Path:
    if value is None:
        return Path("/data")
    if not value.strip() or not Path(value.strip()).is_absolute():
        raise SettingsError(f"EWBOT_LIVE_DIR must be an absolute path, is {value!r}")
    return Path(value.strip())


def _edit_stops(value: str | None) -> bool:
    if value is None:
        return False
    if value.strip() not in ("0", "1"):
        raise SettingsError(f"EWBOT_LIVE_EDIT_STOPS must be 0 or 1, is {value!r}")
    return value.strip() == "1"


def _paper_repo(value: str | None) -> str | None:
    repo = (value or "").strip()
    if not repo:
        return None
    if not _REPO.match(repo) or repo.split("/")[1] in (".", ".."):
        raise SettingsError(f"EWBOT_PAPER_REPO {repo!r} is not owner/repo")
    return repo


def _healthcheck_url(value: str | None) -> Secret | None:
    if not value:
        return None
    try:
        parts = urlsplit(value)
        https = parts.scheme == "https" and bool(parts.hostname)
    except ValueError:  # e.g. an unclosed IPv6 bracket: never a traceback (it would carry the URL)
        https = False
    if not https:
        raise SettingsError("EWBOT_HEALTHCHECK_URL must be an https:// URL (the healthchecks.io ping URL)")
    return Secret(value)


def load_settings(environ: Mapping[str, str] | None = None) -> LiveSettings:
    env = os.environ if environ is None else environ
    for name in FIXED_PRODUCTS:
        if name in env:
            raise SettingsError(f"{name} is not a setting: the products are fixed ({FUTURES_PRODUCT}, {SPOT_PRODUCT})")
    for name in SECRET_KEYS:
        if name in env:
            raise SettingsError(f"{name} is set in the environment: secrets are read only from the mode-600 secrets "
                                "file (EWBOT_SECRETS_FILE) - unset it")
    sf = env.get("EWBOT_SECRETS_FILE", "").strip()
    secrets = read_env_file(Path(sf)) if sf else {}
    kf = env.get("EWBOT_COINBASE_KEY_FILE", "").strip()
    if kf:
        check_private(Path(kf))

    def secret(name: str) -> Secret | None:
        return Secret(secrets[name]) if secrets.get(name) else None

    return LiveSettings(
        data_dir=_live_dir(env.get("EWBOT_LIVE_DIR")),
        key_file=Path(kf) if kf else None,
        telegram_token=secret("TELEGRAM_BOT_TOKEN"),
        telegram_chat=private_chat(secrets.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "").strip() or None),
        healthcheck_url=_healthcheck_url(secrets.get("EWBOT_HEALTHCHECK_URL")),
        github_token=secret("EWBOT_GITHUB_TOKEN"),
        paper_repo=_paper_repo(env.get("EWBOT_PAPER_REPO")),
        edit_stops=_edit_stops(env.get("EWBOT_LIVE_EDIT_STOPS")),
        expiry=tuple(sorted(parse_expiry(env.get("EWBOT_EXPIRY", "")).items())),
    )
