"""Verify `chrono panel`: a cue panel attached to an already-running daemon.

This path had NO coverage, and it was broken in several ways at once:

* ``cli.py`` carried a private ``_RemoteConductor`` that duplicated
  ``remote.RemoteConductor``.  It lacked ``clock_sync_payload``, ``calibration``
  and ``note_panel_press``, so ``/api/clock``, ``/api/calibration`` and
  ``/api/press`` all returned HTTP 500.  ``/api/clock`` is what the page's
  ``syncClock()`` calls on load, so the countdown was unusable.
* Its ``get()`` returned a raw dict, so ``run.plan`` raised AttributeError.
* Its ``on_armed`` could never fire -- nothing polled the daemon -- so the page
  never learned a cue had been armed.
* ``_RunRef.status`` was a plain string while CuePanel reads ``run.status.value``.

The test drives the REAL ``cmd_panel`` against a REAL conductor over a REAL IPC
port.  Run: .venv/Scripts/python.exe scripts/e2e_panel_attach.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("CHRONO_HOME", str(Path(__file__).resolve().parents[1] / "logs" / "_home"))

from chrono_handoff.cli import cmd_panel  # noqa: E402
from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.mcp_server import ConductorServer  # noqa: E402

IPC_PORT = 8841
PANEL_PORT = 8842
OUT: dict = {}
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<50} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def req(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_panel_attach")
    conductor.start()
    server = ConductorServer(conductor, host="127.0.0.1", port=IPC_PORT)
    server.bind()
    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    time.sleep(0.4)

    # exactly what `chrono panel --port 8841 --panel-port 8842 --no-open` runs
    args = argparse.Namespace(host="127.0.0.1", port=IPC_PORT, panel_port=PANEL_PORT,
                              no_open=True, timeout=8.0)
    threading.Thread(target=cmd_panel, args=(args,), name="cmd_panel", daemon=True).start()

    base = f"http://127.0.0.1:{PANEL_PORT}"
    for _ in range(40):
        if req(base + "/api/health")[0] == 200:
            break
        time.sleep(0.25)

    try:
        st, h = req(base + "/api/health")
        check("panel attached to the daemon is reachable", st == 200, f"HTTP {st} {h.get('error', '')}")

        st, c = req(base + "/api/clock")
        check("GET /api/clock succeeds (the page's syncClock path)",
              st == 200 and "server_recv_wall" in c, f"HTTP {st} {c.get('error', '')}")

        st, cal = req(base + "/api/calibration")
        check("GET /api/calibration succeeds", st == 200 and "items" in cal, f"HTTP {st} {cal.get('error', '')}")

        st, s = req(base + "/api/settings")
        check("GET /api/settings succeeds", st == 200 and "settings" in s, f"HTTP {st}")

        # ---- the panel must LEARN that a cue was armed -------------------
        run = conductor.cue(instruction="press RESET", target_in_s=8.0, lead_s=2.0,
                            ioi_ms=400, tail_beats=4, expect_source="none",
                            duration_s=30, open_panel=False)
        rid = run["run_id"]
        st, armed = req(f"{base}/api/wait-armed?timeout=3")
        check("panel learns the armed cue (poll watcher + on_armed)",
              st == 200 and armed.get("run_id") == rid, f"got {armed.get('run_id')!r} want {rid!r}")

        st, sch = req(f"{base}/api/schedule?run_id={rid}")
        check("GET /api/schedule carries the grid", st == 200 and len(sch.get("beats") or []) > 0,
              f"HTTP {st} beats={len(sch.get('beats') or [])}")

        # panel IS the evidence source here -> the click is recorded
        st, p1 = req(base + "/api/press", "POST", {"run_id": rid})
        check("press on a panel-observed run is recorded",
              st == 200 and p1.get("recorded") is True,
              f"HTTP {st} recorded={p1.get('recorded')} {p1.get('error', '')}")

        # ---- a run owned by a real sensor: the click is a note, not evidence
        run2 = conductor.cue(instruction="wait for PMON", target_in_s=8.0, lead_s=2.0,
                             ioi_ms=400, tail_beats=4, expect_source="console",
                             duration_s=30, open_panel=False)
        rid2 = run2["run_id"]
        time.sleep(0.8)
        st, p2 = req(base + "/api/press", "POST", {"run_id": rid2})
        check("press on a sensor-observed run is NOT recorded",
              st == 200 and p2.get("recorded") is False,
              f"HTTP {st} recorded={p2.get('recorded')} observed_by={p2.get('observed_by')} {p2.get('error', '')}")
        check("...and reports the real observer", p2.get("observed_by") == "console",
              f"observed_by={p2.get('observed_by')!r}")

        st, ev = req(f"{base}/api/events?since=0&run_id={rid2}")
        kinds = [e["kind"] for e in ev.get("events") or []]
        check("the note reached the journal as panel_press_not_evidence",
              "panel_press_not_evidence" in kinds, f"kinds={kinds[-6:]}")
    finally:
        conductor.stop()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("attached-panel checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
