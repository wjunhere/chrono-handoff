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
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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


def main() -> int:
    triple = target_triple()
    exe = ".exe" if sys.platform == "win32" else ""
    name = f"chrono-core-{triple}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # `--onefile` because both consumers want one file: Tauri's externalBin is a
    # single binary, and the shell resolves a single path beside itself.
    #
    # `--console` (the default; deliberately NOT --noconsole): the cue channel
    # wants a console for keystroke capture -- `chrono doctor` reports
    # "console: NO" without one, and the frozen app must not lose a capability
    # the source checkout has.
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
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc != 0:
        return rc

    built = OUT_DIR / f"{name}{exe}"
    print(f"\nbuilt: {built}  ({built.stat().st_size / 1024 / 1024:.1f} MiB)")

    # A dev run has no bundling step, so point it at the same binary Tauri will
    # place beside the app in a real bundle.
    dev_dest = ROOT / "desktop" / "src-tauri" / "target" / "debug" / f"chrono-core{exe}"
    if dev_dest.parent.is_dir():
        shutil.copy2(built, dev_dest)
        print(f"copied for cargo-build dev runs: {dev_dest}")
    else:
        print(f"for a dev run, copy it next to the shell binary as chrono-core{exe}")

    # Prove the bundle is functional rather than assuming it: a frozen app that
    # silently lost PortAudio or pyserial looks exactly like a working one until
    # a cue needs sound or a serial read.
    print("\n-- self-check --", flush=True)
    out = subprocess.run([str(built), "doctor"], capture_output=True, encoding="utf-8",
                         errors="replace")
    print(out.stdout or out.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
