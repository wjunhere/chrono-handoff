"""Command line interface.

    chrono daemon      --background          start the conductor (detached)
    chrono doctor                             check this machine's capabilities
    chrono probe --trials 24                  run the M1 measurement
    chrono submit -f plan.json                submit a plan to the daemon
    chrono observe <run_id>                   incremental journal read
    chrono status [run_id]                    status / run list
    chrono cancel <run_id>
    chrono calibrate [--key K] [--reset]
    chrono selftest                           verify the machinery (no human)
    chrono mcp                                run the MCP stdio server
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import CONTRACT_VERSION, __version__
from .calibrate import CalibrationStore, simulate_calibration
from .clock import CLOCK, Firing, Scheduler, drift_report
from .conductor import Conductor
from .audio import AUDIO_CHOICES
from .cue import BellAudio, NullAudio, default_audio, describe_audio
from .inputs import DEFAULT_KEY_LATENCY_S, analyse_offsets, make_source
from .mcp_server import DEFAULT_HOST, DEFAULT_IPC_PORT, ConductorClient, ConductorServer
from .model import CuePlan, SignalSpec, WindowSpec
from .shell import ShellRecord
from .shell import record_path as shell_record_path

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _client(args: argparse.Namespace) -> ConductorClient:
    return ConductorClient(
        getattr(args, "host", DEFAULT_HOST),
        getattr(args, "port", DEFAULT_IPC_PORT),
        timeout_s=float(getattr(args, "timeout", 8.0)),
    )


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load_plan(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif args.json:
        raw = args.json
    else:
        raw = sys.stdin.read()
    data = json.loads(raw)
    if "plan" in data and isinstance(data["plan"], dict):
        data = data["plan"]
    return data


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what this machine can actually do.  Cheap, and it catches the
    two failure modes that silently ruin measurements: no audio sink, and no
    console (a detached daemon cannot read keystrokes)."""
    rows: list[tuple[str, str, str]] = []
    #: Things that are actually wrong, collected explicitly.
    #:
    #: The exit code used to be `all("NO" not in value)`, i.e. a substring
    #: search over *rendered* text -- so a user whose home directory contained
    #: "NO" (``C:\Users\NOAH``) made `doctor` report a broken machine.  A
    #: diagnostic that can be fooled by a path is not a diagnostic.
    problems: list[str] = []

    # console
    try:
        import ctypes
        import msvcrt  # noqa: PLC0415

        has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
        rows.append(("console", "yes" if has_console else "NO", "keystroke capture needs a console (msvcrt)"))
        if not has_console:
            problems.append(
                "no console: keystroke capture will not work -- start the conductor with "
                "`chrono daemon --background` (it gets its own console)"
            )
        rows.append(("msvcrt.kbhit", "yes" if hasattr(msvcrt, "kbhit") else "NO", "mechanism-level key timestamp"))
    except Exception as exc:
        rows.append(("console", "NO", f"{type(exc).__name__}: {exc}"))
        problems.append(f"console unavailable ({type(exc).__name__}: {exc})")

    # audio
    sink = default_audio("auto")
    rows.append(("audio sink", sink.name, describe_audio(sink)))
    if sink.name == "wav":
        import winsound

        # Everything below touches the *device*, and a device that refuses is not
        # an exotic case: a CI runner, a VM, a machine used over RDP and a session
        # with the audio service stopped all answer "Failed to beep".  That used to
        # raise straight out of `doctor`, which made the one command whose job is
        # to report what a machine can do be the one command that crashed on a
        # machine that cannot do it -- found by CI, which is exactly such a
        # machine.
        try:
            # Warm the device first: the very first PlaySound after process start
            # pays a one-off device-initialisation cost and would be misreported as
            # the steady-state per-beat cost.
            sink.play("tick")
            time.sleep(0.15)
            samples = []
            for _ in range(20):
                t = time.perf_counter()
                sink.play("tick")
                samples.append((time.perf_counter() - t) * 1000)
                time.sleep(0.03)
            rows.append(
                (
                    "audio call cost",
                    f"median {statistics.median(samples):.3f} ms / max {max(samples):.3f} ms",
                    "steady state; must be << 1ms or it steals window time",
                )
            )

            t = time.perf_counter()
            winsound.Beep(880, 30)
            beep_ms = (time.perf_counter() - t) * 1000
            rows.append(("winsound.Beep(30ms)", f"blocks {beep_ms:.1f} ms", "NOT used by the conductor -- blocking"))
        except Exception as exc:
            # Reported, not raised, and not a `problem`: a cue that silently has no
            # sound is bad, but this row is about the *device this process can
            # reach right now*, and doctor's job is to say so.
            rows.append(("audio device", f"{type(exc).__name__}: {exc}",
                         "this machine cannot play a cue tone right now"))
    sink.close()

    # clock
    s = Scheduler("doctor").start()
    hits: list[float] = []
    done = threading.Event()
    t0 = CLOCK() + 0.05

    def cb(d: float) -> None:
        hits.append((CLOCK() - d) * 1000.0)
        if len(hits) >= 20:
            done.set()

    s.grid(t0, 0.02, cb, count=20, name="doctor")
    done.wait(3.0)
    s.stop()
    if hits:
        rows.append(
            (
                "scheduler landing",
                f"median {statistics.median(hits):.3f} ms / max {max(hits):.3f} ms",
                "over 20 firings at 20ms spacing",
            )
        )

    # serial
    try:
        import serial  # noqa: F401, PLC0415

        from serial.tools import list_ports  # noqa: PLC0415

        ports = [p.device for p in list_ports.comports()]
        rows.append(("pyserial", "yes", f"ports: {', '.join(ports) if ports else '(none)'}"))
    except Exception:
        rows.append(("pyserial", "not installed", "only needed for the real serial case study"))

    # mcp
    try:
        import mcp  # noqa: F401, PLC0415

        rows.append(("mcp sdk", "yes", "chrono mcp can run"))
    except Exception:
        rows.append(("mcp sdk", "not installed", "pip install 'chrono-handoff[mcp]'"))

    # panel window
    #
    # Two different things can be on screen, and they are NOT alternatives to
    # each other: the Tauri shell owns its own window and talks to the conductor
    # over HTTP, while `chrono window` would host a pywebview window in its own
    # process. What the user wants from doctor is the one that is actually
    # running, so the live shell wins when there is one.
    shell_rec = None
    try:
        shell_rec = ShellRecord.load()
    except (OSError, ValueError, KeyError) as exc:
        # Reporting, not swallowing: an unreadable record is a finding.
        rows.append(("desktop shell", "UNREADABLE", f"{shell_record_path()}: {exc}"))
        problems.append(f"shell record at {shell_record_path()} is unreadable: {exc}")
    if shell_rec is not None and shell_rec.alive:
        rows.append(("panel window", f"{shell_rec.backend} shell", shell_rec.describe()))
    elif shell_rec is not None:
        rows.append((
            "desktop shell",
            "stale record",
            f"pid {shell_rec.pid} is gone ({shell_rec.exe}); window not running",
        ))

    # pywebview, i.e. what `chrono window` would use.  Checked even when the
    # shell is up, so a checkout with a broken pywebview still says so.
    #
    # This was missing, and its absence is why a missing pywebview went
    # unnoticed: `chrono window` silently degrades to a browser tab, so the user
    # sees a web page and concludes the project is a web app.  The check has to
    # name the INTERPRETER too, because the usual cause is running a different
    # Python than the one the dependencies were installed into.
    try:
        import webview  # noqa: F401, PLC0415

        rows.append((
            "pywebview window",
            "native",
            f"`chrono window` can open a native window  [{sys.executable}]",
        ))
    except Exception as exc:
        rows.append((
            "pywebview window",
            "browser tab",
            f"pywebview missing ({type(exc).__name__}) -> install into {sys.executable}",
        ))

    # daemon
    client = _client(args)
    healthy = client.healthy()
    rows.append(
        ("daemon", f"up @ {client.host}:{client.port}" if healthy else f"down @ {client.host}:{client.port}",
         "start with: chrono daemon --background")
    )
    if healthy:
        try:
            st = client.call("ping")
            rows.append(("daemon stats", json.dumps({k: st.get(k) for k in ("runs", "active", "audio", "scheduler_firings")}), ""))
            got = st.get("contract")
            if got != CONTRACT_VERSION:
                # The two halves of an install can come from different builds --
                # a leftover sidecar, a half-finished upgrade, a stale MSI.  Say
                # which pair is on this machine, because "the UI is acting
                # strangely" is the symptom and this is the cause.
                rows.append((
                    "daemon contract",
                    f"{got} (this build speaks {CONTRACT_VERSION})",
                    f"daemon is version {st.get('version')}; these are different builds",
                ))
                problems.append(
                    f"the daemon speaks contract {got}, this build speaks {CONTRACT_VERSION} "
                    f"(daemon version {st.get('version')}): the window and the conductor "
                    "are from different builds -- reinstall so both come from one installer"
                )
        except Exception as exc:
            # Not silent: `healthy()` already answered, so a failure here is news.
            rows.append(("daemon stats", "unavailable", f"{type(exc).__name__}: {exc}"))

    cal = CalibrationStore()
    rows.append(("calibration", str(cal.path), f"{len(cal.all())} signature(s)"))
    if cal.readonly_reason:
        problems.append(cal.readonly_reason)

    # A marker left behind means the last update did not finish starting.  The
    # shell writes it before an installer runs and removes it once the window and
    # the conductor are both up, so its presence is the only evidence a failed
    # update leaves -- and this is where someone looks when the app "behaves
    # oddly since it updated".  The kept installer is named because that is the
    # way back.
    from .home import chrono_home

    marker = chrono_home() / "pending_update.json"
    if marker.is_file():
        try:
            pending = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as exc:
            rows.append(("pending update", "UNREADABLE", f"{marker}: {exc}"))
            problems.append(f"the update marker at {marker} is unreadable: {exc}")
        else:
            artifact = chrono_home() / "updates" / str(pending.get("artifact", "?"))
            rows.append((
                "pending update",
                f"{pending.get('from')} -> {pending.get('to')}",
                f"the last update never came up; go back with {artifact}",
            ))
            problems.append(
                f"the last update ({pending.get('from')} -> {pending.get('to')}) did not "
                f"finish starting; install {artifact} to go back"
            )

    # State files carry a schema (see schema.py).  When one cannot be written,
    # doctor is the place that says so: the panel can only say "could not save".
    from .schema import SCHEMA
    from .settings import Settings, default_settings_path

    settings = Settings.load()
    rows.append((
        "settings",
        str(default_settings_path()),
        settings.readonly_reason or f"schema {SCHEMA}, writable",
    ))
    if settings.readonly_reason:
        problems.append(settings.readonly_reason)

    if getattr(args, "json", False):
        # Machine-readable, so a build check can compare this app's capabilities
        # against the source checkout's without scraping the aligned columns
        # below.  Deliberately ASCII (json's default): the reader may be a pipe
        # whose encoding is the local ANSI codepage, not UTF-8.
        print(json.dumps({
            "version": __version__,
            "contract": CONTRACT_VERSION,
            "rows": [{"name": n, "value": v, "note": d} for n, v, d in rows],
            "problems": problems,
        }))
        return 1 if problems else 0

    width = max(len(r[0]) for r in rows)
    print(f"chrono-handoff {__version__} (contract {CONTRACT_VERSION}) -- environment")
    for name, value, note in rows:
        print(f"  {name:<{width}}  {value:<34} {note}")
    if problems:
        print()
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def _selftest_clock(n: int = 40, ioi: float = 0.05) -> tuple[bool, str]:
    s = Scheduler("selftest").start()
    hits: list[Any] = []
    done = threading.Event()
    t0 = CLOCK() + 0.05

    def cb(d: float) -> None:
        hits.append((CLOCK(), d))
        if len(hits) >= n:
            done.set()

    s.grid(t0, ioi, cb, count=n, name="st")
    ok_wait = done.wait(5.0)
    s.stop()
    if not ok_wait or len(hits) < n:
        return False, f"only {len(hits)}/{n} firings arrived"
    offsets = [(a - d) * 1000.0 for a, d in hits]
    drift = (hits[-1][0] - t0) - (n - 1) * ioi
    max_off = max(abs(o) for o in offsets)
    ok = max_off < 5.0 and abs(drift) < 0.005
    return ok, f"n={n} max|offset|={max_off:.3f}ms total_drift={drift*1000:+.3f}ms (thresholds 5ms / 5ms)"


def _selftest_grid_budget() -> tuple[bool, str]:
    """A counted grid must fire exactly ``count`` times and then stop.

    This is the invariant that was silently broken while writing the M1 runner:
    an over-running grid keeps firing on the *previous* run's schedule, so the
    actions of run N land inside the measurement window of run N+1.  It is
    invisible in a single-trial test and obvious only in the aggregate, which
    is exactly the kind of bug that would quietly poison the experiment.
    """
    s = Scheduler("budget").start()
    n = 6
    fired: list[float] = []
    t0 = CLOCK() + 0.05

    def cb(d: float) -> None:
        fired.append(CLOCK())

    s.grid(t0, 0.03, cb, count=n, name="b")
    time.sleep(0.03 * (n + 5) + 0.15)   # wait well past the end of the grid
    s.stop()
    ok = len(fired) == n
    return ok, f"count={n} fired={len(fired)} (extra firings = schedule leak between runs)"


def _grid_burst_report(firings: list[Firing], hits: list[float], every: float,
                       n: int) -> tuple[bool, str]:
    """Judge a stalled grid: dropped-and-counted, or replayed in a burst?

    Pure, so the judgement can be tested against fabricated firings.  The two
    patterns differ only in the offsets of the beats that follow a near-zero gap,
    which is the whole distinction, and no scheduler can be asked to produce a
    replay on demand.
    """
    dropped = sum(f.skipped for f in firings)
    gaps = [(b - a) * 1000.0 for a, b in zip(hits, hits[1:])]
    offs = [f.offset * 1000.0 for f in firings]
    # A burst is a *replay*, and a small gap cannot show it on its own.  On an
    # absolute grid a beat that lands L ms late is followed by the next beat only
    # `every - L` later, so 4.9ms is what a 115ms lateness looks like on a 30ms grid
    # -- with nothing replayed at all.  That is what a loaded macOS runner reported
    # as `bursts=1`, for a run that had dropped 3 of its 10 beats.
    #
    # What separates a replay from a late beat is the *second* beat's own offset: on
    # time after a late beat (the drop path advances the next deadline past the
    # firing), but still overdue when several due beats are fired in one pass.
    replayed = [
        (round((b.at - a.at) * 1000.0, 3), round(b.offset * 1000.0, 1))
        for a, b in zip(firings, firings[1:])
        if (b.at - a.at) < 0.001 and b.offset >= every * 0.5
    ]
    # And every beat that was not fired has to be *counted*, not silently gone: that
    # is what "dropped and counted" means, and it is what a replay breaks.
    accounted = len(hits) + dropped == n
    ok = not replayed and accounted and len(hits) < n
    return ok, (f"fired={len(hits)} dropped={dropped} accounted={accounted} "
                f"gaps={[round(g, 1) for g in gaps]} offsets={[round(o, 1) for o in offs]} "
                f"replayed={replayed}")


def _selftest_grid_skip() -> tuple[bool, str]:
    """When the process stalls, missed beats are dropped and counted, not
    replayed in a burst.

    Two stalls, because they fail differently: 120ms lets the grid carry on after
    the drop, while 400ms drops everything that was left and ends the grid on the
    firing that dropped them -- which is where the count used to be lost entirely
    (`fired=3 dropped=0 accounted=False` on a loaded macOS runner).
    """
    import threading as _th

    every, n = 0.03, 10
    reports: list[str] = []
    ok_all = True
    for stall in (0.12, 0.40):
        s = Scheduler(f"skip{stall}").start()
        hits: list[float] = []
        blocked = _th.Event()

        def cb(_d: float, hits: list[float] = hits, blocked: _th.Event = blocked,
               stall: float = stall) -> None:
            hits.append(CLOCK())
            if len(hits) == 2:
                blocked.set()
                time.sleep(stall)

        s.grid(CLOCK() + 0.05, every, cb, count=n, name="sk")
        blocked.wait(2.0)
        # Longer than the stall: the grid resumes when the callback returns, and
        # stopping the scheduler first means the beat after the stall never fires at
        # all (`fired=2 dropped=0` with a 400ms stall and a 400ms wait).
        time.sleep(stall + 0.3)
        s.stop()
        ok, detail = _grid_burst_report([f for f in s.firings if f.name == "sk"],
                                        hits, every, n)
        ok_all = ok_all and ok
        reports.append(f"stall={stall * 1000:.0f}ms {detail}")
    return ok_all, " | ".join(reports)


def _selftest_late_addition() -> tuple[bool, str]:
    """Work submitted after a far-future job is already on top must still fire
    on time.

    Regression test for the worst bug found while building M1: the scheduler
    slept toward the top job's deadline, so a beat grid submitted a moment later
    fired seconds late (measured 3.8 s) -- which for a real human means the
    countdown simply does not happen.  It is invisible in any single-run test.
    """
    s = Scheduler("late").start()
    far = CLOCK() + 3.0
    near_done = threading.Event()
    offsets: list[float] = []

    def far_cb(_d: float) -> None:
        pass

    def near_cb(d: float) -> None:
        offsets.append((CLOCK() - d) * 1000.0)
        near_done.set()

    s.at(far, far_cb, name="far")                 # commit the thread far ahead
    time.sleep(0.05)
    s.after(0.25, near_cb, name="near")           # then add much earlier work
    ok_wait = near_done.wait(1.5)
    s.stop()
    if not ok_wait:
        return False, "near job never fired"
    worst = max(abs(o) for o in offsets)
    return worst < 20.0, f"near job landed with {offsets[0]:+.3f}ms offset while a +3s job was on top (threshold 20ms)"


def _selftest_under_load() -> tuple[bool, str]:
    """Scheduling accuracy while other Python threads hog the CPU.

    This is the honest limit of a pure-Python conductor and it is worth
    tracking: the GIL means a CPU-bound sibling thread can delay a beat, and a
    late beat is a late cue to a human.  The measured table lives in
    clock.GIL_SWITCH_INTERVAL_S; this check keeps it from silently regressing.
    """
    from .clock import tune_interpreter

    tune_interpreter()
    stop = threading.Event()

    def busy() -> None:
        while not stop.is_set():
            sum(range(2000))

    threads = [threading.Thread(target=busy, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        s = Scheduler("load").start()
        offs: list[float] = []
        done = threading.Event()
        t0 = CLOCK() + 0.1

        def cb(d: float) -> None:
            offs.append((CLOCK() - d) * 1000.0)
            if len(offs) >= 20:
                done.set()

        s.grid(t0, 0.05, cb, count=20, name="load")
        got = done.wait(5.0)
        s.stop()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=0.5)
    if not got or not offs:
        return False, "no firings collected"
    worst = max(abs(o) for o in offs)
    med = statistics.median(offs)
    return worst < 20.0, (
        f"with 2 CPU-bound sibling threads: median={med:.3f}ms worst={worst:.3f}ms "
        f"(GIL switch interval now {sys.getswitchinterval()*1000:.2f}ms)"
    )


def _selftest_audio() -> tuple[bool, str]:
    """The load-bearing invariant: the beat callback must not block.

    Asserted on the **median**, not the max, and the reason is the point of the
    test rather than a loosened threshold.  What this check exists to prevent is
    a *systematically* blocking audio path -- ``winsound.Beep`` blocks 35-60 ms
    on every beat, which would eat a fifth of a 300 ms window, every time.  A
    single 5 ms outlier from a GC pause or device contention is noise that the
    median already proves is not systematic.

    So: median must be ~sub-millisecond, and the max only has to stay below the
    blocking regime (20 ms) -- which ``Beep`` would blow through on the first
    beat.  Using max < 5 ms made this check flaky at roughly 1 run in 8 while
    providing no extra protection.
    """
    sink = default_audio("auto")
    if sink.precision != "device":
        # A spawn-based or terminal-based sink has tens-to-hundreds of ms of
        # unknown jitter by construction; there is nothing meaningful to assert
        # about a per-call duration, and pretending otherwise would be a green
        # check that proves nothing.
        sink.close()
        return True, f"skipped (sink={sink.name} is {sink.precision}-grade, not device-anchored)"
    sink.play("tick")
    time.sleep(0.15)  # warm the device: first call pays one-off init cost
    lat = []
    for _ in range(40):
        t = time.perf_counter()
        sink.play("tick")
        lat.append((time.perf_counter() - t) * 1000)
        time.sleep(0.02)
    sink.close()
    med = statistics.median(lat)
    worst = max(lat)
    # The *claim* is that the call is non-blocking, and the median is what proves it:
    # `Beep` blocks 35-60ms on **every** beat, so a systematically blocking path can
    # never hide in a sub-millisecond median.  The max is a single scheduling sample,
    # and on a starved runner one call took 116ms with a 0.137ms median -- which is
    # the machine, not the sink.  So the max is still asserted off CI (where a real
    # "sometimes it blocks" regression should fail), and on CI it is printed and not
    # required: the median keeps the verdict either way.
    tolerant = os.environ.get("CHRONO_TOLERANT_TIMING") == "1"
    ok = med < 1.0 and (tolerant or worst < 20.0)
    note = "  (max excused: one sample, measures the machine)" if tolerant and worst >= 20.0 else ""
    return ok, (
        f"{sink.name} call: median={med:.3f}ms max={worst:.3f}ms "
        f"(median <1ms, max <20ms; Beep would block 35ms+){note}"
    )


def _selftest_pipeline(offsets_ms: list[float], ioi_ms: float = 500.0, lead_s: float = 0.5) -> tuple[bool, str]:
    """End-to-end with a synthetic human: grid -> offset -> statistics."""
    conductor = Conductor(audio="null", visual=False, key_latency_s=0.0)
    conductor.start()
    observed: list[float] = []
    try:
        for i, off in enumerate(offsets_ms):
            plan = CuePlan(
                id=f"st_{i}",
                instruction="synthetic",
                lead_s=lead_s,
                ioi_ms=ioi_ms,
                audio=False,
                visual=False,
                target_in_s=lead_s + ioi_ms / 1000.0,
                expect=SignalSpec(source="sim", timeout_s=3.0, min_offset_ms=-ioi_ms / 2.0,
                                  opts={"offsets_ms": [off], "seed": i}),
                duration_s=4.0,
                meta={"calibrate": False},
            )
            run = conductor.submit(plan)
            conductor.wait(run.id, timeout_s=5.0, poll_s=0.002)
            if run.human_offset_ms is None:
                return False, f"trial {i} produced no signal (status={run.status.value})"
            observed.append(run.human_offset_ms)
    finally:
        conductor.stop(terminate_runs=False)

    pairs = list(zip(offsets_ms, observed))
    errs = [abs(a - b) for a, b in pairs]
    worst = max(errs)
    # Both sides are measured against the same monotonic clock, so the
    # synthetic human's offset must come back essentially exactly.
    return worst < 5.0, f"recovered offsets {[round(o,1) for o in observed]} vs injected {offsets_ms} (max err {worst:.2f}ms)"


def _selftest_calibration() -> tuple[bool, str]:
    r = simulate_calibration(true_offset_ms=45.0, noise_ms=25.0, n=20)
    ok = r["abs_error_ms"] < 30.0 and abs(r["outlier_shift_ms"]) <= 31.0
    return ok, (
        f"true={r['true_offset_ms']}ms estimate={r['final_estimate_ms']:.1f}ms "
        f"err={r['abs_error_ms']:.1f}ms MAD={r['final_mad_ms']:.1f}ms; "
        f"one 5000ms outlier moved the estimate by only {r['outlier_shift_ms']:+.1f}ms"
    )


def _selftest_ipc() -> tuple[bool, str]:
    """Exercise the exact dispatch path the MCP tools use, without sockets."""
    conductor = Conductor(audio="null", visual=False, key_latency_s=0.0)
    conductor.start()
    srv = ConductorServer(conductor, port=0)
    try:
        sub = srv.dispatch(
            "submit",
            {
                "plan": {
                    "id": "ipc_st",
                    "instruction": "ipc selftest",
                    "lead_s": 0.3,
                    "ioi_ms": 200.0,
                    "audio": False,
                    "visual": False,
                    "target_in_s": 0.5,
                    "expect": {"source": "sim", "timeout_s": 2.0, "opts": {"offsets_ms": [10.0]}},
                    "duration_s": 3.0,
                    "meta": {"calibrate": False},
                }
            },
        )
        run_id = sub["run_id"]
        snap = srv.dispatch("wait", {"run_id": run_id, "timeout_s": 4.0})
        obs = srv.dispatch("observe", {"run_id": run_id, "since_seq": 0})
        kinds = [e["kind"] for e in obs["events"]]
        ok = snap["status"] == "satisfied" and "human_signal" in kinds and "cue_zero" in kinds
        return ok, f"status={snap['status']} offset={snap['human_offset_ms']} events={len(kinds)}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conductor.stop(terminate_runs=False)


def _selftest_source_fallback() -> tuple[bool, str]:
    """An unavailable source must degrade to a stated note, never a crash."""
    conductor = Conductor(audio="null", visual=False)
    conductor.start()
    try:
        plan = CuePlan(
            id="st_fallback",
            lead_s=0.2,
            ioi_ms=200.0,
            audio=False,
            visual=False,
            target_in_s=0.4,
            expect=SignalSpec(source="serial", timeout_s=0.6, opts={"port": "COM_NOT_REAL"}),
            duration_s=1.2,
            meta={"calibrate": False},
        )
        run = conductor.submit(plan)
        snap = conductor.wait(run.id, timeout_s=3.0, poll_s=0.005)
        ok = run.source is None and any("unavailable" in n for n in run.notes) and snap["status"] in ("timeout", "partial")
        return ok, f"status={snap['status']} notes={run.notes}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conductor.stop(terminate_runs=False)


#: The checks that measure the *machine*, not the code: they compare the
#: scheduler's landing error against fixed thresholds (5 ms, and 20 ms for the
#: starved-near-job check), which a shared CI runner cannot promise.  Measured on a
#: hosted macOS runner: 19.2 ms max offset and a 7.9 ms median under contention, with
#: nothing wrong in the code -- and the same checks read 0.006 ms on the author's
#: machine.  The starved-near-job check landed at 21.659 ms against its 20 ms bar in a
#: run where nine jobs shared the runner, and passed on the identical build 20 minutes
#: earlier; it is a scheduling latency like the contention check above, not a claim
#: about the code's ordering.
#:
#: Named explicitly rather than inferred, because a list of "failures we do not
#: count" that quietly grows is exactly how a real regression gets ignored.
TIMING_ONLY_CHECKS = ("clock: grid accuracy & drift", "clock: accuracy under CPU contention",
                      "clock: late-arriving work is not starved")


def cmd_selftest(args: argparse.Namespace) -> int:
    checks: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("clock: grid accuracy & drift", _selftest_clock),
        ("clock: counted grid stops at count", _selftest_grid_budget),
        ("clock: stalled grid drops, never bursts", _selftest_grid_skip),
        ("clock: late-arriving work is not starved", _selftest_late_addition),
        ("clock: accuracy under CPU contention", _selftest_under_load),
        ("cue: audio call is non-blocking", _selftest_audio),
        ("pipeline: sim human end-to-end", lambda: _selftest_pipeline([-40.0, 0.0, 60.0, 120.0])),
        ("calibration: converges, outlier-immune", _selftest_calibration),
        ("ipc: submit/observe/wait dispatch path", _selftest_ipc),
        ("robustness: unavailable source degrades", _selftest_source_fallback),
    ]
    tolerant = bool(getattr(args, "tolerant_timing", False))
    failures = 0
    excused = 0
    print(f"chrono-handoff {__version__} selftest")
    for name, fn in checks:
        t = time.perf_counter()
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"EXCEPTION {type(exc).__name__}: {exc}"
        dur = (time.perf_counter() - t) * 1000
        note = ""
        if not ok and tolerant and name in TIMING_ONLY_CHECKS:
            # Downgraded, not hidden: the line still says FAIL and still shows the
            # number, because that number is the only way to tell a loaded runner
            # from a real regression.
            note = "  (excused: measures the machine -- see --tolerant-timing)"
            excused += 1
        else:
            if not ok:
                failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<42} ({dur:6.1f}ms)  {msg}{note}")
    print(f"\n{len(checks) - failures - excused}/{len(checks)} passed"
          + (f", {excused} excused as environment noise" if excused else ""))
    return 1 if failures else 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    from .probe import run_probe, save_report

    rep = run_probe(
        trials=args.trials,
        conditions=[c.strip() for c in args.conditions.split(",") if c.strip()],
        ioi_ms=args.ioi,
        lead_s=args.lead,
        target_in_s=args.target_in,
        source=args.source,
        warmup=args.warmup,
        audio=args.audio,
        visual=not args.no_visual,
        visual_style=args.visual_style,
        tail_beats=args.tail_beats,
        calibrate=not args.no_calibrate,
        calibrate_every=args.calibrate_every,
        journal_dir=args.journal_dir,
        auto=args.auto,
        sim_offsets_ms=[float(x) for x in args.sim_offsets.split(",")] if args.sim_offsets else None,
        key_latency_ms=args.key_latency,
        store_path=args.store,
    )
    if args.out:
        p = save_report(rep, args.out)
        print(f"\nreport written to {p}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from . import daemon as daemon_mod

    argv = ["--host", args.host, "--port", str(args.port), "--audio", args.audio]
    if args.journal_dir:
        argv += ["--journal-dir", args.journal_dir]
    if args.no_visual:
        argv += ["--no-visual"]
    if args.verbose:
        argv += ["--verbose"]
    if args.background:
        argv += ["--background"]
    if getattr(args, "window", False):
        argv += ["--window"]
        if args.no_on_top:
            argv += ["--no-on-top"]
        if args.window_hidden:
            argv += ["--window-hidden"]
    if getattr(args, "no_panel", False):
        argv += ["--no-panel"]
    if args.panel_port:
        argv += ["--panel-port", str(args.panel_port)]
    if getattr(args, "panel_open", False):
        argv += ["--panel-open"]
    return daemon_mod.main(argv)


def cmd_submit(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .mcp_server import plan_from_dict

    # Same normalisation the MCP tools use, so `chrono submit -f plan.json` and
    # an agent tool call cannot disagree about the plan schema.
    plan = plan_from_dict(_load_plan(args))
    res = _client(args).call("submit", plan=asdict(plan), paused=args.paused, queue=args.queue)
    _print_json(res)
    return 0


def cmd_go(args: argparse.Namespace) -> int:
    """Start a run that was submitted with ``--paused``.

    The IPC method existed all along; only the CLI did not, so a paused cue
    submitted from a shell could be started from the panel and nowhere else.
    """
    res = _client(args).call("go", run_id=args.run_id, in_s=args.in_s)
    _print_json(res)
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    res = _client(args).call("observe", run_id=args.run_id, since_seq=args.since_seq)
    if args.events_only:
        for e in res["events"]:
            print(f"{e['seq']:>4}  t+{e['t']:8.3f}  {e['kind']:<22} {json.dumps(e['detail'], ensure_ascii=False)}")
    else:
        _print_json(res)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.run_id:
        snap = _client(args).call("status", run_id=args.run_id, include_events=args.include_events)
        # Lead with the verdict, not the state name.  `status: timeout` on a cue
        # that asked nobody for anything is the successful end of that cue, and
        # reading it as a failure is the mistake this line exists to prevent.
        verdict = snap.get("verdict") or "-"
        print(f"verdict: {verdict}  ({snap.get('verdict_reason') or '-'})  status: {snap.get('status')}")
        _print_json(snap)
    else:
        res = _client(args).call("list")
        for r in res["runs"]:
            off = r.get("human_offset_ms")
            off_s = f"{off:+8.1f}ms" if off is not None else "      n/a"
            verdict = r.get("verdict") or ("queued" if r.get("queued_behind") else "-")
            print(f"  {r['run_id']:<28} {r['status']:<10} {verdict:<16} {off_s}  {r['instruction'][:40]}")
        _print_json({"stats": res["stats"]})
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    _print_json(_client(args).call("wait", run_id=args.run_id, timeout_s=args.timeout_ms / 1000.0))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _print_json(_client(args).call("cancel", run_id=args.run_id, reason=args.reason))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _print_json(_client(args).call("verify", run_id=args.run_id, payload=args.payload))
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    """Open the native always-on-top cue window.

    Delegates to :mod:`chrono_handoff.window` so the CLI and the module entry
    point cannot drift apart in their argument handling.
    """
    from . import window as window_mod

    argv: list[str] = []
    if args.instruction:
        argv += ["--instruction", args.instruction]
    if args.in_s is not None:
        argv += ["--in", str(args.in_s)]
    if args.ioi is not None:
        argv += ["--ioi", str(args.ioi)]
    if args.lead is not None:
        argv += ["--lead", str(args.lead)]
    if args.tail_beats is not None:
        argv += ["--tail-beats", str(args.tail_beats)]
    if args.paused:
        argv.append("--paused")
    if args.bordered:
        argv.append("--bordered")
    if args.no_on_top:
        argv.append("--no-on-top")
    if args.width:
        argv += ["--width", str(args.width)]
    if args.height:
        argv += ["--height", str(args.height)]
    argv += ["--port", str(args.window_port)]
    if args.hidden:
        argv.append("--hidden")
    if args.debug:
        argv.append("--debug")
    if args.print_url:
        argv.append("--print-url")
    if args.attach is not None:
        argv += ["--attach", str(args.attach)]
    return window_mod.main(argv)


def cmd_settings(args: argparse.Namespace) -> int:
    """Inspect or edit the panel/window settings file."""
    from .settings import Settings, default_settings_path

    path = default_settings_path()
    s = Settings.load()
    if args.reset:
        s = Settings()
        s.save()
        print(f"reset to defaults: {path}")
        return 0
    if args.set:
        patch = {}
        for item in args.set:
            if "=" not in item:
                print(f"ignoring {item!r} (expected key=value)", file=sys.stderr)
                continue
            k, v = item.split("=", 1)
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
            patch[k] = v
        s.update(patch)
        s.save()
        print(f"updated: {', '.join(patch)}  ->  {path}")
        return 0
    print(f"settings file: {path}\n")
    for k, v in sorted(s.to_dict().items()):
        print(f"  {k:<24} {v}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    store = CalibrationStore()
    if args.reset:
        store.reset(args.key)
        print(f"reset {'all signatures' if not args.key else args.key} in {store.path}")
        return 0
    if args.key and args.offset is not None:
        cal = store.observe(args.key, args.offset, source="cli")
        print(f"{cal.key}: offset={cal.offset_ms:+.1f}ms n={cal.n} MAD={cal.spread_ms():.1f}ms")
        return 0
    print(f"calibration store: {store.path}\n")
    print(store.report())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Bring up the whole component for a hand trial: conductor + panel + cue.

    Kept as one command because what is being evaluated is the *experience* --
    does a panel appear, does the pulse lead the human, does the press land --
    not the internals.
    """
    from .webui import CuePanel

    conductor = Conductor(
        journal_dir=args.journal_dir,
        audio=args.conductor_audio,
        visual=not args.no_terminal,
    )
    conductor.start()
    panel = CuePanel(conductor, host=args.host, port=args.panel_port, auto_open=False)
    panel.bind()

    # on_panel_open's contract is ``-> None``; open_browser() returns a bool, so
    # it is forwarded through an explicit void callback.
    def _open_panel(_rid: str) -> None:
        panel.open_browser()

    conductor.on_panel_open(_open_panel)

    if not args.no_open:
        # Raise the panel BEFORE the first cue.  A panel that opens when the
        # countdown starts is a panel that opens too late -- the human needs to
        # already be looking at it.
        panel.open_browser()
        panel.opened = True

    run = conductor.cue(
        instruction=args.instruction,
        target_in_s=args.in_s,
        lead_s=args.lead,
        ioi_ms=args.ioi,
        tail_beats=args.tail_beats,
        audio=True,
        visual=True,
        duration_s=args.timeout,
        expect_source=args.source,
        paused=args.paused,
        open_panel=True,
    )
    print(f"cue panel : {panel.url}")
    print(f"run       : {run['run_id']}  (status={run['status']})")
    print(f"target    : t+{args.in_s:.1f}s  lead={args.lead:.1f}s  ioi={args.ioi:.0f}ms  tail={args.tail_beats}")
    if args.paused:
        print("paused    : press Start in the panel to begin the countdown")
    print("follow the pulse; do the action above on the accented GO beat; Ctrl-C to stop")
    try:
        panel.serve_forever(open_browser=False)
    except KeyboardInterrupt:
        pass
    finally:
        conductor.stop()
        panel.stop()
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """Run a cue panel attached to an already-running daemon."""
    from .remote import RemoteConductor
    from .webui import CuePanel

    client = _client(args)
    if not client.healthy():
        print(
            f"no conductor at {client.host}:{client.port} -- start one with: chrono daemon --background",
            file=sys.stderr,
        )
        return 1
    # NOTE: this used to build a private _RemoteConductor defined in this file,
    # a stale duplicate of remote.RemoteConductor.  It was missing
    # clock_sync_payload, calibration and note_panel_press (so /api/clock,
    # /api/calibration and /api/press all returned 500), its get() returned a raw
    # dict instead of a run object (AttributeError on .plan), and its on_armed
    # could never fire because nothing polled the daemon -- so the page never
    # learned a cue had been armed.  Use the real one.
    remote = RemoteConductor(client)
    panel = CuePanel(remote, host=args.host, port=args.panel_port, auto_open=not args.no_open)
    panel.bind()
    stop_watch = remote.watch_in_background()
    print(f"cue panel: {panel.url}   (attached to conductor at {client.host}:{client.port})")
    try:
        panel.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_watch()
        panel.stop()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    return mcp_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chrono", description="chrono-handoff -- time-anchored action handoff")
    p.add_argument("--version", action="version", version=f"chrono-handoff {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def net(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--host", default=DEFAULT_HOST)
        sp.add_argument("--port", type=int, default=int(os.environ.get("CHRONO_IPC_PORT", DEFAULT_IPC_PORT)))
        sp.add_argument("--timeout", type=float, default=8.0)

    d = sub.add_parser("doctor", help="check machine capabilities")
    net(d)
    d.add_argument("--json", action="store_true", help="machine-readable report for scripts")
    d.set_defaults(fn=cmd_doctor)

    st = sub.add_parser("selftest", help="verify the machinery without a human")
    st.add_argument("--tolerant-timing", action="store_true",
                    help="exit 0 when the only failures are the two checks that measure the "
                         "machine's scheduling accuracy rather than the code (for shared CI "
                         "runners, which cannot promise 5 ms)")
    st.set_defaults(fn=cmd_selftest)

    pr = sub.add_parser("probe", help="M1: measure the human offset against a beat grid")
    pr.add_argument("--trials", type=int, default=24)
    pr.add_argument("--conditions", default="both", help="comma list from: both,audio,visual,silent")
    pr.add_argument("--ioi", type=float, default=500.0, help="beat interval ms")
    pr.add_argument(
        "--tail-beats",
        type=int,
        default=6,
        help="beats kept running AFTER the target. Essential: without a continuing pulse the "
        "subject can only react to the GO beat, and the result is a reaction time, not a "
        "synchronisation measurement. 0 = old (reaction-forcing) behaviour.",
    )
    pr.add_argument("--lead", type=float, default=3.0, help="countdown seconds")
    pr.add_argument("--target-in", type=float, default=6.0, help="seconds from cue start to the physical target")
    pr.add_argument("--source", default="console_key", help="console_key|stdin_line|sim|http")
    pr.add_argument("--warmup", type=int, default=2, help="trials excluded from the report")
    pr.add_argument("--audio", default="auto", choices=list(AUDIO_CHOICES))
    pr.add_argument("--no-visual", action="store_true")
    pr.add_argument(
        "--visual-style",
        default="marker",
        choices=["marker", "bar", "flash"],
        help="visual beat style: marker (best metronome) | bar | flash",
    )
    pr.add_argument("--no-calibrate", action="store_true")
    pr.add_argument("--calibrate-every", type=int, default=0, help="apply the learned offset after N trials (adaptive run)")
    pr.add_argument("--journal-dir", default=None)
    pr.add_argument("--store", default=None, help="calibration store path")
    pr.add_argument("--key-latency", type=float, default=None, help="override the keystroke latency correction (ms)")
    pr.add_argument("--sim-offsets", default=None, help="comma list of synthetic offsets in ms (dry run)")
    pr.add_argument("--auto", action="store_true", help="no interactive 'press Enter when ready' prompt")
    pr.add_argument("--out", default=None, help="write the JSON report here")
    pr.set_defaults(fn=cmd_probe)

    dm = sub.add_parser("daemon", help="run the conductor daemon")
    net(dm)
    dm.add_argument("--journal-dir", default=None)
    dm.add_argument("--audio", default="auto", choices=list(AUDIO_CHOICES))
    dm.add_argument("--no-visual", action="store_true")
    dm.add_argument("--verbose", action="store_true")
    dm.add_argument("--background", action="store_true")
    dm.add_argument("--window", action="store_true",
                    help="host the always-on-top native cue window (recommended)")
    dm.add_argument("--no-on-top", action="store_true", help="native window: allow other windows above it")
    dm.add_argument("--window-hidden", action="store_true", help="native window: start hidden, reveal on cue")
    dm.add_argument("--no-panel", action="store_true", help="do not serve the browser panel")
    dm.add_argument("--panel-port", type=int, default=8790)
    dm.add_argument("--panel-open", action="store_true", help="open the browser panel on start")
    dm.set_defaults(fn=cmd_daemon)

    sb = sub.add_parser("submit", help="submit a plan")
    net(sb)
    sb.add_argument("-f", "--file", default=None)
    sb.add_argument("--json", default=None)
    sb.add_argument("--paused", action="store_true", help="register but do not start the clock")
    sb.add_argument(
        "--no-queue",
        dest="queue",
        action="store_false",
        help="run alongside the cue already on screen instead of waiting for it "
        "(the MCP tools have the same switch; without it the two entry points disagreed)",
    )
    sb.set_defaults(fn=cmd_submit, queue=True)

    gs = sub.add_parser("go", help="start a submitted (paused) run")
    net(gs)
    gs.add_argument("run_id")
    gs.add_argument("--in", dest="in_s", type=float, default=0.0, help="start this many seconds from now")
    gs.set_defaults(fn=cmd_go)

    ob = sub.add_parser("observe", help="incremental journal read")
    net(ob)
    ob.add_argument("run_id")
    ob.add_argument("--since-seq", type=int, default=0)
    ob.add_argument("--events-only", action="store_true")
    ob.set_defaults(fn=cmd_observe)

    ss = sub.add_parser("status", help="run status / list")
    net(ss)
    ss.add_argument("run_id", nargs="?")
    ss.add_argument("--include-events", action="store_true")
    ss.set_defaults(fn=cmd_status)

    wt = sub.add_parser("wait", help="bounded wait for a snapshot")
    net(wt)
    wt.add_argument("run_id")
    wt.add_argument("--timeout-ms", type=int, default=8000)
    wt.set_defaults(fn=cmd_wait)

    cn = sub.add_parser("cancel", help="abort a run")
    net(cn)
    cn.add_argument("run_id")
    cn.add_argument("--reason", default="cli_cancel")
    cn.set_defaults(fn=cmd_cancel)

    vf = sub.add_parser("verify", help="mark a run's action as confirmed effective")
    net(vf)
    vf.add_argument("run_id")
    vf.add_argument("--payload", default="verified")
    vf.set_defaults(fn=cmd_verify)

    cb = sub.add_parser("calibrate", help="inspect or update the human offset estimate")
    cb.add_argument("--key", default=None)
    cb.add_argument("--offset", type=float, default=None, help="fold in a manual measurement (ms)")
    cb.add_argument("--reset", action="store_true")
    cb.set_defaults(fn=cmd_calibrate)

    dm2 = sub.add_parser("demo", help="interactive cue panel + a hand cue")
    # The demo text is what a first-time user reads, so it must not describe the
    # panel's own button as the task: the point of the component is "do X at T",
    # not "press GO".  Naming a concrete object keeps the physical action where
    # it belongs while staying useful on any bench.
    dm2.add_argument("--instruction", default="在重拍那一刻按一下开发板上的 RESET 键")
    dm2.add_argument("--in", dest="in_s", type=float, default=6.0,
                     help="seconds until the physical action should land")
    dm2.add_argument("--lead", type=float, default=3.0, help="countdown seconds before GO")
    dm2.add_argument("--ioi", type=float, default=500.0, help="beat interval ms")
    dm2.add_argument("--tail-beats", type=int, default=6, help="beats after GO (keeps the pulse)")
    dm2.add_argument("--timeout", type=float, default=30.0)
    # `http` on purpose: the demo runs with no hardware attached, so the panel is
    # the only observation surface that can work out of the box.  It is *a* valid
    # use of the component (and the one the probe/calibration paths measure), not
    # the shape of the product -- `expect_source` defaults to the sensor in real
    # plans, and the panel hides its button entirely when a sensor owns the
    # evidence.
    dm2.add_argument("--source", default="http",
                     help="how the action is observed: http (the panel itself) | console_key | serial | none")
    dm2.add_argument("--paused", action="store_true", help="arm, then wait for the panel's Start")
    dm2.add_argument("--host", default=DEFAULT_HOST)
    dm2.add_argument("--panel-port", type=int, default=8790)
    dm2.add_argument("--conductor-audio", default="null", choices=list(AUDIO_CHOICES))
    dm2.add_argument("--no-terminal", action="store_true", help="do not also render in this terminal")
    dm2.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    dm2.add_argument("--journal-dir", default=None)
    dm2.set_defaults(fn=cmd_demo)

    pn = sub.add_parser("panel", help="cue panel attached to a running daemon")
    net(pn)
    pn.add_argument("--panel-port", type=int, default=8790)
    pn.add_argument("--no-open", action="store_true")
    pn.set_defaults(fn=cmd_panel)

    wn = sub.add_parser("window", help="native always-on-top cue window (WebView2)")
    wn.add_argument("--instruction", default="", help="arm this cue immediately")
    wn.add_argument("--in", dest="in_s", type=float, default=None, help="seconds until the action should land")
    wn.add_argument("--ioi", type=float, default=None, help="beat interval ms")
    wn.add_argument("--lead", type=float, default=None, help="countdown seconds")
    wn.add_argument("--tail-beats", type=int, default=None, help="beats after GO")
    wn.add_argument("--paused", action="store_true", help="arm but wait for the panel's Start button")
    wn.add_argument("--bordered", action="store_true", help="use the OS title bar")
    wn.add_argument("--no-on-top", action="store_true")
    wn.add_argument("--width", type=int, default=None)
    wn.add_argument("--height", type=int, default=None)
    wn.add_argument("--window-port", type=int, default=0, help="panel port (0 = free port)")
    wn.add_argument("--hidden", action="store_true", help="start hidden")
    wn.add_argument("--debug", action="store_true", help="enable the webview inspector")
    wn.add_argument("--print-url", action="store_true")
    wn.add_argument("--attach", type=int, default=None, metavar="IPC_PORT",
                    help="display client only: watch an existing daemon (recommended with 'daemon --background')")
    wn.set_defaults(fn=cmd_window)

    se = sub.add_parser("settings", help="inspect/edit the settings file")
    se.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a value (repeatable)")
    se.add_argument("--reset", action="store_true")
    se.set_defaults(fn=cmd_settings)

    mc = sub.add_parser("mcp", help="run the MCP stdio server")
    mc.set_defaults(fn=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"chrono: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
