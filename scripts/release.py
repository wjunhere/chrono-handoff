"""Stage a release and refuse to publish it until the update chain checks out.

Releasing is the one step that cannot be taken back.  A `latest.json` that names the wrong
file, or a signature that does not match the artifact, reaches every installed copy, and
`git revert` does not reach them.  So this script does the mechanical parts -- stage the
installers, write the manifest -- and then *verifies* the result the way the app will: the
same `minisign-verify` crate the updater links, against the pubkey compiled into the binary.

It does not publish unless asked.  By default it prints the `gh release create` commands,
because the last step before the world sees something is a good place for a human to look.

    python scripts/release.py --notes notes.md            # stage + verify, print the commands
    python scripts/release.py --notes notes.md --publish  # ... and run them, then check the endpoint
    python scripts/release.py --selftest                  # calibrate the gates only

The signed build needs the private key, so it is never run on its own: pass `--build` and
this script runs it with whatever `TAURI_SIGNING_PRIVATE_KEY` (and `..._PASSWORD`) are in the
environment.  See `docs/update.md` for where that key lives.

Why the gates are these gates, in the order they run: a release must correspond to a commit
(clean worktree), that commit must be the one CI passed (otherwise the artifacts come from a
revision nobody checked), the frozen conductor must have been rebuilt for this version (the
version gate inside `build.py` is what says so), and only then does the artifact get signed,
staged, and cryptographically verified.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chrono_handoff import __version__  # noqa: E402

CONF = ROOT / "desktop/src-tauri/tauri.conf.json"
BUNDLE = ROOT / "desktop/src-tauri/target/release/bundle"
VERIFIER_CRATE = ROOT / "scripts/updater_sig_check"
SIDECAR_BUILD = ROOT / "desktop/sidecar/build.py"
MANIFEST_NAME = "latest.json"

# Which platform keys the manifest carries, and why both.  `tauri-plugin-updater` looks up
# `{os}-{arch}-{installer}` first and `{os}-{arch}` second (updater.rs, `get_urls`), so the
# first is the one that resolves in practice and the second is what an install whose bundle
# type cannot be determined falls back to.  Both point at the same NSIS installer: that is
# the installer this project ships, and an MSI install updating through it is documented
# rather than wired up.
PLATFORM_KEYS = ("windows-x86_64-nsis", "windows-x86_64")


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(command, text=True, **kwargs)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def repo_slug() -> str:
    """`owner/name`, from the remote rather than hardcoded, so a rename cannot rot this."""
    url = git("remote", "get-url", "origin").strip()
    slug = url.removesuffix(".git")
    for prefix in ("https://github.com/", "git@github.com:"):
        if slug.startswith(prefix):
            return slug[len(prefix):]
    raise SystemExit(f"refusing: origin is not a GitHub remote ({url})")


def asset_name(version: str) -> str:
    return f"chrono-handoff_{version}_x64-setup.exe"


def asset_url(repo: str, version: str) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}/{asset_name(version)}"


# ---------------------------------------------------------------- gates on the checkout


def check_worktree() -> None:
    dirty = git("status", "--porcelain").strip()
    if dirty:
        raise SystemExit(
            "refusing: the worktree has uncommitted changes, so the installers would not "
            f"correspond to any commit:\n{dirty}"
        )


def check_ci(wait_s: float) -> None:
    """The revision being released must be the revision CI passed.

    Always waits a little even when `--wait-ci` is 0: a run that was just pushed takes a
    moment to appear, and "no run found" would otherwise refuse the release somebody is
    standing there waiting for.
    """
    head = git("rev-parse", "HEAD").strip()
    deadline = time.monotonic() + max(wait_s, 60.0)
    while True:
        runs = json.loads(gh("run", "list", "--commit", head, "--limit", "20",
                             "--json", "status,conclusion,url,workflowName"))
        failed = [r for r in runs if r["status"] == "completed" and r["conclusion"] != "success"]
        pending = [r for r in runs if r["status"] != "completed"]
        if failed:
            raise SystemExit(f"refusing: CI did not pass for {head[:7]}: {failed[0]['url']}")
        if runs and not pending:
            print(f"  CI: {len(runs)} run(s) passed for {head[:7]}")
            return
        if time.monotonic() >= deadline:
            state = pending[0]["url"] if pending else "no run found"
            raise SystemExit(
                f"refusing: CI for {head[:7]} is not finished ({state}).  "
                "Re-run with --wait-ci 900 to wait for it."
            )
        print(f"  CI: {len(pending)} run(s) still going for {head[:7]}, waiting...", flush=True)
        time.sleep(20)


def check_sidecar() -> None:
    """The frozen conductor must match this checkout, at this version."""
    for flag in ("--stale-check", "--no-build"):
        done = run([sys.executable, str(SIDECAR_BUILD), flag], cwd=ROOT, capture_output=True)
        if done.returncode:
            raise SystemExit(
                f"refusing: `build.py {flag}` failed, so the bundled conductor is not the one "
                f"this release would claim to ship.\n{done.stdout}{done.stderr}"
            )


def build(version: str) -> None:
    if not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        raise SystemExit(
            "refusing to build: TAURI_SIGNING_PRIVATE_KEY is not set.  It takes the private "
            "key's path or its contents, and TAURI_SIGNING_PRIVATE_KEY_PASSWORD the password "
            "-- see docs/update.md."
        )
    done = run(["npx", "tauri", "build", "--config", '{"bundle":{"createUpdaterArtifacts":true}}'],
               cwd=ROOT / "desktop/src-tauri")
    if done.returncode:
        raise SystemExit(f"refusing: the signed build failed (exit {done.returncode})")


# ---------------------------------------------------------------- staging and the manifest


def stage(version: str, stage_dir: Path) -> Path:
    setup = BUNDLE / "nsis" / asset_name(version)
    msi = BUNDLE / "msi" / f"chrono-handoff_{version}_x64_en-US.msi"
    wanted = [setup, setup.with_suffix(".exe.sig"), msi, msi.with_suffix(".msi.sig")]
    missing = [p for p in wanted if not p.is_file()]
    if missing:
        raise SystemExit(
            "refusing: the signed build has not produced these -- run with --build, or build "
            "with `createUpdaterArtifacts` as docs/update.md shows:\n"
            + "\n".join(f"  {p}" for p in missing)
        )
    stage_dir.mkdir(parents=True, exist_ok=True)
    for src in wanted:
        shutil.copy2(src, stage_dir / src.name)
    return stage_dir / setup.name


def write_manifest(version: str, stage_dir: Path, repo: str, notes: str) -> dict:
    signature = (stage_dir / (asset_name(version) + ".sig")).read_text(encoding="utf-8").strip()
    if not signature or "\n" in signature:
        raise SystemExit("refusing: the .sig file is not one line, so it is not a signature blob")
    manifest = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.datetime.now(datetime.timezone.utc)
                            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "platforms": {
            key: {"url": asset_url(repo, version), "signature": signature} for key in PLATFORM_KEYS
        },
    }
    (stage_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


# ---------------------------------------------------------------- verification


def decode_base64(value: str, where: str, findings: list[str]) -> str | None:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except Exception as exc:
        findings.append(
            f"{where} is not base64 ({type(exc).__name__}: {exc}).  Both the pubkey in "
            "tauri.conf.json and the signature in the manifest are base64 of a minisign "
            "*text* form; the plugin decodes them before verifying, and storing the decoded "
            "text instead makes every build or check fail on the way in."
        )
        return None


def signed_version(signature_text: str) -> str | None:
    for line in signature_text.splitlines():
        if line.startswith("trusted comment:"):
            for field in line.split("\t"):
                if field.startswith("version:"):
                    return field[len("version:"):]
    return None


def structural_findings(manifest: dict, pubkey_b64: str, signature_b64: str,
                        version: str, repo: str, asset: str) -> list[str]:
    """Everything checkable without the artifact's bytes, in one place so it can be driven
    by `--selftest` with fabricated inputs."""
    findings: list[str] = []
    pubkey_text = decode_base64(pubkey_b64, "tauri.conf.json plugins.updater.pubkey", findings)
    signature_text = decode_base64(signature_b64, "the manifest's signature", findings)
    if pubkey_text is None or signature_text is None:
        return findings

    # The pubkey has to be a well-formed minisign key, and the id it carries has to be the
    # id of the key that signed -- otherwise the build would have accepted a key it cannot
    # verify with.
    try:
        lines = pubkey_text.splitlines()
        body = base64.b64decode(lines[1], validate=True)
    except Exception as exc:
        findings.append(f"the pubkey does not decode to a minisign key ({exc})")
        return findings
    stated = lines[0].rsplit(":", 1)[-1].strip()
    key_id = f"{int.from_bytes(body[2:10], 'little'):016X}"
    if len(body) != 42 or body[:2] != b"Ed" or key_id != stated:
        findings.append(
            f"the pubkey is {len(body)} bytes with id {key_id} while its own comment says "
            f"{stated}: not a well-formed minisign public key"
        )
    try:
        signature_body = base64.b64decode(signature_text.splitlines()[1], validate=True)
    except Exception as exc:
        findings.append(f"the signature does not decode to a minisign signature ({exc})")
        return findings
    signature_id = f"{int.from_bytes(signature_body[2:10], 'little'):016X}"
    if signature_id != key_id:
        findings.append(
            f"the artifact was signed with key {signature_id} but the app trusts {key_id}: "
            "the installed copy would refuse this update"
        )

    announced = manifest.get("version")
    signed = signed_version(signature_text)
    if signed is None:
        findings.append(
            "the signature carries no version in its trusted comment, and the app has "
            "requireSignedVersion on, so it would reject this manifest outright"
        )
    elif signed != announced:
        findings.append(
            f"the manifest announces {announced} but the signature is over {signed}: "
            "requireSignedVersion compares them and refuses on a mismatch"
        )

    for key in PLATFORM_KEYS:
        entry = manifest.get("platforms", {}).get(key)
        if not entry:
            findings.append(f"the manifest has no `{key}` entry, so that lookup falls through")
            continue
        url = entry.get("url", "")
        if not url.endswith(f"/v{version}/{asset}"):
            findings.append(f"`{key}` points at {url!r}, which is not /v{version}/{asset}")
        elif not url.startswith(f"https://github.com/{repo}/releases/download/"):
            findings.append(f"`{key}` points outside the release downloads of {repo}: {url!r}")
        if not entry.get("signature"):
            findings.append(f"`{key}` carries no signature, so the download would be rejected")
    return findings


def verify(version: str, repo: str, stage_dir: Path, artifact: Path, manifest: dict) -> None:
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    updater = conf["plugins"]["updater"]
    if not updater.get("requireSignedVersion"):
        raise SystemExit(
            "refusing: plugins.updater.requireSignedVersion is off, so the manifest's version "
            "is not bound to the signature and an attacker can announce any version"
        )
    findings = structural_findings(
        manifest, updater["pubkey"], manifest["platforms"][PLATFORM_KEYS[0]]["signature"],
        version, repo, artifact.name,
    )
    if findings:
        report(findings)

    # The artifact's bytes, through the crate the updater links.
    with tempfile.TemporaryDirectory(prefix="chrono-release-verify-") as tmp:
        pubkey_file = Path(tmp) / "pubkey.txt"
        signature_file = Path(tmp) / "signature.txt"
        pubkey_file.write_text(
            base64.b64decode(updater["pubkey"], validate=True).decode("utf-8"), encoding="utf-8"
        )
        signature_file.write_text(
            base64.b64decode(manifest["platforms"][PLATFORM_KEYS[0]]["signature"],
                             validate=True).decode("utf-8"),
            encoding="utf-8",
        )
        done = run(["cargo", "run", "--quiet", "--manifest-path", str(VERIFIER_CRATE / "Cargo.toml"),
                    "--", str(artifact), str(pubkey_file), str(signature_file)],
                   cwd=ROOT, capture_output=True)
    print(done.stdout.rstrip())
    if done.returncode:
        report([f"the installer does not verify against the pubkey the app trusts: "
                f"{done.stderr.strip() or 'see above'}"])


def report(findings: list[str]) -> None:
    print(f"\nRELEASE CHECK FAILED -- {len(findings)} problem(s):", flush=True)
    for finding in findings:
        print(f"  - {finding}")
    raise SystemExit(1)


# ---------------------------------------------------------------- publishing


def publish_argv(repo: str, version: str, stage_dir: Path, notes_file: Path) -> list[list[str]]:
    tag = f"v{version}"
    assets = [str(stage_dir / asset_name(version)),
              str(stage_dir / (asset_name(version) + ".sig")),
              str(stage_dir / MANIFEST_NAME)]
    return [["gh", "release", "create", tag, *assets, "--title", tag,
             "--notes-file", str(notes_file)]]


def render(argv: list[str]) -> str:
    """A copy-pasteable line.  Kept separate from the argv that actually runs, so a path
    with a space in it cannot quietly become two arguments."""
    return " ".join(f"'{a}'" if " " in a else a for a in argv)


def check_published(repo: str, stage_dir: Path) -> None:
    """Fetch what an installed copy will fetch, with no credentials, and compare bytes."""
    url = f"https://github.com/{repo}/releases/latest/download/{MANIFEST_NAME}"
    done = subprocess.run(["curl", "-sS", "-L", url], capture_output=True, text=True)
    if done.returncode or done.stdout != (stage_dir / MANIFEST_NAME).read_text(encoding="utf-8"):
        raise SystemExit(
            f"published, but {url} does not match the staged manifest.  An installed copy "
            "would fetch something else -- check that this release is the latest one and that "
            f"`{MANIFEST_NAME}` was attached.\n{done.stderr}"
        )
    print(f"  the endpoint serves the staged manifest byte for byte: {url}")


# ---------------------------------------------------------------- selftest


def selftest() -> int:
    """Drive the structural gates with fabricated inputs, good and tampered.

    A gate that cannot be made to fail is not a gate, and this one cannot be exercised for
    real without the private key -- so the calibration uses a synthetic keypair-shaped pair.
    """
    key_id = 0x1122334455667788
    blob = b"Ed" + key_id.to_bytes(8, "little") + bytes(32)
    pubkey_text = f"untrusted comment: minisign public key: {key_id:016X}\n{base64.b64encode(blob).decode()}\n"
    pubkey_b64 = base64.b64encode(pubkey_text.encode()).decode()

    def signature(version: str | None, id_value: int = key_id) -> str:
        body = b"ED" + id_value.to_bytes(8, "little") + bytes(64)
        fields = ["timestamp:1792297430", "file:chrono-handoff_1.2.3_x64-setup.exe"]
        if version is not None:
            fields.append(f"version:{version}")
        trusted = "trusted comment: " + "\t".join(fields)
        text = ("untrusted comment: signature from tauri secret key\n"
                f"{base64.b64encode(body).decode()}\n"
                f"{trusted}\n"
                f"{base64.b64encode(bytes(64)).decode()}\n")
        return base64.b64encode(text.encode()).decode()

    version, repo, asset = "1.2.3", "owner/name", "chrono-handoff_1.2.3_x64-setup.exe"
    url = asset_url(repo, version)

    def manifest(**overrides) -> dict:
        base = {"version": version,
                "platforms": {k: {"url": url, "signature": signature(version)} for k in PLATFORM_KEYS}}
        base.update(overrides)
        return base

    cases: list[tuple[str, dict, str, str, bool]] = [
        ("the manifest a release produces", manifest(), pubkey_b64, signature(version), True),
        ("announced version differs from the signed one",
         manifest(version="1.2.4"), pubkey_b64, signature(version), False),
        ("the signature carries no version",
         manifest(), pubkey_b64, signature(None), False),
        ("the url names another file",
         manifest(platforms={k: {"url": url.replace(asset, "other.exe"), "signature": signature(version)}
                             for k in PLATFORM_KEYS}), pubkey_b64, signature(version), False),
        ("the url points at another tag",
         manifest(platforms={k: {"url": url.replace("/v1.2.3/", "/v1.2.4/"), "signature": signature(version)}
                             for k in PLATFORM_KEYS}), pubkey_b64, signature(version), False),
        ("the url points at another repository",
         manifest(platforms={k: {"url": url.replace("owner/name", "evil/name"), "signature": signature(version)}
                             for k in PLATFORM_KEYS}), pubkey_b64, signature(version), False),
        ("a platform entry is missing",
         manifest(platforms={PLATFORM_KEYS[1]: {"url": url, "signature": signature(version)}}),
         pubkey_b64, signature(version), False),
        ("the pubkey is the decoded text, not the base64",
         manifest(), pubkey_text, signature(version), False),
        ("the signature is the decoded text, not the base64",
         manifest(), pubkey_b64, base64.b64decode(signature(version)).decode(), False),
        ("the artifact was signed by another key",
         manifest(), pubkey_b64, signature(version, id_value=0xDEADBEEFDEADBEEF), False),
        ("the pubkey body is the wrong length",
         manifest(), base64.b64encode(
             f"untrusted comment: minisign public key: {key_id:016X}\n"
             f"{base64.b64encode(b'Ed' + key_id.to_bytes(8, 'little') + bytes(16)).decode()}\n"
             .encode()).decode(), signature(version), False),
    ]

    failures = 0
    for name, man, pub, sig, should_pass in cases:
        got = structural_findings(man, pub, sig, version, repo, asset)
        ok = (not got) if should_pass else bool(got)
        failures += 0 if ok else 1
        detail = ""
        if not ok:
            detail = (f"   <-- expected a finding, got none" if not should_pass
                      else f"   <-- unexpected finding: {got[0]}")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{detail}")
    print(f"\n{len(cases) - failures}/{len(cases)} gate checks behave as intended")
    return 1 if failures else 0


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--notes", type=Path,
                        help="markdown release notes; used for the GitHub release and the "
                             "manifest's `notes` field")
    parser.add_argument("--stage", type=Path,
                        help="where to copy the artifacts (default: a fresh temp directory)")
    parser.add_argument("--build", action="store_true",
                        help="run the signed build first (needs the signing key in the env)")
    parser.add_argument("--publish", action="store_true",
                        help="run the gh/curl commands instead of printing them")
    parser.add_argument("--wait-ci", type=float, default=0.0, metavar="SECONDS",
                        help="wait this long for CI on HEAD to finish before refusing")
    parser.add_argument("--selftest", action="store_true",
                        help="calibrate the structural gates with fabricated inputs, then stop")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.notes:
        parser.error("--notes is required for a release (or use --selftest)")

    version, repo = __version__, repo_slug()
    print(f"release {version} of {repo}")

    print("\n-- gates on the checkout --")
    check_worktree()
    check_ci(args.wait_ci)
    check_sidecar()

    print("\n-- the signed build --")
    if args.build:
        build(version)
    else:
        print("  (not run; pass --build to run it here)")

    stage_dir = args.stage or Path(tempfile.mkdtemp(prefix=f"chrono-release-{version}-"))
    artifact = stage(version, stage_dir)
    notes = args.notes.read_text(encoding="utf-8")
    manifest = write_manifest(version, stage_dir, repo, notes)
    print(f"  staged in {stage_dir}")
    for item in sorted(stage_dir.iterdir()):
        print(f"    {item.stat().st_size:>10}  {item.name}")

    print("\n-- verification --")
    verify(version, repo, stage_dir, artifact, manifest)

    print("\n-- publishing --")
    commands = publish_argv(repo, version, stage_dir, args.notes.resolve())
    for argv in commands:
        print(f"  {render(argv)}")
    if args.publish:
        for argv in commands:
            done = run(argv, cwd=ROOT)
            if done.returncode:
                raise SystemExit(f"publishing failed: {render(argv)}")
        check_published(repo, stage_dir)
    else:
        print("\n  (printed, not run: pass --publish to run them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
