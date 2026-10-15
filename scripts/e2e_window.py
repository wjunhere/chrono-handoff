"""Native-window end-to-end check (Windows / WebView2).

Verifies the things that only exist once a real OS window is involved:

* the shell layout actually activates (``body.shell``, our own title bar),
* the JS bridge is reachable and **awaitable** the way the page uses it,
* settings load into the UI through the bridge and persist to disk,
* a cue reaches the page and a press lands with a plausible offset,
* the cue tail keeps pulsing after the response.

Two traps this file exists to document:

1. ``Window.expose()`` in pywebview 6.x takes **plain functions**, not a class
   instance.  Passing an instance raises ``TypeError`` *before*
   ``webview.start()`` -- the HTTP panel is already listening, so the program
   looks alive while no window appears.  That cost real debugging time.
2. ``window.pywebview.api.*`` is **async in JS**.  Wrapping a call in
   ``JSON.stringify`` yields ``{}``; the test must await it, exactly as
   panel.html does.

Run:  python scripts/e2e_window.py     (needs a desktop session; skips otherwise)
"""

from __future__ import annotations

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

from chrono_handoff.settings import Settings  # noqa: E402

FAILURES: list[str] = []
OUT: dict = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<44} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def main() -> int:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); skipping native window checks.")
        return 0

    from chrono_handoff.window import CueWindow  # noqa: PLC0415

    settings_path = Path("logs/_win/settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        settings_path.unlink()

    def ready(w: "CueWindow") -> None:
        win = w.window
        js = win.evaluate_js
        time.sleep(2.5)
        try:
            OUT["body_class"] = js("document.body.className")
            OUT["titlebar_display"] = js("getComputedStyle(document.getElementById('titlebar')).display")
            OUT["settings_overlay"] = js("!!document.getElementById('settings')")
            OUT["bridge_available"] = js("!!(window.pywebview && window.pywebview.api)")
            OUT["shell_backend"] = js("shellBackend()")
            # the liveness poller depends on both of these
            OUT["liveness_wired"] = js("typeof pollHealth === 'function'")
            # evaluate_js does NOT await: an async IIFE hands back a promise and
            # the value reads as {}.  Kick it off, then read the global it sets.
            js("window.__h = null;"
               "(async () => { try { const r = await fetch('/api/health', {cache:'no-store'});"
               " const j = await r.json(); window.__h = !!j.ok; }"
               " catch (e) { window.__h = false; } })();")
            time.sleep(0.7)
            OUT["health_endpoint_ok"] = js("window.__h")
            OUT["cfg_loaded"] = js("!!CFG")
            OUT["form_value"] = js("document.getElementById('s-ioi').value")

            # Settings must travel over HTTP now, not the seam.  Counting POSTs
            # to /api/settings while driving the page's OWN save path is a direct
            # proof -- unlike deleting the bridge keys, which a proxy object may
            # silently ignore, so the test would pass without proving anything.
            js("window.__posts = 0;"
               "window.__saved = null;"
               "const _fetch = window.fetch;"
               "window.fetch = function (u, o) {"
               "  if (String(u) === '/api/settings' && o && o.method === 'POST') window.__posts++;"
               "  return _fetch.apply(this, arguments); };"
               "document.getElementById('s-ioi').value = '350';"
               "document.getElementById('s-opacity').value = '0.71';"
               "saveSettings().then(() => { window.__saved = true; })"
               "              .catch(e => { window.__saved = 'ERR:' + e; });")
            time.sleep(1.0)
            OUT["save_resolved"] = js("window.__saved")
            OUT["settings_posts"] = js("window.__posts")
            OUT["disk_ioi"] = Settings.load(settings_path).ioi_ms
            # The POST is handled on the HTTP server thread, so this also proves
            # CuePanel.window_hooks reaches the OS window from off the UI thread.
            OUT["disk_opacity"] = Settings.load(settings_path).opacity
            OUT["win_alpha_var"] = js("document.documentElement.style.getPropertyValue('--win-alpha')")

            # settings open/close, and the title bar must stay usable (a
            # frameless window whose chrome is covered cannot be moved/closed)
            js("document.getElementById('tb-set').click()")
            time.sleep(0.45)
            OUT["overlay_opens"] = js("document.body.classList.contains('settings-open')")
            OUT["titlebar_usable_while_open"] = js(
                "(() => { const r = document.getElementById('tb-close').getBoundingClientRect();"
                " const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);"
                " return !!(el && el.closest('#titlebar')); })()"
            )
            js("document.getElementById('s-close').click()")
            time.sleep(0.2)
            OUT["overlay_closes"] = js("!document.body.classList.contains('settings-open')")

            # arm a cue and press relative to GO
            run = w.conductor.cue(instruction="press RESET", target_in_s=3.0, lead_s=1.2,
                                  ioi_ms=300, tail_beats=4, expect_source="none",
                                  duration_s=12, open_panel=False)
            time.sleep(1.5)
            OUT["page_has_schedule"] = js("!!S.sched")
            OUT["page_beats"] = js("S.sched ? S.sched.beats.length : 0")
            js("window.__p = null;"
               "setTimeout(() => { window.__p = doPress(); },"
               " Math.max(0, S.zeroMs - (performance.timeOrigin + performance.now()) + 40));")
            time.sleep(2.8)
            r = w.conductor.get(run["run_id"])
            assert r is not None, "the conductor lost a run it had just armed"
            OUT["press_offset_ms"] = None if r.human_offset_ms is None else round(r.human_offset_ms, 1)
            OUT["press_seen"] = r.signal_seen
            OUT["ui_result"] = js("document.getElementById('m-result').textContent")
            time.sleep(1.2)
            evs = r.events
            OUT["tail_after_response"] = sum(
                1 for e in evs
                if e.kind in ("cue_beat", "cue_zero")
                and e.t > next((x.t for x in evs if x.kind == "human_signal"), 1e9)
            )
        except Exception as exc:
            OUT["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            time.sleep(0.3)
            win.destroy()

    CueWindow(panel_port=0, width=460, height=600, settings_path=settings_path).run(
        on_ready=ready, auto_cue=None
    )

    print()
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))
    print()
    check("shell layout activated", "shell" in str(OUT.get("body_class", "")), f"body.class={OUT.get('body_class')!r}")
    check("custom title bar is visible", OUT.get("titlebar_display") == "flex", f"display={OUT.get('titlebar_display')}")
    check("settings overlay exists", OUT.get("settings_overlay") is True)
    check("JS bridge available", OUT.get("bridge_available") is True)
    check("shell seam resolves to pywebview", OUT.get("shell_backend") == "pywebview",
          f"backend={OUT.get('shell_backend')!r}")
    check("liveness poller installed", OUT.get("liveness_wired") is True)
    check("liveness endpoint reachable", OUT.get("health_endpoint_ok") is True)
    check("settings loaded over HTTP", OUT.get("cfg_loaded") is True)
    check("settings form pre-filled", OUT.get("form_value") not in (None, ""), f"ioi={OUT.get('form_value')}")
    check("save path resolved", OUT.get("save_resolved") is True, f"result={OUT.get('save_resolved')!r}")
    check("save went over HTTP (1 POST /api/settings)", OUT.get("settings_posts") == 1,
          f"posts={OUT.get('settings_posts')}")
    check("settings persisted to disk", OUT.get("disk_ioi") == 350.0, f"on-disk ioi={OUT.get('disk_ioi')}")
    check("window_hooks ran: opacity applied to the window from the HTTP thread",
          OUT.get("win_alpha_var") == "0.71" and OUT.get("disk_opacity") == 0.71,
          f"--win-alpha={OUT.get('win_alpha_var')!r} disk={OUT.get('disk_opacity')!r}")
    check("settings overlay opens", OUT.get("overlay_opens") is True)
    check("title bar usable while settings open", OUT.get("titlebar_usable_while_open") is True)
    check("settings overlay closes", OUT.get("overlay_closes") is True)
    check("schedule reaches the page", OUT.get("page_has_schedule") is True, f"{OUT.get('page_beats')} beats")
    off = OUT.get("press_offset_ms")
    check("press recorded near GO+40ms", isinstance(off, (int, float)) and 0 < off < 150, f"offset={off}ms")
    check("press shown in the UI", OUT.get("ui_result") not in ("—", "--", None, ""),
          f"field={OUT.get('ui_result')!r}")
    check("tail kept pulsing after press", (OUT.get("tail_after_response") or 0) >= 3,
          f"{OUT.get('tail_after_response')} beats after response")
    if OUT.get("error"):
        check("no bridge errors", False, OUT["error"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all native-window checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
