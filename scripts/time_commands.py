"""Run a command, time it, append a line to docs/backtest-runtime.md, optionally enforce a budget.

Usage: uv run python scripts/time_commands.py "<label>" [--budget SECONDS] -- <command...>
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

LOG = Path("docs/backtest-runtime.md")
HEADER = (
    "# Backtest runtime log\n\n"
    "| when (UTC) | label | seconds | budget | result | exit | machine |\n"
    "|---|---|---|---|---|---|---|\n"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label")
    ap.add_argument("--budget", type=float, default=None, help="seconds; mark FAIL when exceeded")
    argv = sys.argv[1:]
    if "--" not in argv:
        ap.error("missing `-- <command...>`")
    split = argv.index("--")
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1 :]
    if not cmd:
        ap.error("missing command after --")

    t0 = time.perf_counter()
    rc = subprocess.run(cmd).returncode
    secs = time.perf_counter() - t0

    verdict = "n/a" if args.budget is None else ("PASS" if secs <= args.budget and rc == 0 else "FAIL")
    machine = f"{platform.machine()} {os.cpu_count()} cpu {platform.system()}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not LOG.exists():
        LOG.write_text(HEADER, encoding="utf-8")
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    budget = "-" if args.budget is None else f"{args.budget:g}"
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"| {when} | {args.label} | {secs:.1f} | {budget} | {verdict} | {rc} | {machine} |\n")
    print(f"[time] {args.label}: {secs:.1f}s ({verdict})", file=sys.stderr)
    return rc if rc != 0 else (1 if verdict == "FAIL" else 0)


if __name__ == "__main__":
    sys.exit(main())
