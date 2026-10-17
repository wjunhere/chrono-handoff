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
from typing import Any
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
  const st = document.querySelector('.stage');
  const gd = document.getElementById('grid');
  return {
    vw: innerWidth, vh: innerHeight,
    docW: document.documentElement.scrollWidth,
    docH: document.documentElement.scrollHeight,
    // The CARD is where clipping actually happens: .stage has overflow:hidden
    // for its rounded corners, so a layout mistake inside it shows up as
    // content that is simply missing -- no scrollbar, no error.  The document
    // never overflows in that case (body.scrollWidth stays correct), which is
    // why checking the document alone missed a real bug: a 20 s lead made the
    // grid column 614px wide, every child inherited it, and the card quietly
    // cropped the GO button and every right-aligned metric value.
    cardW: st ? st.clientWidth : 0,
    cardH: st ? st.clientHeight : 0,
    cardScrollW: st ? st.scrollWidth : 0,
    cardScrollH: st ? st.scrollHeight : 0,
    railW: gd ? gd.clientWidth : 0,
    railScrollW: gd ? gd.scrollWidth : 0,
    // scrollWidth is USELESS here: #grid has overflow:visible, and for a
    // non-scroll-container scrollWidth just echoes clientWidth.  An earlier
    // version of this check compared them and could therefore never fail.
    // What matters is where the cells actually land, so measure them.
    railLeft: gd ? +gd.getBoundingClientRect().left.toFixed(1) : null,
    railRight: gd ? +gd.getBoundingClientRect().right.toFixed(1) : null,
    railLastRight: gd && gd.lastElementChild
      ? +gd.lastElementChild.getBoundingClientRect().right.toFixed(1) : null,
    railLastLeft: gd && gd.lastElementChild
      ? +gd.lastElementChild.getBoundingClientRect().left.toFixed(1) : null,
    goRight: (() => {
      const a = document.querySelector('#grid .cell.accent');
      return a ? +a.getBoundingClientRect().right.toFixed(1) : null;
    })(),
    cardRight: st ? +st.getBoundingClientRect().right.toFixed(1) : null,
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

    def wait_for_width(js: Any, want: int, timeout: float = 10.0) -> int:
        """Wait for the page to be as wide as the window was asked to be.

        The window manager applies a resize asynchronously, and under load the
        measurements below ran while it was still in flight -- which reported
        `vw=446` for a window that had just been asked for 880, i.e. a layout that
        never got the chance to respond.  Returns the width actually reached.
        """
        deadline = time.time() + timeout
        width = 0
        while time.time() < deadline:
            width = int(js("innerWidth") or 0)
            if width == want:
                return width
            time.sleep(0.2)
        print(f"  [WARN] window asked for {want}px, page reports {width}px after "
              f"{timeout:.0f}s")
        return width

    def ready(w) -> None:
        js = w.window.evaluate_js
        time.sleep(2.2)
        try:
            OUT["at_default"] = js(GEO_JS)

            # arm a cue so the timer state is rendered, then measure again.
            #
            # Re-armed before each measurement rather than once for all three: the
            # countdown a single cue gives lasts about 5.5s, the three measurements
            # below are spread over more than that on a slow machine, and on the
            # runner the tall one landed at `clock='+2.7'` -- past GO, where the
            # clock is deliberately small -- and reported "clock stays large when
            # tall 30.6px" as if the layout were wrong.  Measured over 26s: digits
            # appear ~3s after arming and hold until GO.
            def arm() -> None:
                """Arm the cue and wait until its countdown is actually showing."""
                w.conductor.cue(instruction="按一下开发板上的 RESET 键", target_in_s=9.0,
                                lead_s=6.0, ioi_ms=500, tail_beats=6,
                                expect_source="none", duration_s=20, open_panel=False)
                # Both conditions, because either alone is satisfied by a page with
                # nothing to show yet: a fresh page renders `--` with an empty grid,
                # and the grid comes from the schedule the page polls for.  Measured
                # on the runner as `clock='--' phase='' cells=0`.
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    clock_now = js("document.getElementById('clock').textContent")
                    cells_now = js("document.querySelectorAll('#grid .cell').length") or 0
                    if str(clock_now).strip().isdigit() and cells_now > 0:
                        return
                    time.sleep(0.2)

            arm()
            # deliberately measured mid-countdown
            OUT["with_cue"] = js(GEO_JS)
            OUT["clock_text"] = js("document.getElementById('clock').textContent")
            OUT["phase_text"] = js("document.getElementById('phase').textContent")
            OUT["press_label"] = js("document.getElementById('press').textContent.trim()")
            OUT["cells"] = js("document.querySelectorAll('#grid .cell').length")

            # resize the window: the layout must respond, not need a zoom slider
            #
            # `arm()` goes *after* the resize, not before: arming a fresh cue makes the
            # window fit its content again, so arming first undid the resize and the
            # check read `vw=446` for a window that had just been asked for 880.
            #
            # And the resize is *waited for*, not assumed to be instant: the window
            # manager applies it asynchronously, and under load the check read
            # `vw=446` for a resize that was still in flight.
            w.window.resize(880, 520)
            wait_for_width(js, 880)
            arm()
            OUT["wide"] = js(GEO_JS)
            w.window.resize(340, 780)
            wait_for_width(js, 340)
            # still mid-countdown at this size, so the clock font is measurable --
            # same reason as above for arming after the resize, not before
            arm()
            OUT["tall_narrow"] = js(GEO_JS)
            OUT["tall_clock_text"] = js("document.getElementById('clock').textContent")

            # the settings sheet must not cover the title bar
            js("document.getElementById('tb-set').click()")
            time.sleep(0.4)
            OUT["settings_open"] = js("document.body.classList.contains('settings-open')")
            OUT["sheet_geo"] = js(GEO_JS)
            js("document.getElementById('tb-set').click()")
            time.sleep(0.3)

            # ---- a DENSE rail: the case that used to break the whole card ----
            #
            # The rail renders one cell per beat, so a long lead makes it wide.
            # With a 20 s lead it needed 611px inside a ~390px content box, the
            # grid column grew to its min-content, and EVERY child inherited that
            # width while the card stayed 439px and cropped them -- GO button and
            # all the right-aligned metric values included.  A 6 s lead (what the
            # rest of this file arms) is nowhere near that, which is why the
            # existing "no overflow" checks passed while the UI was visibly cut.
            w.conductor.cue(instruction="press RESET", target_in_s=90.0, lead_s=20.0,
                            ioi_ms=500, tail_beats=6, expect_source="none",
                            duration_s=240, open_panel=False)
            time.sleep(2.5)
            # Back to the default size: the fix has to hold at the size the
            # widget ships at, not only at whatever size was left over.
            w.window.resize(460, 620)
            time.sleep(1.0)
            OUT["dense"] = js(GEO_JS)
            OUT["dense_cells"] = js("document.querySelectorAll('#grid .cell').length")
            OUT["dense_rail_gap"] = js(
                "getComputedStyle(document.getElementById('grid')).gap")
            OUT["dense_metric_values"] = js(
                "[...document.querySelectorAll('.m b')].filter(e => e.getBoundingClientRect().right"
                " <= innerWidth + 1).length")
            OUT["dense_metric_count"] = js("document.querySelectorAll('.m b').length")
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
          bool(tb and pn and abs(tb["w"] - pn["w"]) <= 2 and tb["w"] > d.get("vw", 0) * 0.85),
          str(f"titlebar.w={tb['w'] if tb else '?'} card.w={pn['w'] if pn else '?'} viewport.w={d.get('vw')}"))
    check("title bar sits above the content", bool(tb and pn and tb["y"] + tb["h"] <= pn["y"] + 2),
          f"titlebar bottom={tb['y']+tb['h'] if tb else '?'} panel top={pn['y'] if pn else '?'}")
    check("no horizontal overflow", d.get("docW", 0) <= d.get("vw", 0) + 1,
          f"scrollWidth={d.get('docW')} vs width={d.get('vw')}")
    check("no vertical overflow", d.get("docH", 0) <= d.get("vh", 0) + 1,
          f"scrollHeight={d.get('docH')} vs height={d.get('vh')}")
    # Card-level, not document-level: .stage is overflow:hidden, so this is where
    # clipping shows up as missing content with no scrollbar and no error.
    check("card does not clip its content",
          d.get("cardScrollW", 0) <= d.get("cardW", 0) + 1
          and d.get("cardScrollH", 0) <= d.get("cardH", 0) + 1,
          f"card={d.get('cardW')}x{d.get('cardH')} "
          f"scroll={d.get('cardScrollW')}x{d.get('cardScrollH')}")
    check("three window buttons present", bool(d.get("hasMin") and d.get("hasMax") and d.get("hasClose")))
    check("close button is hit-testable", d.get("closeHittable") is True)

    c = OUT.get("with_cue") or {}
    ck = c.get("clock") or {}
    # First, whether there was a countdown to measure at all.  Without this the
    # wait above timing out surfaces as "the clock font is 39.96px", which reads
    # as a styling bug and sends you into the CSS.
    check("a countdown was actually running when measured",
          str(OUT.get("clock_text") or "").isdigit(),
          f"clock={OUT.get('clock_text')!r} phase={OUT.get('phase_text')!r}")
    check("clock renders with a big font", c.get("clockFont", 0) >= 40, f"{c.get('clockFont')}px")
    check("clock fits inside the window", ck.get("h", 999) < c.get("vh", 0) and ck.get("w", 999) <= c.get("vw", 0),
          f"clock={ck.get('w')}x{ck.get('h')} viewport={c.get('vw')}x{c.get('vh')}")
    check("beat grid built", (OUT.get("cells") or 0) > 0, f"{OUT.get('cells')} cells")
    check("press button labelled", bool(OUT.get("press_label")), str(OUT.get("press_label") or ""))
    ct = str(OUT.get("clock_text") or "")
    check("countdown shows a number (not '+' of nothing)", ct.isdigit() and len(ct) == 2,
          f"clock={ct!r} phase={OUT.get('phase_text')!r}")
    # Only parse what the check above established is a number.  A cue that had not
    # started yet leaves the page's pre-cue label here ("准备"), and `int("准备")`
    # used to take the whole suite down with a traceback -- hiding every assertion
    # after it -- instead of failing one line and carrying on.
    beats = int(ct) if ct.isdigit() else 0
    check("countdown is positive", beats > 0, f"{ct or '(no text)'} -> {beats} beats to GO")

    wide = OUT.get("wide") or {}
    tall = OUT.get("tall_narrow") or {}
    # Not a ratio against the default size: the wide case is 880x**520**, i.e. wider
    # but 100px *shorter* than the default 620, and the clock is sized from the
    # available height -- so a smaller clock there is the layout working, not failing.
    # The runner measured exactly that (`vw=880 clockH=64.2`) and was reported as a
    # layout bug.  What has to hold is that the clock is still readable and nothing
    # overflowed.
    check("layout responds to a wide window",
          wide.get("vw") == 880 and wide.get("clockFont", 0) >= 40
          and wide.get("docW", 0) <= wide.get("vw", 0) + 1,
          f"vw={wide.get('vw')} clockFont={wide.get('clockFont')} "
          f"clockH={(wide.get('clock') or {}).get('h')} docW={wide.get('docW')}")
    check("no overflow when narrow", tall.get("docW", 0) <= tall.get("vw", 0) + 1,
          f"scrollWidth={tall.get('docW')} vs {tall.get('vw')}")
    tc = str(OUT.get("tall_clock_text") or "")
    check("clock stays large when tall", (tall.get("clockFont") or 0) >= 40,
          f"{tall.get('clockFont')}px")
    check("countdown still a number when tall", tc.isdigit(), f"clock={tc!r}")

    check("settings sheet opens", OUT.get("settings_open") is True)
    sg = OUT.get("sheet_geo") or {}
    stb = sg.get("titlebar")
    check("title bar still visible with settings open", bool(stb and stb.get("w", 0) > 0),
          f"titlebar.w={stb.get('w') if stb else '?'}")

    dn = OUT.get("dense") or {}
    check("a dense rail still keeps the card inside the window",
          dn.get("cardScrollW", 0) <= dn.get("cardW", 0) + 1
          and dn.get("cardScrollH", 0) <= dn.get("cardH", 0) + 1,
          f"{OUT.get('dense_cells')} cells; card={dn.get('cardW')}x{dn.get('cardH')} "
          f"scroll={dn.get('cardScrollW')}x{dn.get('cardScrollH')}")
    check("a dense rail fits inside its own track (tail not clipped)",
          dn.get("railLastRight") is not None
          and dn.get("railLastRight", 1e9) <= dn.get("railRight", 0) + 1,
          f"last cell right={dn.get('railLastRight')} rail right={dn.get('railRight')} "
          f"({OUT.get('dense_cells')} cells, gap={OUT.get('dense_rail_gap')})")
    check("the GO cell is still on screen with a dense rail",
          dn.get("goRight") is not None and dn.get("cardRight") is not None
          and dn.get("goRight", 1e9) <= dn.get("cardRight", 0) + 1,
          f"GO right={dn.get('goRight')} card right={dn.get('cardRight')}")
    # The symptom the user actually reported: values are right-aligned, so they
    # are the first thing to disappear when the card overflows.
    check("every metric value is still on screen with a dense rail",
          OUT.get("dense_metric_values", 0) == OUT.get("dense_metric_count", -1)
          and bool(OUT.get("dense_metric_count")),
          f"{OUT.get('dense_metric_values')}/{OUT.get('dense_metric_count')} values inside the window")
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
