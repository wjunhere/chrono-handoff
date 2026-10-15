"""Where chrono-handoff keeps per-user state.

One place decides this, because four files used to decide it independently and
one of them decided differently:

* ``daemon.json``, ``settings.json``, ``calibration.json`` -- **now**
  ``CHRONO_HOME`` or ``~/.chrono-handoff``.
* the cue journal -- **used to be** ``cwd/logs``.

The odd one out was a real bug, not a style issue.  Start the desktop app from
the Start Menu and its working directory is the install directory, so a cue's
journal landed next to the program instead of next to the user's other state.
Nothing failed; the file was just somewhere nobody would look for it.  Worse,
the location depended on *how* the app was launched -- the same cue run from a
terminal and from the Start Menu wrote to different directories, so "where is
my journal" had two answers.
"""

from __future__ import annotations

import os
from pathlib import Path


def chrono_home() -> Path:
    """The per-user state directory, created if missing.

    ``CHRONO_HOME`` overrides it wholesale, which is what the test suite uses to
    keep its state out of the real home directory.
    """
    env = os.environ.get("CHRONO_HOME")
    base = Path(env) if env else Path.home() / ".chrono-handoff"
    base.mkdir(parents=True, exist_ok=True)
    return base


def journal_dir() -> Path:
    """Default cue-journal directory: per-user, never relative to cwd."""
    base = chrono_home() / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base
