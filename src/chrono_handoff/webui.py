"""The interactive cue panel: a browser component the agent pops up on demand.

Why a browser at all, when the conductor can already beep?

* **It is the surface the human is already looking at.** A terminal cue only
  works if the human has a terminal in front of them; a panel works on a
  laptop, a phone on the same LAN, or a second monitor.
* **It keeps the slow planner out of the timing path.** The page receives the
  whole schedule once and then drives itself from ``AudioContext``, which
  schedules at sample accuracy.  Streaming one message per beat would put
  HTTP + JS + render jitter on every cue -- the exact failure this project
  exists to avoid.  This is the Two-Rate split applied to the UI layer: the
  server is the slow planner, the page is the fast controller.
* **It can be opened before the agent needs it.** A cue that has to wait for a
  browser launch is not a cue.

Endpoints (all JSON, all loopback by default):

    GET  /                       the panel (self-contained HTML)
    GET  /api/health             liveness + config
    GET  /api/clock              one clock-alignment sample
    GET  /api/schedule           current/most recent run's schedule
    GET  /api/events?since=N     incremental journal tail
    POST /api/press              a press, timestamped in the client's clock
    POST /api/prepare            arm a run that will not start yet
    POST /api/start              start the armed run
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .conductor import Conductor

if TYPE_CHECKING:  # remote.py imports model.py only, so this is not a cycle
    from .remote import RemoteConductor
from . import CONTRACT_VERSION, __version__
from .model import CuePlan, RunStatus, SignalSpec

PANEL_PATH = Path(__file__).with_name("statics") / "panel.html"
FONTS_DIR = Path(__file__).with_name("statics") / "fonts"
ICON_PATH = Path(__file__).with_name("statics") / "icon.svg"
DEFAULT_PANEL_PORT = 8790

#: Origins allowed to call this API cross-origin.  Exactly one caller needs it:
#: the desktop shell (Tauri), which *bundles* the page instead of being served
#: it, so the page's origin is the shell's, not ours.
#:
#: Deliberately a closed list rather than ``*``.  This server listens on
#: 127.0.0.1 and driving a cue is a physical action, so widening it to every web
#: page the operator happens to have open is not a trade worth making -- and the
#: preflight below is what would let such a page read responses back.
CORS_ORIGINS = frozenset({
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
})


class CuePanel:
    """HTTP server that renders and plays the cue grid for one conductor.

    Deliberately *not* an MCP UI iframe: an MCP UI is tied to a tool result and
    disappears when the conversation moves on, whereas the point here is a
    component that persists, can be re-used across cues, and can be open
    before it is needed.  It is a plain page, so it also works on a phone.
    """

    def __init__(
        self,
        # A display client hands over a RemoteConductor, not a Conductor: same
        # surface, but the clock lives in another process (see
        # remote.RemoteConductor, which exists precisely to be this argument).
        conductor: "Conductor | RemoteConductor",
        host: str = "127.0.0.1",
        port: int = DEFAULT_PANEL_PORT,
        auto_open: bool = False,
        panel_html: Path | None = None,
        settings_path: Path | str | None = None,
    ) -> None:
        self.conductor = conductor
        self.host = host
        self.port = port
        self.panel_html = panel_html or PANEL_PATH
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._current_run: str | None = None
        self._prepared: str | None = None
        #: Set when the panel had to fall back to a browser tab, so the page can
        #: say so instead of silently looking like a web app.  Empty = native.
        self.degraded_reason: str = ""
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[str], None]] = []
        #: Monotonic counter of armed cues, so a client can ask for "anything
        #: newer than what I have already displayed".
        self._generation: float = 0.0
        self.settings_path: Path | str | None = settings_path
        self.auto_open = auto_open
        self.opened = False
        from .settings import Settings

        self.settings_path = settings_path
        self.settings = Settings.load(settings_path)
        #: Set by the native window so a settings change can drive the OS window
        #: (top-most, opacity, size).  Left None in server-only mode.
        self.window_hooks: dict[str, Any] = {}

        # When a run is armed, remember it as current and notify any long-poll
        # listener so the page can pull the schedule immediately.
        conductor.on_armed(self._on_armed)

    # -- lifecycle ---------------------------------------------------------

    def bind(self) -> None:
        import socket

        if self.port:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.4):
                    raise SystemExit(
                        f"chrono-handoff: a cue panel is already serving on {self.host}:{self.port}"
                    )
            except SystemExit:
                raise
            except OSError:
                pass

        panel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # Signature must match BaseHTTPRequestHandler.log_message(self,
            # format, *args); a bare ``*_a`` override is not compatible with it.
            # This panel is polled every few seconds, so the default per-request
            # stderr spam would drown the operator's terminal.
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def handle_one_request(self) -> None:
                """Swallow disconnects.

                A long-poll holds a connection open for up to 25 s by design, so
                *every* time the page navigates away, the window closes, or the
                browser recycles the socket, the accept loop sees a reset.  That
                is normal traffic here, not an error, and letting it print a
                traceback would train the operator to ignore the log -- which is
                worse than the noise itself.
                """
                try:
                    super().handle_one_request()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
                    self.close_connection = True

            def finish(self) -> None:
                try:
                    super().finish()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
                    pass

            # -- helpers --
            def _cors(self) -> None:
                """Echo the shell's origin back, but only for a known shell.

                No header for anything else, which is what keeps an arbitrary
                page in the operator's browser from reading this API.
                """
                origin = self.headers.get("Origin")
                if origin and origin in CORS_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def do_OPTIONS(self) -> None:  # noqa: N802
                """CORS preflight.

                A POST carrying ``Content-Type: application/json`` is not a
                "simple" request, so a bundled shell's webview asks permission
                first.  Without this, save/press/reset would fail *only* under
                the shell -- the hardest place to notice, because the page still
                loads and the browser-tab path still works.
                """
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _json(self, obj: Any, status: int = 200) -> None:
                body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _font(self, path: str) -> None:
                name = os.path.basename(path)
                # Resolve and confirm the target stays inside FONTS_DIR: a
                # request for "/fonts/../../etc/passwd" must not escape.
                try:
                    target = (FONTS_DIR / name).resolve()
                    if not str(target).startswith(str(FONTS_DIR.resolve())) or not target.is_file():
                        self._json({"error": "not found"}, 404)
                        return
                except Exception:
                    self._json({"error": "bad font path"}, 400)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(body)))
                # Fonts never change within a version; let the browser cache.
                self.send_header("Cache-Control", "public, max-age=86400")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _icon(self) -> None:
                target = ICON_PATH
                if not target.is_file():
                    self._json({"error": "no icon"}, 404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _html(self, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            # -- routes --
            def do_GET(self) -> None:  # noqa: N802
                path, _, query = self.path.partition("?")
                qs = dict(
                    (kv.split("=", 1)[0], kv.split("=", 1)[1])
                    for kv in query.split("&")
                    if "=" in kv
                )
                try:
                    if path in ("/", "/panel", "/index.html"):
                        self._html(panel.panel_html.read_text(encoding="utf-8"))
                    elif path.startswith("/fonts/"):
                        # Fonts are served locally on purpose.  This is a tool
                        # for hardware work, often on an isolated or air-gapped
                        # network, and a countdown that renders in the wrong face
                        # (or blocks on a font request) is a worse failure than a
                        # slightly bigger repository.  165 KB, three files.
                        self._font(path)
                    elif path == "/icon.svg":
                        # The page's favicon, in a browser tab or a pywebview
                        # window.  It is the very file the desktop shell's icons
                        # are generated from, so the tab and the taskbar show the
                        # same mark.  Only this path is served: a browser that
                        # sees the <link rel="icon"> in panel.html uses it and
                        # never asks for /favicon.ico, and answering that name
                        # with SVG bytes would be a lie about the content type.
                        self._icon()
                    elif path == "/api/health":
                        self._json(panel.health())
                    elif path == "/api/clock":
                        self._json(panel.conductor.clock_sync_payload())
                    elif path == "/api/schedule":
                        run_id = qs.get("run_id") or panel.current_run()
                        if not run_id:
                            self._json({"run_id": None, "beats": []})
                        else:
                            self._json(panel.conductor.schedule_payload(run_id))
                    elif path == "/api/events":
                        self._json(panel.events(int(qs.get("since", "0") or 0)))
                    elif path == "/api/settings":
                        self._json({"settings": panel.settings.to_dict(), "path": str(panel.settings_file())})
                    elif path == "/api/calibration":
                        store = panel.conductor.calibration
                        self._json({
                            "items": [
                                {"key": k, "offset_ms": c.offset_ms, "n": c.n, "mad_ms": c.spread_ms()}
                                for k, c in sorted(store.all().items())
                            ],
                            "path": str(store.path),
                        })
                    elif path == "/api/next-cue-defaults":
                        # What the panel should pre-fill for a new cue, so the
                        # settings screen and the conductor cannot disagree.
                        s = panel.settings
                        self._json({
                            "lead_s": s.lead_s,
                            "ioi_ms": s.ioi_ms,
                            "tail_beats": s.tail_beats,
                            "target_in_s": s.default_target_in_s,
                            "duration_s": s.duration_s,
                            "audio": s.audio_enabled,
                        })
                    elif path == "/api/wait-armed":
                        # Long-poll: lets the page react the instant a cue is
                        # armed without hammering the server, and keeps the
                        # panel responsive when opened *before* it is needed.
                        self._json(panel.wait_for_armed(float(qs.get("timeout", "20") or 20)))
                    else:
                        self._json({"error": "not found", "path": path}, 404)
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

            def do_POST(self) -> None:  # noqa: N802
                path, _, _ = self.path.partition("?")
                body = self._body()
                try:
                    if path == "/api/settings":
                        self._json(panel.update_settings(body))
                    elif path == "/api/settings/reset":
                        self._json({"settings": panel.reset_settings()})
                    elif path == "/api/press":
                        self._json(panel.press(body))
                    elif path == "/api/prepare":
                        self._json(panel.prepare(body))
                    elif path == "/api/start":
                        self._json(panel.start(body))
                    elif path == "/api/cancel":
                        self._json(panel.cancel(body))
                    else:
                        self._json({"error": "not found", "path": path}, 404)
                except Exception as exc:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        class QuietServer(ThreadingHTTPServer):
            # Closed long-polls during shutdown are routine; do not log them.
            def handle_error(self, request: Any, client_address: Any) -> None:
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        srv = QuietServer((self.host, self.port), Handler)
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        self._server = srv

    def serve_forever(self, open_browser: bool | None = None) -> None:
        if self._server is None:
            self.bind()
        assert self._server is not None
        if open_browser is None:
            open_browser = self.auto_open
        if open_browser and not self.opened:
            self.opened = True
            self.open_browser()
        self._server.serve_forever()

    def start_background(self) -> "CuePanel":
        if self._server is None:
            self.bind()
        self._thread = threading.Thread(target=self.serve_forever, name="chrono-panel", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def open_browser(self, url: str | None = None) -> bool:
        """Best-effort: never let a missing browser break a cue."""
        target = url or self.url
        try:
            import webbrowser

            return bool(webbrowser.open(target))
        except Exception:
            return False

    # -- state -------------------------------------------------------------

    def _on_armed(self, run: Any) -> None:
        with self._lock:
            self._current_run = run.id
            self._generation = time.time()
        for fn in list(self._subscribers):
            try:
                fn(run.id)
            except Exception:
                pass

    def current_run(self) -> str | None:
        with self._lock:
            rid = self._current_run
        if not rid:
            return None
        # With a local conductor we can verify the run still exists; with a
        # remote one `get` already degrades to None when the daemon forgets it.
        try:
            return rid if self.conductor.get(rid) is not None else None
        except Exception:
            return rid

    def wait_for_armed(self, timeout_s: float = 20.0, since_generation: float = 0.0) -> dict[str, Any]:
        """Block until a **new** run is armed (or the timeout expires).

        ``since_generation`` lets the page say "I have already shown generation
        N".  Without it, the long-poll would re-deliver the same cue on every
        iteration: the page would re-apply the schedule in a tight loop, resetting
        the press verdict on screen and (if the audio guard were absent) stacking
        duplicate beeps on the same beat.
        """
        with self._lock:
            current_gen = self._generation
        if current_gen > since_generation:
            run_id = self.current_run()
            if run_id:
                return {"run_id": run_id, "generation": current_gen, "already": True}
        event = threading.Event()
        box: list[str] = []

        def notify(rid: str) -> None:
            box.append(rid)
            event.set()

        self._subscribers.append(notify)
        try:
            event.wait(max(0.5, min(timeout_s, 60.0)))
        finally:
            try:
                self._subscribers.remove(notify)
            except ValueError:
                pass
        return {"run_id": box[0] if box else None, "timed_out": not box}

    # -- settings ----------------------------------------------------------

    def settings_file(self) -> Path:
        from .settings import default_settings_path

        return Path(self.settings_path) if self.settings_path else default_settings_path()

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply and persist a settings patch, then push it to the OS window."""
        self.settings.update(patch or {})
        path = self.settings.save(self.settings_path)
        applied: dict[str, Any] = {}
        for key in ("always_on_top", "opacity", "scale", "width", "height", "frameless"):
            hook = self.window_hooks.get(key)
            if hook is not None:
                try:
                    hook(getattr(self.settings, key))
                    applied[key] = True
                except Exception:
                    applied[key] = False
        return {"ok": True, "settings": self.settings.to_dict(), "path": str(path), "applied": applied}

    def reset_settings(self) -> dict[str, Any]:
        from .settings import Settings

        # A fresh Settings() is writable by construction, so the refusal has to
        # be carried across the replacement by hand -- otherwise "reset" is the
        # one button that still overwrites a file this build cannot read.
        blocked = self.settings.readonly_reason
        self.settings = Settings()
        self.settings.readonly_reason = blocked
        self.settings.save(self.settings_path)
        for key, hook in self.window_hooks.items():
            try:
                hook(getattr(self.settings, key))
            except Exception:
                pass
        return self.settings.to_dict()

    def health(self) -> dict[str, Any]:
        """Who is answering, and can it keep what the caller tells it.

        ``version``/``contract`` are what a client checks *before* acting: the
        two halves of an install can come from different builds (stale files are
        a measured upgrade behaviour), and a client that skips this finds out by
        being confusing rather than by being wrong.  ``writable`` is the same
        question for the state files -- a page that cannot save the numbers the
        user just typed must say so instead of looking like it worked.
        """
        return {
            "ok": True,
            "panel": "chrono-cue",
            "url": self.url,
            "version": __version__,
            "contract": CONTRACT_VERSION,
            "current_run": self.current_run(),
            "stats": self.conductor.stats(),
            "degraded": self.degraded_reason,
            "writable": not self.settings.readonly_reason,
            "why_not_writable": self.settings.readonly_reason,
        }

    def events(self, since_seq: int = 0) -> dict[str, Any]:
        run_id = self.current_run()
        if not run_id:
            return {"run_id": None, "events": []}
        from dataclasses import asdict

        return {
            "run_id": run_id,
            "events": [asdict(e) for e in self.conductor.events(run_id, since_seq)],
            "snapshot": self.conductor.status(run_id),
        }

    # -- actions -----------------------------------------------------------

    #: Sources for which the PANEL ITSELF is the observation surface.  Anything
    #: else means a real sensor owns the evidence, and a click here must never
    #: be allowed to close the loop -- otherwise the physical closed loop is
    #: silently replaced by a self-report.  (Verified: a click used to satisfy a
    #: run that was watching a serial port for a pattern that never arrived.)
    PANEL_OBSERVED_SOURCES = frozenset({"", "none", "http", "inject"})

    def press(self, body: dict[str, Any]) -> dict[str, Any]:
        """Record the human's press.

        ``client_wall_ms`` is the click handler's own timestamp, which is far
        more accurate than our receive time -- the HTTP round trip plus event
        loop scheduling would otherwise land in the measurement.  We convert it
        through the client's clock offset when provided.
        """
        run_id = body.get("run_id") or self.current_run()
        if not run_id:
            return {"ok": False, "error": "no active run"}
        payload = str(body.get("payload") or "space")
        wall_ms = body.get("client_wall_ms")
        wall_t = None
        if isinstance(wall_ms, (int, float)):
            wall_t = float(wall_ms) / 1000.0
            offset = body.get("clock_offset_ms")
            if isinstance(offset, (int, float)):
                # client_wall = server_wall + offset  =>  subtract to get ours
                wall_t -= float(offset) / 1000.0
        recv = time.time()

        run = self.conductor.get(run_id)
        spec = run.plan.expect if run is not None else None
        source = (spec.source if spec is not None else "none") or "none"
        if source.split(":")[0] not in self.PANEL_OBSERVED_SOURCES:
            # The agent is watching the physical world for this run (a serial
            # prompt, a console key).  A click is a useful note -- the human
            # thought they acted -- but it is NOT the evidence, so it must not
            # satisfy the run.  Journal it rather than dropping the input.
            self.conductor.note_panel_press(run_id, wall_t=wall_t)
            return {
                "ok": True,
                "run_id": run_id,
                "recorded": False,
                "observed_by": source,
                "reason": f"this run is observed by {source}, not the panel",
                "status": None if run is None else run.status.value,
            }

        # No input-stack correction: a click/keydown handler's timestamp measures
        # the browser event loop, not a keyboard driver, and applying the
        # console's 15 ms would bias every panel measurement by -15 ms.
        self.conductor.inject(run_id, payload=payload, wall_t=wall_t, latency_s=0.0)
        run = self.conductor.get(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "recorded": True,
            "observed_by": source,
            "received_wall_ms": recv * 1000.0,
            "used_client_time": wall_t is not None,
            "offset_ms": None if run is None else run.human_offset_ms,
            "status": None if run is None else run.status.value,
        }

    def cue_defaults(self) -> dict[str, Any]:
        """Saved defaults merged with whatever the caller supplied."""
        s = self.settings
        return {
            "lead_s": s.lead_s,
            "ioi_ms": s.ioi_ms,
            "tail_beats": s.tail_beats,
            "target_in_s": s.default_target_in_s,
            "duration_s": s.duration_s,
            "audio": s.audio_enabled,
            "visual": True,
        }

    def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        """Arm a cue without starting its clock.

        Separating prepare from start is what lets the panel be *already
        showing* when the moment matters, and it lets the conductor open its
        input source (serial port, key listener) before anything can happen --
        otherwise the leading edge of a signal can be missed.
        """
        from .mcp_server import plan_from_dict

        raw = dict(body.get("plan") or body)
        merged = {**self.cue_defaults(), **raw}     # explicit values win
        plan = plan_from_dict(merged)
        # The panel's cue *is* the stimulus: let the pulse run past the response
        # so the human entrains on the grid instead of reacting to one event.
        # A generic plan defaults to cutting the grid on response (correct in
        # production, where continued beeping is just noise), but a panel-driven
        # cue is a different case.
        plan.cancel_cue_on_signal = bool(body.get("cancel_cue_on_signal", False))
        plan.meta.setdefault("panel", True)
        if not plan.expect:
            plan.expect = SignalSpec(source="none", timeout_s=plan.duration_s)
        run = self.conductor.submit(plan, paused=True)
        with self._lock:
            self._prepared = run.id
        # A paused submit resolves no grid, so the conductor's on_armed -- which
        # means "the grid is now absolute times" -- correctly does not fire.  The
        # panel must still learn that a cue exists, or an already-open panel goes
        # on showing the previous run and a freshly-opened page is handed that
        # stale run instead of this one (so `--paused` cues were invisible, and
        # the Start button appeared to do nothing).  Announce it on the channel
        # the panel already listens to; the page renders it as "armed, not
        # started" because the schedule still has no beats.
        self._on_armed(run)
        return {"ok": True, "run_id": run.id, "status": run.status.value, "url": self.url}

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id = body.get("run_id") or self._prepared or self.current_run()
        if not run_id:
            return {"ok": False, "error": "nothing prepared"}
        run = self.conductor.go(run_id, in_s=float(body.get("in_s") or 0.0))
        return {"ok": True, "run_id": run.id, "status": run.status.value, "url": self.url}

    def cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id = body.get("run_id") or self.current_run()
        if not run_id:
            return {"ok": False, "error": "no active run"}
        return {"ok": True, **self.conductor.cancel(run_id, reason="panel_cancel")}


def _dev_main() -> int:
    """Run a panel with a self-demo cue -- handy for eyeballing the component."""
    import argparse

    ap = argparse.ArgumentParser(prog="chrono-panel", description="standalone cue panel")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PANEL_PORT)
    ap.add_argument("--audio", default="null", help="conductor-side audio (panel plays its own)")
    ap.add_argument("--visual", action="store_true", help="also render in this terminal")
    ap.add_argument("--demo", action="store_true", help="arm a demo cue immediately")
    ap.add_argument("--open", action="store_true", help="open a browser")
    args = ap.parse_args()

    conductor = Conductor(audio=args.audio, visual=args.visual)
    conductor.start()
    panel = CuePanel(conductor, host=args.host, port=args.port, auto_open=args.open)
    panel.bind()

    if args.demo:
        from .mcp_server import plan_from_dict

        plan = plan_from_dict(
            {
                "instruction": "Press the big button on the GO beat",
                "lead_s": 3.0,
                "ioi_ms": 500,
                "tail_beats": 4,
                "audio": True,
                "visual": True,
                "target_in_s": 6.0,
                "duration_s": 20,
                "expect_signal": {"source": "none", "timeout_s": 15},
                "meta": {"target": "demo", "calibrate": False},
            }
        )
        run = conductor.submit(plan, paused=True)
        panel._prepared = run.id
        print(f"demo cue prepared: {run.id} (open {panel.url} and press Start)", flush=True)

    print(f"cue panel: {panel.url}\n  conductor audio: {conductor.audio.name}\nCtrl-C to stop.", flush=True)
    try:
        panel.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        conductor.stop()
        panel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_dev_main())
