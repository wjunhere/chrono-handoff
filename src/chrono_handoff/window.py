"""Native desktop window for the cue panel (Windows / WebView2).

Why a native window instead of a browser tab:

* **It can be a small always-on-top widget.**  A cue is a peripheral-attention
  task -- the operator is looking at a board, a screen, or their hands, not at a
  browser tab they must first find.  A 460x620 always-on-top window that can sit
  in a corner is a different tool from a tab buried among twenty others.
* **It has no browser chrome.**  Tabs, address bar, bookmarks and a menu bar all
  steal vertical space and invite mis-clicks exactly when precision matters.
* **It survives the session that launched it.**  The agent may be mid-turn; the
  window is a separate process that keeps working.

It reuses the *same* panel HTML, loaded with ``?shell=desktop`` so the page adds
its own title bar and settings overlay.  The timing core (clock sync,
AudioContext scheduling, tick, press) is untouched -- that code is verified by
scripts/e2e_panel.py and must not fork.

Backends, in order of preference:

1. **pywebview** (WebView2 on Windows) -- full Chromium: ``AudioContext`` with
   sample-accurate scheduling, which is non-negotiable for this project.
2. **Browser fallback** -- if pywebview is missing, open the system browser.
   Correct, but it is a tab, so the window affordances are lost.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import winshell
from .conductor import Conductor
from .remote import RemoteConductor
from .settings import MIN_LOGIC_H, MIN_LOGIC_W, Settings
from .webui import ICON_ICO_PATH, CuePanel

DEFAULT_TITLE = "chrono-handoff"

#: Taskbar identity for the native window.  The same value the desktop shell's
#: bundle carries (``tauri.conf.json`` -> ``identifier``), so both hosts are one
#: product in the taskbar rather than "this app" and "a python script".
APP_USER_MODEL_ID = "dev.chrono.handoff"


def _claim_taskbar_identity() -> bool:
    """Give this process its own taskbar group, before any window is created.

    Hosted by a shared interpreter, the window would otherwise group with every
    other ``python.exe`` window: one taskbar button, one icon, shared between
    unrelated programs.  Windows reads this per *process*, so it must be called
    before the first window exists -- hence here rather than in ``_boot``.

    A failure is not fatal: the window still works, it just shares a group.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            == 0
        )
    except Exception:
        return False


def _set_on_top(window: Any, value: bool) -> bool:
    """Set always-on-top across pywebview versions.

    ``create_window(on_top=...)`` and ``Window.on_top`` are not consistently
    available/settable, so fall back to the Win32 call directly.  Failing to
    stay on top is a usability regression, not a correctness one, so every
    failure is swallowed.
    """
    try:
        window.on_top = bool(value)
        return True
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
            if hwnd:
                HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
                SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
                ctypes.windll.user32.SetWindowPos(
                    hwnd, HWND_TOPMOST if value else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
                )
                return True
        except Exception:
            pass
    return False


#: Set CHRONO_RESIZE_DEBUG=1 to trace every resize iteration.
_RESIZE_DEBUG = os.environ.get("CHRONO_RESIZE_DEBUG") == "1"

#: Which window edges the panel offers as resize grips.
RESIZE_EDGES = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}

#: Minimum window size, in **logical** pixels (what the user perceives and what
#: create_window takes).  Converted to physical for SetWindowPos -- see
#: _dpi_scale: this machine runs at 150%, so the physical minimum is 1.5x this.
#:
#: Imported from settings.py because the settings file's own clamp has to use
#: the same floor -- it used to clamp to 380x240, sizes no window would take.


def _dpi_scale(window: Any) -> float:
    """Physical pixels per CSS pixel for this window.

    Getting this wrong is the whole ballgame for resize: ``screenX`` from the
    page is in CSS pixels, while ``GetWindowRect``/``SetWindowPos`` are in device
    pixels.  On a 150%-scaled display (measured: dpi=144, devicePixelRatio=1.5)
    ignoring the ratio makes every drag resize the window by the wrong factor --
    a 110 px drag grew it by 503 px in the first attempt.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes

        dpi = ctypes.windll.user32.GetDpiForWindow(_hwnd(window))
        return (dpi / 96.0) if dpi else 1.0
    except Exception:
        try:
            import ctypes

            return ctypes.windll.user32.GetDeviceCaps(
                ctypes.windll.user32.GetDC(0), 88  # LOGPIXELSX
            ) / 96.0
        except Exception:
            return 1.0


def _min_px(window: Any) -> tuple[int, int]:
    scale = _dpi_scale(window)
    return int(MIN_LOGIC_W * scale), int(MIN_LOGIC_H * scale)


def _to_logical(window: Any, w: int, h: int) -> tuple[int, int]:
    scale = _dpi_scale(window) or 1.0
    return int(round(w / scale)), int(round(h / scale))


def _hwnd(window: Any) -> int:
    import ctypes

    # pywebview exposes no hwnd; the title is unique for our own window.
    return int(ctypes.windll.user32.FindWindowW(None, window.title))


def _clamp_to_screens(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """Pull a window back onto a visible screen if it would open off-screen.

    A remembered position is a liability: unplug the external monitor, change
    resolution, or change the DPI scale, and the saved coordinates can land
    entirely outside the desktop.  Then the widget starts *invisible* and there
    is nothing on screen to drag back -- the user has to edit the JSON by hand.
    Measured here: a stale y=2047 on a 1067-tall desktop.

    Requires at least a corner's worth of overlap with the virtual desktop;
    otherwise the window is centred on the primary monitor.
    """
    if sys.platform != "win32":
        return x, y
    try:
        import ctypes

        u = ctypes.windll.user32
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)     # virtual screen origin
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)     # virtual screen size
    except Exception:
        return x, y
    if vw <= 0 or vh <= 0:
        return x, y
    margin = 80                               # keep a grabbable strip on screen
    if vx - margin < x < vx + vw - margin and vy - margin < y < vy + vh - margin:
        return x, y
    cx = vx + max(0, (vw - w) // 2)
    cy = vy + max(0, (vh - h) // 2)
    return int(cx), int(cy)


def _apply_move(window: Any, x: int, y: int) -> None:
    """Move without resizing (SWP_NOSIZE), in physical pixels."""
    if sys.platform == "win32":
        import ctypes

        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        ctypes.windll.user32.SetWindowPos(
            _hwnd(window), 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        )
        return
    window.move(int(x), int(y))


def _window_rect(window: Any) -> tuple[int, int, int, int]:
    """(x, y, w, h) in physical device pixels."""
    import ctypes
    import ctypes.wintypes

    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(_hwnd(window), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _apply_geometry(window: Any, x: int, y: int, w: int, h: int) -> None:
    """Move and resize in ONE Win32 call, in physical pixels.

    Deliberately bypasses pywebview's ``move``/``resize``: those are two
    separately marshalled calls (a frame can be presented between them, so the
    window visibly jumps), and their units are logical while GetWindowRect is
    physical.  ``SetWindowPos`` is atomic, synchronous and unambiguously in
    device pixels -- which is what makes "the pointer moved 110 px and the window
    grew 110 px" actually true.
    """
    if sys.platform == "win32":
        import ctypes

        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        ctypes.windll.user32.SetWindowPos(
            _hwnd(window), 0, int(x), int(y), int(w), int(h), SWP_NOZORDER | SWP_NOACTIVATE
        )
        return
    window.move(int(x), int(y))
    window.resize(int(w), int(h))


def _set_foreground(window: Any) -> bool:
    """Force a window to the front on Windows.

    ``on_top`` keeps the widget above others but does **not** give it focus, and
    a window that stays behind the editor with the board output on top is a
    window the operator will not notice.  ``SetForegroundWindow`` is subject to
    focus-stealing prevention, so this is best-effort: the flashing and the
    top-most flag still carry the message if it fails.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _bind_css_window_tweaks(window: Any, settings: Settings) -> None:
    """Apply window opacity *without* touching the theme's colours.

    The previous implementation wrote a literal dark colour into the document's
    inline style::

        documentElement.style.background = 'rgba(11,14,20,0.96)'
        body.style.background = 'transparent'

    An inline style outranks every stylesheet rule, so that hardcoded dark
    background permanently overrode the theme.  In light mode the title bar
    (which uses a themed variable) turned light while the content stayed dark,
    leaving dark text on a dark background -- effectively unreadable.

    Now opacity is expressed as a CSS variable and the colour keeps coming from
    the active theme, so the two features cannot fight.
    """
    try:
        o = float(settings.opacity)
        window.evaluate_js(
            "document.documentElement.style.setProperty('--win-alpha', %r);" % o
        )
    except Exception:
        pass


class CueWindow:
    """Owns the conductor, the panel server and the OS window in one process."""

    def __init__(
        self,
        conductor: Any | None = None,
        settings: Settings | None = None,
        settings_path: Path | str | None = None,
        panel_port: int = 0,
        on_top: bool | None = None,
        frameless: bool | None = None,
        width: int | None = None,
        height: int | None = None,
        start_hidden: bool = False,
        debug: bool = False,
    ) -> None:
        self.settings = settings or Settings.load(settings_path)
        if on_top is not None:
            self.settings.always_on_top = on_top
        if frameless is not None:
            self.settings.frameless = frameless
        if width is not None:
            self.settings.width = width
        if height is not None:
            self.settings.height = height

        self.conductor = conductor or Conductor(audio="null", visual=False)
        #: Set when this window is a display client for another process's
        #: conductor (see :class:`chrono_handoff.remote.RemoteConductor`).  In
        #: that mode the window must NOT own a clock: two conductors would each
        #: schedule the cue and the human would hear every beat twice.
        self.remote_conductor: RemoteConductor | None = (
            conductor if isinstance(conductor, RemoteConductor) else None
        )
        #: True only when this window started its own conductor, so it is the
        #: one that must shut it down.
        self._owns_conductor = conductor is None
        self.panel = CuePanel(
            self.conductor,
            host="127.0.0.1",
            port=panel_port,
            settings_path=settings_path,
        )
        self.panel.settings = self.settings
        self.panel.bind()
        self.window: Any = None
        self.chrome_info: dict[str, Any] = {}
        self.debug = debug
        self._start_hidden = start_hidden
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def url(self) -> str:
        return self.panel.url + "?shell=desktop"

    @property
    def browser_url(self) -> str:
        return self.panel.url

    # -- js bridge ---------------------------------------------------------

    def _register_bridge(self, window: Any) -> None:
        """Expose the JS bridge.

        Note the API shape: pywebview 6.x ``Window.expose(*functions)`` takes
        **plain functions keyed by ``__name__``** -- it does not accept a class
        or instance.  Passing one raises ``TypeError`` *before*
        ``webview.start()``, which is a nasty failure mode: the HTTP panel is
        already listening, so the program looks alive while no window ever
        appears.  Hence plain closures below, and ``__name__`` must match what
        the page calls (``window.pywebview.api.<name>``).
        """
        panel = self.panel
        win = window
        maximized = False
        session: dict[str, Any] = {}
        drag_session: dict[str, int] = {}

        # -- settings ------------------------------------------------------
        # Settings are NOT on the bridge.  This process also serves the page
        # over HTTP (``url`` is ``panel.url``), so the page reads and writes
        # them with ``fetch("/api/settings")``.  The native window reacts
        # through ``panel.window_hooks``, which CuePanel.update_settings() and
        # reset_settings() call -- so an HTTP write and a bridge write would do
        # the same thing, and a bridge copy is dead weight with a second,
        # divergent path to maintain.
        #
        # Hooks take the new value and are called for the keys listed here.
        # ``scale`` (a page-side UI scale) and ``frameless`` (fixed at window
        # creation) have no runtime hook on purpose.
        def _resize_to_settings(_value: Any = None) -> None:
            try:
                win.resize(int(panel.settings.width), int(panel.settings.height))
            except Exception:
                pass

        panel.window_hooks.update({
            "always_on_top": lambda value: _set_on_top(win, bool(value)),
            "opacity": lambda _value: _bind_css_window_tweaks(win, panel.settings),
            "width": _resize_to_settings,
            "height": _resize_to_settings,
        })

        # -- window controls ----------------------------------------------
        def set_on_top(value: bool = True) -> dict:
            panel.settings.always_on_top = bool(value)
            panel.settings.save(panel.settings_path)
            _set_on_top(win, bool(value))
            return {"on_top": bool(value)}

        def apply_theme(theme: str = "dark", glass: bool | None = None) -> dict:
            """Re-apply the native chrome after a theme change.

            The DWM attributes are per-window state, not CSS: switching to light
            mode has to flip the immersive-dark flag and the backdrop material,
            or the frame stays dark around a light card.
            """
            if glass is not None:
                panel.settings.glass = bool(glass)
            info = {}
            if panel.settings.glass and sys.platform == "win32":
                info = winshell.apply_floating(win, theme=theme)
            _bind_css_window_tweaks(win, panel.settings)
            return {"ok": True, "theme": theme, **info}

        def set_opacity(value: float = 1.0) -> dict:
            panel.settings.opacity = float(value)
            panel.settings.save(panel.settings_path)
            _bind_css_window_tweaks(win, panel.settings)
            return {"opacity": panel.settings.opacity}

        def minimize() -> dict:
            try:
                win.minimize()
            except Exception:
                pass
            return {"ok": True}

        def close() -> dict:
            try:
                win.destroy()
            except Exception:
                pass
            return {"ok": True}

        def toggle_maximize() -> dict:
            """Maximize/restore the widget.

            Tracked ourselves rather than queried from the OS: pywebview's
            ``maximize()`` has no reliable "is maximized" query, and a toggle
            that guesses wrong is worse than none.
            """
            nonlocal maximized
            try:
                if maximized:
                    win.restore()
                else:
                    win.maximize()
                maximized = not maximized
            except Exception:
                return {"ok": False}
            return {"ok": True, "maximized": maximized}

        # -- interactive resize -------------------------------------------
        # A frameless WebView2 window has no OS non-client area, so it cannot be
        # resized by dragging its borders (measured: all eight directions inert).
        # Hence our own grips.
        #
        # The drag is driven by the PAGE, not by Python polling the cursor.  An
        # earlier polling implementation was a race: the pointerdown -> JS ->
        # bridge -> thread-start path costs enough that a quick flick (about
        # 0.28 s) released the button before the first poll, so the loop exited
        # immediately and the window did not move at all.  With the page sending
        # explicit deltas there is no timing assumption to violate, and a fast
        # drag works exactly like a slow one.
        #
        # The page supplies ``dx``/``dy`` in CSS pixels from ``screenX/screenY``
        # (which are screen-absolute, so they stay correct even as the window
        # moves out from under the cursor) and Python applies them to the
        # baseline captured at pointerdown.

        # -- window drag ---------------------------------------------------
        # pywebview's easy_drag=True makes the WHOLE window draggable, with no
        # way to exclude interactive elements -- so dragging a volume slider also
        # dragged the window.  We implement the drag ourselves, driven from the
        # title bar only, and set easy_drag=False.
        #
        # Same absolute-delta contract as the resize grips: the page sends the
        # total offset since pointerdown and Python applies it to the baseline.

        def begin_drag() -> dict:
            x, y, _w, _h = _window_rect(win)
            drag_session.clear()
            drag_session.update(x=x, y=y)
            return {"ok": True, "x": x, "y": y}

        def update_drag(dx: float = 0, dy: float = 0) -> dict:
            if not drag_session:
                return {"ok": False, "error": "no drag in progress"}
            scale = _dpi_scale(win)
            x = drag_session["x"] + int(round(float(dx) * scale))
            y = drag_session["y"] + int(round(float(dy) * scale))
            try:
                _apply_move(win, x, y)
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "x": x, "y": y}

        def end_drag() -> dict:
            if drag_session and panel.settings.remember_position:
                try:
                    x, y, _w, _h = _window_rect(win)
                    panel.settings.x, panel.settings.y = x, y
                    panel.settings.save(panel.settings_path)
                except Exception:
                    pass
            drag_session.clear()
            return {"ok": True}

        def begin_resize(edge: str = "se") -> dict:
            if edge not in RESIZE_EDGES:
                return {"ok": False, "error": f"bad edge {edge!r}"}
            x, y, w, h = _window_rect(win)
            session.clear()
            session.update(edge=edge, x=x, y=y, w=w, h=h)
            return {"ok": True, "edge": edge, "x": x, "y": y, "w": w, "h": h}

        def update_resize(dx: float = 0, dy: float = 0) -> dict:
            """Apply the TOTAL pointer delta since pointerdown to the baseline.

            ``dx``/``dy`` are absolute-since-drag-start, not incremental.  If a
            caller ever passes increments instead, only the last one takes
            effect and drags appear to move by a few pixels -- that exact bug
            shipped once.
            """
            if not session:
                return {"ok": False, "error": "no resize in progress"}
            scale = _dpi_scale(win)
            ddx, ddy = int(round(float(dx) * scale)), int(round(float(dy) * scale))
            edge = session["edge"]
            x0, y0, w0, h0 = session["x"], session["y"], session["w"], session["h"]

            min_w, min_h = _min_px(win)
            x, y, w, h = x0, y0, w0, h0
            if "e" in edge:
                w = max(min_w, w0 + ddx)
            if "s" in edge:
                h = max(min_h, h0 + ddy)
            if "w" in edge:
                w = max(min_w, w0 - ddx)
                x = x0 + (w0 - w)           # dragging the left edge moves the origin
            if "n" in edge:
                h = max(min_h, h0 - ddy)
                y = y0 + (h0 - h)

            try:
                _apply_geometry(win, x, y, w, h)
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "x": x, "y": y, "w": w, "h": h}

        def end_resize() -> dict:
            """Remember the size the user settled on."""
            if session and panel.settings.remember_position:
                try:
                    x, y, w, h = _window_rect(win)
                    panel.settings.x, panel.settings.y = x, y
                    panel.settings.width, panel.settings.height = _to_logical(win, w, h)
                    panel.settings.save(panel.settings_path)
                except Exception:
                    pass
            session.clear()
            return {"ok": True}

        # NOTE: no write_position().  An earlier version exposed one so the page
        # could report where the user dragged the widget, but _on_closed() reads
        # x/y/size straight off the real window -- which is more accurate and
        # cannot disagree with the OS.  Dead bridge surface is worse than none:
        # it invites a future caller to trust a second source of truth.
        #
        # What is left is *only* window chrome: an operation on the OS window,
        # which no HTTP route can perform because the server does not own the
        # window.  Settings and health used to be here too; they moved to HTTP.
        # The contract is written down in docs/shell-contract.md -- keep the two
        # in step, and add a probe to SHELL_BACKENDS in statics/panel.html when
        # a new host (Tauri, Qt) implements it.
        # -- diagnostics ------------------------------------------------
        def page_note(text: str = "") -> dict:
            """A log sink the page can write to.

            The page has no console anyone can read -- a throw inside its script
            leaves a panel that looks merely idle, with nothing anywhere to say
            what happened.  Kept in step with the Tauri shell's command of the
            same name so the seam behaves the same on either host.
            """
            print(f"[page] {text}", flush=True)
            return {"ok": True}

        def _update_answer(op: str) -> dict[str, Any]:
            """The seam's three update operations, answered rather than omitted.

            This host has no updater -- auto-update is a property of the desktop
            shell's install, not of a pywebview window that may be running from a
            source checkout.  The seam's rule is that a host either implements an
            operation or says it cannot (see docs/shell-contract.md); a page that
            calls into nothing finds out by breaking, and "the button does
            nothing" is the failure mode this contract exists to prevent.
            """
            return {
                "phase": "unsupported",
                "detail": f"{op}: pywebview 宿主没有更新器；自动更新属于桌面外壳的安装形态",
            }

        def update_status() -> dict[str, Any]:
            return _update_answer("update_status")

        def update_check() -> dict[str, Any]:
            return _update_answer("update_check")

        def update_install() -> dict[str, Any]:
            return _update_answer("update_install")

        window.expose(
            apply_theme,
            page_note,
            set_on_top,
            set_opacity,
            minimize,
            close,
            toggle_maximize,
            begin_resize,
            update_resize,
            end_resize,
            begin_drag,
            update_drag,
            end_drag,
            update_status,
            update_check,
            update_install,
        )

    def _flash_visible(self) -> None:
        """Briefly show the window so the user can locate it, then hide again."""
        import time as _t

        try:
            self.window.show()
            _t.sleep(2.2)
            self.window.hide()
        except Exception:
            pass

    def _push_settings(self, window: Any) -> None:
        try:
            import json

            cfg = json.dumps(self.panel.settings.to_dict())
            window.evaluate_js(f"window.__chronoApplySettings && window.__chronoApplySettings({cfg})")
        except Exception:
            pass

    # -- run ---------------------------------------------------------------

    # -- raise on a new cue -------------------------------------------------

    def _start_remote_watch(self) -> None:
        """Watch the daemon and raise the window whenever a cue is armed.

        There is no cross-process window control: the *window* asks the daemon
        what the current run is and reacts.  That is robust in the ways that
        matter -- a restarted daemon, a changed IPC shape, or a dropped poll all
        self-heal on the next tick, and nothing needs a reverse channel.
        """
        remote = self.remote_conductor
        if remote is None:
            # Display-client only.  A hosted window learns about arming through
            # Conductor.on_armed and must not poll itself.
            return
        remote.watch_in_background()

    def raise_window(self) -> bool:
        """Bring the window to the front (used when a cue is armed)."""
        if self.window is None:
            return False
        try:
            self.window.show()
        except Exception:
            pass
        try:
            self.window.restore()
        except Exception:
            pass
        _set_foreground(self.window)
        return True

    def _on_armed_raise(self, _run: Any = None) -> None:
        """Bring the widget back when a cue is armed (both hosting modes).

        ``-> None`` is what ``on_armed`` asks for: the return value is
        discarded, so forwarding ``raise_window()``'s bool would misdescribe
        the callback.  A missed cue must be impossible to miss, hence both
        hosting modes subscribe to this same function.
        """
        self.raise_window()

    def run(self, on_ready: Any = None, auto_cue: dict | None = None) -> int:
        """Open the window and block until it closes.

        ``auto_cue`` lets the caller demo the component without an agent in the
        loop; it is submitted *after* the window has loaded, so the page is
        already listening when the schedule is pushed.
        """
        try:
            import webview  # noqa: PLC0415
        except Exception as exc:
            return self._run_browser_fallback(f"{type(exc).__name__}: {exc}")

        # Taskbar identity, before any window exists.  Hosted by `python.exe`,
        # this window groups with every other Python window (shared button, shared
        # icon), so the app looked like a script even once it had its own icon.
        # The identifier is the shell's bundle id: same product, one taskbar
        # identity, whichever host is running.
        _claim_taskbar_identity()

        s = self.panel.settings
        self.conductor.start()
        if s.x is not None and s.y is not None:
            # Never open off-screen -- see _clamp_to_screens.
            s.x, s.y = _clamp_to_screens(int(s.x), int(s.y), int(s.width), int(s.height))
        try:
            self.window = webview.create_window(
                DEFAULT_TITLE,
                url=self.url,
                width=int(s.width),
                height=int(s.height),
                x=s.x,
                y=s.y,
                resizable=True,          # free resize by dragging the edges
                frameless=bool(s.frameless),
                # False on purpose: easy_drag hijacks every press, so dragging a
                # settings slider would also drag the window.  The title bar
                # drives the drag through begin_drag/update_drag instead.
                easy_drag=False,
                on_top=bool(s.always_on_top),
                min_size=(MIN_LOGIC_W, MIN_LOGIC_H),
                # Transparency is what lets the DWM acrylic backdrop show through.
                # It requires a frameless window (pywebview's transparent path
                # only applies there), which is fine: the page paints its own
                # rounded card and the corners outside it are truly transparent.
                transparent=bool(s.transparent),
                shadow=not bool(s.transparent),
                # Must be a hex TRIPLET: pywebview rejects 8-digit hex with
                # "is not a valid hex triplet color".  Passing "#00000000" here
                # raised inside create_window, so webview.start() never ran and
                # the process simply hung with a window-less HTTP server --
                # a confusing failure with an unrelated-looking cause.
                # Transparency itself comes from `transparent=True`; this is only
                # the colour painted before the page loads.
                background_color=(
                    "#0f172a" if s.theme == "dark" else
                    "#f1f5f9" if s.theme == "light" else "#1a1206"),
                hidden=self._start_hidden,
                confirm_close=False,
            )
            self._register_bridge(self.window)

            # Our own chrome closes/minimises the window, so remember where the
            # user put it (frameless windows lose their position on restart).
            def _on_closed() -> None:
                self._closed = True
                if self.panel.settings.remember_position:
                    try:
                        self.panel.settings.x = int(self.window.x)
                        self.panel.settings.y = int(self.window.y)
                        self.panel.settings.width = int(self.window.width)
                        self.panel.settings.height = int(self.window.height)
                        self.panel.settings.save(self.panel.settings_path)
                    except Exception:
                        pass
                if self._owns_conductor:
                    self.conductor.stop()

            self.window.events.closed += _on_closed

            self._thread = threading.Thread(target=self.panel.serve_forever, args=(False,), daemon=True)
            self._thread.start()

            def _boot() -> None:
                _bind_css_window_tweaks(self.window, self.panel.settings)
                self._push_settings(self.window)
                if self.panel.settings.glass:
                    info = winshell.apply_floating(self.window, theme=self.panel.settings.theme)
                    self.chrome_info = info
                    if self.debug:
                        print(f"[window] floating chrome: {info}", file=sys.stderr)
                # A cue must be impossible to miss: if the widget is minimised,
                # hidden behind other windows, or on another monitor, arming a
                # cue brings it back. That is the entire point of a cue widget.
                #
                # Both hosting modes need this, and they learn about arming from
                # different sources:
                #   * remote (display client): polls the daemon, see _start_remote_watch
                #   * hosted (owns the conductor): subscribes directly
                # The hosted case was missed at first, which meant that once the
                # operator minimised the widget, an agent-armed cue beeped at an
                # empty desk.
                # NOTE: ``self.conductor`` is never None -- __init__ falls back to
                # a fresh Conductor -- so this is an if/else, not an
                # ``elif ... is not None``.  Spelling it as a comparison was worse
                # than redundant: the negative branch narrowed the attribute to
                # None for the rest of ``_boot``, turning a guaranteed-non-None
                # value into an Optional and hiding real mistakes.
                if self.remote_conductor is not None:
                    self.remote_conductor.on_armed(self._on_armed_raise)
                    self._start_remote_watch()
                else:
                    self.conductor.on_armed(self._on_armed_raise)
                if self._start_hidden:
                    # Show once so the human learns where the widget lives.
                    threading.Thread(target=self._flash_visible, daemon=True).start()
                if on_ready is not None:
                    try:
                        on_ready(self)
                    except Exception:
                        pass
                if auto_cue:
                    # Give the page a moment to finish clock sync and start
                    # long-polling, then arm.  A cue armed before the page is
                    # listening would be missed.
                    import time

                    time.sleep(1.0)
                    try:
                        self.conductor.cue(**auto_cue)
                    except Exception:
                        pass

            # The icon is passed to `start`, not to `create_window`: pywebview's
            # WinForms backend reads it from `_state['icon']`, which only
            # `start(icon=...)` fills, and otherwise takes
            # `ExtractIconW(sys.executable)` -- the Python logo.  Measured on the
            # real window: the taskbar showed the interpreter's icon, so the
            # widget looked like a script next to the desktop app's own mark.
            webview.start(
                _boot,
                icon=str(ICON_ICO_PATH) if ICON_ICO_PATH.is_file() else None,
                debug=self.debug,
            )
            return 0
        finally:
            try:
                self.panel.stop()
            except Exception:
                pass
            if self._owns_conductor:
                try:
                    self.conductor.stop()
                except Exception:
                    pass

    def _run_browser_fallback(self, reason: str = "") -> int:
        """No usable native backend: degrade to a browser tab, LOUDLY.

        Degrading is correct -- a cue panel that refuses to open is worse than
        a cue panel in a tab.  Degrading *silently* is not: it is how a user
        ends up believing the project is a web app (and how a missing native
        window gets mistaken for a rendering bug).  So the downgrade is
        announced on every channel available: stderr, the panel itself, and
        ``chrono doctor``.
        """
        detail = f" ({reason})" if reason else ""
        print(
            f"chrono-handoff: no native window backend{detail} -- opening a browser tab instead.\n"
            "  A tab cannot go always-on-top and has browser chrome stealing the space\n"
            "  the countdown needs.  For the real widget:\n"
            f"      {sys.executable} -m pip install pywebview\n"
            "  (this interpreter is the one that must have it -- check with "
            "chrono doctor, which reports the panel backend)",
            file=sys.stderr,
            flush=True,
        )
        # Surface it in the UI too: the window is often spawned detached, where
        # stderr is a log file nobody reads, and the operator has no idea why
        # the widget looks like a web page.  Short and action-free on purpose --
        # an exception repr belongs in stderr, not in front of the operator.
        self.panel.degraded_reason = "以浏览器标签页运行：缺少 pywebview，无法置顶、无边框"
        self.conductor.start()
        self.panel.open_browser(self.browser_url)
        try:
            self.panel.serve_forever(open_browser=False)
        except KeyboardInterrupt:
            pass
        finally:
            self.panel.stop()
            if self._owns_conductor:
                self.conductor.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="chrono-window", description="native cue-panel window")
    ap.add_argument("--instruction", default="", help="arm this cue immediately")
    ap.add_argument("--in", dest="in_s", type=float, default=None, help="seconds until the action should land")
    ap.add_argument("--ioi", type=float, default=None, help="beat interval ms (overrides settings)")
    ap.add_argument("--lead", type=float, default=None, help="countdown seconds")
    ap.add_argument("--tail-beats", type=int, default=None)
    ap.add_argument("--paused", action="store_true", help="arm but wait for the panel's Start button")
    ap.add_argument("--frameless", action="store_true", default=None, help="custom title bar (default: from settings)")
    ap.add_argument("--bordered", action="store_true", help="use the OS title bar instead")
    ap.add_argument("--no-on-top", action="store_true", help="do not keep the window above others")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--port", type=int, default=0, help="panel port (0 = pick a free one)")
    ap.add_argument("--hidden", action="store_true", help="start hidden (tray-style)")
    ap.add_argument("--debug", action="store_true", help="enable the webview inspector")
    ap.add_argument("--print-url", action="store_true", help="print the panel URL even with a native window")
    ap.add_argument("--attach", type=int, default=None, metavar="IPC_PORT",
                    help="display client only: watch an existing daemon instead of owning a conductor. "
                         "Cues then fire in the daemon even if this window is closed.")
    ap.add_argument("--ipc-host", default="127.0.0.1")
    args = ap.parse_args(argv)

    settings = Settings.load()
    if args.width:
        settings.width = args.width
    if args.height:
        settings.height = args.height

    cue_kwargs: dict[str, Any] = {}
    if args.instruction:
        cue_kwargs = {
            "instruction": args.instruction,
            "target_in_s": args.in_s if args.in_s is not None else settings.default_target_in_s,
            "lead_s": args.lead if args.lead is not None else settings.lead_s,
            "ioi_ms": args.ioi if args.ioi is not None else settings.ioi_ms,
            "tail_beats": args.tail_beats if args.tail_beats is not None else settings.tail_beats,
            "duration_s": settings.duration_s,
            # The panel itself reports the press through its own HTTP route, so
            # the conductor should not also try to read the console.
            "expect_source": "none",
            "paused": args.paused,
            "open_panel": False,
        }

    conductor: Any = None
    if args.attach is not None:
        from .mcp_server import ConductorClient

        client = ConductorClient(args.ipc_host, args.attach, timeout_s=5.0)
        if not client.healthy():
            print(
                f"chrono-handoff: no conductor at {args.ipc_host}:{args.attach}\n"
                f"  start one first:  chrono daemon --background",
                file=sys.stderr,
            )
            return 1
        conductor = RemoteConductor(client)
        if cue_kwargs:
            # Arming is the daemon's job; a display client must not do it.
            print("chrono-handoff: --attach cannot arm a cue (the daemon owns the clock); ignoring it", file=sys.stderr)
            cue_kwargs = {}

    win = CueWindow(
        conductor=conductor,
        settings=settings,
        panel_port=args.port,
        on_top=False if args.no_on_top else None,
        frameless=False if args.bordered else (True if args.frameless else None),
        width=args.width,
        height=args.height,
        start_hidden=args.hidden,
        debug=args.debug,
    )
    if args.print_url:
        print(f"panel: {win.browser_url}")

    def _ready(w: CueWindow) -> None:
        if cue_kwargs:
            import time

            time.sleep(0.6)
            r = w.conductor.cue(**cue_kwargs)
            print(f"cue armed: {r['run_id']}  ({'paused' if args.paused else 'running'})", flush=True)

    return win.run(on_ready=_ready if cue_kwargs else None)


if __name__ == "__main__":
    raise SystemExit(main())
