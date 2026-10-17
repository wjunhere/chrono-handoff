"""Run a command with a deadline, so a suite that hangs fails instead of hanging.

`timeout(1)` cannot be used here: macOS runners do not have it, and on Windows
runners the name resolves to `timeout.exe`, which is a different program entirely.
Python is already present in every job, and on expiry this kills the child and exits
124 -- the convention `timeout` uses -- after printing the command that ran out of
time, so the log names the suite rather than leaving a job stuck for half an hour.

Usage: python scripts/with_deadline.py <seconds> <command> [args...]
"""

from __future__ import annotations

import subprocess
import sys

EXIT_TIMEOUT = 124


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python scripts/with_deadline.py <seconds> <command> [args...]", file=sys.stderr)
        return 2
    seconds = float(argv[1])
    command = argv[2:]
    try:
        return subprocess.call(command, timeout=seconds)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {seconds:.0f}s: {' '.join(command)}", flush=True)
        return EXIT_TIMEOUT


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
