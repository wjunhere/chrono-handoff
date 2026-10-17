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

A successful build and check writes ``<binary>.stamp``: the hash of every file
the frozen conductor is made of.  ``--stale-check`` compares it with the working
tree, and ``build.rs`` runs that in release builds -- because PyInstaller is a
separate step `cargo build` knows nothing about, so editing Python leaves the
artifact behind, and a stale conductor answers, passes its own selftest and
reports the same version.  Nothing else would ever notice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
# is the frozen conductor still the one these sources produce?
# --------------------------------------------------------------------------
#
# PyInstaller cannot be driven from `cargo build`, so the editable half of the
# project moves on while the artifact stays behind -- measured once: the sidecar
# was built at 13:58 while the Python kept changing until 22:15, and the installer
# that followed carried the morning's code.  Nothing else catches it: a stale
# conductor answers, passes its own selftest, and reports the same version and
# contract, so every other check in this file is satisfied by it.

def stamp_path(name: str) -> Path:
    """Beside the binary, not in the work directory: it describes that artifact.

    Named after the target triple rather than the executable, so the name is the
    same shape on every platform (``...msvc.stamp`` beside ``...msvc.exe``).
    """
    return OUT_DIR / f"{name}.stamp"


def sidecar_inputs() -> list[Path]:
    """Every file the frozen conductor is made of.

    The statics because `--add-data` ships them, and the entry/build scripts
    because they decide how the bundle is assembled -- editing any of them means
    the artifact on disk is no longer what the sources say it is.
    """
    files = [p for p in (ROOT / "src" / "chrono_handoff").rglob("*.py") if p.is_file()]
    files += [p for p in STATICS.rglob("*") if p.is_file()]
    files += [HERE / "entry.py", HERE / "build.py"]
    return sorted(set(files))


def source_fingerprint() -> dict[str, str]:
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sidecar_inputs()
    }


def write_stamp(built: Path, name: str) -> None:
    stamp_path(name).write_text(
        json.dumps({"binary": built.name, "files": source_fingerprint()},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def stale_check(name: str) -> int:
    """Compare the stamp with the working tree.  0 = fresh, 1 = do not ship it."""
    stamp = stamp_path(name)
    if not stamp.is_file():
        print(f"NO STAMP: {stamp.name} does not exist, so what this sidecar was built "
              "from is unknown.  Run desktop/sidecar/build.py to build it and record one.")
        return 1
    recorded: dict[str, str] = json.loads(stamp.read_text(encoding="utf-8"))["files"]
    now = source_fingerprint()
    added = sorted(set(now) - set(recorded))
    removed = sorted(set(recorded) - set(now))
    changed = sorted(k for k in set(now) & set(recorded) if now[k] != recorded[k])
    if not (added or removed or changed):
        print(f"FRESH: the frozen conductor was built from exactly these {len(now)} files.")
        return 0
    print(f"STALE: the sidecar was built from different sources "
          f"({len(changed)} changed, {len(added)} added, {len(removed)} removed).")
    for label, names in (("changed", changed), ("added", added), ("removed", removed)):
        for n in names[:6]:
            print(f"  {label}: {n}")
        if len(names) > 6:
            print(f"  {label}: ... and {len(names) - 6} more")
    print("Rebuild it:  python desktop/sidecar/build.py")
    return 1


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
    deadline = time.monotonic() + 15
    started = time.monotonic()
    while True:
        # Signalled on **every** pass, not once.  The child that holds the sockets can
        # appear after the first signal -- that is the whole reason this loop exists --
        # and on POSIX nothing else in here can find it, because `_listening_pids`
        # reads Windows' `netstat`.  Measured on ubuntu: the onefile bootloader's
        # child outlived its parent, the single group kill was skipped (`proc.poll()`
        # was no longer None), and the loop could only watch the port stay open for
        # 15s and report a leak it had no way to clean up.
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            # `proc.pid` *is* the group id: the bundle is started with
            # `start_new_session=True`.  Used directly instead of
            # `os.getpgid(proc.pid)`, which raises once the parent has been reaped --
            # exactly the case that leaked.  Graceful first, then forceful.
            graceful = (time.monotonic() - started) < 2.0
            try:
                os.killpg(proc.pid, signal.SIGTERM if graceful else signal.SIGKILL)
            except OSError:
                pass
        # Whoever listens on the port is the conductor, however late it showed up.
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
    """The mechanisms, run inside the frozen process rather than the checkout.

    `--tolerant-timing` because two of the checks compare the scheduler's landing
    error against 5 ms, and a shared CI runner cannot promise that: measured on a
    hosted macOS runner, 19.2 ms max and 7.9 ms median under contention, with
    nothing wrong in the code.  The threshold lines are still printed with their
    numbers, so a loaded machine is distinguishable from a real regression -- this
    check's job is "the frozen bundle works", not "this machine is quiet".  The
    quiet-machine measurement lives on the author's box and in the e2e medians.
    """
    out = subprocess.run([str(built), "selftest", "--tolerant-timing"], capture_output=True,
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
        # The contract's surface, not just its files: a bundle built before this
        # endpoint existed 404s here, which is the "edited Python, forgot to
        # rebuild" case that the version and contract numbers cannot see (they
        # do not change for an addition).
        status, _ = _get(f"{base}/api/update-ready")
        _line("serves", "/api/update-ready", str(status))
        if status != 200:
            findings.append(
                f"the bundle does not answer /api/update-ready (HTTP {status}): it predates "
                "the update window, so this is a stale snapshot even though its version matches"
            )
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


def _check_parity(source: dict, frozen: dict, findings: list[str]) -> None:
    """The bundle must not be able to do less than the checkout it came from."""
    srows = {r["name"]: r["value"] for r in source["rows"]}
    frows = {r["name"]: r["value"] for r in frozen["rows"]}
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

    # The bundle must also *be* the same build as the checkout, not merely as
    # capable as it: a conductor that reports a different contract is one a window
    # would refuse to drive, and one that reports a different version is a stale
    # frozen snapshot -- the failure mode `--no-build` exists to catch, and the
    # one that makes the journal's own `version` field a lie.
    for field, want in (("version", source.get("version")), ("contract", source.get("contract"))):
        got = frozen.get(field)
        _line("", field, f"{want} -> {got}{'   <-- differs' if got != want else ''}")
        if got != want:
            findings.append(
                f"the bundle reports {field} {got!r}, the checkout it was built from reports "
                f"{want!r}: it is a stale snapshot, so rebuild it (`build.py` -- the frozen app "
                "is not updated by editing Python)"
            )


def _check_versions(findings: list[str]) -> None:
    """One version and one contract, declared in every place that labels a half.

    Not busywork: the shell stamps its own `CARGO_PKG_VERSION` into `shell.json`
    and the conductor stamps `__version__` into every cue journal, so a pair
    labelled with two different versions makes both records lie about which code
    produced them -- and the page refuses to drive a conductor whose contract
    differs, which turns a mislabeled build into a window that will not arm.
    """
    import chrono_handoff

    version, contract = chrono_handoff.__version__, chrono_handoff.CONTRACT_VERSION
    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    declared: list[tuple[str, str, str]] = [
        ("tauri.conf.json", str(conf.get("version")), version),
        ("Cargo.toml", _cargo_package_version((ROOT / "desktop/src-tauri/Cargo.toml")
                                               .read_text(encoding="utf-8")), version),
        ("panel.html", _declared(ROOT / "src/chrono_handoff/statics/panel.html",
                                 r"const PAGE_CONTRACT = (\d+);"), str(contract)),
        ("src-tauri/src/lib.rs", _declared(ROOT / "desktop/src-tauri/src/lib.rs",
                                            r"const CONTRACT: u64 = (\d+);"), str(contract)),
    ]
    print(f"  {'label':<12}{'file':<22}declares (python says {version} / contract {contract})")
    for name, got, want in declared:
        ok = got == want
        _line("", name, f"{got}{'' if ok else f'  <-- expected {want}'}")
        if not ok:
            findings.append(
                f"{name} declares {got}, the Python package declares {want}: the two halves "
                "of one install would be labelled differently"
            )


def _cargo_package_version(text: str) -> str:
    """`version` from Cargo.toml's `[package]` section.

    Walked line by line rather than matched with a regex, because every
    dependency's `version = "..."` has exactly the same shape.
    """
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == "[package]" and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("self-check: no version in Cargo.toml's [package] section")


def _declared(path: Path, pattern: str) -> str:
    """A constant read out of a source file, or a loud failure.

    A regex that quietly stops matching would make this check pass by finding
    nothing, which is the class of bug the check exists for -- so a miss is an
    error naming what was looked for.
    """
    found = re.search(pattern, path.read_text(encoding="utf-8"))
    if found is None:
        raise SystemExit(f"self-check: {path.name} does not declare {pattern!r} any more")
    return found.group(1)


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
        _check_parity(source, frozen, findings)
        _check_subsystem(built, findings)
        _check_versions(findings)
        _check_selftest(built, env, findings)
        _check_serves(built, env, findings)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    if findings:
        print(f"\nself-check FAILED -- {len(findings)} problem(s):", flush=True)
        for f in findings:
            print(f"  - {f}")
        return 1
    # Only now: the stamp says "this artifact was built from these sources *and*
    # passed the checks", so a failing build must not leave a fresh one behind.
    write_stamp(built, built.name.removesuffix(".exe"))
    print("\nself-check passed", flush=True)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    triple = target_triple()
    exe = ".exe" if sys.platform == "win32" else ""
    name = f"chrono-core-{triple}"
    built = OUT_DIR / f"{name}{exe}"

    if "--stale-check" in argv:
        return stale_check(name)

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
