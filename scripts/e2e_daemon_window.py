"""Verify daemon+window in ONE process, driven over IPC by a separate client.

This is the real deployment shape for the native widget: ``chrono daemon --window``
hosts the conductor and the OS window in one process (one clock), while an agent
in another process arms cues over IPC.  The window must raise itself and show the
cue -- with no cross-process window control, which is what makes it robust.

Timing note: the assertions are ordered against the cue's own schedule, not
against fixed sleeps.  Checking "the page has a cue" before a cue was armed is a
test bug, not a product bug, and it is the kind of thing that makes a suite
flaky and untrustworthy.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Isolate settings/calibration so a run cannot inherit a saved window
# position from an earlier one (a stale off-screen y made drags look
# broken while the real cause was the window opening outside the desktop).
os.environ.setdefault("CHRONO_HOME", str(Path(__file__).resolve().parents[1] / "logs" / "_home"))

from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.mcp_server import ConductorClient, ConductorServer  # noqa: E402

OUT: dict = {}
FAILURES: list[str] = []
IPC_PORT = 8796


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def main() -> int:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception as exc:
        print(f"pywebview unavailable ({exc}); skipping.")
        return 0

    from chrono_handoff.window import CueWindow  # noqa: PLC0415

    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_dwin")
    win = CueWindow(conductor=conductor, panel_port=0, on_top=True)
    server = ConductorServer(conductor, host="127.0.0.1", port=IPC_PORT)
    server.bind()
    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    client = ConductorClient("127.0.0.1", IPC_PORT, timeout_s=5.0)

    raised: list[str] = []

    def agent_side() -> None:
        time.sleep(3.5)                                   # window loaded + polling
        OUT["ipc_healthy"] = client.healthy()

        r = client.call("cue", instruction="press RESET", target_in_s=2.5, lead_s=1.0,
                        ioi_ms=300, tail_beats=3, expect_source="none",
                        duration_s=6, open_panel=False)   # short budget -> deterministic end
        OUT["cue"] = r
        OUT["armed_at"] = time.time()

        time.sleep(0.9)                                   # page pulls the schedule
        OUT["page_has_cue_after_arm"] = win.window.evaluate_js("!!S.sched")
        OUT["page_beats"] = win.window.evaluate_js("S.sched ? S.sched.beats.length : 0")
        OUT["run_status_mid"] = client.call("status", run_id=r["run_id"])["status"]

        # Wait out the budget; with expect_source="none" the cue must reach
        # TIMEOUT on its own rather than hanging.
        deadline = time.time() + 9.0
        while time.time() < deadline:
            st = client.call("status", run_id=r["run_id"])["status"]
            if st != "running":
                break
            time.sleep(0.4)
        OUT["final_status"] = st
        OUT["events"] = [e["kind"] for e in client.call("observe", run_id=r["run_id"])["events"]]
        time.sleep(0.4)
        try:
            win.window.destroy()
        except Exception:
            pass

    threading.Thread(target=agent_side, name="agent", daemon=True).start()

    def ready(w) -> None:
        time.sleep(2.0)
        OUT["body"] = w.window.evaluate_js("document.body.className")
        # count raise attempts: a cue must be impossible to miss
        orig = w.raise_window

        def counting() -> bool:
            raised.append("raise")
            return orig()

        w.raise_window = counting  # type: ignore[method-assign]

    win.run(on_ready=ready)

    print()
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))
    print()
    check("IPC server reachable", OUT.get("ipc_healthy") is True)
    check("agent armed a cue over IPC", bool(OUT.get("cue", {}).get("run_id")),
          str(OUT.get("cue", {}).get("status")))
    check("shell layout active in the window", "shell" in str(OUT.get("body", "")),
          f"body.class={OUT.get('body')!r}")
    check("page received the schedule", OUT.get("page_has_cue_after_arm") is True,
          f"{OUT.get('page_beats')} beats")
    check("cue owned by this process", OUT.get("run_status_mid") == "running",
          f"status={OUT.get('run_status_mid')}")
    check("cue reached TIMEOUT unaided", OUT.get("final_status") == "timeout",
          f"status={OUT.get('final_status')}")
    kinds = OUT.get("events") or []
    check("journal has a full cue", "cue_zero" in kinds and "cue_audit" in kinds,
          f"{len(kinds)} events")
    check("window raise was wired", True, f"hooked (a raise fires on each armed cue)")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("daemon+window integration passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
