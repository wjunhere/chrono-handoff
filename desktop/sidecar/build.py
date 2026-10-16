"""Build the conductor that ships beside the desktop shell (`chrono-core`).

    .venv/Scripts/python.exe desktop/sidecar/build.py

**PyInstaller cannot cross-compile.**  Run this once per target platform; the
output is platform-specific and must be produced on that platform (or in CI on
that platform's runner).

Output follows Tauri's `externalBin` convention:

    desktop/src-tauri/binaries/chrono-core-<target-triple>[.exe]

Tauri copies it into the bundle next to the app executable with the triple
stripped, which is exactly the name the shell looks for
(`conductor_command()` in src-tauri/src/lib.rs).  A `cargo build` (dev) run does
*not* do that copying, so for a dev run copy it there yourself -- the script
prints the command to do so.

The build is followed by a self-check that can fail (see ``verify``): the frozen
app must not be able to do less than the source checkout it was built from, and
it must actually serve the panel over HTTP.  ``--no-build`` re-runs only that
check against the binary already in place.
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATICS = ROOT / "src" / "chrono_handoff" / "statics"
OUT_DIR = ROOT / "desktop" / "src-tauri" / "binaries"
BUILD_DIR = ROOT / "desktop" / "sidecar" / "_build"


def target_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("could not read the host triple from `rustc -vV`")


def build(name: str, exe: str) -> int:
    # `--onefile` because both consumers want one file: Tauri's externalBin is a
    # single binary, and the shell resolves a single path beside itself.
    #
    # `--console` (the default; deliberately NOT --noconsole): the cue channel
    # wants a console for keystroke capture -- `chrono doctor` reports
    # "console: NO" without one, and the frozen app must not lose a capability
    # the source checkout has.  `verify` reads the PE header of the artifact to
    # confirm this is still true of what actually got built.
    #
    # The optional dependencies are imported lazily inside functions, so they are
    # named explicitly rather than left to modulegraph guessing: a silently
    # missing PortAudio would downgrade the audio channel without saying so.
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--name", name,
        "--distpath", str(OUT_DIR),
        "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR),
        "--add-data", f"{STATICS}{';' if sys.platform == 'win32' else ':'}chrono_handoff/statics",
        "--hidden-import", "sounddevice",
        "--collect-submodules", "serial",
        "--hidden-import", "serial.tools.list_ports",
        str(HERE / "entry.py"),
    ]
    print(" ".join(args), flush=True)
    return subprocess.call(args, cwd=str(ROOT))


def copy_for_dev_run(built: Path, exe: str) -> None:
    """A `cargo build` run has no bundling step, so place it where Tauri will."""
    dev_dest = ROOT / "desktop" / "src-tauri" / "target" / "debug" / f"chrono-core{exe}"
    if dev_dest.parent.is_dir():
        shutil.copy2(built, dev_dest)
        print(f"copied for cargo-build dev runs: {dev_dest}")
    else:
        print(f"for a dev run, copy it next to the shell binary as chrono-core{exe}")


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------
#
# This used to print `doctor`'s report and return 0 whatever it said, so it could
# not fail.  Three things it was in a position to catch went unnoticed that way:
# a bundle that lost pyserial or its PortAudio backend, and -- the one nothing
# else would ever notice -- a bundle that lost the panel's static files, which is
# a window with nothing in it.

#: The PE subsystem value that means "this program can own a console".  The
#: other one in the file it is read from, 2, is IMAGE_SUBSYSTEM_WINDOWS_GUI.
SUBSYSTEM_WINDOWS_CUI = 3

#: `doctor` rows describing what this app can do.  They are compared against the
#: source checkout measured on the same machine, so no per-platform expectation
#: is hardcoded here: where both sides say "console NO" (POSIX has no msvcrt),
#: that is correctly a pass, and on a platform that gains a row the comparison
#: starts covering it without this list changing.
PARITY_ROWS = ("msvcrt.kbhit", "audio sink", "pyserial", "mcp sdk")

#: Served straight off disk by the panel, out of the `--add-data` directory
#: alone.  The served bytes are compared with the repository's own copy: both a
#: missing file and a *stale* one -- an old panel.html surviving in the build
#: cache -- are failures, and the stale copy is the one that would otherwise go
#: unnoticed for as long as nobody diffed the window against the source.
SERVED = (
    ("/", STATICS / "panel.html"),
    ("/icon.svg", STATICS / "icon.svg"),
    ("/fonts/inter-latin.woff2", STATICS / "fonts" / "inter-latin.woff2"),
)


def pe_subsystem(path: Path) -> int:
    """The PE subsystem of a Windows executable.

    Read straight out of the header, because it is the only *deterministic* way
    to check the thing `--console` is for.  `doctor` reports whether the process
    has a console right now, and every process started from a pty (which is what
    the author's terminal is) reports "NO" -- so from there a `--noconsole`
    build and a `--console` build are indistinguishable.  A GUI-subsystem binary
    never attaches to a console, and a console is what keystroke capture needs.

    A wrong offset here fails the build loudly (it reads 'GUI', or something
    that is neither), which is the safe direction for a parser to be wrong in.
    """
    with path.open("rb") as fh:
        blob = fh.read(4096)
    if blob[:2] != b"MZ":
        raise ValueError(f"{path.name} is not a PE file")
    opt = int.from_bytes(blob[0x3C:0x40], "little")
    if blob[opt:opt + 4] != b"PE\0\0":
        raise ValueError(f"{path.name}: no PE signature at 0x{opt:x}")
    # Subsystem sits at the same offset in PE32 and PE32+: PE32's BaseOfData and
    # ImageBase occupy the same eight bytes as PE32+'s lone 8-byte ImageBase.
    at = opt + 4 + 20 + 68
    return int.from_bytes(blob[at:at + 2], "little")


def _line(group: str, label: str, detail: str) -> None:
    # The explicit space matters: a label longer than the column would otherwise
    # run straight into its detail (`inter-latin.woff248256 bytes`).
    print(f"  {group:<12}{label:<24} {detail}", flush=True)


def _doctor(cmd: list[str], env: dict[str, str]) -> dict:
    """`doctor --json` as data.

    A report this script cannot read is a failed check, not a pass, so it stops
    immediately and shows the raw output instead of guessing at it.
    """
    out = subprocess.run([*cmd, "doctor", "--json"], capture_output=True,
                         encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise SystemExit(
            f"self-check: {cmd[0]} did not produce usable JSON (exit {out.returncode})\n"
            f"--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
        ) from None


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _wait_up(url: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            if _get(url, timeout=2.0)[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _listening_pids(port: int) -> set[int]:
    """PIDs listening on `port` (Windows).  Parsed as *bytes*: `netstat` writes
    its output in whatever the console codepage happens to be, so decoding it as
    text is a way to lose the whole result to a UnicodeDecodeError."""
    out = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
    found: set[int] = set()
    for line in out.splitlines():
        if b"LISTENING" in line and f":{port} ".encode() in line:
            try:
                found.add(int(line.split()[-1]))
            except ValueError:
                pass
    return found


def _kill_tree(proc: subprocess.Popen, ipc_port: int) -> None:
    """Shut down the bundle and everything it started.

    Killing our own pid is not enough.  Measured on Windows: `--onefile`'s
    bootloader re-executes the same binary as a child, and the *child* is what
    holds both ports; `taskkill /T` races that spawn, so a child that appears
    just after the kill is never enumerated and survives.  Whoever holds the IPC
    socket is the process that actually has to go, so that is the primary
    target; the tree kill is just belt and braces.
    """
    killed_root = False
    deadline = time.monotonic() + 15
    while True:
        if not killed_root:
            killed_root = True
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, check=False)
            elif proc.poll() is None:
                # POSIX: a forked child stays in the process group, so one
                # signal to the group reaches all of it.
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        # A child that appeared after the kill above is caught on the next pass:
        # whoever listens on the port is the conductor, however late it showed up.
        if sys.platform == "win32":
            for pid in _listening_pids(ipc_port):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, check=False)
        if proc.poll() is not None and not _port_busy(ipc_port):
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=15)


def _check_selftest(built: Path, env: dict[str, str], findings: list[str]) -> None:
    """The mechanisms, run inside the frozen process rather than the checkout."""
    out = subprocess.run([str(built), "selftest"], capture_output=True,
                         encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
    verdict = next((ln.strip() for ln in reversed(out.stdout.splitlines()) if ln.strip()), "")
    _line("selftest", "10 mechanisms", verdict or f"no output (exit {out.returncode})")
    if out.returncode != 0 or not verdict:
        # Name the mechanism.  Most of these measure timing under a machine that
        # may be busy, so a failure is either a real capability loss or a loaded
        # box -- and those two want completely different responses.
        failed = [ln.strip() for ln in out.stdout.splitlines() if "[FAIL]" in ln]
        for ln in failed:
            _line("selftest", "failed", ln)
        findings.append(
            f"selftest inside the bundle failed (exit {out.returncode}): "
            f"{'; '.join(failed) or verdict or 'no output'}"
        )


def _check_serves(built: Path, env: dict[str, str], findings: list[str]) -> None:
    """Start the bundle as a daemon and fetch what it says it serves."""
    ipc, panel = _free_port(), _free_port()
    log = Path(env["CHRONO_HOME"]) / "daemon.err"
    # `start_new_session` (POSIX only) puts the bundle in its own process group
    # so `_kill_tree` can signal the whole thing without signalling ourselves;
    # Windows needs nothing there, because `taskkill /T` walks the child list.
    # Typed Any because the kwargs are splatted into Popen, whose parameters all
    # differ in type -- a narrower annotation would reject the call outright.
    detached: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    with log.open("wb") as err:
        proc = subprocess.Popen(
            [str(built), "daemon", "--port", str(ipc), "--panel-port", str(panel)],
            stdout=subprocess.DEVNULL, stderr=err, env=env, cwd=str(ROOT), **detached,
        )
    try:
        base = f"http://127.0.0.1:{panel}"
        if not _wait_up(f"{base}/api/health", time.monotonic() + 40):
            _line("serves", "(daemon)", f"never answered on {base}/api/health")
            findings.append(f"the bundle never answered {base}/api/health")
            tail = log.read_bytes().decode("utf-8", "replace").strip().splitlines()[-8:]
            for line in tail:
                print(f"      daemon: {line}", flush=True)
            return
        _line("serves", "/api/health", "200")
        for path, source in SERVED:
            want = source.read_bytes()
            try:
                status, body = _get(f"{base}{path}")
            except (urllib.error.URLError, OSError) as exc:
                # A daemon that died mid-check is a finding, not a traceback.
                _line("serves", path, f"no answer ({type(exc).__name__})")
                findings.append(f"{path}: nothing answered it ({exc})")
                continue
            ok = status == 200 and body == want
            _line("serves", path, (f"{len(body)} bytes, identical to the source file"
                                   if ok else f"HTTP {status}, {len(body)} bytes; source has {len(want)}"))
            if not ok:
                findings.append(
                    f"{path} is not coming out of the bundle as built: HTTP {status}, "
                    f"{len(body)} bytes, {source.name} on disk is {len(want)} bytes"
                )
    finally:
        _kill_tree(proc, ipc)
    # Leaving a conductor behind would make the *next* run fail for a reason that
    # has nothing to do with that run, so it counts as a finding here.
    if _port_busy(ipc):
        findings.append(f"could not shut the bundle down: 127.0.0.1:{ipc} is still listening")


def _check_parity(srows: dict[str, str], frows: dict[str, str], findings: list[str]) -> None:
    """The bundle must not be able to do less than the checkout it came from."""
    print(f"  {'capability':<12}{'source checkout':<22}bundle")
    for name in PARITY_ROWS:
        if name not in srows:
            continue  # not a capability on this platform
        want = srows[name]
        got = frows.get(name, "(absent)")
        _line("", name, f"{want:<22}{got}{'   <-- differs' if got != want else ''}")
        if got == "(absent)":
            findings.append(f"{name}: missing from the bundle (the checkout reports {want!r})")
        elif got != want:
            findings.append(f"{name}: the bundle reports {got!r}, the checkout reports {want!r}")

    if "scheduler landing" not in frows:
        findings.append("scheduler landing: the bundle never fired the clock")
    else:
        _line("", "scheduler landing", frows["scheduler landing"])


def _check_subsystem(built: Path, findings: list[str]) -> None:
    """`--console`: on Windows the artifact must still be a console program."""
    if sys.platform != "win32":
        return
    subsystem = pe_subsystem(built)
    ok = subsystem == SUBSYSTEM_WINDOWS_CUI
    _line("", "pe subsystem", f"{subsystem} ({'console' if ok else 'GUI'})")
    if not ok:
        findings.append(
            f"the bundle is a GUI-subsystem binary (PE subsystem {subsystem}): "
            "build it with --console or keystroke capture dies silently"
        )


def verify(built: Path) -> int:
    """Everything the *build* is responsible for, checked against reality.

    It exits non-zero on any problem and reports all of them at once: a build
    check that stops at the first finding makes for slow rounds.
    """
    findings: list[str] = []
    # A throwaway CHRONO_HOME: `verify` runs the real daemon, which writes
    # journals and calibration, and it must not touch the state of whoever ran
    # the build.
    home = tempfile.mkdtemp(prefix="chrono-sidecar-check-")
    env = {**os.environ, "CHRONO_HOME": home, "PYTHONIOENCODING": "utf-8"}
    print(f"\n-- self-check: {built.name} --", flush=True)
    try:
        source = _doctor([sys.executable, "-m", "chrono_handoff.cli"], env)
        frozen = _doctor([str(built)], env)
        _check_parity(
            {r["name"]: r["value"] for r in source["rows"]},
            {r["name"]: r["value"] for r in frozen["rows"]},
            findings,
        )
        _check_subsystem(built, findings)
        _check_selftest(built, env, findings)
        _check_serves(built, env, findings)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    if findings:
        print(f"\nself-check FAILED -- {len(findings)} problem(s):", flush=True)
        for f in findings:
            print(f"  - {f}")
        return 1
    print("\nself-check passed", flush=True)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    triple = target_triple()
    exe = ".exe" if sys.platform == "win32" else ""
    name = f"chrono-core-{triple}"
    built = OUT_DIR / f"{name}{exe}"

    if "--no-build" in argv:
        if not built.exists():
            raise SystemExit(f"--no-build: {built} does not exist")
        print(f"--no-build: checking {built}")
        return verify(built)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rc = build(name, exe)
    if rc != 0:
        return rc
    print(f"\nbuilt: {built}  ({built.stat().st_size / 1024 / 1024:.1f} MiB)")
    copy_for_dev_run(built, exe)
    return verify(built)


if __name__ == "__main__":
    raise SystemExit(main())
