"""The daemon entry point.

Exists as a separate module because "the conductor must outlive the MCP server"
is an architectural requirement, not a runtime detail.  Start it detached; give
it its own console window if you want it to be able to capture keystrokes and
beep (a detached process has no console and therefore no audio and no keys).

    python -m chrono_handoff.daemon --port 8791            # foreground
    python -m chrono_handoff.daemon --background           # detached, own window

On Windows, ``--background`` uses ``pythonw`` plus a fresh console via
CREATE_NEW_CONSOLE so the human-facing cue channel keeps working while the
shell that launched it can be closed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .audio import AUDIO_CHOICES
from .conductor import Conductor
from .mcp_server import DEFAULT_HOST, DEFAULT_IPC_PORT, ConductorServer


def pidfile_path() -> Path:
    env = os.environ.get("CHRONO_HOME")
    base = Path(env) if env else Path.home() / ".chrono-handoff"
    base.mkdir(parents=True, exist_ok=True)
    return base / "daemon.json"


def write_pidfile(port: int, extra: dict | None = None) -> None:
    data = {"pid": os.getpid(), "host": DEFAULT_HOST, "port": port, "started_wall": time.time()}
    if extra:
        data.update(extra)
    pidfile_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_daemon(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_IPC_PORT,
    journal_dir: str | None = None,
    audio: str = "auto",
    visual: bool = True,
    verbose: bool = False,
    panel: bool = True,
    panel_port: int = 8790,
    panel_open: bool = False,
    window: bool = False,
    window_on_top: bool = True,
    window_hidden: bool = False,
) -> int:
    conductor = Conductor(
        journal_dir=journal_dir,
        audio=audio,
        visual=visual,
        verbose=verbose,
    )

    # The native-window mode owns its own IPC bind and its own main loop, so it
    # must branch **before** anything here claims a port.  Doing it later bound
    # the same port twice and made `daemon --window` fail with "cannot bind" on
    # a completely free port -- a confusing error whose cause was ordering, not
    # the port.
    if window:
        return _run_with_window(
            conductor,
            host=host,
            port=port,
            journal_dir=journal_dir,
            on_top=window_on_top,
            hidden=window_hidden,
        )

    conductor.start()
    server = ConductorServer(conductor, host=host, port=port)
    # Bind BEFORE announcing anything: a second conductor must fail loudly and
    # early, not print a cheerful "listening" banner and then die.
    server.bind()
    port = server.port
    write_pidfile(port, {"audio": conductor.audio.name, "journal_dir": str(conductor.journal_dir)})

    def _shutdown(_signum: int, _frame: object) -> None:
        if panel_srv is not None:
            panel_srv.stop()
        conductor.stop()
        try:
            pidfile_path().unlink()
        except Exception:
            pass
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

    panel_srv = None
    panel_url = "(disabled)"
    if panel:
        from .webui import CuePanel

        try:
            panel_srv = CuePanel(conductor, host=host, port=panel_port, auto_open=panel_open)
            panel_srv.start_background()
            conductor.on_panel_open(lambda _rid: panel_srv.open_browser())
            panel_url = panel_srv.url
        except SystemExit as exc:
            panel_srv = None
            panel_url = f"(unavailable: {exc})"

    print(
        f"chrono-handoff conductor listening on {host}:{port}\n"
        f"  cue panel  : {panel_url}\n"
        f"  audio sink : {conductor.audio.name}\n"
        f"  journal dir: {conductor.journal_dir}\n"
        f"  calibration: {conductor.calibration.path}\n"
        f"  pid        : {os.getpid()}\n"
        "Ctrl-C to stop.",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if panel_srv is not None:
            panel_srv.stop()
        conductor.stop()
        try:
            pidfile_path().unlink()
        except Exception:
            pass
    return 0


def _run_with_window(
    conductor: Any,
    host: str,
    port: int,
    journal_dir: str | None,
    on_top: bool = True,
    hidden: bool = False,
) -> int:
    """Run the conductor *inside* the native window process.

    One process, one clock.  The alternative -- daemon in one process, window in
    another -- needs the display side to be a :class:`RemoteConductor` and is
    what ``chrono window --attach`` is for; it is the better choice when the
    cues must survive the window being closed.
    """
    import sys

    from .window import CueWindow

    # Bind the IPC port FIRST.  Printing a banner and writing a pidfile before
    # binding means a second instance cheerfully announces itself, writes its
    # pid over the running one, and only then dies on the bind -- leaving a
    # pidfile that points at a corpse.  Claim the port before claiming anything
    # else.
    server = ConductorServer(conductor, host=host, port=port)
    try:
        server.bind()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        conductor.stop()
        return 1

    conductor.start()
    win = CueWindow(conductor=conductor, panel_port=0, on_top=on_top, start_hidden=hidden)
    write_pidfile(port, {
        "audio": conductor.audio.name,
        "journal_dir": str(conductor.journal_dir),
        "panel_url": win.browser_url,
        "mode": "window",
    })
    print(
        f"chrono-handoff conductor + native window\n"
        f"  ipc        : {host}:{port}\n"
        f"  panel      : {win.browser_url}\n"
        f"  audio sink : {conductor.audio.name}\n"
        f"  journal dir: {conductor.journal_dir}\n"
        f"  pid        : {os.getpid()}",
        file=sys.stderr,
        flush=True,
    )

    # IPC runs in the background; the GUI owns the main thread, which is
    # required by WinForms/WebView2.
    threading.Thread(target=server.serve_forever, name="chrono-ipc", daemon=True).start()

    def _shutdown(*_a: Any) -> None:
        try:
            pidfile_path().unlink()
        except Exception:
            pass
        try:
            conductor.stop()
        except Exception:
            pass

    try:
        rc = win.run()
    except KeyboardInterrupt:
        rc = 0
    finally:
        _shutdown()
    return rc


def spawn_background(args: argparse.Namespace) -> int:
    """Detach into a new console so the cue channel (audio + keys) still works."""
    import subprocess

    exe = sys.executable
    cmd = [exe, "-m", "chrono_handoff.daemon", "--port", str(args.port), "--host", args.host, "--audio", args.audio]
    if args.journal_dir:
        cmd += ["--journal-dir", args.journal_dir]
    if not args.visual:
        cmd += ["--no-visual"]
    if args.verbose:
        cmd += ["--verbose"]
    if getattr(args, "window", False):
        cmd += ["--window"]
        if getattr(args, "no_on_top", False):
            cmd += ["--no-on-top"]
        if getattr(args, "window_hidden", False):
            cmd += ["--window-hidden"]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(
        cmd,
        creationflags=creationflags,
        close_fds=True,
        cwd=str(Path.cwd()),
    )
    # Wait for the IPC port to come up so callers do not race the daemon.
    from .mcp_server import ConductorClient

    client = ConductorClient(args.host, args.port, timeout_s=0.5)
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if client.healthy():
            print(f"daemon up: pid={proc.pid} {args.host}:{args.port}")
            return 0
        if proc.poll() is not None:
            print(f"daemon exited immediately (code {proc.returncode})", file=sys.stderr)
            return 1
        time.sleep(0.15)
    print(f"daemon did not become healthy within 8s (pid={proc.pid})", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chrono-daemon", description="chrono-handoff conductor daemon")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=int(os.environ.get("CHRONO_IPC_PORT", DEFAULT_IPC_PORT)))
    p.add_argument("--journal-dir", default=None)
    p.add_argument("--audio", default="auto", choices=list(AUDIO_CHOICES))
    p.add_argument("--no-visual", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--background", action="store_true", help="detach into its own console window")
    p.add_argument("--no-panel", action="store_true", help="do not serve the browser cue panel")
    p.add_argument("--panel-port", type=int, default=8790)
    p.add_argument("--panel-open", action="store_true", help="open the panel in a browser on start")
    p.add_argument("--window", action="store_true",
                   help="host the always-on-top native cue window (recommended): chrono_cue then "
                        "raises a real window instead of a browser tab")
    p.add_argument("--no-on-top", action="store_true", help="native window: do not stay above other windows")
    p.add_argument("--window-hidden", action="store_true", help="native window: start hidden, reveal on cue")
    args = p.parse_args(argv)

    if args.background:
        return spawn_background(args)
    return run_daemon(
        host=args.host,
        port=args.port,
        journal_dir=args.journal_dir,
        audio=args.audio,
        visual=not args.no_visual,
        verbose=args.verbose,
        panel=not args.no_panel,
        panel_port=args.panel_port,
        panel_open=args.panel_open,
        window=args.window,
        window_on_top=not args.no_on_top,
        window_hidden=args.window_hidden,
    )


if __name__ == "__main__":
    raise SystemExit(main())
