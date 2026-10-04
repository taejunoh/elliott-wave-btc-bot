"""Settings of `ewbot live` (Plan 3 spec 3.4).

Non-secret values come from the environment (deploy/live/docker-compose.yml). Secrets live in two files
on the VPS that only their owner may read (mode 600, refused otherwise), mounted read-only into the
container and never stored in the repository, the image or a log:
  EWBOT_COINBASE_KEY_FILE  the CDP API key JSON as downloaded ({"name": ..., "privateKey": ...})
  EWBOT_SECRETS_FILE       KEY=VALUE lines: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_GITHUB_TOKEN,
                           EWBOT_HEALTHCHECK_URL (values in the secrets file win over the environment).
TELEGRAM_CHAT_ID must be a positive number: the user's private chat with the bot (its id is the user's own id),
the only chat Telegram commands are taken from (live/control.py); a group or channel id (negative) or an @name
is refused. A LiveSettings repr never shows a secret or the key file's path."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT


class SettingsError(RuntimeError):
    """A setting or a secrets file is missing, malformed or readable by others."""


@dataclass(frozen=True)
class LiveSettings:
    data_dir: Path = Path("/data")
    key_file: Path | None = field(default=None, repr=False)  # secrets and where they are: never in a repr
    futures_id: str = FUTURES_PRODUCT
    spot_id: str = SPOT_PRODUCT
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat: str | None = None
    healthcheck_url: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)
    paper_repo: str | None = None
    edit_stops: bool = False
    expiry: dict[str, str] = field(default_factory=dict)

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def check_private(path: Path) -> None:
    """The file exists and neither its group nor others have any permission on it."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise SettingsError(f"{path} is not readable: {type(exc).__name__}") from None
    if mode & 0o077:
        raise SettingsError(f"{path} must be mode 600 (chmod 600), is {oct(mode & 0o777)}")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise SettingsError(f"{path}:{n} is not KEY=VALUE")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def parse_expiry(text: str) -> dict[str, str]:
    """'coinbase=2027-10-01,github=2027-09-30' -> {name: ISO date}."""
    out: dict[str, str] = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        name, sep, day = item.partition("=")
        try:
            out[name.strip()] = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD") from None
        if not sep or not name.strip():
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD")
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


def load_settings(environ: Mapping[str, str] | None = None) -> LiveSettings:
    env = os.environ if environ is None else environ
    secrets: dict[str, str] = {}
    sf = env.get("EWBOT_SECRETS_FILE", "").strip()
    if sf:
        check_private(Path(sf))
        secrets = read_env_file(Path(sf))

    def get(name: str) -> str | None:
        return (secrets.get(name) or env.get(name) or "").strip() or None

    kf = env.get("EWBOT_COINBASE_KEY_FILE", "").strip()
    if kf:
        check_private(Path(kf))
    return LiveSettings(
        data_dir=Path(env.get("EWBOT_LIVE_DIR", "/data")),
        key_file=Path(kf) if kf else None,
        futures_id=env.get("EWBOT_FUTURES_PRODUCT", FUTURES_PRODUCT).strip() or FUTURES_PRODUCT,
        spot_id=env.get("EWBOT_SPOT_PRODUCT", SPOT_PRODUCT).strip() or SPOT_PRODUCT,
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat=private_chat(get("TELEGRAM_CHAT_ID")),
        healthcheck_url=get("EWBOT_HEALTHCHECK_URL"),
        github_token=get("EWBOT_GITHUB_TOKEN"),
        paper_repo=(env.get("EWBOT_PAPER_REPO", "").strip() or None),
        edit_stops=env.get("EWBOT_LIVE_EDIT_STOPS", "0").strip() == "1",
        expiry=parse_expiry(env.get("EWBOT_EXPIRY", "")),
    )
