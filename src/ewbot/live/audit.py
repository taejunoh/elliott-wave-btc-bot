"""Live vs. paper decision audit (Plan 3 spec 3.5; SHADOW pass criterion ① of spec 6.2).

The live runtime records the engine's decisions (live/target.py) in the paper ledger's format; the
paper runtime (GitHub Actions, the independent audit) records the same decisions on its `paper` branch.
Both replay the same frozen engine on the same Coinbase spot candles, so every decision both runtimes
can know must be identical: same id (kind:ts:trade_key) and `paper.decisions.same_decision`.

The window is the paper ledger's current epoch, from its recording floor - the epoch's start for epoch 1,
the previous epoch's through_ts for a later one (as `paper.runner._recording_floor`) - to the last bar both
runtimes have decided: known_at in (floor, min(epoch through_ts, until)], `until` being the last bar whose
live cycle ended OK (a cycle held for late data or failed decides that bar at the next cycle, while the
paper runtime may know it already). The live runtime records every decision since 2024 on its first cycle,
so it has no gap. An epoch whose fingerprint is not the baseline is a mismatch of its own
(PAPER_FINGERPRINT): the two engines differ.

Every mismatch has a cause, like the paper runtime's replay check (`paper.decisions.classify`):
DATA_REVISION when the candles the two runtimes decided on differ up to the decision's bar although they
were equal up to the paper's previous run (a bar Coinbase changed between the live fetch at hh:00:30 and the
paper's at :37); FINGERPRINT_CHANGE for PAPER_FINGERPRINT; else UNEXPLAINED - also whenever the data cannot
be compared (no live candles, no paper hash), and once the two candle histories differ before the decision
(a revision is only recognised at the bar where they part). `data_hash(ts)` is the live candles'
`data_fingerprint(df, 0, ts)`; the paper ledger holds the same hash per decision and per run.

A ledger that is not a schema-1 paper ledger (`paper.ledger.Ledger.from_dict`) or whose epochs or decisions
are malformed is an AuditError, like a failed fetch: never a passing audit.

The ledger is read with the GitHub contents API (`EWBOT_PAPER_REPO`, fine-grained token with
Contents: read on that repository only, `EWBOT_GITHUB_TOKEN`), raw media type (up to 100 MB)."""

from __future__ import annotations

import bisect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import requests

from ewbot.paper.decisions import known_at, same_decision
from ewbot.paper.ledger import Ledger, LedgerError

CONTENTS_API = "https://api.github.com/repos/{repo}/contents/ledger.json"
FETCH_TIMEOUT = (5.0, 20.0)  # connect, each read
FETCH_DEADLINE_S = 30.0  # the whole fetch, body included: it runs on the protective main loop
CHUNK = 1 << 16


class AuditError(RuntimeError):
    """The paper ledger could not be read or is not a valid paper ledger (the message never contains the token)."""


def fetch_paper_ledger(repo: str, token: str, session: Any | None = None, ref: str = "paper", *,
                       clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The ledger as a JSON object. Each request step is bounded by FETCH_TIMEOUT and the whole fetch by
    FETCH_DEADLINE_S. Any failure is an AuditError naming only the HTTP status or the exception type; it is
    raised outside the handler, so no exception carrying the request (and its token) is chained to it. A
    session created here is closed here."""
    own = session is None
    sess: Any = requests.Session() if own else session
    headers = {"Accept": "application/vnd.github.raw+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    start = clock()
    failure: str | None = None
    status = 0
    data: Any = None
    try:
        resp = sess.get(CONTENTS_API.format(repo=repo), params={"ref": ref}, headers=headers, timeout=FETCH_TIMEOUT,
                        stream=True)
        try:
            status = int(resp.status_code)
            if status == 200:
                body = bytearray()
                for chunk in resp.iter_content(CHUNK):
                    body += chunk
                    if clock() - start > FETCH_DEADLINE_S:
                        raise TimeoutError
                data = json.loads(bytes(body))
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001 - never echo request details (they carry the token)
        failure = type(exc).__name__
    finally:
        if own:
            sess.close()
    if failure is not None:
        raise AuditError(f"paper ledger fetch failed: {failure}")
    if status != 200:
        raise AuditError(f"paper ledger fetch failed: HTTP {status}")
    if not isinstance(data, dict):
        raise AuditError(f"paper ledger fetch failed: HTTP 200 but not a JSON object ({type(data).__name__})")
    return data


@dataclass(frozen=True)
class AuditResult:
    window: tuple[int, int]  # (exclusive, inclusive) known_at bounds
    compared: int
    matched: int
    mismatches: tuple[dict[str, Any], ...]  # {"id", "type", "cause", "paper", "live"}
    as_of: float  # when the audit ran
    error: str | None = None  # the audit failed (fetch, invalid ledger): never a pass

    @property
    def ok(self) -> bool:
        return self.error is None and self.compared > 0 and not self.mismatches

    @classmethod
    def failed(cls, error: str, as_of: float) -> AuditResult:
        return cls((0, 0), 0, 0, (), float(as_of), error)

    def to_json(self) -> dict[str, Any]:
        """For the journal: every mismatch as id, type and cause (the decisions go to the alert's event)."""
        return {"as_of": self.as_of, "window": list(self.window), "compared": self.compared, "matched": self.matched,
                "mismatches": [{"id": m.get("id"), "type": m.get("type"), "cause": m.get("cause")}
                               for m in self.mismatches], "error": self.error}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuditResult:
        return cls((int(data["window"][0]), int(data["window"][1])), int(data["compared"]), int(data["matched"]),
                   tuple(data["mismatches"]), float(data["as_of"]), data.get("error"))


def recording_floor(epochs: Sequence[dict[str, Any]]) -> int:
    """known_at bound (exclusive) of the current epoch's decisions (`paper.runner._recording_floor`)."""
    if len(epochs) == 1:
        return int(epochs[0]["started_ts"])
    return int(epochs[-2]["through_ts"])


def audit(live: Sequence[dict[str, Any]], ledger: dict[str, Any], baseline_fingerprint: str | None = None,
          since: int | None = None, until: int | None = None, *, now: float,
          data_hash: Callable[[int], str | None] | None = None) -> AuditResult:
    """Compare the live decisions with the paper ledger's current epoch (module docstring). Raises AuditError
    for a ledger that is not a valid paper ledger."""
    try:
        led = Ledger.from_dict(ledger)
    except LedgerError as exc:
        raise AuditError(f"paper ledger invalid: {exc}"[:300]) from None
    try:
        return _audit(live, led, baseline_fingerprint, since, until, now, data_hash)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise AuditError(f"paper ledger malformed: {type(exc).__name__}: {exc}"[:300]) from None


def _audit(live: Sequence[dict[str, Any]], led: Ledger, baseline: str | None, since: int | None, until: int | None,
           now: float, data_hash: Callable[[int], str | None] | None) -> AuditResult:
    if not isinstance(led.epochs, list) or not led.epochs or not isinstance(led.decisions, list):
        raise AuditError("paper ledger malformed: no epoch, or decisions not a list")
    epoch = led.epochs[-1]
    floor = recording_floor(led.epochs)
    lo = floor if since is None else max(floor, int(since))
    hi = int(epoch["through_ts"]) if until is None else min(int(epoch["through_ts"]), int(until))

    def inside(d: dict[str, Any]) -> bool:
        return lo < known_at(d) <= hi

    paper = {d["id"]: d for d in led.decisions if inside(d)}
    mine = {d["id"]: d for d in live if inside(d)}
    runs = sorted((int(r["last_bar_ts"]), str(r["data_sha256"])) for r in led.runs
                  if isinstance(r, dict) and r.get("status") == "ok" and r.get("data_sha256"))
    run_bars = [b for b, _ in runs]

    def revised(ts: int, recorded: dict[str, Any] | None) -> bool:
        """The live candles up to `ts` differ from the paper's (its decision's hash, else its run at that bar)
        while both were equal at the paper's previous run."""
        if data_hash is None:
            return False
        k = bisect.bisect_left(run_bars, ts)
        theirs = recorded.get("data_sha256") if recorded is not None else None
        if theirs is None and k < len(runs) and runs[k][0] == ts:
            theirs = runs[k][1]
        if theirs is None or k == 0:
            return False
        anchor_bar, anchor_hash = runs[k - 1]
        mine_now = data_hash(ts)
        return mine_now is not None and mine_now != theirs and data_hash(anchor_bar) == anchor_hash

    mismatches: list[dict[str, Any]] = []
    if baseline is not None and epoch.get("fingerprint") != baseline:
        mismatches.append({"id": f"EPOCH:{epoch['id']}", "type": "PAPER_FINGERPRINT", "cause": "FINGERPRINT_CHANGE",
                           "paper": epoch.get("fingerprint"), "live": baseline})
    matched = 0
    for did in sorted(paper.keys() | mine.keys()):
        a, b = mine.get(did), paper.get(did)
        if a is not None and b is not None and same_decision(a, b):
            matched += 1
            continue
        kind = "MISSING_IN_LIVE" if a is None else "MISSING_IN_PAPER" if b is None else "DIFFERENT"
        ref = b if b is not None else a
        assert ref is not None
        cause = "DATA_REVISION" if revised(int(ref["ts"]), b) else "UNEXPLAINED"
        mismatches.append({"id": did, "type": kind, "cause": cause, "paper": b, "live": a})
    return AuditResult((lo, hi), len(paper.keys() | mine.keys()), matched, tuple(mismatches), float(now))
