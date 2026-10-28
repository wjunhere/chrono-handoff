"""What does the cue panel actually SAY, and how big is each thing it says?

This exists because the panel's worst defects were invisible to every check the
project already had.  The suite asserted "the element exists" and "nothing
overflows"; the panel meanwhile

* rendered the instruction -- the one thing the component exists to deliver -- at
  16px, below a 58px countdown and a 97px telemetry table,
* collapsed a documented four-line timeline into a single run-on sentence,
* put "GO 时按下" in a 51px button as the largest interactive surface, whatever
  the agent had asked the person to do,
* printed the internal enum `console_key` on the line a human reads,
* kept a 40px `+22.8` (seconds since a target nobody was still acting on) as the
  second-biggest thing on the card.

None of that fails an existence or overflow assertion.  All of it is one
measurement away, which is what this script is: drive the real window through
every phase of a cue, measure the type sizes / line counts / pixel share, print
them as a table, and fail when a ratio says the hierarchy has inverted again.

Not a replacement for `e2e_ui_layout.py`.  That one asserts the *geometry*
(nothing clipped, the rail fits, the window controls work).  This one asserts the
*reading order*, which is a different claim and needs different numbers.

    python scripts/audit_panel.py                # table + verdicts, exit 0/1
    python scripts/audit_panel.py --json         # machine-readable
    python scripts/audit_panel.py --verbose      # per-phase detail as it goes

Thresholds are RATIOS, never pixels: the panel is deliberately responsive (a
clamp() plus a vmin term plus a line-count scale), so an absolute size is a claim
about this monitor, while "the instruction is bigger than the telemetry" is a
claim about the design.  A machine whose DPI or window size differs therefore
still gets the same verdict.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Isolate settings/calibration: a stale saved window position made an earlier
# suite open the widget off-screen, and then every measurement read zeroes.
os.environ.setdefault("CHRONO_HOME", str(Path(__file__).resolve().parents[1] / "logs" / "_audit_home"))

from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.window import CueWindow  # noqa: E402

#: One probe, evaluated against the live page.  Everything measured here is read
#: from the DOM *after layout*: `getComputedStyle` for the type size and
#: `getBoundingClientRect` for the space actually taken.  Reading the CSS source
#: instead is what let the original problems through -- the stylesheet said
#: `clamp(16px,2.7vmin,23px)` and nobody asked what that came to.
PROBE = r"""
(() => {
  const info = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const cs = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    const font = parseFloat(cs.fontSize) || 0;
    const lh = parseFloat(cs.lineHeight) || font * 1.5;
    const visible = r.width > 0 && r.height > 0 && cs.display !== "none";
    // Clipping is only meaningful for elements that are supposed to CONTAIN
    // their text block.  `#clock` has `line-height:0.95` and a 190px glyph box,
    // so its glyphs legitimately overflow the line box and `scrollHeight`
    // exceeds `clientHeight` in every phase -- that is typography, not
    // clipping.  The elements that really can hide text are the multi-line
    // blocks with `overflow:hidden` + `text-overflow:ellipsis`.
    const isTextBlock = sel === "#instruction" || sel === "#guidance";
    return {
      font: Math.round(font * 10) / 10,
      h: Math.round(r.height),
      w: Math.round(r.width),
      lines: visible ? Math.max(1, Math.round(r.height / lh)) : 0,
      visible,
      clipped: isTextBlock
        && (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1),
      text: (e.textContent || "").trim().slice(0, 80),
      cls: e.className,
    };
  };
  const rows = [...document.querySelectorAll(".meter .m")];
  return {
    vw: innerWidth, vh: innerHeight,
    card: Math.round((document.querySelector(".stage") || {clientHeight: 0}).clientHeight),
    instruction: info("#instruction"),
    guidance: info("#guidance"),
    clock: info("#clock"),
    clockClass: (document.querySelector("#clock") || {}).className || "",
    clockText: ((document.querySelector("#clock") || {}).textContent || "").trim(),
    phase: info("#phase"),
    press: info("#press"),
    record: info(".record"),
    meter: info(".meter"),
    meterRows: rows.length,
    meterVisible: rows.filter((m) => m.getBoundingClientRect().height > 0).length,
    clockScale: getComputedStyle(document.documentElement)
                  .getPropertyValue("--clock-scale").trim(),
    bodyClass: document.body.className,
  };
})()
"""

#: The documented main use case (docs/usage-findings.md §5): one cue whose
#: instruction holds the whole timeline, several lines long.
TIMELINE = "\n".join([
    "+0s   三颗全灭 → led_all_off.jpg",
    "+30s  三颗全亮 → led_all_on.jpg",
    "+60s  只亮中间 → led_mid.jpg",
    "+90s  三颗闪烁 → 录 10 秒视频",
])

#: `expect_source` values, and the human word each must render as.  The enum is
#: the agent's vocabulary; the panel's job is to speak the operator's.
#:
#: `none` is "nothing is watching": the page says so plainly instead of naming a
#: channel, and renders no record control at all.
SOURCE_WORDS = {
    "console_key": "控制台按键",
    "stdin_line": "终端输入",
    "serial": "串口",
    "http": "面板",
    "inject": "面板",
    "none": "（无，仅提示）",
    "": "（无，仅提示）",
    "sim": "模拟源",
}


class Report:
    """Collected measurements plus the verdicts derived from them."""

    def __init__(self, verbose: bool = False) -> None:
        self.phases: dict[str, dict[str, Any]] = {}
        self.failures: list[str] = []
        self.verbose = verbose

    def add(self, phase: str, probe: dict[str, Any]) -> None:
        self.phases[phase] = probe
        if self.verbose:
            self._print_phase(phase, probe)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<56} {detail}", flush=True)
        if not ok:
            self.failures.append(name)

    @staticmethod
    def _print_phase(phase: str, p: dict[str, Any]) -> None:
        print(f"\n  -- {phase}  (card {p.get('card')}px, vw {p.get('vw')})")
        for key in ("instruction", "guidance", "clock", "phase", "press", "meter"):
            e = p.get(key)
            if not e:
                continue
            flags = ""
            if not e["visible"]:
                flags += " hidden"
            if e["clipped"]:
                flags += " CLIPPED"
            print(f"     {key:<11} {e['font']:>6.1f}px  {e['h']:>4}px  "
                  f"{e['lines']} line(s){flags}  {e['text'][:44]!r}")

    def table(self) -> None:
        print("\n  measured (font px / height px / lines)")
        print(f"  {'phase':<22}{'instr':>14}{'clock':>14}"
              f"{'guide':>13}{'press':>11}{'meter':>12}")
        for phase, p in self.phases.items():
            def cell(key: str) -> str:
                e = p.get(key)
                if not e:
                    return "-"
                if not e["visible"]:
                    return "hidden"
                return f"{e['font']:.0f}/{e['h']}/{e['lines']}"

            print(f"  {phase:<22}{cell('instruction'):>14}{cell('clock'):>14}"
                  f"{cell('guidance'):>13}{cell('press'):>11}{cell('meter'):>12}")


def wait_for(js: Any, expr: str, timeout: float) -> bool:
    """Poll a page expression.  Every phase is *waited for*, never slept into:
    a fixed sleep is how a measurement lands in the wrong phase and reports a
    layout bug that does not exist."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.08)
    return False


def wait_for_cue(js: Any, run_id: str, timeout: float = 30.0) -> bool:
    """Wait until the page is rendering THIS run's COUNTDOWN (not its lead-in).

    Three conditions, and each one exists because omitting it produced a wrong
    measurement:

    * ``S.runId === run_id`` -- the page applied the new schedule.  Without it a
      page still holding the previous run satisfied the other two conditions and
      the probe measured a stale grid (58px vs 107px on alternate runs).
    * ``S.sched`` -- there is a grid to render at all.
    * the phase line says "距 GO" -- the digits are counting DOWN.  A digit on
      screen is not enough: `#clock` shows "准备" during the lead-in and its size
      is a different clamp (58px), so "a digit exists" can be satisfied by the
      wrong phase.

    ...and the whole condition must hold **twice, a frame apart**.  `applySchedule`
    sets `S.runId`/`S.sched` immediately but the phase text is only repainted by
    the next `tick()`, so for one frame the page carries the *previous* cue's
    "距 GO" -- which satisfies the three conditions above while the new cue is
    still in its lead-in.  That is not theory: on CI this audit recorded
    `timeline='clock ready'/'准备'` and failed two verdicts with messages that
    pointed at the layout.  A stable second reading is what makes the sample
    provably this run's paint rather than the last one's.
    """
    expr = (
        f'(S.runId === {json.dumps(run_id)} && !!S.sched'
        ' && document.getElementById("phase").textContent.indexOf("距 GO") >= 0)'
    )
    end = time.time() + timeout
    while time.time() < end:
        if wait_for(js, expr, max(0.1, end - time.time())):
            time.sleep(0.15)          # ~2 frames: let tick() repaint
            if js(expr):
                return True
        time.sleep(0.08)
    return False


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    verbose = "--verbose" in argv or "-v" in argv

    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); this audit needs the real window.")
        return 0          # not a failure: the suite skips the same way

    journal = Path("logs/_audit_panel")
    journal.mkdir(parents=True, exist_ok=True)
    conductor = Conductor(audio="null", visual=False, journal_dir=str(journal))
    win = CueWindow(conductor=conductor, panel_port=0, width=460, height=620, on_top=True)
    rep = Report(verbose=verbose)

    def clear() -> None:
        for r in conductor.list_runs():
            if r["status"] in ("running", "pending"):
                conductor.cancel(r["run_id"], "audit")
        time.sleep(1.0)

    def cue(**kw: Any) -> dict[str, Any]:
        clear()
        return conductor.cue(queue=False, **kw)

    def cue_and_settle(js: Any, **kw: Any) -> dict[str, Any]:
        """Arm a cue and wait until the page renders ITS countdown.

        The two-phase wait is not paranoia: `wait_for_cue` proves `S.runId` has
        moved to this run *and* a countdown digit is on screen.  A cue whose page
        has not applied the schedule yet still shows the previous run's digit, so
        "a digit exists" is satisfied by stale state -- the exact reason an
        earlier version of this audit measured 58px on one run and 107px on the
        next with no code difference.

        The wait's result is **asserted**, not ignored.  It used to be discarded,
        so a phase that never settled was still sampled: on CI the timeline phase
        recorded `clock ready / '准备'` (the lead-in, 58px, wrong clamp) and the
        two verdicts that compare this sample with the previous one failed with a
        message that pointed at the layout.  A timeout is a failed measurement and
        has to say so.
        """
        run = cue(**kw)
        if not wait_for_cue(js, run["run_id"], 30):
            raise AssertionError(
                f"the page never reached {run['run_id']}'s countdown: "
                f"runId={js('S.runId')!r} phase={js('document.getElementById(\"phase\").textContent')!r}"
            )
        return run

    def ready(w: Any) -> None:
        js = w.window.evaluate_js
        time.sleep(2.0)                       # window + first clock sync
        try:
            # -- idle ---------------------------------------------------
            rep.add("idle", js(PROBE))

            # -- ready (armed, countdown not started) -------------------
            # Not `cue_and_settle`: that waits for a countdown DIGIT, and this is
            # the phase *before* the digits exist.  It waits for the new run id AND
            # the "准备" label together -- the run id alone is satisfied by a page
            # still holding the previous run, which is the stale-sample trap.
            r = cue(instruction="给板载 LED 拍照", target_in_s=40.0, lead_s=30.0,
                    ioi_ms=500, tail_beats=3, duration_s=60)
            wait_for(js, f'S.runId === {json.dumps(r["run_id"])} && '
                         'document.getElementById("clock").textContent === "准备"', 25)
            rep.add("ready", js(PROBE))

            # -- countdown, panel-owned (the record control is legitimate) --
            #
            # `http` on purpose: the panel is the only thing that can see the
            # operator for this cue, so it may offer the optional "记下我做的时刻"
            # control.  With the default (`none`) it must not -- that phase is
            # measured right after this one.
            cue_and_settle(js, instruction="给板载 LED 拍照", target_in_s=8.0,
                           lead_s=5.0, ioi_ms=500, tail_beats=3, duration_s=40,
                           expect_source="http")
            rep.add("countdown/panel", js(PROBE))

            # -- countdown, reminder only (no record control at all) ------
            # The component's common case: "do X at T", nothing watches.  A
            # control here would be the panel asking to be clicked, which is the
            # inversion this whole audit exists to catch.
            cue_and_settle(js, instruction="给板载 LED 拍照", target_in_s=8.0,
                           lead_s=5.0, ioi_ms=500, tail_beats=3, duration_s=40)
            rep.add("countdown/reminder", js(PROBE))

            # -- countdown, sensor-owned (no button, human channel named) --
            cue_and_settle(js, instruction="给板载 LED 拍照", target_in_s=8.0,
                           lead_s=5.0, ioi_ms=500, tail_beats=3, duration_s=40,
                           expect_source="console_key")
            rep.add("countdown/sensor", js(PROBE))

            # -- the documented multi-line timeline ---------------------
            cue_and_settle(js, instruction=TIMELINE, target_in_s=8.0, lead_s=5.0,
                           ioi_ms=500, tail_beats=3, duration_s=40,
                           expect_source="console_key")
            wait_for(js, 'getComputedStyle(document.documentElement)'
                         '.getPropertyValue("--clock-scale").trim() !== "1"', 10)
            time.sleep(0.3)                   # fitInstruction() runs next frame
            rep.add("timeline/sensor", js(PROBE))

            # -- telemetry expanded ------------------------------------
            js('if (!document.body.classList.contains("meter-open"))'
               ' document.getElementById("meter-toggle").click()')
            time.sleep(0.3)
            rep.add("timeline/meter-open", js(PROBE))

            # -- at GO -------------------------------------------------
            #
            # The `go` class only lasts 300ms of wall time (`sinceZero <= 300`),
            # which is shorter than a probe round trip under load -- so this phase
            # could silently be sampled in the TAIL state, and then the "post-GO
            # reading is demoted" verdict compared 20px against 20px.  Waiting for
            # the class and then sampling immediately is the closest this can get;
            # the verdict below is written against a fixed reference instead of
            # against this sample, precisely because this sample can be late.
            cue(instruction="给板载 LED 拍照", target_in_s=3.0, lead_s=2.0,
                ioi_ms=300, tail_beats=10, duration_s=30, expect_source="console_key")
            wait_for(js, 'document.getElementById("clock").className.includes("go")', 20)
            rep.add("at-go", js(PROBE))

            # -- after GO (the diagnostic reading) ----------------------
            wait_for(js, 'document.getElementById("clock").className.includes("tail")', 10)
            time.sleep(0.4)
            rep.add("after-go", js(PROBE))

            # -- which source words each enum renders as ---------------
            # Driven through the page's own mapping, so this measures the table
            # the panel actually uses rather than a copy of it.
            rep.add("_sourcewords", js(
                '(() => { const out = {}; for (const k of '
                + json.dumps(list(SOURCE_WORDS))
                + ') out[k] = (typeof sourceWord === "function") ? sourceWord(k) : null;'
                  ' return {vw: innerWidth, vh: innerHeight, card: 0, instruction: null,'
                  ' guidance: null, clock: null, phase: null, press: null, meter: null,'
                  ' words: out}; })()'
            ))
        except Exception as exc:
            rep.failures.append(f"probe raised {type(exc).__name__}: {exc}")
        finally:
            try:
                w.window.destroy()
            except Exception:
                pass

    win.run(on_ready=ready)

    words = (rep.phases.pop("_sourcewords", {}) or {}).get("words") or {}

    # ------------------------------------------------------------------
    # verdicts: the reading order, as ratios
    # ------------------------------------------------------------------
    idle = rep.phases.get("idle") or {}
    ready_p = rep.phases.get("ready") or {}
    panel = rep.phases.get("countdown/panel") or {}
    sensor = rep.phases.get("countdown/sensor") or {}
    timeline = rep.phases.get("timeline/sensor") or {}
    meter_open = rep.phases.get("timeline/meter-open") or {}
    after = rep.phases.get("after-go") or {}

    def f(p: dict[str, Any], key: str, field: str = "font") -> float:
        e = p.get(key)
        return float(e[field]) if e and e.get("visible") else 0.0

    print("\n  verdicts")

    # 1. The instruction must be the largest TEXT on the panel.  The countdown is
    #    allowed to be bigger (it is read from across a bench), but the sentence
    #    must beat every piece of chrome: the guidance line, the telemetry, the
    #    phase line.  That is the whole hierarchy claim.
    ins = f(panel, "instruction")
    chrome = max(f(panel, "guidance"), f(panel, "phase"),
                 f(panel, "meter"), f(panel, "press"), 1.0)
    rep.check("the instruction outranks all chrome type",
              ins >= chrome * 1.4,
              f"instruction={ins:.0f}px vs chrome max={chrome:.0f}px "
              f"(ratio {ins / chrome:.2f}, need >= 1.40)")

    # 2. Multi-line instructions keep their lines and are not clipped.
    tl = timeline.get("instruction") or {}
    rep.check("a four-line timeline renders as four lines",
              (tl.get("lines") or 0) >= 4, f"{tl.get('lines')} line(s)")
    rep.check("the timeline is not clipped",
              tl.get("clipped") is False, f"clipped={tl.get('clipped')}")

    # 3. ...and the countdown steps back so the timeline is read first.
    #
    #    This compares two COUNTDOWN samples, and the precondition is asserted: both
    #    must actually be in the countdown phase (class `clock`, a digit on screen,
    #    scale 1 for the single-line case).  Without it the check compared the
    #    "准备" reading (58px, `#clock.ready`'s own clamp) against the scaled
    #    countdown and called it a regression at random -- a verdict built on a
    #    sample that may have landed in the previous phase is not a verdict.
    scale = float(timeline.get("clockScale") or 1)
    clock_tl, clock_1 = f(timeline, "clock"), f(panel, "clock")
    ok_phase = (
        str(panel.get("clockClass") or "") == "clock"
        and str(panel.get("clockText") or "").isdigit()
        and str(timeline.get("clockText") or "").isdigit()
    )
    rep.check("both countdown samples were taken in the countdown phase",
              ok_phase,
              f"panel={panel.get('clockClass')!r}/{panel.get('clockText')!r} "
              f"timeline={timeline.get('clockClass')!r}/{timeline.get('clockText')!r}")
    rep.check("the countdown steps back for a multi-line instruction",
              ok_phase and scale < 1.0 and clock_tl < clock_1 * 0.85,
              f"scale={scale} clock {clock_1:.0f}px -> {clock_tl:.0f}px")

    # 4. The countdown still has to be readable at a glance: it must stay the
    #    biggest NUMBER, which is a much weaker claim than "biggest thing".
    rep.check("the countdown is still the largest number on the card",
              clock_tl >= f(timeline, "instruction") * 1.5,
              f"clock={clock_tl:.0f}px instruction={f(timeline, 'instruction'):.0f}px")

    # 5. After GO the elapsed reading is a diagnostic, not a second countdown.
    #
    #    Compared against the countdown measured in the SAME window size and with
    #    a single-line instruction -- never against the `at-go` sample, which can
    #    be taken after the 300ms GO window has already closed.  A verdict built on
    #    a sample that may have landed in the next phase is not a verdict.
    tail_f = f(after, "clock")
    cd_f = f(panel, "clock")
    rep.check("the post-GO reading is demoted below a third of the countdown",
              cd_f > 0 and 0 < tail_f < cd_f / 3.0,
              f"tail={tail_f:.1f}px countdown={cd_f:.0f}px "
              f"(ratio {tail_f / cd_f:.3f}, need < 0.333)")

    # 6. Telemetry is folded by default and expands on demand.
    rep.check("telemetry is folded to one row by default",
              (panel.get("meterVisible") or 0) == 1 and (panel.get("meterRows") or 0) > 1,
              f"{panel.get('meterVisible')}/{panel.get('meterRows')} rows")
    rep.check("telemetry expands when asked",
              (meter_open.get("meterVisible") or 0) == (panel.get("meterRows") or -1),
              f"{meter_open.get('meterVisible')}/{panel.get('meterRows')} rows")

    # 6b. The record control: offered when the panel is the observer, absent when
    #     nothing is watching, and never shaped like the primary button it used to
    #     be (full-width, in the action row, labelled "GO 时按下").
    reminder = rep.phases.get("countdown/reminder") or {}
    rep.check("a cue that only reminds renders no record control",
              f(reminder, "record", "h") == 0 and f(reminder, "press", "h") == 0
              and "no-press" in str(reminder.get("bodyClass")),
              f"record h={f(reminder, 'record', 'h'):.0f} body={reminder.get('bodyClass')!r}")
    rep.check("the record control is offered when the panel observes",
              f(panel, "record", "h") > 0 and f(panel, "press", "h") > 0,
              f"record h={f(panel, 'record', 'h'):.0f} press h={f(panel, 'press', 'h'):.0f}")
    quiet_w = float(panel.get("vw") or 0) * 0.6
    rep.check("...and it is a quiet control, not a full-width primary button",
              f(panel, "press", "w") < quiet_w,
              f"press w={f(panel, 'press', 'w'):.0f}px of {panel.get('vw')}px viewport "
              f"(need < {quiet_w:.0f})")

    # 7. A sensor-owned cue does not advertise a button, and its guidance sends
    #    the operator to the instruction.
    rep.check("a sensor-owned cue hides the record control",
              f(sensor, "record", "h") == 0 and f(sensor, "press", "h") == 0
              and "no-press" in str(sensor.get("bodyClass")),
              f"record h={f(sensor, 'record', 'h'):.0f} press h={f(sensor, 'press', 'h'):.0f} "
              f"body={sensor.get('bodyClass')!r}")
    g = str((sensor.get("guidance") or {}).get("text") or "")
    rep.check("the guidance does not tell the operator to press the panel",
              "按" not in g.replace("按键", ""), repr(g))

    # 8. The guidance fits one line: the old wording wrapped to three and pushed
    #    the countdown down the card.
    rep.check("the guidance fits one line",
              (sensor.get("guidance") or {}).get("lines") == 1,
              f"{(sensor.get('guidance') or {}).get('lines')} line(s)")

    # 9. No internal enum ever reaches the operator.  Checked against every
    #    expect_source value, not just the one this run used.
    leaked = [k for k in SOURCE_WORDS if k and k in g]
    rep.check("the observing channel is named in human words, not as an enum",
              not leaked and any(w in g for w in SOURCE_WORDS.values()),
              f"guidance={g!r} leaked={leaked}")
    wrong = {k: words.get(k) for k, v in SOURCE_WORDS.items() if words.get(k) != v}
    rep.check("every expect_source value has a human word",
              not wrong, f"wrong/missing={wrong}" if wrong else
              f"{len(SOURCE_WORDS)} values mapped")

    # 10. Nothing clipped in any phase, and the card never overflows.
    clipped = [f"{name}/{key}" for name, p in rep.phases.items()
               for key in ("instruction", "guidance", "clock", "press")
               if (p.get(key) or {}).get("clipped")]
    rep.check("no measured element is clipped in any phase",
              not clipped, f"clipped={clipped}" if clipped else "all clear")

    # 11. The idle state says something.  A panel showing a bare "--" with no
    #     explanation reads as a broken clock rather than "no cue yet".
    rep.check("the idle panel explains itself",
              bool(str((idle.get("guidance") or {}).get("text") or "").strip())
              or bool(str((idle.get("clock") or {}).get("text") or "").strip()),
              f"clock={(idle.get('clock') or {}).get('text')!r}")

    # 12. The ready state is visibly distinct from the countdown: it must not
    #     render a number where a countdown number would be.
    rtext = str((ready_p.get("clock") or {}).get("text") or "")
    rep.check("the armed-but-not-started state is not a fake countdown",
              bool(rtext) and not rtext.strip().isdigit(), f"clock={rtext!r}")

    rep.table()

    if as_json:
        print(json.dumps({"phases": rep.phases, "failures": rep.failures},
                         ensure_ascii=False, indent=1, default=str))

    print()
    if rep.failures:
        print(f"{len(rep.failures)} FAILED: {rep.failures}")
        return 1
    print("panel copy + hierarchy audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
