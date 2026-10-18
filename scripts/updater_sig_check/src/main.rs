//! Verify a release artifact against the pubkey compiled into the app.
//!
//! `scripts/release.py` runs this before it will print a `gh release create` command.  A
//! signature that does not match the artifact, or a version that does not match the
//! manifest, is otherwise discovered by the *user*, after publishing, with no way to take
//! it back -- and a release that verifies only in the release script's own reading of the
//! minisign format would prove nothing about the app.
//!
//! Inputs are the decoded *text* forms (the pubkey text, the signature text).  Decoding the
//! base64 happens in `release.py` with `validate=True`, which is the step that catches a
//! config value pasted in the wrong shape.

use std::env;
use std::fs;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.len() != 3 {
        eprintln!("usage: updater_sig_check <artifact> <pubkey-text-file> <signature-text-file>");
        return ExitCode::from(2);
    }
    let (artifact, pubkey_path, signature_path) = (&args[0], &args[1], &args[2]);

    let data = match fs::read(artifact) {
        Ok(data) => data,
        Err(e) => {
            eprintln!("cannot read {artifact}: {e}");
            return ExitCode::from(1);
        }
    };
    let pubkey_text = fs::read_to_string(pubkey_path).expect("read the pubkey text file");
    let signature_text = fs::read_to_string(signature_path).expect("read the signature text file");

    let key = match minisign_verify::PublicKey::decode(&pubkey_text) {
        Ok(key) => key,
        Err(e) => {
            eprintln!("the pubkey did not decode: {e}");
            return ExitCode::from(1);
        }
    };
    let signature = match minisign_verify::Signature::decode(&signature_text) {
        Ok(signature) => signature,
        Err(e) => {
            eprintln!("the signature did not decode: {e}");
            return ExitCode::from(1);
        }
    };
    // `allow_legacy = true` is what the updater passes; it is also what makes the check
    // cover the *global* signature over the trusted comment, which is the only thing
    // binding the announced version to the artifact.
    if let Err(e) = key.verify(&data, &signature, true) {
        eprintln!("verify FAILED: {e}");
        return ExitCode::from(1);
    }

    let trusted = signature.trusted_comment();
    let signed_version = trusted
        .split('\t')
        .find_map(|field| field.strip_prefix("version:"))
        .unwrap_or("<absent>");
    println!("OK: {} bytes verify against the compiled pubkey", data.len());
    println!("  trusted comment: {trusted}");
    println!("  signed version : {signed_version}");
    ExitCode::SUCCESS
}
