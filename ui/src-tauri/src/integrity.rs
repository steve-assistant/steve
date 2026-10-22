//! Steve's own files, checked against the list its build signed, before the
//! backend runs them (docs/plan/08-packaging.md, section 4).
//!
//! An install is some 24,000 files the app hands to Python. A file cut short
//! by a full disk, an update half applied, a file an old version left behind,
//! or a module written where none should be would each run as Steve, and fail
//! later in a way that names nothing. So the build lists every file under
//! `backend\` and `runtime\` with its SHA-256 in `manifest.json`, and signs
//! the list with the update key (minisign, as the updater's downloads are
//! signed). Before the backend starts, the app checks the signature, every
//! file, and that nothing under `backend\` is a module, DLL or `.pth` the
//! list does not name (`before_start`). On a mismatch the backend is not
//! started, and the app says which file.
//!
//! Until step 11 only the files Windows loads as code were checked first,
//! and the rest while the backend ran: a changed `.py` file ran once, as
//! Steve, before the check stopped it (the step 11 audit, F3). A start
//! now waits for the whole check, about 3 seconds on step 8's measure.
//!
//! It does not stop a program running as the user, which could rewrite the
//! checker as well; the security model accepts that (section 2.3).

use std::collections::BTreeMap;
use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::time::Instant;

use base64::Engine;
use sha2::{Digest, Sha256};

use crate::backend::Backend;

pub const MANIFEST: &str = "manifest.json";
pub const SIGNATURE: &str = "manifest.json.sig";

/// What the build signed: each file's path under the install, with `/`,
/// and its SHA-256 in hex.
#[derive(Debug, serde::Deserialize)]
pub struct Manifest {
    pub version: String,
    pub files: BTreeMap<String, String>,
}

/// What is wrong, naming the file.
#[derive(Debug, PartialEq)]
pub enum Problem {
    /// The list, its signature or the key: missing, unreadable, or no match.
    Unsigned(String),
    Changed(String),
    Missing(String),
    Unreadable(String),
    /// Code the build did not ship, where Python would run it.
    Extra(String),
    /// The list is another version's: the app was replaced and its backend
    /// was not, or the other way round.
    OtherVersion { files: String, app: String },
}

impl fmt::Display for Problem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Problem::Unsigned(why) => {
                write!(f, "the list of Steve's files is not the one its build signed ({why})")
            }
            Problem::Changed(file) => write!(f, "{file} is not the file this version shipped"),
            Problem::Missing(file) => write!(f, "{file} is missing"),
            Problem::Unreadable(file) => write!(f, "{file} could not be read"),
            Problem::Extra(file) => {
                write!(f, "{file} was not shipped with this version, and Python would run it")
            }
            Problem::OtherVersion { files, app } => {
                write!(f, "the backend's files are version {files}, and the app is version {app}")
            }
        }
    }
}

/// The list, if the key signed it. `pubkey` is the update key as Tauri's
/// configuration holds it: base64 of minisign's text.
pub fn read(install: &Path, pubkey: &str, version: &str) -> Result<Manifest, Problem> {
    let bytes = std::fs::read(install.join(MANIFEST))
        .map_err(|err| Problem::Unsigned(format!("{MANIFEST}: {err}")))?;
    let signature = std::fs::read_to_string(install.join(SIGNATURE))
        .map_err(|err| Problem::Unsigned(format!("{SIGNATURE}: {err}")))?;
    verify(&bytes, &signature, pubkey).map_err(Problem::Unsigned)?;
    let manifest: Manifest = serde_json::from_slice(&bytes)
        .map_err(|err| Problem::Unsigned(format!("{MANIFEST}: {err}")))?;
    if manifest.version != version {
        return Err(Problem::OtherVersion { files: manifest.version, app: version.into() });
    }
    Ok(manifest)
}

/// minisign's check as the updater plugin makes it, the key and the
/// signature each base64 of minisign's own text. Prehashed signatures only,
/// which is what Tauri's signer writes.
fn verify(data: &[u8], signature: &str, pubkey: &str) -> Result<(), String> {
    let text = |encoded: &str, what: &str| -> Result<String, String> {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(encoded.trim())
            .map_err(|err| format!("{what}: {err}"))?;
        String::from_utf8(bytes).map_err(|err| format!("{what}: {err}"))
    };
    let key = minisign_verify::PublicKey::decode(&text(pubkey, "the key")?)
        .map_err(|err| format!("the key: {err}"))?;
    let signature = minisign_verify::Signature::decode(&text(signature, "the signature")?)
        .map_err(|err| format!("the signature: {err}"))?;
    key.verify(data, &signature, false).map_err(|err| format!("the signature: {err}"))
}

/// The first file `which` picks from the list that is not as shipped.
pub fn check(install: &Path, manifest: &Manifest) -> Option<Problem> {
    for (file, expected) in &manifest.files {
        match sha256(&install.join(file)) {
            Ok(found) if found.eq_ignore_ascii_case(expected) => {}
            Ok(_) => return Some(Problem::Changed(file.clone())),
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                return Some(Problem::Missing(file.clone()))
            }
            Err(_) => return Some(Problem::Unreadable(file.clone())),
        }
    }
    None
}

fn sha256(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 16];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            return Ok(hex::encode(hasher.finalize()));
        }
        hasher.update(&buffer[..read]);
    }
}

/// Code the list does not name anywhere under `backend\`: a module, a DLL,
/// or a `.pth`, which Python runs at every start. Python writes none of
/// these there itself, since the backend runs with `-B`, so any one is a
/// stray. Only Steve's own folder (`backend\app\`) and site-packages' top
/// `.pth` files were looked at, and a `sitecustomize.py` in site-packages,
/// which Python also runs at every start, went unnamed. Until step 11's
/// audit so did a `.pyw`, a module's suffix on Windows, and a `._pth`
/// beside python.exe, which replaces Python's whole path; the build ships
/// neither.
pub fn strays(install: &Path, manifest: &Manifest) -> Option<Problem> {
    let mut found = None;
    walk(&install.join("backend"), &mut |path| {
        if found.is_some() {
            return;
        }
        let code = path.extension().and_then(|ext| ext.to_str()).is_some_and(|ext| {
            ["py", "pyw", "pyc", "pyd", "dll", "pth", "_pth"]
                .contains(&ext.to_ascii_lowercase().as_str())
        });
        if !code {
            return;
        }
        let rel = path.strip_prefix(install).unwrap_or(path);
        let rel: Vec<String> =
            rel.components().map(|part| part.as_os_str().to_string_lossy().into_owned()).collect();
        let rel = rel.join("/");
        if !manifest.files.contains_key(&rel) {
            found = Some(Problem::Extra(rel));
        }
    });
    found
}

fn walk(folder: &Path, each: &mut impl FnMut(&Path)) {
    for entry in std::fs::read_dir(folder).into_iter().flatten().flatten() {
        let path = entry.path();
        match entry.file_type() {
            Ok(kind) if kind.is_dir() => walk(&path, each),
            Ok(_) => each(&path),
            Err(_) => {}
        }
    }
}

/// Every file as shipped, and no file Python would load that was not:
/// what must hold before the backend runs anything.
pub fn before_start(install: &Path, manifest: &Manifest) -> Option<Problem> {
    check(install, manifest).or_else(|| strays(install, manifest))
}

/// The install checked, then the backend started. A problem refuses the
/// backend, and `damaged` is told which file.
pub fn start_checked(
    install: &Path,
    pubkey: Option<&str>,
    version: &str,
    backend: &Backend,
    damaged: impl Fn(String),
) {
    let refuse = |problem: Problem| {
        let why = problem.to_string();
        log::error!("Steve's files are damaged: {why}");
        backend.refuse(why.clone());
        damaged(why);
    };
    let began = Instant::now();
    let manifest = match pubkey {
        None => Err(Problem::Unsigned("this build has no update key".into())),
        Some(key) => read(install, key, version),
    };
    let manifest = match manifest {
        Ok(manifest) => manifest,
        Err(problem) => return refuse(problem),
    };
    if let Some(problem) = before_start(install, &manifest) {
        return refuse(problem);
    }
    log::info!(
        "Steve's {} files checked in {} ms",
        manifest.files.len(),
        began.elapsed().as_millis()
    );
    backend.start();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    const FIXTURE_KEY: &str = include_str!("../fixtures/integrity/fixture.pub");

    fn install(name: &str, files: &[(&str, &[u8])]) -> (PathBuf, Manifest) {
        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-scratch")
            .join(name);
        let _ = std::fs::remove_dir_all(&base);
        let mut listed = BTreeMap::new();
        for (rel, bytes) in files {
            let path = base.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, bytes).unwrap();
            listed.insert(rel.to_string(), hex::encode(Sha256::digest(bytes)));
        }
        (base, Manifest { version: "0.1.0".into(), files: listed })
    }

    fn signed_fixture(name: &str) -> PathBuf {
        let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures").join("integrity");
        let (base, _) = install(name, &[]);
        std::fs::create_dir_all(&base).unwrap();
        for file in [MANIFEST, SIGNATURE] {
            std::fs::copy(fixtures.join(file), base.join(file)).unwrap();
        }
        base
    }

    #[test]
    fn a_list_the_key_signed_is_read() {
        let base = signed_fixture("signed");

        let manifest = read(&base, FIXTURE_KEY, "0.1.0").unwrap();

        assert_eq!(manifest.version, "0.1.0");
        assert!(manifest.files.contains_key("backend/python.exe"));
    }

    #[test]
    fn a_list_changed_after_signing_is_refused() {
        let base = signed_fixture("resigned");
        let listed = std::fs::read_to_string(base.join(MANIFEST)).unwrap();
        std::fs::write(base.join(MANIFEST), listed.replace("c3a1", "c3a2")).unwrap();

        assert!(matches!(read(&base, FIXTURE_KEY, "0.1.0"), Err(Problem::Unsigned(_))));
    }

    #[test]
    fn a_list_of_another_version_than_the_apps_is_refused() {
        let base = signed_fixture("other-version");

        assert_eq!(
            read(&base, FIXTURE_KEY, "0.2.0").err(),
            Some(Problem::OtherVersion { files: "0.1.0".into(), app: "0.2.0".into() })
        );
    }

    #[test]
    fn a_list_signed_by_another_key_or_not_at_all_is_refused() {
        let base = signed_fixture("other-key");
        let ours = std::fs::read_to_string(
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tauri.conf.json"),
        )
        .unwrap();
        let config: serde_json::Value = serde_json::from_str(&ours).unwrap();
        let release_key = config["plugins"]["updater"]["pubkey"].as_str().unwrap();

        assert!(matches!(read(&base, release_key, "0.1.0"), Err(Problem::Unsigned(_))));
        std::fs::remove_file(base.join(SIGNATURE)).unwrap();
        assert!(matches!(read(&base, FIXTURE_KEY, "0.1.0"), Err(Problem::Unsigned(_))));
    }

    #[test]
    fn a_changed_or_missing_file_is_named() {
        let (base, manifest) = install(
            "files",
            &[("backend/python.exe", b"MZ"), ("backend/app/core/x.py", b"x = 1\n")],
        );
        assert_eq!(check(&base, &manifest), None);

        std::fs::write(base.join("backend/app/core/x.py"), b"x = 2\n").unwrap();
        assert_eq!(check(&base, &manifest), Some(Problem::Changed("backend/app/core/x.py".into())));

        std::fs::write(base.join("backend/app/core/x.py"), b"x = 1\n").unwrap();
        std::fs::remove_file(base.join("backend/python.exe")).unwrap();
        assert_eq!(check(&base, &manifest), Some(Problem::Missing("backend/python.exe".into())));
    }

    #[test]
    fn a_changed_python_file_or_a_stray_module_stops_the_start_before_anything_runs() {
        // Until step 11 a `.py` file was checked only after the backend had
        // started, so a changed one ran once as Steve (the audit's F3).
        let (base, manifest) = install(
            "before-start",
            &[("backend/python.exe", b"MZ"), ("backend/app/core/x.py", b"x = 1\n")],
        );
        assert_eq!(before_start(&base, &manifest), None);

        std::fs::write(base.join("backend/app/core/x.py"), b"x = 2\n").unwrap();
        assert_eq!(
            before_start(&base, &manifest),
            Some(Problem::Changed("backend/app/core/x.py".into()))
        );
        std::fs::write(base.join("backend/app/core/x.py"), b"x = 1\n").unwrap();
        std::fs::write(base.join("backend/app/core/planted.py"), b"").unwrap();
        assert_eq!(
            before_start(&base, &manifest),
            Some(Problem::Extra("backend/app/core/planted.py".into()))
        );
    }

    #[test]
    fn a_module_or_pth_the_build_did_not_ship_is_named_and_other_files_are_not() {
        let (base, manifest) = install(
            "strays",
            &[
                ("backend/app/core/x.py", b"x = 1\n"),
                ("backend/Lib/site-packages/steve.pth", b"../../app\n"),
            ],
        );
        std::fs::write(base.join("backend/app/core/notes.txt"), b"").unwrap();
        std::fs::write(base.join("backend/Lib/site-packages/README.txt"), b"").unwrap();
        assert_eq!(strays(&base, &manifest), None);

        // Each where Python, or Windows for Python, would load it.
        for stray in [
            "backend/Lib/site-packages/evil.pth",
            "backend/app/core/old_module.py",
            "backend/Lib/site-packages/sitecustomize.py",
            "backend/Lib/site-packages/somepkg/__pycache__/x.cpython-312.pyc",
            "backend/DLLs/_planted.pyd",
            "backend/python312.DLL",
            "backend/evil.pth",
            // Python's path, rewritten by the file beside python.exe.
            "backend/python312._pth",
            "backend/python._pth",
            // A module's suffix on Windows: run as `sitecustomize` too.
            "backend/Lib/site-packages/sitecustomize.pyw",
        ] {
            let path = base.join(stray);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, b"").unwrap();
            assert_eq!(strays(&base, &manifest), Some(Problem::Extra(stray.into())), "{stray}");
            std::fs::remove_file(&path).unwrap();
        }
        assert_eq!(strays(&base, &manifest), None);
    }
}
