"""The cue-safe window, end to end, with a daemon that really leaves.

An updater that restarts the window in the middle of a cue leaves somebody who
was told to press at T holding a dead window -- the one failure this component
exists to avoid.  So the conductor is the authority on "may I be stopped", and
this drives that over the real HTTP surface of a real daemon process:

  * idle                      -> safe, and it says why there is nothing to wait for
  * a submitted (not started) cue -> not safe, and `retry_in_s` is null rather
    than a plausible-looking guess
  * a running cue             -> not safe, with a retry that is a real number
  * a terminal run            -> safe again
  * preparing while unsafe    -> 409, and the daemon is still there
  * preparing while safe      -> the process exits 0 by itself, ports released,
    and its journal is on disk (which is what `journal_flushed` claims)

Run:  python scripts/e2e_update_window.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.mcp_server import ConductorClient  # noqa: E402
from chrono_handoff.model import CuePlan  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<52} {detail}", flush=True)
    if not ok:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def get_json(url: str, method: str = "GET") -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body}


def wait_panel(port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            if get_json(f"http://127.0.0.1:{port}/api/health")[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def in_process_refusal() -> None:
    """The quiescent state itself, without a process to leave.

    A conductor with no registered loop can only *ask* to be left alone, and it
    must still refuse new work -- otherwise the shell's "stopped" is news to the
    only process that matters.
    """
    print("\n-- a conductor that has stood down refuses new work --")
    home = tempfile.mkdtemp(prefix="chrono-standdown-")
    c = Conductor(journal_dir=Path(home) / "logs", audio="null", visual=False)
    try:
        c.start()
        check("idle conductor reports itself safe", c.update_ready()["safe"] is True)
        res = c.stand_down()
        check("with no loop registered it says so", res.get("exiting") is False and "note" in res,
              str(res.get("note"))[:60])
        check("and it is quiescent", c.update_ready()["quiescent"] is True)
        try:
            c.submit(CuePlan(id="", instruction="must be refused", lead_s=0.2, ioi_ms=200,
                             tail_beats=0))
            check("a new cue is refused", False, "submit went through")
        except RuntimeError as exc:
            check("a new cue is refused", "standing down" in str(exc), str(exc)[:50])
        check("standing down twice is not an error", c.stand_down().get("ok") is True)
    finally:
        c.stop()
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    in_process_refusal()

    home = tempfile.mkdtemp(prefix="chrono-update-window-")
    ipc, panel = free_port(), free_port()
    env = {**os.environ, "CHRONO_HOME": home, "PYTHONIOENCODING": "utf-8"}
    log_path = Path(home) / "daemon.log"
    url = f"http://127.0.0.1:{panel}"
    print(f"\n-- a real daemon: ipc={ipc} panel={panel} --")
    with log_path.open("w", encoding="utf-8") as log:
        # Its own process group on POSIX, so the cleanup below can signal all of it;
        # Windows needs nothing there (taskkill walks the child list).  Typed Any
        # because these kwargs are splatted into Popen, whose parameters all differ
        # in type -- anything narrower than Any rejects the call outright.
        detach: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
        proc = subprocess.Popen(
            [sys.executable, "-m", "chrono_handoff.cli", "daemon",
             "--port", str(ipc), "--panel-port", str(panel), "--audio", "null"],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, **detach,
        )
    try:
        if not wait_panel(panel, time.monotonic() + 30):
            tail = log_path.read_text(encoding="utf-8", errors="replace").strip()[-400:]
            print(f"  daemon never answered; its log says:\n{tail}")
            return 1
        client = ConductorClient(port=ipc, timeout_s=5.0)

        st, ready = get_json(url + "/api/update-ready")
        check("an idle conductor may be stopped", st == 200 and ready.get("safe") is True,
              f"HTTP {st} safe={ready.get('safe')}")
        check("and nothing is waiting to finish", ready.get("retry_in_s") == 0.0
              and ready.get("active") == [], f"retry_in_s={ready.get('retry_in_s')}")
        check("it claims its journal is already on disk", ready.get("journal_flushed") is True)
        check("the answer is self-describing", bool(ready.get("contract")) and bool(ready.get("version")),
              f"version={ready.get('version')} contract={ready.get('contract')}")

        # ---- a cue that was submitted but has not started ---------------
        run = client.call("submit", plan=CuePlan(id="", instruction="pending cue", lead_s=1.0,
                                                 ioi_ms=200, tail_beats=2, duration_s=30).__dict__,
                          paused=True)
        st, ready = get_json(url + "/api/update-ready")
        check("a submitted cue blocks the update", ready.get("safe") is False, f"safe={ready.get('safe')}")
        check("...and it says which cue", ready.get("active", [{}])[0].get("run_id") == run["run_id"],
              str(ready.get("active"))[:80])
        check("...with no invented retry time", ready.get("retry_in_s") is None,
              f"retry_in_s={ready.get('retry_in_s')}")
        st, refused = get_json(url + "/api/update-prepare", method="POST")
        check("preparing while a cue is in flight is refused with 409",
              st == 409 and refused.get("ok") is False, f"HTTP {st}")
        check("...and the daemon is still running", proc.poll() is None)

        # ---- a cue that is actually running ----------------------------
        client.call("go", run_id=run["run_id"], in_s=0.0)
        time.sleep(0.4)
        st, ready = get_json(url + "/api/update-ready")
        retry = ready.get("retry_in_s")
        check("a running cue blocks the update", ready.get("safe") is False)
        check("...and the retry is a real number inside the budget",
              isinstance(retry, (int, float)) and 0 < retry <= 30, f"retry_in_s={retry}")

        # ---- a terminal run frees it ----------------------------------
        client.call("cancel", run_id=run["run_id"], reason="e2e")
        time.sleep(0.3)
        st, ready = get_json(url + "/api/update-ready")
        check("a cancelled run stops blocking", ready.get("safe") is True, f"safe={ready.get('safe')}")

        # ---- now let it go --------------------------------------------
        journal = Path(client.call("list")["stats"]["journal_dir"]) / f"{run['run_id']}.jsonl"
        check("the run's journal is on disk before we stop anything",
              journal.is_file() and "aborted" in journal.read_text(encoding="utf-8"),
              f"{journal.name} exists={journal.is_file()}")

        st, prepared = get_json(url + "/api/update-prepare", method="POST")
        check("preparing while safe is accepted", st == 200 and prepared.get("ok") is True
              and prepared.get("exiting") is True, f"HTTP {st} {prepared}")
        left = False
        for _ in range(60):
            if proc.poll() is not None:
                left = True
                break
            time.sleep(0.25)
        check("the daemon leaves by itself", left, f"exit code {proc.poll()}")
        check("...with success, not a crash", proc.poll() == 0, f"rc={proc.poll()}")
        check("...and it released both ports",
              port_free(panel) and port_free(ipc), f"panel_free={port_free(panel)} ipc_free={port_free(ipc)}")
        check("...and nothing answers as a conductor any more",
              not ConductorClient(port=ipc, timeout_s=1.0).healthy())
    finally:
        if proc.poll() is None:
            # `taskkill` only exists on Windows, and this suite runs on three
            # platforms -- the first CI run died here with FileNotFoundError
            # 'taskkill' on macOS, which is a bug in the test, not the product.
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=15)
        shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("cue-safe update window passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
