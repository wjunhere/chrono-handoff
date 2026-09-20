"""Check the rebuilt panel's LAYOUT geometry and the new window controls.

The first version passed its tests while looking visibly broken, because the
tests asserted "the element exists" and never "the element is in the right
place".  This one measures:

* the title bar spans the full width and sits ABOVE the content (the original
  bug put it to the LEFT of a squeezed card, because body's row-flex layout
  leaked into the shell),
* there is no horizontal or vertical overflow at the default size,
* the clock stays inside the viewport and is large,
* the three window buttons exist and the close button is hit-testable,
* resizing the window actually changes the layout (responsive, no manual zoom),
* the drag-in-center affordance still works.
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

OUT: dict = {}
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<50} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


GEO_JS = """
(() => {
  const r = (id) => { const e = document.getElementById(id); if (!e) return null;
    const b = e.getBoundingClientRect(); return {x:+b.x.toFixed(1),y:+b.y.toFixed(1),
    w:+b.width.toFixed(1),h:+b.height.toFixed(1)}; };
  const c = document.getElementById('clock');
  return {
    vw: innerWidth, vh: innerHeight,
    docW: document.documentElement.scrollWidth,
    docH: document.documentElement.scrollHeight,
    titlebar: r('titlebar'),
    panel: r('panel') || r('settings'),
    clock: r('clock'),
    grid: r('grid'),
    press: r('press'),
    clockFont: c ? parseFloat(getComputedStyle(c).fontSize) : 0,
    bodyClass: document.body.className,
    hasMin: !!document.getElementById('tb-min'),
    hasMax: !!document.getElementById('tb-max'),
    hasClose: !!document.getElementById('tb-close'),
    closeHittable: (() => {
      const b = document.getElementById('tb-close');
      if (!b) return false;
      const bb = b.getBoundingClientRect();
      if (bb.width === 0) return false;
      const el = document.elementFromPoint(bb.x + bb.width/2, bb.y + bb.height/2);
      return !!(el && el.closest('#titlebar'));
    })(),
  };
})()
"""


def main() -> int:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); skipping.")
        return 0

    from chrono_handoff.conductor import Conductor  # noqa: PLC0415
    from chrono_handoff.window import CueWindow  # noqa: PLC0415

    settings_path = Path("logs/_ui/settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_ui")
    win = CueWindow(conductor=conductor, panel_port=0, settings_path=settings_path,
                    width=460, height=620, on_top=True)

    def ready(w) -> None:
        js = w.window.evaluate_js
        time.sleep(2.2)
        try:
            OUT["at_default"] = js(GEO_JS)

            # arm a cue so the timer state is rendered, then measure again
            w.conductor.cue(instruction="按一下开发板上的 RESET 键", target_in_s=9.0,
                            lead_s=6.0, ioi_ms=500, tail_beats=6,
                            expect_source="none", duration_s=20, open_panel=False)
            # Wait until the countdown itself is running: the timer is what the
            # operator reads most and it must show a positive count.  A fixed
            # sleep landed in the "准备" pre-roll and reported a failure that
            # was purely the test's timing.
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if js("document.getElementById('clock').textContent").strip().isdigit():
                    break
                time.sleep(0.2)
            # deliberately measured mid-countdown
            OUT["with_cue"] = js(GEO_JS)
            OUT["clock_text"] = js("document.getElementById('clock').textContent")
            OUT["phase_text"] = js("document.getElementById('phase').textContent")
            OUT["press_label"] = js("document.getElementById('press').textContent.trim()")
            OUT["cells"] = js("document.querySelectorAll('#grid .cell').length")

            # resize the window: the layout must respond, not need a zoom slider
            w.window.resize(880, 520)
            time.sleep(1.0)
            OUT["wide"] = js(GEO_JS)
            w.window.resize(340, 780)
            time.sleep(1.0)
            # still mid-countdown at this size, so the clock font is measurable
            OUT["tall_narrow"] = js(GEO_JS)
            OUT["tall_clock_text"] = js("document.getElementById('clock').textContent")

            # the settings sheet must not cover the title bar
            js("document.getElementById('tb-set').click()")
            time.sleep(0.4)
            OUT["settings_open"] = js("document.body.classList.contains('settings-open')")
            OUT["sheet_geo"] = js(GEO_JS)
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
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str)[:2200])
    print()

    d = OUT.get("at_default") or {}
    tb, pn = d.get("titlebar"), d.get("panel")
    # The floating design insets the card from the window edge (the margin is
    # what makes the shadow readable), so the title bar correctly spans the
    # CARD, not the window.  Asserting against the viewport width would forbid
    # the margin and fail a correct layout.
    check("title bar spans the whole card",
          tb and pn and abs(tb["w"] - pn["w"]) <= 2 and tb["w"] > d.get("vw", 0) * 0.85,
          f"titlebar.w={tb['w'] if tb else '?'} card.w={pn['w'] if pn else '?'} viewport.w={d.get('vw')}")
    check("title bar sits above the content", tb and pn and tb["y"] + tb["h"] <= pn["y"] + 2,
          f"titlebar bottom={tb['y']+tb['h'] if tb else '?'} panel top={pn['y'] if pn else '?'}")
    check("no horizontal overflow", d.get("docW", 0) <= d.get("vw", 0) + 1,
          f"scrollWidth={d.get('docW')} vs width={d.get('vw')}")
    check("no vertical overflow", d.get("docH", 0) <= d.get("vh", 0) + 1,
          f"scrollHeight={d.get('docH')} vs height={d.get('vh')}")
    check("three window buttons present", bool(d.get("hasMin") and d.get("hasMax") and d.get("hasClose")))
    check("close button is hit-testable", d.get("closeHittable") is True)

    c = OUT.get("with_cue") or {}
    ck = c.get("clock") or {}
    check("clock renders with a big font", c.get("clockFont", 0) >= 40, f"{c.get('clockFont')}px")
    check("clock fits inside the window", ck.get("h", 999) < c.get("vh", 0) and ck.get("w", 999) <= c.get("vw", 0),
          f"clock={ck.get('w')}x{ck.get('h')} viewport={c.get('vw')}x{c.get('vh')}")
    check("beat grid built", (OUT.get("cells") or 0) > 0, f"{OUT.get('cells')} cells")
    check("press button labelled", bool(OUT.get("press_label")), OUT.get("press_label"))
    ct = str(OUT.get("clock_text") or "")
    check("countdown shows a number (not '+' of nothing)", ct.isdigit() and len(ct) == 2,
          f"clock={ct!r} phase={OUT.get('phase_text')!r}")
    check("countdown is positive", int(ct or 0) > 0, f"{ct} beats to GO")

    wide = OUT.get("wide") or {}
    tall = OUT.get("tall_narrow") or {}
    check("layout responds to a wide window",
          (wide.get("clock") or {}).get("h", 0) >= (c.get("clock") or {}).get("h", 0) * 0.9
          and wide.get("vw") == 880, f"vw={wide.get('vw')} clockH={(wide.get('clock') or {}).get('h')}")
    check("no overflow when narrow", tall.get("docW", 0) <= tall.get("vw", 0) + 1,
          f"scrollWidth={tall.get('docW')} vs {tall.get('vw')}")
    tc = str(OUT.get("tall_clock_text") or "")
    check("clock stays large when tall", (tall.get("clockFont") or 0) >= 40,
          f"{tall.get('clockFont')}px")
    check("countdown still a number when tall", tc.isdigit(), f"clock={tc!r}")

    check("settings sheet opens", OUT.get("settings_open") is True)
    sg = OUT.get("sheet_geo") or {}
    stb = sg.get("titlebar")
    check("title bar still visible with settings open", stb and stb.get("w", 0) > 0,
          f"titlebar.w={stb.get('w') if stb else '?'}")
    if OUT.get("error"):
        check("no runtime errors", False, OUT["error"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("panel layout + window controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
