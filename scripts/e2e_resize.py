"""Does the custom resize grip actually resize the window?

A frameless WebView2 window cannot be resized by its OS borders (measured: all
eight directions are inert), so the panel draws grips and the *Python* side
tracks the cursor.  This drives a real mouse drag over a grip and checks the
window geometry changed in the expected direction.

It also checks the window buttons: minimize, maximize/restore, close.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Isolate settings/calibration so a run cannot inherit a saved window
# position from an earlier one (a stale off-screen y made drags look
# broken while the real cause was the window opening outside the desktop).
os.environ.setdefault("CHRONO_HOME", str(Path(__file__).resolve().parents[1] / "logs" / "_home"))

from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.window import CueWindow  # noqa: E402

OUT: dict = {}
FAILURES: list[str] = []
TITLE = "chrono-handoff"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def hwnd() -> int:
    return int(ctypes.windll.user32.FindWindowW(None, TITLE))


def rect() -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd(), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def focus_window() -> bool:
    """Bring the cue window to the foreground.

    Synthetic mouse input is delivered to whatever window the OS considers
    active, so a drag against a *background* window does nothing at all: the
    events go somewhere else and no page handler runs.  Windows also refuses a
    plain ``SetForegroundWindow`` from a process that does not own the current
    foreground, so several documented workarounds are applied in order.
    """
    u = ctypes.windll.user32
    h = hwnd()
    if not h:
        return False
    if u.GetForegroundWindow() == h:
        return True
    # Attaching our input queue to the foreground thread lifts the restriction.
    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != my_tid:
        attached = bool(u.AttachThreadInput(fg_tid, my_tid, True))
    try:
        u.ShowWindow(h, 9)                 # SW_RESTORE
        u.BringWindowToTop(h)
        u.SetForegroundWindow(h)
        u.SetActiveWindow(h)
        u.SetFocus(h)
    finally:
        if attached:
            u.AttachThreadInput(fg_tid, my_tid, False)
    time.sleep(0.35)
    return u.GetForegroundWindow() == h


def drag(x0: int, y0: int, x1: int, y1: int, steps: int = 14,
         edge: str | None = None) -> None:
    """Drag the cursor, or fall back to the bridge call if input cannot land.

    ``edge`` selects the fallback path; when it is given the two routes apply
    the same delta, so the assertion below is about the product either way.
    """
    u = ctypes.windll.user32
    if INPUT_UNAVAILABLE:
        if edge:
            resize_by_api(edge, x1 - x0, y1 - y0)
        return
    focus_window()
    u.SetCursorPos(x0, y0)
    time.sleep(0.15)
    u.mouse_event(0x0002, 0, 0, 0, 0)               # left down
    time.sleep(0.15)
    for i in range(1, steps + 1):
        u.SetCursorPos(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        time.sleep(0.02)
    time.sleep(0.15)
    u.mouse_event(0x0004, 0, 0, 0, 0)               # left up
    time.sleep(0.35)


def resize_by_api(edge: str, dx: int, dy: int) -> None:
    """Drive the same bridge calls the page makes, without synthetic input.

    Used as a fallback so the *product* behaviour can still be asserted on a
    machine where synthetic mouse input cannot reach the window (headless, a
    locked session, or a foreground-lock the workarounds cannot break).  This
    path is weaker -- it skips the page's pointer handling -- and the suite says
    so explicitly rather than reporting a silent pass.
    """
    js = _WIN[0].window.evaluate_js
    js(f"window.pywebview.api.begin_resize({edge!r})")
    time.sleep(0.15)
    js(f"window.pywebview.api.update_resize({dx}, {dy})")
    time.sleep(0.15)
    js("window.pywebview.api.end_resize()")
    time.sleep(0.25)


#: Set by main() so the helpers can reach the live window.
_WIN: list = []

#: Flipped to True once a synthetic drag is observed to have no effect, so the
#: remaining assertions use the bridge path instead of silently passing.
INPUT_UNAVAILABLE = False


def main() -> int:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); skipping.")
        return 0

    settings_path = Path("logs/_rs/settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_rs")
    win = CueWindow(conductor=conductor, panel_port=0, settings_path=settings_path,
                    width=460, height=600, on_top=True)
    _WIN.clear()
    _WIN.append(win)

    def ready(w) -> None:
        time.sleep(2.4)
        try:
            OUT["grips_in_dom"] = w.window.evaluate_js(
                "Array.from(document.querySelectorAll('.grip')).map(g => g.dataset.grip)"
            )
            OUT["grips_visible"] = w.window.evaluate_js(
                "(() => { const g = document.querySelector('.grip.se');"
                " const b = g.getBoundingClientRect();"
                " const el = document.elementFromPoint(b.x + b.width/2, b.y + b.height/2);"
                " return !!(el && el.classList.contains('grip')); })()"
            )

            # Establish once whether synthetic mouse input reaches the window.
            # Everything below depends on it, so probe it rather than assume --
            # and once means *once*: mixing the two paths inside a run is worse
            # than either.  A real drag leaves the host's drag baseline and the
            # persisted size in a state the bridge sequence then computes its own
            # deltas from, and the result was a window that would not shrink.
            #
            # A landing (the rectangle changed) is necessary but not sufficient:
            # the press can land on a *different* grip, which counts as movement
            # while every edge-specific assertion below still fails.  Requiring
            # the drag to move the width *and* the height it was aimed at is the
            # cheapest signal that this is the grip it meant to grab; a run that
            # does not clear that bar takes the bridge for the whole suite.
            x, y, w0, h0 = rect()
            drag(x + w0 - 3, y + h0 - 3, x + w0 + 110, y + h0 + 90, edge='se')
            _, _, w1, h1 = rect()
            landed = w1 >= w0 + 50 and h1 >= h0 + 40
            OUT["synthetic_input"] = landed
            global INPUT_UNAVAILABLE
            INPUT_UNAVAILABLE = not landed
            OUT["foreground"] = OUT.get("foreground") or (
                ctypes.windll.user32.GetForegroundWindow() == hwnd())
            if not landed:
                resize_by_api("se", 110, 90)
                _, _, w1, h1 = rect()

            # ---- resize from the SE corner -------------------------------
            OUT["se"] = {"before": [w0, h0], "after": [w1, h1]}

            # ---- resize from the E edge ----------------------------------
            x, y, w0, h0 = rect()
            drag(x + w0 - 3, y + h0 // 2, x + w0 + 90, y + h0 // 2, edge='e')
            _, _, w2, h2 = rect()
            OUT["e"] = {"before": [w0, h0], "after": [w2, h2]}

            # ---- resize from the W edge (origin must move too) -----------
            x, y, w0, h0 = rect()
            drag(x + 3, y + h0 // 2, x - 80, y + h0 // 2, edge='w')
            x3, _, w3, _ = rect()
            OUT["w"] = {"before_x": x, "after_x": x3, "before_w": w0, "after_w": w3}

            # ---- a FAST flick must work too -------------------------------
            # The polling implementation failed exactly here: a ~0.28 s drag
            # released the button before the first poll, so nothing moved.
            x, y, w0, h0 = rect()
            drag(x + w0 - 3, y + h0 - 3, x + w0 + 70, y + h0 + 50, steps=4, edge='se')
            _, _, wf, hf = rect()
            OUT["fast_flick"] = {"before": [w0, h0], "after": [wf, hf]}

            # ---- shrink must respect the minimum -------------------------
            x, y, w0, h0 = rect()
            drag(x + w0 - 3, y + h0 - 3, x + 3, y + 3, edge='se')
            _, _, w4, h4 = rect()
            OUT["shrunk"] = [w4, h4]

            # ---- maximize / restore --------------------------------------
            # Order matters: maximize() only *enlarges* from a smaller size, so
            # start from an explicitly small window.  Measuring it after the
            # drags above compared against an already-oversized window and made
            # a working maximize look broken.
            w.window.resize(420, 560)
            time.sleep(0.9)
            _, _, wb, hb = rect()
            OUT["before_max"] = [wb, hb]
            js = w.window.evaluate_js
            js("(async () => { await window.pywebview.api.toggle_maximize(); })();")
            time.sleep(1.0)
            _, _, wm, hm = rect()
            OUT["maximized"] = [wm, hm]
            js("(async () => { await window.pywebview.api.toggle_maximize(); })();")
            time.sleep(1.0)
            _, _, wr, hr = rect()
            OUT["restored"] = [wr, hr]

            # ---- UI still healthy after all that -------------------------
            OUT["clock_text"] = js("document.getElementById('clock').textContent")
            OUT["body"] = js("document.body.className")
        except Exception as exc:
            OUT["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            time.sleep(0.3)
            try:
                w.window.destroy()
            except Exception:
                pass

    win.run(on_ready=ready)

    print()
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))
    print()

    g = OUT.get("grips_in_dom") or []
    check("resize grips exist in the DOM", len(g) == 8, f"{len(g)} grips: {g}")
    check("a grip is hit-testable at the corner", OUT.get("grips_visible") is True)

    print(f"  [INFO] synthetic mouse input reaches the window: {OUT.get('synthetic_input')}"
          f" | foreground: {OUT.get('foreground')}")
    if OUT.get("synthetic_input") is False:
        print("  [WARN] synthetic input could not reach the window; the resize")
        print("         assertions below exercised the Python bridge directly")
        print("         (page pointer handling NOT covered by this run).")

    se = OUT.get("se") or {}
    check("drag SE corner grows the window",
          se.get("after", [0, 0])[0] > se.get("before", [0, 0])[0] + 50
          and se.get("after", [0, 0])[1] > se.get("before", [0, 0])[1] + 40,
          f"{se.get('before')} -> {se.get('after')}")

    e = OUT.get("e") or {}
    check("drag right edge widens the window",
          e.get("after", [0, 0])[0] > e.get("before", [0, 0])[0] + 40,
          f"{e.get('before')} -> {e.get('after')}")

    wd = OUT.get("w") or {}
    check("drag left edge moves the origin AND resizes",
          wd.get("after_w", 0) > wd.get("before_w", 0) + 30
          and wd.get("after_x", 0) < wd.get("before_x", 0) - 20,
          f"x {wd.get('before_x')}->{wd.get('after_x')} w {wd.get('before_w')}->{wd.get('after_w')}")

    ff = OUT.get("fast_flick") or {}
    check("fast flick resizes too (no polling race)",
          ff.get("after", [0, 0])[0] > ff.get("before", [0, 0])[0] + 30,
          f"{ff.get('before')} -> {ff.get('after')}")

    sh = OUT.get("shrunk") or [0, 0]
    # minimum is 320x360 logical; this machine runs at 150% DPI, so the physical
    # floor is ~1.5x that.  Assert generously rather than pinning the DPI.
    check("minimum size enforced", 300 <= sh[0] <= 1000 and 340 <= sh[1] <= 1000,
          f"shrunk to {sh} (logical floor is 320x360)")

    bm = OUT.get("before_max") or [0, 0]
    mx, rs = OUT.get("maximized") or [0, 0], OUT.get("restored") or [0, 0]
    check("maximize enlarges the window", mx[0] > bm[0] and mx[1] > bm[1], f"{bm} -> {mx}")
    check("restore returns to the pre-maximize size",
          abs(rs[0] - bm[0]) <= 40 and abs(rs[1] - bm[1]) <= 40, f"{mx} -> {rs} (was {bm})")

    check("UI healthy after resizing", str(OUT.get("clock_text") or "") != ""
          and "shell" in str(OUT.get("body", "")), f"clock={OUT.get('clock_text')!r}")
    if OUT.get("error"):
        check("no runtime errors", False, OUT["error"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("resize + window controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
