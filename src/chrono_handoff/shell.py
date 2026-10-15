"""Which desktop shell is running, if any.

The shell is the one piece of the system a user can see but the conductor
cannot: it owns a window, not a clock, and it talks to the conductor over HTTP
like any other client.  So there is nothing in the conductor's own state that
says "a Tauri window is on screen right now" -- and ``chrono doctor``, whose
whole job is to answer "what am I actually running", could only describe the
pywebview path it might *hypothetically* take.

So the shell leaves a record.  This is the same trick as ``daemon.json``: the
process that knows writes down what it knows, and a reader that cannot see
in-process state reads the file.  The record is deliberately explicit about
which conductor it started (``conductor_launcher``), because "which clock am I
talking to" is the question the window itself cannot answer.

A record whose process is gone is reported as **stale**, never as the running
shell.  A crash leaves the file behind, and a diagnostic tool that confidently
describes a dead process is worse than one that says nothing.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .home import chrono_home

RECORD_NAME = "shell.json"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def record_path() -> Path:
    return chrono_home() / RECORD_NAME


def process_alive(pid: int) -> bool:
    """Whether a process with this pid currently exists.

    Not the same question as "is it the process I think it is" -- a recycled pid
    answers yes.  The record's ``exe`` is checked separately where it matters.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just not ours to signal.
        return True
    return True


@dataclass(frozen=True)
class ShellRecord:
    """What a desktop shell wrote down about itself."""

    backend: str
    pid: int
    exe: str
    api_base: str
    started_wall: float
    version: str = ""
    conductor_launcher: str = ""
    conductor_program: str = ""
    conductor_pid: int = 0

    @classmethod
    def load(cls) -> "ShellRecord | None":
        """Read the record, or ``None`` when no shell has ever run.

        Raises on a malformed file: the shell writes this atomically, so garbage
        means the shell is broken, and that is exactly what a diagnostic should
        not paper over.
        """
        path = record_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            backend=str(data["backend"]),
            pid=int(data["pid"]),
            exe=str(data.get("exe", "")),
            api_base=str(data.get("api_base", "")),
            started_wall=float(data.get("started_wall", 0.0)),
            version=str(data.get("version", "")),
            conductor_launcher=str(data.get("conductor_launcher", "")),
            conductor_program=str(data.get("conductor_program", "")),
            conductor_pid=int(data.get("conductor_pid", 0) or 0),
        )

    @property
    def alive(self) -> bool:
        return process_alive(self.pid)

    def describe_conductor(self) -> str:
        if self.conductor_launcher == "attached":
            return "attached to a conductor that was already running"
        if not self.conductor_launcher:
            return "no conductor launched"
        prog = Path(self.conductor_program).name if self.conductor_program else "?"
        pid = f" pid={self.conductor_pid}" if self.conductor_pid else ""
        return f"conductor {self.conductor_launcher}: {prog}{pid}"

    def describe(self) -> str:
        name = Path(self.exe).name if self.exe else "(unknown exe)"
        ver = f" v{self.version}" if self.version else ""
        return f"{name}{ver} pid={self.pid}; {self.describe_conductor()}"


def clear_record_for(pid: int) -> bool:
    """Remove the record if it belongs to ``pid``.

    Called on a clean shell exit.  A record left by a killed shell is the
    reader's problem (see :attr:`ShellRecord.alive`), but a record left by a
    shell that *exited normally* is ours to clean up.
    """
    try:
        rec = ShellRecord.load()
    except (OSError, ValueError, KeyError):
        return False
    if rec is not None and rec.pid == pid:
        record_path().unlink(missing_ok=True)
        return True
    return False
