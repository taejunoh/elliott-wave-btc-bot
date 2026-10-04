"""Live vs. paper decision audit (Plan 3 spec 3.5; SHADOW pass criterion ① of spec 6.2).

The live runtime records the engine's decisions (live/target.py) in the paper ledger's format; the
paper runtime (GitHub Actions, the independent audit) records the same decisions on its `paper` branch.
Both replay the same frozen engine on the same Coinbase spot candles, so every decision both runtimes
can know must be identical: same id (kind:ts:trade_key) and `paper.decisions.same_decision`.

The window is the paper ledger's current epoch: known_at in (epoch started_ts, epoch through_ts] (the
live runtime records every decision since 2024 on its first cycle, so it has no gap). An epoch whose
fingerprint is not the baseline is reported as a mismatch of its own: the two engines differ.

The ledger is read with the GitHub contents API (`EWBOT_PAPER_REPO`, fine-grained token with
Contents: read on that repository only, `EWBOT_GITHUB_TOKEN`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import requests

from ewbot.paper.decisions import known_at, same_decision

CONTENTS_API = "https://api.github.com/repos/{repo}/contents/ledger.json"


class AuditError(RuntimeError):
    """The paper ledger could not be read (the message never contains the token)."""


def fetch_paper_ledger(repo: str, token: str, session: Any | None = None, ref: str = "paper") -> dict[str, Any]:
    sess = session if session is not None else requests.Session()
    headers = {"Accept": "application/vnd.github.raw+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        resp = sess.get(CONTENTS_API.format(repo=repo), params={"ref": ref}, headers=headers, timeout=30)
        status = int(resp.status_code)
        data = resp.json() if status == 200 else None
    except Exception as exc:  # noqa: BLE001 - never echo request details (they carry the token)
        raise AuditError(f"paper ledger fetch failed: {type(exc).__name__}") from None
    if status != 200 or not isinstance(data, dict):
        raise AuditError(f"paper ledger fetch failed: HTTP {status}")
    return data


@dataclass(frozen=True)
class AuditResult:
    window: tuple[int, int]  # (exclusive, inclusive) known_at bounds
    compared: int
    matched: int
    mismatches: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return self.compared > 0 and not self.mismatches


def audit(live: Sequence[dict[str, Any]], ledger: dict[str, Any], baseline_fingerprint: str | None = None,
          since: int | None = None) -> AuditResult:
    epoch = ledger["epochs"][-1]
    lo = int(epoch["started_ts"]) if since is None else max(int(epoch["started_ts"]), int(since))
    hi = int(epoch["through_ts"])

    def inside(d: dict[str, Any]) -> bool:
        return lo < known_at(d) <= hi

    paper = {d["id"]: d for d in ledger.get("decisions", []) if inside(d)}
    mine = {d["id"]: d for d in live if inside(d)}
    mismatches: list[dict[str, Any]] = []
    if baseline_fingerprint is not None and epoch.get("fingerprint") != baseline_fingerprint:
        mismatches.append({"id": f"EPOCH:{epoch['id']}", "type": "PAPER_FINGERPRINT",
                           "paper": epoch.get("fingerprint"), "live": baseline_fingerprint})
    matched = 0
    for did in sorted(paper.keys() | mine.keys()):
        a, b = mine.get(did), paper.get(did)
        if a is None:
            mismatches.append({"id": did, "type": "MISSING_IN_LIVE", "paper": b, "live": None})
        elif b is None:
            mismatches.append({"id": did, "type": "MISSING_IN_PAPER", "paper": None, "live": a})
        elif not same_decision(a, b):
            mismatches.append({"id": did, "type": "DIFFERENT", "paper": b, "live": a})
        else:
            matched += 1
    return AuditResult((lo, hi), len(paper.keys() | mine.keys()), matched, tuple(mismatches))
