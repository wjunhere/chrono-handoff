//! Build script for the shell.
//!
//! Beyond Tauri's own codegen this checks one thing that nothing else can see:
//! **whether the frozen conductor beside us was built from the sources that are
//! here now.**
//!
//! The shell and the conductor are one product made by two build systems, and
//! only the shell is built by cargo.  PyInstaller cannot cross-compile, so the
//! sidecar is a separate step (`desktop/sidecar/build.py`), which means editing
//! Python leaves the artifact behind.  Measured, once: the sidecar was built at
//! 13:58 while the Python kept changing until 22:15, and the installer that
//! followed carried the morning's code.  Nothing notices on its own, because a
//! stale conductor answers, passes its own selftest, and reports the same version
//! and contract as a fresh one.
//!
//! Release builds fail; debug builds do not even check.  A developer editing
//! Python should not be blocked from `cargo build`, but an artifact that is about
//! to be packaged into an installer must be coherent.

use std::path::{Path, PathBuf};
use std::process::Command;

fn main() {
    tauri_build::build();

    // `cargo build` only re-runs this script when one of its inputs changes, so
    // the inputs have to be declared -- otherwise a Python edit would not even
    // reach the check, and `tauri build` would quietly bundle the old conductor.
    // Declared per file rather than per directory on purpose: directory tracking
    // depends on how a given cargo version walks the tree.
    for file in files_under(&repo_root().join("src/chrono_handoff")) {
        println!("cargo:rerun-if-changed={}", file.display());
    }
    for file in ["./build.rs", "../../desktop/sidecar/build.py", "../../desktop/sidecar/entry.py"] {
        println!("cargo:rerun-if-changed={file}");
    }

    if std::env::var("PROFILE").as_deref() != Ok("release") {
        return;
    }
    check_sidecar_freshness();
}

fn repo_root() -> PathBuf {
    // cargo runs build scripts with the working directory set to the package
    // root (`desktop/src-tauri`).  Canonicalised because these paths go into
    // `cargo:rerun-if-changed`, where a wrong relative path is silently a path
    // that never changes -- which is exactly the bug that hid here first: the
    // walk was joined onto a base that already contained `../..`, found nothing,
    // and the empty list read as "no inputs to watch".
    std::fs::canonicalize("../..").expect("repo root is not reachable from the package root")
}

/// Every source file under `dir`, recursively.
///
/// `__pycache__` is skipped deliberately: bytecode churn is not a source change,
/// and watching it would recompile this crate every time anything ran Python.
///
/// Fails loudly when the directory cannot be read: this is the list of inputs
/// that decide whether an artifact is shippable, and an empty list would make the
/// check pass by watching nothing at all.
fn files_under(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let entries = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("cannot read {} ({}): the sidecar's inputs must be watchable", dir.display(), e));
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if path.file_name().is_some_and(|n| n == "__pycache__") {
                continue;
            }
            out.extend(files_under(&path));
        } else if path.is_file() {
            out.push(path);
        }
    }
    assert!(!out.is_empty(), "{} holds no files: refusing to watch nothing", dir.display());
    out
}

/// The interpreter that owns this repo's checkout, in the two places the project
/// documents it.  Not "whatever `python` is on PATH": running the check under an
/// unrelated interpreter is how you get a build tool that behaves differently on
/// one machine, and this check only needs the standard library anyway.
fn venv_python() -> Option<PathBuf> {
    [".venv/Scripts/python.exe", ".venv/bin/python"]
        .iter()
        .map(|p| repo_root().join(p))
        .find(|p| p.is_file())
}

fn check_sidecar_freshness() {
    let Some(python) = venv_python() else {
        println!(
            "cargo:warning=cannot check whether the frozen conductor is current: no .venv \
             interpreter found (build it with `python desktop/sidecar/build.py`, or run this \
             from a checkout that has one)"
        );
        return;
    };
    let out = Command::new(&python)
        .args(["desktop/sidecar/build.py", "--stale-check"])
        .current_dir(repo_root())
        .output();
    match out {
        Err(e) => println!("cargo:warning=could not run the sidecar freshness check: {e}"),
        Ok(out) if out.status.success() => {}
        Ok(out) => {
            let text = String::from_utf8_lossy(&out.stdout);
            let err = String::from_utf8_lossy(&out.stderr);
            panic!(
                "the frozen conductor beside this shell is not built from these sources.\n\
                 {text}{err}\n\
                 Refusing to produce a release artifact that would ship a stale conductor."
            );
        }
    }
}
