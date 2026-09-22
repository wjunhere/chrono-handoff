"""Shared helpers for driving a real native window from a test.

Two problems this solves, both of which produced *false failures* in the UI
suites and cost real time to diagnose:

1. **Synthetic mouse input goes to the foreground window.**  ``SetCursorPos`` +
   ``mouse_event`` are delivered to whatever window the OS considers active.  A
   drag aimed at a background window therefore does nothing at all -- no page
   handler runs, the geometry never changes, and the suite reports "dragging the
   title bar does not move the window" when the product is fine.  Measured
   directly: with the window in the background, a page-level ``pointerdown``
   capture listener never fired.

2. **Windows blocks ``SetForegroundWindow`` from a background process.**  A
   plain call is silently ignored.  Attaching the input queues of the two
   threads lifts the restriction, which is why ``focus_window`` does that rather
   than just calling ``ShowWindow`` harder.

``synthetic_input_works`` is exposed so a suite can *say* it could not exercise
the page's pointer handling, instead of reporting a pass it did not earn.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time

_u = ctypes.windll.user32


def find_window(title: str) -> int:
    return int(_u.FindWindowW(None, title))


def get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def focus_window(hwnd: int) -> bool:
    """Best-effort foreground activation.  Returns the resulting state."""
    if not hwnd:
        return False
    if _u.GetForegroundWindow() == hwnd:
        return True

    fg = _u.GetForegroundWindow()
    fg_tid = _u.GetWindowThreadProcessId(fg, None) if fg else 0
    my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != my_tid:
        attached = bool(_u.AttachThreadInput(fg_tid, my_tid, True))
    try:
        _u.ShowWindow(hwnd, 9)          # SW_RESTORE
        _u.BringWindowToTop(hwnd)
        _u.SetForegroundWindow(hwnd)
        _u.SetActiveWindow(hwnd)
        _u.SetFocus(hwnd)
    finally:
        if attached:
            _u.AttachThreadInput(fg_tid, my_tid, False)
    time.sleep(0.35)
    return _u.GetForegroundWindow() == hwnd


def drag(x0: int, y0: int, x1: int, y1: int, steps: int = 14, settle: float = 0.35) -> None:
    """A real left-button drag with intermediate motion the page can sample."""
    _u.SetCursorPos(x0, y0)
    time.sleep(0.15)
    _u.mouse_event(0x0002, 0, 0, 0, 0)               # LEFT DOWN
    time.sleep(0.15)
    for i in range(1, steps + 1):
        _u.SetCursorPos(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        time.sleep(0.02)
    time.sleep(0.15)
    _u.mouse_event(0x0004, 0, 0, 0, 0)               # LEFT UP
    time.sleep(settle)


def synthetic_input_works(evaluate_js, hwnd: int, rect_fn) -> bool:
    """Probe whether a synthetic drag can reach the page at all.

    Installs a page-level capture listener, performs a focus attempt and a
    small drag, then reads the listener back.  When this returns False the
    caller must not assert drag behaviour -- it has no way to deliver input.
    """
    evaluate_js(
        "window.__probe = 0;"
        "document.addEventListener('pointerdown', () => { window.__probe++; }, true);"
        "true;"
    )
    focus_window(hwnd)
    x, y, w, h = rect_fn()
    drag(x + w // 2, y + 8, x + w // 2 + 30, y + 8 + 30, steps=6)
    try:
        return bool(evaluate_js("window.__probe > 0"))
    except Exception:
        return False
