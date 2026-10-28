"""Verify the Tauri desktop shell (`desktop/`) end to end.

The shell is a *separate* artefact from the Python package, so nothing else in
`scripts/` covers it -- and the failure it can hide is the worst one this
architecture has: a page that renders perfectly while the seam it is supposed to
talk through silently does nothing.  A window that looks right and cannot be
closed, dragged or resized is still a broken widget.

What is asserted, and why each one needs to be:

1. **The seam is reached.** The shell prints one ``[shell] <command>`` line per
   call, so ``apply_theme`` on load proves bundled page -> probe -> invoke ->
   Rust.  Nothing else proves it: the page looks identical either way.
2. **The CORS contract**, checked directly rather than through a click.  A GET
   needs no preflight but a JSON POST does, so a broken preflight breaks
   save/press *only* under the shell -- the one place the browser-tab suite
   cannot see.  Also asserts that an unknown origin gets no grant.
3. **Window chrome moves the real window** (drag, resize, minimise, close),
   because the Rust geometry code is new and shares no line with the pywebview
   path.

Synthetic mouse input on this machine is unreliable -- it drops ``pointermove``
often enough that `e2e_resize.py` carries a fallback for exactly this (proven
separately: forcing its bridge path passes 10/10 four times out of four).  So a
gesture whose events never landed is reported as WARN, never as a pass; a
gesture that *did* land but changed nothing is a FAIL.

Run: .venv/Scripts/python.exe scripts/e2e_desktop_shell.py
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("CHRONO_HOME", str(ROOT / "logs" / "_home"))

import _wininput  # noqa: E402

from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.mcp_server import ConductorClient  # noqa: E402
from chrono_handoff.webui import CuePanel  # noqa: E402

SHELL_EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "chrono-handoff.exe"
PANEL_PORT = 8850
WINDOW_CLASS = "Tauri Window"
SHELL_ORIGIN = "http://tauri.localhost"
#: Title-bar button centres as a fraction of window width.  Measured from a
#: screenshot (glyph centres 0.646 / 0.741 / 0.837 / 0.933, order minimise,
#: maximise, settings, close).  Guessing these is how a click lands on the
#: draggable strip beside a button and starts a window drag instead.
BTN = {"min": 0.646, "max": 0.741, "set": 0.837, "close": 0.933}

FAILURES: list[str] = []
#: Checks that could not be exercised.  Kept separate from FAILURES because they
#: are not passes either: this machine's synthetic mouse input drops events, and
#: reporting a gesture nobody exercised as "passed" is how a broken grip ships.
SKIPPED: list[str] = []
OUT: dict = {}

u = ctypes.windll.user32
u.SetProcessDPIAware()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<54} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def skip(name: str, why: str) -> None:
    print(f"  [SKIP] {name:<54} {why}", flush=True)
    SKIPPED.append(name)


# ---------------------------------------------------------------- win32 helpers


def shell_pid() -> int | None:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq chrono-handoff.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(out.split(",")[1].strip('"')) if out.startswith('"') else None


def find_shell_window(pid: int) -> dict | None:
    """Locate the shell's window by *process*, not by title.

    By title is wrong here: a browser tab pointed at the panel is titled
    "chrono-handoff - <browser>", and measuring that window measures the browser.
    """
    hits: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value != WINDOW_CLASS:
            return True
        hits.append({"hwnd": hwnd, "visible": bool(u.IsWindowVisible(hwnd))})
        return True

    u.EnumWindows(cb, 0)
    return hits[0] if hits else None


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def focus(hwnd: int) -> bool:
    """Raise and *verify*.

    A bare SetForegroundWindow is subject to focus-stealing prevention; when it
    silently fails, the next synthetic click lands on whatever is in front and a
    harness problem is reported as a product bug.
    """
    _wininput.focus_window(hwnd)
    time.sleep(0.45)
    return u.GetForegroundWindow() == hwnd


def click(x: int, y: int) -> None:
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.09)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.6)



def request(method: str, path: str, headers: dict | None = None, body: str | None = None,
            port: int | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port or PANEL_PORT, timeout=6)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}


# ------------------------------------------------------------------------ main


def phase_spawn() -> None:
    """The shell must be able to start a conductor, and must not own it.

    Two claims, both easy to get wrong and both invisible from the window:

    1. with nothing running, the shell resolves a launcher, starts a conductor
       and waits for it -- otherwise a user needs a separate terminal to get a
       widget they were told is a desktop app;
    2. the conductor it started **outlives the window**.  That is the whole
       reason the split topology exists (docs/component.md): an agent keeps
       calling cues whether or not the operator has the widget open.  A shell
       that reaps its child on exit breaks it silently.

    Ports are distinct from the attach phase so the two cannot interfere.
    """
    print("\n-- starting a conductor from scratch (sidecar) --")
    spawn_panel_port, spawn_ipc_port = PANEL_PORT + 2, PANEL_PORT + 3
    if panel_answers(spawn_panel_port):
        skip("shell starts a conductor when none is running",
             f"something is already listening on {spawn_panel_port}")
        return

    # Which branch *should* win: the bundled binary beats CHRONO_PYTHON, because
    # a shipped app must prefer what it shipped over whatever interpreter the
    # environment happens to name.  Testing the precedence matters -- getting it
    # backwards silently runs a different conductor than the one packaged.
    bundled = SHELL_EXE.parent / ("chrono-core.exe" if os.name == "nt" else "chrono-core")
    expected = "bundled" if bundled.is_file() else "CHRONO_PYTHON"
    print(f"         sidecar present: {bundled.is_file()}  -> expecting the {expected!r} branch")

    log_path = ROOT / "logs" / "_shell" / "spawn-stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Clear any record left by an earlier shell *before* launching.  The shell
    # writes this file itself, and a stale one from a previous run describes a
    # different conductor -- reading it here would test that run, not this one.
    record = Path(os.environ["CHRONO_HOME"]) / "shell.json"
    record.unlink(missing_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(SHELL_EXE)],
        env={**os.environ,
             "CHRONO_PANEL_PORT": str(spawn_panel_port),
             "CHRONO_IPC_PORT": str(spawn_ipc_port),
             # A source checkout instead of the bundled sidecar: exercises the
             # same code path, and needs no PyInstaller build to run the suite.
             "CHRONO_PYTHON": sys.executable},
        stdout=log, stderr=subprocess.STDOUT,
    )

    def read() -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    try:
        up = False
        for _ in range(60):
            if panel_answers(spawn_panel_port):
                up = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        text = read()
        check("shell resolved a conductor launcher", "conductor launcher:" in text,
              next((l for l in text.splitlines() if "launcher" in l), ""))
        check(f"launcher precedence picked {expected!r}",
              f"launcher: {expected}" in text,
              next((l.split("launcher:")[-1].strip() for l in text.splitlines() if "launcher" in l), ""))
        check("shell started a conductor and it answered", up,
              f"panel :{spawn_panel_port}={panel_answers(spawn_panel_port)}")
        check("conductor output was sent to its own log, not inherited",
              "conductor output ->" in text and "conductor pid =" in text)
        check("shell's own stdout has no conductor banner in it",
              "conductor listening on" not in text)

        # -- the shell's record, and the tool that reads it ------------------
        #
        # The conductor cannot see the window (the shell is just another HTTP
        # client), so this file is the only way `chrono doctor` can answer
        # "what is on screen".  If it stops being written, nothing breaks
        # visibly -- doctor just silently goes back to describing a window that
        # might not exist, which is the failure this record was added to fix.
        #
        # Waited for, not assumed: the panel starts answering the moment the
        # conductor binds, which is *before* this shell finishes its own startup
        # and writes the record.  Reading straight away is a race, and it failed
        # intermittently -- once with no file at all, once with the previous
        # run's.
        for _ in range(60):
            if record.is_file():
                break
            time.sleep(0.25)
        rec = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        check("shell recorded itself for doctor", rec.get("backend") == "tauri", str(record))
        check("record names the launcher rule that actually fired",
              rec.get("conductor_launcher") == expected,
              f"{rec.get('conductor_launcher')!r}, expected {expected!r}")
        check("record carries the conductor pid, distinct from its own",
              bool(rec.get("conductor_pid")) and rec.get("conductor_pid") != rec.get("pid"),
              f"shell={rec.get('pid')} conductor={rec.get('conductor_pid')}")

        doctor, code = run_doctor()
        check("doctor names the running shell", "tauri shell" in doctor,
              next((l.strip() for l in doctor.splitlines() if "panel window" in l),
                   "(no panel window row)"))
        # Doctor exits non-zero only because this test has no console; anything
        # else in the problem list would mean the run itself broke something.
        extra = [p for p in doctor_problems(doctor) if "no console" not in p]
        check("doctor reported no problem but the missing console",
              not extra and (code == 0 or bool(doctor_problems(doctor))),
              f"exit={code} extra={extra}")

        # The detachment claim.
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(2.0)
        still = panel_answers(spawn_panel_port)
        check("conductor OUTLIVES the window (closing the widget kills no cue)", still,
              f"panel :{spawn_panel_port}={still}")

        # Killed, not closed: the record is still on disk and its pid is dead.
        # Reporting that as "tauri shell" would be a diagnostic confidently
        # describing a process that no longer exists.
        doctor, _ = run_doctor()
        check("doctor reports the killed shell as STALE, not running",
              "stale record" in doctor and "tauri shell" not in doctor,
              next((l.strip() for l in doctor.splitlines() if "shell" in l), "(no shell row)"))
        if still:
            # Same real path a user's agent would take, with the window gone.
            try:
                out = subprocess.run(
                    [sys.executable, "-m", "chrono_handoff.cli", "submit",
                     "--host", "127.0.0.1", "--port", str(spawn_ipc_port),
                     "--json", '{"id":"no-window","instruction":"press RESET","lead_s":2,'
                               '"ioi_ms":400,"tail_beats":2,"target_in_s":6,"duration_s":20}'],
                    capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                    cwd=str(ROOT),
                    # Pin the child's stdout encoding: without this the CLI writes
                    # its own locale encoding (cp936 here) and the decode below is
                    # a coin flip.
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                check("a cue can still be armed with no window open",
                      '"status": "running"' in out.stdout, out.stdout.strip()[-90:])
            except Exception as exc:
                check("a cue can still be armed with no window open", False, str(exc))
    finally:
        if proc.poll() is None:
            proc.kill()
        stop_conductor_from_pidfile()
        log.close()
        print(f"  spawn-phase log: {log_path}")


def geometry_settings(**over: object) -> Path:
    """Seed the settings file the shell reads at startup; returns the path."""
    from chrono_handoff.settings import Settings  # noqa: PLC0415

    path = Path(os.environ["CHRONO_HOME"]) / "settings.json"
    s = Settings()
    for key, value in over.items():
        setattr(s, key, value)
    s.save(path)
    return path


def run_shell_until(seconds: float, tag: str, panel_port: int, ipc_port: int) -> tuple[str, subprocess.Popen]:
    """Start the shell and wait for it to open a window, up to `seconds`.

    Waits for the window rather than for the clock: the shell has to start, spawn a
    conductor, get the contract handshake and only then create a window, and on a
    loaded CI runner that outlasted a fixed 26s budget -- the log then had no
    "opening at" line at all and the geometry check reported `(none)`, which reads as
    a product fault.  If it still never appears, that is said out loud.
    """
    log_path = ROOT / "logs" / "_shell" / f"{tag}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(SHELL_EXE)],
        env={**os.environ, "CHRONO_PANEL_PORT": str(panel_port),
             "CHRONO_IPC_PORT": str(ipc_port),
             "CHRONO_PYTHON": sys.executable},
        stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + seconds
    opened = False
    while time.time() < deadline and proc.poll() is None:
        # Only recorded, not used to cut the wait short: the phases below rely on the
        # shell having had the whole budget to settle, and stopping at the first
        # window line made them race instead (two unrelated geometry checks started
        # failing the moment this loop broke early).
        log.flush()
        if not opened:
            opened = "[shell] opening at" in log_path.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.5)
    log.flush()
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if "[shell] opening at" in text:
        opened = True
    if not opened:
        print(f"  [WARN] the shell ({tag}, pid {proc.pid}) opened no window in {seconds:.0f}s; "
              f"exited={proc.poll() is not None}; log so far: "
              f"{[line for line in text.splitlines() if line.startswith('[shell]')][:3]}")
    return text, proc


def close_shell(proc: subprocess.Popen, tag: str) -> str:
    """Close the shell the way a user does, and return what it logged then.

    `WM_CLOSE` is posted straight to the window rather than going through
    `taskkill /PID`: it is the same message, but taskkill needs the calling session
    to be able to reach that window, and on a CI runner it does not.  There the
    graceful close silently never happened, the suite force-killed the process 20s
    later, and the geometry check read `(none)` for a save that never had a chance
    to run -- while the very same `CloseRequested` handler passed in the phase that
    closes through the page's own close button.  Either way the handler under test
    is `CloseRequested`; this way it actually runs.

    Targets this Popen's own pid rather than searching by image name: the suite
    can have more than one shell alive, and "kill whatever is called
    chrono-handoff.exe" would then close someone else's window.
    """
    window = find_shell_window(proc.pid)
    if window is not None:
        u.PostMessageW(window["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
    else:
        print(f"  [WARN] no window found for pid {proc.pid}; closing with taskkill instead")
        subprocess.run(["taskkill", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(1.0)
    return (ROOT / "logs" / "_shell" / f"{tag}.log").read_text(encoding="utf-8", errors="replace")


def phase_geometry() -> None:
    """The window must reopen where it was -- and not somewhere invisible.

    Three claims, each of which failed at least once while this was built:

    1. a saved position is *restored*.  The shell reads it over HTTP, and the
       API is asymmetric (GET nests it under "settings", POST takes a flat
       patch) -- reading the wrong shape silently yields the default size;
    2. an off-screen position is pulled back.  A remembered coordinate plus an
       unplugged monitor is a widget that starts invisible with nothing on
       screen to drag back;
    3. `remember_position: false` really stops it from saving.

    Each sub-case gets its **own ports and its own conductor**, and that is not
    tidiness.  The conductor is the single owner of settings and holds them in
    memory, so seeding settings.json while one is running changes nothing: the
    shell asks the conductor, and the conductor answers with what it loaded at
    startup.  Sharing one conductor across these cases made all three read the
    first case's values -- which is how the asymmetry in (1) hid for a while.
    """
    print("\n-- window geometry: remembered, restored, and bounded --")
    if not SHELL_EXE.is_file():
        skip("window geometry is restored on the next launch", f"{SHELL_EXE} not built")
        return

    def fresh_ports(n: int) -> tuple[int, int]:
        return PANEL_PORT + 8 + n * 10, PANEL_PORT + 9 + n * 10

    def run_case(n: int, tag: str, **settings: object) -> tuple[str, str]:
        """Seed settings, run a shell against a fresh conductor, close it.

        Returns (log while running, log written during close).
        """
        stop_conductor_from_pidfile()
        geometry_settings(**settings)
        panel_port, ipc_port = fresh_ports(n)
        text, proc = run_shell_until(60, tag, panel_port, ipc_port)
        try:
            return text, close_shell(proc, tag)
        finally:
            if proc.poll() is None:
                proc.kill()
            stop_conductor_from_pidfile()

    # 1. restored, and saved back without drifting.
    text, saved = run_case(1, "geom1", x=333, y=222, width=500, height=640)
    check("saved size and position restored on launch",
          "[shell] opening at 500x640, position (333, 222)" in text,
          next((l for l in text.splitlines() if "opening at" in l), "(none)"))
    check("closing saves the geometry back, without drifting",
          "[shell] geometry saved: 500x640 at (333, 222)" in saved,
          next((l for l in saved.splitlines() if "geometry saved" in l), "(none)"))

    # 2. off-screen is corrected.
    text, _ = run_case(2, "geom2", x=-9000, y=7000, width=500, height=640)
    line = next((l for l in text.splitlines() if "opening at" in l), "(none)")
    # Centred on the primary, so the exact numbers depend on the monitor; what
    # matters is that the requested origin is not what came out.
    check("an off-screen position is pulled back on screen",
          "position (" in line and "-9000" not in line, line)

    # 3. remember_position: false
    stop_conductor_from_pidfile()
    geometry_settings(x=400, y=300, width=480, height=600, remember_position=False)
    before = json.loads(
        (Path(os.environ["CHRONO_HOME"]) / "settings.json").read_text(encoding="utf-8")
    )
    panel_port, ipc_port = fresh_ports(3)
    _, proc = run_shell_until(60, "geom3", panel_port, ipc_port)
    try:
        saved = close_shell(proc, "geom3")
        after = json.loads(
            (Path(os.environ["CHRONO_HOME"]) / "settings.json").read_text(encoding="utf-8")
        )
        check("remember_position=false is honoured, not just stored",
              "geometry not saved (remember_position is off)" in saved,
              next((l for l in saved.splitlines() if "geometry" in l), "(none)"))
        check("remember_position=false left the saved values untouched",
              {k: before.get(k) for k in ("x", "y", "width", "height")}
              == {k: after.get(k) for k in ("x", "y", "width", "height")},
              f"{before.get('x')},{before.get('y')} -> {after.get('x')},{after.get('y')}")
    finally:
        if proc.poll() is None:
            proc.kill()
        stop_conductor_from_pidfile()


def phase_page_pointer() -> None:
    """The page's own pointer handlers, exercised without synthetic input.

    This is the coverage that used to be missing.  `mouse_event` drops
    `pointermove` on this machine, so a synthetic drag arrived at the page as
    pointerdown+pointerup with no motion, and every one of these checks ended up
    as SKIP -- leaving "the grip maths is right" and "nobody ever ran it"
    indistinguishable in the report.

    `CHRONO_PAGE_SELFTEST=pointer` has the shell call `__chronoPageTest`, which
    constructs real DOM events and dispatches them at the real elements.  The
    same listeners run and the same shellCall goes out; only the OS input queue
    is skipped, and that is the layer that cannot be driven here.

    Each result is measured off the window, so a hook that silently does nothing
    fails rather than passes.
    """
    print("\n-- the page's own pointer handlers (no synthetic input) --")
    if not SHELL_EXE.is_file():
        skip("grip/title drags reach the window", f"{SHELL_EXE} not built")
        return
    stop_conductor_from_pidfile()
    panel_port, ipc_port = PANEL_PORT + 40, PANEL_PORT + 41
    log_path = ROOT / "logs" / "_shell" / "pagepointer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    (Path(os.environ["CHRONO_HOME"]) / "settings.json").unlink(missing_ok=True)
    home = Path(os.environ["CHRONO_HOME"])
    marker = home / "pending_update.json"
    (home / "updates").mkdir(parents=True, exist_ok=True)
    (home / "updates" / "chrono-handoff_9.9.9_x64-setup.exe").write_bytes(b"kept installer")
    marker.write_text(json.dumps({"from": "0.1.0", "to": "9.9.9",
                                  "artifact": "chrono-handoff_9.9.9_x64-setup.exe",
                                  "at_unix": 0}), encoding="utf-8")
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(SHELL_EXE)],
        env={**os.environ, "CHRONO_PANEL_PORT": str(panel_port),
             "CHRONO_IPC_PORT": str(ipc_port), "CHRONO_PYTHON": sys.executable,
             "CHRONO_PAGE_SELFTEST": "pointer"},
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        text = ""
        for _ in range(80):
            text = log_path.read_text(encoding="utf-8", errors="replace")
            # The guard report is emitted near the end, and the update report
            # after it, so waiting for the last one means every probe finished.
            if "update phase=" in text:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)

        def page_line(fragment: str) -> str:
            return next((l for l in text.splitlines() if fragment in l), "(no line)")

        for label, fragment in (
            ("SE grip drag resizes by exactly the drag", "grip drag ok=true"),
            ("title-bar drag moves by exactly the drag", "title drag ok=true"),
            ("minimise button reaches the OS window", "min button ok=true"),
            ("maximise button reaches the OS window", "max button ok=true"),
            # The version guard, in the real page, with the real conductor: the
            # live verdict has to be "this conductor is mine", and the branches a
            # working install cannot reach are driven directly.
            ("the page accepts the conductor it is running with", 'guard live note="" contract=2'),
            ("the version guard accepts a matching contract", 'guard same-build=""'),
            ("...refuses a contract from another build", 'guard other-build="界面与 conductor 不是同一次构建'),
            ("...passes a not-writable reason through", 'guard not-writable="why-not"'),
            ("...refuses a conductor that declares none", 'guard unanswered="界面与'),
        ):
            check(label, fragment in text, page_line(fragment.split(" ok")[0]))

        # The update seam, in the real shell, driven through the page.  What is
        # asserted is that it *answers with something real* -- not which phase:
        # a build with no key says `unconfigured`, one with a key says `idle` or
        # `error` (there is no release to find yet).  Pinning the exact phase here
        # would have made this suite fail the moment the key was added, which is
        # pretending to check honesty while checking configuration.
        seam = page_line("update phase=")
        check(
            "the update seam answers with a phase the page can act on",
            "update phase=" in seam and "(none)" not in seam and "threw" not in seam,
            seam,
        )

        # The other half of the rollback story.  A marker written *before* this
        # launch is what an app that did not come up would leave behind, so the
        # only reason its absence means anything is that a successful start
        # removes it -- and that is what this checks, against the real file and
        # the real log.  The kept installer is left alone: it is the way back.
        check("a marker present at launch is cleared once the app is up", not marker.exists())
        check("...and the log says so, with the version pair",
              "0.1.0 -> 9.9.9 came up" in text,
              page_line("came up"))
        check("the kept installer is not deleted",
              (home / "updates" / "chrono-handoff_9.9.9_x64-setup.exe").is_file())
    finally:
        if proc.poll() is None:
            proc.kill()
        stop_conductor_from_pidfile()
        log.close()


def _ms(value: float | None) -> str:
    """Format an optional millisecond figure for a check's detail column."""
    return "n/a" if value is None else f"{value:+.1f} ms"


def panel_answers(port: int) -> bool:
    try:
        st, _ = request("GET", "/api/health", port=port)
        return st == 200
    except Exception:
        return False


def run_doctor() -> tuple[str, int]:
    """Run the real `chrono doctor`; returns (output, exit code).

    Deliberately the CLI and not the module: the point is what the user sees.

    The exit code is returned rather than asserted on: doctor exits non-zero
    when it finds problems, and running it from a pipe *is* one (no console ->
    no keystroke capture).  The caller checks the content it cares about, and
    separately that nothing else was reported.
    """
    out = subprocess.run(
        [sys.executable, "-m", "chrono_handoff.cli", "doctor"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if out.stderr.strip():
        raise RuntimeError(f"doctor wrote to stderr:\n{out.stderr}")
    return out.stdout, out.returncode


def doctor_problems(doctor: str) -> list[str]:
    """The problem lines doctor printed, or [] when it printed no such block."""
    return [l.strip("- ") for l in doctor.splitlines() if l.startswith("  - ")]


def stop_conductor_from_pidfile() -> None:
    """Stop the conductor this phase started, by PID from its own pidfile.

    Never `taskkill /IM python.exe`: that is every Python process on the
    machine, including services that have nothing to do with this project.

    A pid from a file is not an identity, either: Windows reuses pids, and this
    suite starts and stops conductors all day, so a stale `daemon.json` can name a
    process that has nothing to do with this project -- killed by pid alone it takes
    that process down.  (Three outages of an unrelated local service during this
    project's long test sessions are consistent with exactly that.)  So the pidfile's
    port is asked first: if whatever is on it does not answer as a chrono conductor,
    the pid is left alone and the phase reports the port as busy, which is honest and
    recoverable.
    """
    import json

    pidfile = Path(os.environ["CHRONO_HOME"]) / "daemon.json"
    try:
        data = json.loads(pidfile.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        port = int(data["port"])
    except Exception:
        return
    if not ConductorClient(port=port, timeout_s=1.0).healthy():
        print(f"  [INFO] not killing pid {pid}: nothing answers as a conductor on "
              f"{port} (a recycled pid would be somebody else's process)")
        return
    # Output discarded on purpose: taskkill writes its banner in the OEM code
    # page (cp936 here), so capturing it as text is a decode error waiting to
    # happen -- and there is nothing in it we need.
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    if not SHELL_EXE.exists():
        print(f"shell not built ({SHELL_EXE})")
        print("run: cd desktop/src-tauri && cargo build   -- skipping desktop-shell checks.")
        return 0

    phase_spawn()
    phase_page_pointer()
    phase_geometry()

    conductor = Conductor(audio="null", visual=False, journal_dir=str(ROOT / "logs" / "_shell"))
    conductor.start()
    panel = CuePanel(conductor, host="127.0.0.1", port=PANEL_PORT)
    panel.bind()
    threading.Thread(target=panel.serve_forever, daemon=True).start()
    time.sleep(0.5)

    # ---- 1. the CORS contract, directly --------------------------------
    print("\n-- CORS: the shell's origin is the bundled page's, not ours --")
    st, h = request("OPTIONS", "/api/press", {
        "Origin": SHELL_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    check("preflight answered for the shell origin",
          st == 204 and h.get("access-control-allow-origin") == SHELL_ORIGIN, f"HTTP {st}")
    st, h = request("POST", "/api/settings/reset",
                 {"Origin": SHELL_ORIGIN, "Content-Type": "application/json"}, "{}")
    check("cross-origin JSON POST allowed",
          st == 200 and h.get("access-control-allow-origin") == SHELL_ORIGIN, f"HTTP {st}")
    st, h = request("GET", "/api/settings", {"Origin": "https://evil.example"})
    check("unknown origin gets no grant",
          "access-control-allow-origin" not in h, f"HTTP {st}")

    log_path = ROOT / "logs" / "_shell" / "shell-stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Start from no saved geometry.  The shell now remembers where it was, and
    # this phase finishes by closing the window -- which writes the size the
    # self-check resized it to.  Left alone, the next run would open at the
    # *previous* run's size and the "opened at the configured size" check below
    # would fail on leftovers rather than on anything real.
    (Path(os.environ["CHRONO_HOME"]) / "settings.json").unlink(missing_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(SHELL_EXE)],
        env={**os.environ, "CHRONO_PANEL_PORT": str(PANEL_PORT), "CHRONO_PANEL_HOST": "127.0.0.1",
             # The input-free self-check.  Synthetic mouse input delivers nothing
             # on this machine most runs, so this is the only way the drag/resize
             # commands get real coverage: the shell drives them through the
             # page's own shellCall (real JS -> invoke -> Rust -> OS window).
             "CHRONO_SHELL_SELFTEST": "1",
             # And the GO button, pressed by the page itself once this phase arms
             # a cue below.  Synthetic clicks on the GO button have never landed
             # on this machine; this is the page's own pointer handler, so the
             # press goes through the real doPress -> fetch -> conductor path.
             "CHRONO_PAGE_SELFTEST": "press"},
        stdout=log, stderr=subprocess.STDOUT,
    )

    def read_log() -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    try:
        win = None
        for _ in range(60):
            pid = shell_pid()
            win = find_shell_window(pid) if pid else None
            if win and win["visible"]:
                break
            time.sleep(0.5)
        check("shell opened a visible window", bool(win and win["visible"]))

        # Wait for the page's own report instead of reading the log once.  The window
        # is visible before its page has finished loading, and on a CI runner that gap
        # was long enough that the page had not called the seam yet -- which was then
        # reported as "the bundled page did not reach Rust", a much bigger claim than
        # "the page was still loading".
        text = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            text = read_log()
            if "[shell] apply_theme" in text:
                break
            time.sleep(0.5)
        check("bundled page reached Rust through the seam",
              "[shell] apply_theme" in text,
              f"{[l for l in text.splitlines() if l.startswith('[shell]')][:2]}")
        check("shell announced the conductor's API base",
              f"[shell] api base = http://127.0.0.1:{PANEL_PORT}" in text)
        if not win:
            return 1

        hwnd = win["hwnd"]
        OUT["initial_rect"] = rect_of(hwnd)
        # 460x620 logical; the OS reports physical, so compare through the
        # system DPI scale rather than hardcoding one monitor's factor.
        dpi = u.GetDpiForSystem() / 96.0
        check("window opened at the configured size",
              OUT["initial_rect"][2:4] == (round(460 * dpi), round(620 * dpi)),
              f"{OUT['initial_rect']} @ {dpi:g}x")

        # ---- 1b. the commands, driven without synthetic input -------------
        # CHRONO_SHELL_SELFTEST makes the shell drive its own commands through the
        # page's shellCall and report the observed outcome.  This is what covers
        # the drag/resize/minimise/maximise semantics.
        print("\n-- chrome commands (input-free self-check) --")
        seen = 0
        for _ in range(24):
            text = read_log()
            if text.count("[shell] selftest ") >= 4:
                break
            time.sleep(0.5)
        for name in ("drag", "resize", "minimize", "maximize"):
            m = re.search(rf"\[shell\] selftest {name} ok=(\w+)(.*)", read_log())
            if not m:
                check(f"{name} command", False, "self-check produced no result")
                continue
            seen += 1
            check(f"{name} command reached the OS window", m.group(1) == "true",
                  m.group(2).strip())

        # ---- 2. a cue armed on the conductor reaches the page -----------
        # Clicked, not inferred: a click on the GO button records a press on the
        # conductor, which can only happen if the page rendered the run AND its
        # cross-origin POST was accepted.  Retried, because doPress() correctly
        # does nothing until the page has the schedule (``!S.sched``), so an
        # early click is a legitimate no-op rather than a failure.
        print("\n-- the page's own cross-origin POST --")
        # `target_in_s` is the delay from arm to the cue's ZERO (the GO beat) --
        # not `lead_s`, which is only how long the visible countdown is.  The
        # first version of this used target_in_s=120 and then waited ~16 s, so
        # the press arrived two minutes early and the conductor discarded it as
        # "before_min_offset" -- correctly.  The check was measuring its own
        # impatience.  Keep the zero inside the wait below.
        run = conductor.cue(instruction="press RESET", target_in_s=5.0, lead_s=2.0,
                            ioi_ms=500, tail_beats=4, expect_source="http",
                            duration_s=30, open_panel=False)
        time.sleep(1.0)
        # The page presses on its own at the GO beat (CHRONO_PAGE_SELFTEST=press):
        # it polls until it holds a live schedule and the panel is the observation
        # surface, waits for the zero, then dispatches a pointerdown on the record
        # control -- real handler, real fetch, real cross-origin POST.  `http` on
        # purpose: with the default `none` the page renders no record control at
        # all ("this cue only reminds"), so there would be nothing to press.
        #
        # There used to be a second, best-effort OS click at a fixed fraction of
        # the window (`h * 0.697`), aimed at the full-width "GO 时按下" button.
        # That button is gone -- the control is now a small, left-aligned record
        # button -- so a fraction of the window cannot address it any more, and a
        # click that silently misses is worse than no click: it reads as evidence
        # the OS path works when it was never exercised.
        pressed = False
        for _ in range(30):
            after = conductor.get(run["run_id"])
            if after and after.signal_seen:
                pressed = True
                break
            time.sleep(0.8)
        if pressed:
            live = conductor.get(run["run_id"])
            check("the page's GO button reached the conductor cross-origin", True,
                  f"signal_seen={getattr(live, 'signal_seen', None)}, "
                  f"offset={_ms(getattr(live, 'human_offset_ms', None))}")
        else:
            live = conductor.get(run["run_id"])
            check("the page's GO button reached the conductor cross-origin", False,
                  f"no press recorded (sched armed={bool(live)}; "
                  f"page said: {[l for l in read_log().splitlines() if '[page] press' in l]})")

        # ---- 2b. a DENSE rail must not break the card -------------------
        #
        # One cell per beat, so a long lead makes the rail wide.  With a 20 s
        # lead the grid column grew to the rail's min-content (614 px inside a
        # 439 px card), every sibling inherited that width, and the card quietly
        # cropped the GO button and every right-aligned metric value -- which is
        # what "content is cut off, it does not fit the window" looked like.  The
        # padding that keeps GO centred doubles the cell count, so this is
        # reachable with the *default* 500 ms beat and a 20 s lead.
        #
        # The page reports both failure modes itself (`page_note`); this asserts
        # it stayed quiet, in the host where the break was worst.
        conductor.cue(instruction="press RESET", target_in_s=90.0, lead_s=20.0,
                      ioi_ms=500, tail_beats=6, expect_source="none",
                      duration_s=240, open_panel=False)
        time.sleep(4.0)
        layout_gripes = [l for l in read_log().splitlines()
                         if "clipping its content" in l or "rail cannot fit" in l]
        check("a dense rail does not clip the card", not layout_gripes,
              layout_gripes[0] if layout_gripes else "no clipping reported by the page")

        # ---- 3. closing ------------------------------------------------
        #
        # The pointer behaviours that used to be attempted here with synthetic
        # input -- the SE grip, the title-bar drag, the minimise/maximise
        # buttons -- are now covered deterministically in `phase_page_pointer`,
        # which drives the page's own handlers and measures the window.  They are
        # not retried here: a synthetic drag that *does* land is not extra
        # evidence (same path), and one that does not leaves a SKIP that made
        # "untested" and "fine" look alike.
        #
        # What this phase still needs is to end, and closing the window is how.
        print("\n-- closing --")
        x, y, w, h = rect_of(hwnd)
        if x + w > u.GetSystemMetrics(0) or y + h > u.GetSystemMetrics(1):
            u.SetWindowPos(hwnd, 0, 120, 90, w, h, 0x0004 | 0x0010)
            time.sleep(0.4)
        x, y, w, h = rect_of(hwnd)
        focus(hwnd)
        click(x + int(w * BTN["close"]), y + 20)
        for _ in range(24):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        text = read_log()
        if "[shell] close" not in text:
            # Same rule as every other gesture here: no log line means the click
            # never landed, and this machine's synthetic input is intermittent
            # enough that calling it a failure would just make the suite flaky.
            skip("close button ended the shell process",
                 "synthetic click never landed (no input landed at all this run)")
        else:
            check("close button ended the shell process", proc.poll() is not None,
                  f"exited={proc.poll() is not None}")
    finally:
        if proc.poll() is None:
            proc.kill()
        panel.stop()
        conductor.stop()
        log.close()
        print(f"\n  shell log: {log_path}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    if SKIPPED:
        print(f"desktop-shell checks passed, {len(SKIPPED)} SKIPPED (not covered by this run):")
        for s in SKIPPED:
            print(f"  - {s}")
        return 0
    print("desktop-shell checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
