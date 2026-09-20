"""Verify the SEPARATE-process mode: window as a disposable display.

This is the production claim in docs/component.md §四点六:

* the daemon owns the clock and the cues,
* the window is a display client that can be closed and reopened,
* a cue armed while no window exists still fires and is still recorded,
* a reopened window catches up to whatever the daemon is doing.

The failure this guards against is subtle and expensive: if the UI process ever
owned a second conductor, every beat would be scheduled twice and the human
would hear a doubled pulse -- which destroys the rhythmic reference the whole
component exists to provide.
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

from chrono_handoff.mcp_server import ConductorClient, ConductorServer  # noqa: E402

OUT: dict = {}
FAILURES: list[str] = []
IPC_PORT = 8823


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<48} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def main() -> int:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); skipping.")
        return 0

    from chrono_handoff.conductor import Conductor  # noqa: PLC0415
    from chrono_handoff.remote import RemoteConductor  # noqa: PLC0415
    from chrono_handoff.window import CueWindow  # noqa: PLC0415

    # ---- "daemon" side: a conductor plus its IPC server, no window ---------
    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_split")
    conductor.start()
    server = ConductorServer(conductor, host="127.0.0.1", port=IPC_PORT)
    server.bind()
    import threading

    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    agent = ConductorClient("127.0.0.1", IPC_PORT, timeout_s=5.0)
    OUT["daemon_ipc"] = agent.healthy()

    # (1) arm a cue with NO window open at all
    r1 = agent.call("cue", instruction="press RESET (no window open)", target_in_s=1.5,
                    lead_s=0.6, ioi_ms=250, tail_beats=3, expect_source="none",
                    duration_s=4, open_panel=False)
    OUT["armed_without_window"] = r1["run_id"]
    deadline = time.time() + 8
    while time.time() < deadline:
        st = agent.call("status", run_id=r1["run_id"])["status"]
        if st != "running":
            break
        time.sleep(0.3)
    OUT["no_window_status"] = st
    evs = [e["kind"] for e in agent.call("observe", run_id=r1["run_id"])["events"]]
    OUT["no_window_beats"] = sum(1 for k in evs if k in ("cue_beat", "cue_zero"))
    OUT["no_window_audit"] = "cue_audit" in evs

    # (2) now attach a window as a pure display client
    remote = RemoteConductor(agent)
    # A display client must refuse to arm cues.  Call each with arguments it
    # would actually receive: passing a stray positional raises TypeError, which
    # would make the test pass for the wrong reason (arg mismatch, not refusal).
    try:
        remote.submit(None)
        OUT["remote_submit_blocked"] = False
    except RuntimeError:
        OUT["remote_submit_blocked"] = True
    try:
        remote.cue(instruction="nope")
        OUT["remote_cue_blocked"] = False
    except RuntimeError:
        OUT["remote_cue_blocked"] = True

    win = CueWindow(conductor=remote, panel_port=0, on_top=True)
    OUT["window_owns_conductor"] = win._owns_conductor

    def ready(w) -> None:
        js = w.window.evaluate_js
        OUT["body"] = js("document.body.className")
        # Wait for the daemon's cue to arrive rather than sleeping a fixed
        # amount: a hard-coded sleep made this test destroy the window just
        # before the cue landed, which looked like "the window never got it".
        deadline = time.time() + 12.0
        while time.time() < deadline and not js("!!S.sched"):
            time.sleep(0.25)
        OUT["window_saw_cue_ready"] = js("!!S.sched")
        OUT["window_beats"] = js("S.sched ? S.sched.beats.length : 0")
        time.sleep(2.5)
        try:
            w.window.destroy()
        except Exception:
            pass

    # arm a cue ~1.2s after the window opens, from the "daemon" side
    def arm_during_window() -> None:
        time.sleep(3.5)
        r2 = agent.call("cue", instruction="press RESET (window attached)",
                        target_in_s=2.0, lead_s=0.8, ioi_ms=250, tail_beats=3,
                        expect_source="none", duration_s=5, open_panel=False)
        OUT["armed_with_window"] = r2["run_id"]
        time.sleep(2.0)

    threading.Thread(target=arm_during_window, name="arm", daemon=True).start()
    win.run(on_ready=ready)

    # (3) window closed; daemon must still be alive and serving
    OUT["daemon_alive_after_close"] = agent.healthy()
    r3 = agent.call("cue", instruction="press RESET (window closed again)",
                    target_in_s=1.2, lead_s=0.5, ioi_ms=250, tail_beats=2,
                    expect_source="none", duration_s=3, open_panel=False)
    deadline = time.time() + 7
    while time.time() < deadline:
        st = agent.call("status", run_id=r3["run_id"])["status"]
        if st != "running":
            break
        time.sleep(0.3)
    OUT["post_close_status"] = st
    OUT["post_close_events"] = [e["kind"] for e in agent.call("observe", run_id=r3["run_id"])["events"]]

    conductor.stop()

    print()
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))
    print()
    check("daemon IPC up (no window involved)", OUT.get("daemon_ipc") is True)
    check("cue armed with NO window open", bool(OUT.get("armed_without_window")))
    check("that cue ran to a verdict anyway", OUT.get("no_window_status") in ("timeout", "partial", "satisfied"),
          f"status={OUT.get('no_window_status')}")
    check("its beats were scheduled", (OUT.get("no_window_beats") or 0) >= 4, f"{OUT.get('no_window_beats')} beats")
    check("its cue audit was recorded", OUT.get("no_window_audit") is True)
    check("remote display cannot submit", OUT.get("remote_submit_blocked") is True)
    check("remote display cannot arm cues", OUT.get("remote_cue_blocked") is True)
    check("window does not own a conductor", OUT.get("window_owns_conductor") is False)
    check("window rendered the shell", "shell" in str(OUT.get("body", "")), f"body={OUT.get('body')!r}")
    check("window received the daemon's cue", OUT.get("window_saw_cue_ready") is True,
          f"{OUT.get('window_beats')} beats")
    check("daemon survives window close", OUT.get("daemon_alive_after_close") is True)
    check("cue after window close still fires", OUT.get("post_close_status") in ("timeout", "partial", "satisfied"),
          f"status={OUT.get('post_close_status')}")
    check("it was journalled too", "cue_audit" in (OUT.get("post_close_events") or []))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("split-mode (window as display) checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
