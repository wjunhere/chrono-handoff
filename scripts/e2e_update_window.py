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


def wait_panel(port: int, deadline: float, trace: list[str] | None = None) -> bool:
    """Wait for the panel, recording *when* it changed state.

    The timeline is the diagnosis, and silence alone cannot give it: "bound at
    +29.8s" means the daemon was slow to start, while "answering at +0.4s" means
    it came up immediately and something else failed to reach it.  Both look
    identical to a loop that only says "no".
    """
    start = time.monotonic()
    last = ""
    while time.monotonic() < deadline:
        try:
            if get_json(f"http://127.0.0.1:{port}/api/health")[0] == 200:
                if trace is not None:
                    trace.append(f"+{time.monotonic() - start:.1f}s answering 200")
                return True
        except (urllib.error.URLError, OSError):
            pass
        state = port_state(port, timeout=0.3)
        if state != last:
            if trace is not None:
                trace.append(f"+{time.monotonic() - start:.1f}s {state}")
            last = state
        time.sleep(0.3)
    if trace is not None:
        trace.append(f"+{time.monotonic() - start:.1f}s gave up, last state {last or 'unknown'}")
    return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def port_state(port: int, timeout: float = 2.0) -> str:
    """`unbound`, `bound` or `answering`.

    The difference between the first two is the whole diagnosis, and a request
    cannot see it: a conductor whose bind succeeded but whose accept loop never
    started has the kernel accept connections into the backlog and never answer
    them, so it reads as "nothing here".  Measured that way, the macOS hang was
    reported as "nothing is listening on either port" while what was true is that
    the IPC port was bound and stuck -- which points at the code between the bind
    and the accept loop, a completely different search.

    "answering" means bytes came back, not that the bytes were a 200 -- a server
    that answers every request with 500 is still "answering" and must not be
    described as absent.
    """
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        # Refused or unreachable: nothing is bound.  Only *this* failure means that.
        return "unbound"
    with conn:
        try:
            conn.settimeout(timeout)
            conn.sendall(b"GET /api/health HTTP/1.0\r\n\r\n")
            return "answering" if conn.recv(16) else "bound"
        except OSError:
            # Connected, then nothing came back.  `socket.timeout` is `TimeoutError`,
            # which is an `OSError` -- treating it with the connect failure above is
            # how the first version of this reported a listening socket as unbound.
            return "bound"


def raw_http(port: int, path: str, timeout: float = 3.0) -> str:
    """One request over a bare socket, reported as the wire saw it.

    Deliberately not urllib: when two clients disagree, the cruder one is the
    better witness about whether anything is there.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
            lines = s.recv(200).decode("utf-8", "replace").splitlines()
            return lines[0] if lines else "(closed with no response line)"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def urllib_probe(port: int) -> str:
    """The same GET through urllib, which is what this suite actually failed on.

    Catches everything: `wait_panel` only guards against `URLError`/`OSError`, so
    if urllib were raising something else the suite would have crashed there
    instead of timing out -- and guessing which of those happened is exactly what
    this probe exists to stop.
    """
    try:
        status, body = get_json(f"http://127.0.0.1:{port}/api/health")
        return f"HTTP {status} {str(body)[:120]}"
    except Exception as exc:  # noqa: BLE001 - the point is to name whatever it was
        return f"{type(exc).__name__}: {exc}"


def dump_stacks(proc: "subprocess.Popen[Any]", label: str) -> None:
    """Make a stuck child dump every thread's stack, then wait briefly for it.

    `SIGABRT` is handled by CPython's faulthandler at C level, which is why it
    works on a process that is not running Python bytecode: a Python-level
    handler cannot run while the main thread is inside a C call or blocked on a
    lock, and a pending handler is exactly what "ignored SIGTERM" looks like from
    outside.  The child leaves the traceback on the stderr it already shares with
    its log, so the log ends up naming the line it died on.  POSIX only -- there
    is no such signal to send on Windows.
    """
    if sys.platform == "win32":
        return
    print(f"  asking the {label} (pid {proc.pid}) where it is:")
    state = subprocess.run(["ps", "-o", "stat=,wchan=", "-p", str(proc.pid)],
                           capture_output=True, text=True)
    # Printed, not captured: the first version of this ran `ps` and threw the answer
    # away, which is how a stopped process (T) and a sleeping one (S) stayed
    # indistinguishable for another round.
    print(f"    ps: {state.stdout.strip() or state.stderr.strip() or '(no output)'}")
    try:
        os.kill(proc.pid, signal.SIGABRT)
    except OSError as exc:
        print(f"    could not signal it: {exc}")
        return
    deadline = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)


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
    env = {**os.environ, "CHRONO_HOME": home, "PYTHONIOENCODING": "utf-8",
           # So a hang can be asked where it is: the dump below comes from a C-level
           # signal handler, which runs even when the process is stuck somewhere that
           # Python-level handlers never get to.
           "PYTHONFAULTHANDLER": "1"}
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
            # `-u`: a killed process loses whatever is still sitting in its stdout
            # buffer, and "the log is empty" then reads as "it never printed" --
            # which is what this suite reported for the macOS hang until the banner
            # was traced to stderr+flush inside the product.
            [sys.executable, "-u", "-m", "chrono_handoff.cli", "daemon",
             # --verbose: the daemon then reports how long each start-up step took, so a
             # slow start names the line instead of only exceeding a deadline.
             "--verbose",
             "--port", str(ipc), "--panel-port", str(panel), "--audio", "null"],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, **detach,
        )
    try:
        trace: list[str] = []
        if not wait_panel(panel, time.monotonic() + 30, trace):
            print("  daemon never answered on the panel port")
            print(f"  timeline: {' -> '.join(trace)}")
            print(f"  pid alive: {proc.poll() is None} | ipc {ipc}: {port_state(ipc)} | "
                  f"panel {panel}: {port_state(panel)}")
            # The same request that failed, reported three ways: what a raw socket
            # sees, what the product's own client sees, and what the test's urllib
            # sees.  They disagree on macOS, and which one is broken decides whether
            # the product or the test is at fault.
            client = ConductorClient(port=ipc, timeout_s=2.0)
            print(f"  ConductorClient(ipc).healthy() -> {client.healthy()}")
            print(f"  raw GET panel/api/health -> {raw_http(panel, '/api/health')}")
            print(f"  urllib GET panel/api/health -> {urllib_probe(panel)}")
            dump_stacks(proc, label="daemon")
            text = log_path.read_text(encoding="utf-8", errors="replace")
            print(f"  its log ({len(text)} chars):\n{text[-4000:] or '(empty)'}")
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
            # platforms -- the second CI run died here with FileNotFoundError
            # 'taskkill' on macOS, which is a bug in the test, not the product.
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Cleanup must never be the thing that crashes the suite: the third
                # CI run raised TimeoutExpired here, on macOS, *after* the real
                # finding (the daemon never answered) had already been reported --
                # and a traceback hides everything that came before it.
                print(f"  [WARN] the daemon (pid {proc.pid}) ignored SIGTERM for 15s; "
                      "killing it", flush=True)
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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
