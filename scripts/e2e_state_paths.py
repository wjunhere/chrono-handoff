"""Where does per-user state land?  It must not depend on the working directory.

There used to be two answers.  ``daemon.json``/``settings.json``/
``calibration.json`` went to ``CHRONO_HOME`` (or ``~/.chrono-handoff``), but the
cue journal went to ``cwd/logs``.  Start the desktop app from the Start Menu --
cwd is the install directory -- and a cue's journal landed beside the program
instead of beside the user's other state.  Run the same cue from a terminal and
it landed somewhere else entirely.  Nothing errored; "where is my journal" just
had two answers depending on how you launched.

So this checks the invariant, not one path: **every piece of per-user state
lands under one home, whatever the cwd is**, and the home itself follows
``CHRONO_HOME`` when set and the user profile when not.

The child process is this same file re-executed with a marker in the
environment, so the "no CHRONO_HOME" branch is tested for real (against a
temporary USERPROFILE) rather than by clearing a variable and hoping.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CHILD_MARKER = "__CHRONO_PATHS_PROBE__"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<48} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def probe() -> dict[str, str]:
    """Child entry point: report where each kind of state would be written."""
    from chrono_handoff.calibrate import default_store_path  # noqa: PLC0415
    from chrono_handoff.conductor import Conductor  # noqa: PLC0415
    from chrono_handoff.daemon import pidfile_path  # noqa: PLC0415
    from chrono_handoff.settings import default_settings_path  # noqa: PLC0415

    conductor = Conductor(audio="null", visual=False)
    try:
        return {
            "cwd": str(Path.cwd()),
            "journal": str(conductor.journal_dir),
            "settings": str(default_settings_path()),
            "calibration": str(default_store_path()),
            "pidfile": str(pidfile_path()),
        }
    finally:
        conductor.journal.close()


def run_probe(cwd: Path, env_overrides: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CHRONO_HOME", None)
    env.update(env_overrides)
    env[CHILD_MARKER] = "1"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"probe failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def under(path: str, base: Path) -> bool:
    try:
        Path(path).resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="chrono-paths-") as tmp:
        tmp = Path(tmp)
        scratch_cwd = tmp / "somewhere-else"
        scratch_cwd.mkdir()
        home = tmp / "state"

        # --- 1. CHRONO_HOME set: everything under it, nothing in cwd --------
        got = run_probe(scratch_cwd, {"CHRONO_HOME": str(home)})
        for key in ("journal", "settings", "calibration", "pidfile"):
            check(f"{key} under CHRONO_HOME", under(got[key], home), got[key])
        leftover = sorted(p.name for p in scratch_cwd.iterdir())
        check("cwd left untouched", not leftover, f"cwd={got['cwd']} contains {leftover}")
        print()

        # --- 2. CHRONO_HOME unset: falls back to the user profile ----------
        fake_profile = tmp / "fake-profile"
        fake_profile.mkdir()
        got = run_probe(
            scratch_cwd,
            {"USERPROFILE": str(fake_profile), "HOME": str(fake_profile)},
        )
        fallback = fake_profile / ".chrono-handoff"
        for key in ("journal", "settings", "calibration", "pidfile"):
            check(f"{key} under ~/.chrono-handoff", under(got[key], fallback), got[key])
        leftover = sorted(p.name for p in scratch_cwd.iterdir())
        check("cwd still untouched", not leftover, f"contains {leftover}")
        print()

        # --- 3. the bug that started this: journal must not follow cwd -----
        other_cwd = tmp / "a-different-cwd"
        other_cwd.mkdir()
        a = run_probe(scratch_cwd, {"CHRONO_HOME": str(home)})["journal"]
        b = run_probe(other_cwd, {"CHRONO_HOME": str(home)})["journal"]
        check("journal same from two different cwds", a == b, f"{a} == {b}")
        check("journal not inside any cwd", not under(a, scratch_cwd) and not under(b, other_cwd))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("state paths passed")
    return 0


if __name__ == "__main__":
    if os.environ.pop(CHILD_MARKER, None):
        # Child mode: the parent set the cwd and environment it wants to probe.
        print(json.dumps(probe()))
        raise SystemExit(0)
    raise SystemExit(main())
