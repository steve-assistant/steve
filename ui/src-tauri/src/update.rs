//! Updates, and the repair the integrity check offers (docs/plan/08-packaging.md,
//! sections 4 and 5). Only on the user's yes: the window asks for a check at
//! start and once a day unless they turned it off, since each check tells the
//! release's server this computer's address, and an update installs only when
//! they press Install, never in the middle of a reply.
//!
//! What it prevents:
//!
//! - **An older Steve over a newer one.** Core's migrations only go forward,
//!   and a worker refuses a database a newer Steve wrote (`core/db.migrate`).
//!   So nothing goes back, in three places: the updater offers only a newer
//!   release (`offered`), the installer refuses an older one
//!   (`bundle.windows.allowDowngrades`), and the worker refuses the newer
//!   database.
//! - **An old release offered under a new number.** The updater checks the
//!   signature of what it downloads against the update key built into the
//!   app; with `requireSignedVersion` it also checks that the version signed
//!   is the version announced, so a validly signed old installer cannot be
//!   passed off as new.
//! - **An installer copying over a running backend.** The updater runs the
//!   installer and exits the app; the app's job would end the backend as it
//!   exits, but a Python still dying holds its files open and the copy fails
//!   on them. Before the installer starts, the backend is stopped as Quit
//!   stops it and the job is waited on until it is empty
//!   (`Backend::stop_for_update`).
//! - **Files of the old version left beside the new.** The installer's
//!   pre-install hook (`windows/hooks.nsh`) deletes `backend\` and `runtime\`
//!   whole before it copies: two `dist-info` folders for one package confuse
//!   `importlib.metadata`, and the integrity check would name any module the
//!   new version did not ship.
//!
//! A repair is the same download with this version allowed too: the files of
//! this release, from its installer, its signature checked as an update's.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use semver::Version;
use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime, State};
use tauri_plugin_updater::{Error, Update, UpdaterBuilder, UpdaterExt};

use crate::backend::Backend;

/// Whether a release is offered: a newer one, or this one again when the user
/// asked for a repair. Never an older one.
pub fn offered(current: &Version, release: &Version, repair: bool) -> bool {
    release > current || (repair && release == current)
}

/// What the window is told about a release found.
#[derive(Serialize)]
pub struct Offer {
    version: String,
    notes: Option<String>,
    repair: bool,
}

/// The release found by the last check, until Install takes it.
#[derive(Default)]
pub struct Pending(Mutex<Option<Update>>);

fn explain(err: Error) -> String {
    match err {
        // Until the project's releases exist (step 12), a build names no
        // server, and asks none.
        Error::EmptyEndpoints => "Updates are not set up in this build.".into(),
        other => other.to_string(),
    }
}

/// The updater as Steve asks it: Steve's own comparator (`offered`) and a
/// timeout. The key, the signed version and the server come from the
/// plugin's configuration. Apart from the step before the installer runs,
/// this is all of it, so the fetch tests below run exactly this.
fn updater_for<R: Runtime>(app: &AppHandle<R>, repair: bool) -> UpdaterBuilder {
    app.updater_builder()
        .version_comparator(move |current, release| offered(&current, &release.version, repair))
        .timeout(Duration::from_secs(120))
}

/// Ask the release's server for a newer Steve, or with `repair` for this one
/// too.
#[tauri::command]
pub async fn update_check(
    app: AppHandle,
    pending: State<'_, Pending>,
    repair: bool,
) -> Result<Option<Offer>, String> {
    let backend = app.state::<Arc<Backend>>().inner().clone();
    let updater = updater_for(&app, repair)
        .on_before_exit(move || {
            if !backend.stop_for_update() {
                log::error!("the backend was still running when the installer started");
            }
        })
        .build()
        .map_err(explain)?;
    let found = updater.check().await.map_err(explain)?;
    let offer = found.as_ref().map(|update| Offer {
        version: update.version.clone(),
        notes: update.body.clone(),
        repair,
    });
    *pending.0.lock().unwrap() = found;
    Ok(offer)
}

/// Download the release the last check found, check its signature, stop the
/// backend and run its installer, which restarts the app. On Windows the app
/// exits here; it returns only on a failure.
#[tauri::command]
pub async fn update_install(pending: State<'_, Pending>) -> Result<(), String> {
    let update = pending.0.lock().unwrap().take().ok_or("Nothing to install: check again.")?;
    update.download_and_install(|_, _| {}, || {}).await.map_err(explain)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(text: &str) -> Version {
        Version::parse(text).unwrap()
    }

    #[test]
    fn only_a_newer_release_is_offered() {
        assert!(offered(&v("0.1.0"), &v("0.1.1"), false));
        assert!(!offered(&v("0.1.0"), &v("0.1.0"), false));
        assert!(!offered(&v("0.2.0"), &v("0.1.9"), false));
        // A pre-release of this version is older than it.
        assert!(!offered(&v("0.2.0"), &v("0.2.0-rc.1"), false));
    }

    /// A repair reinstalls this version's files; it must never be a way back
    /// to an older one, whose worker would refuse the database.
    #[test]
    fn a_repair_takes_this_version_too_and_never_an_older_one() {
        assert!(offered(&v("0.1.0"), &v("0.1.0"), true));
        assert!(offered(&v("0.1.0"), &v("0.1.1"), true));
        assert!(!offered(&v("0.1.0"), &v("0.0.9"), true));
    }

    /// The updater's own settings: the release's version must be the one it
    /// signed, and no setting may let it go back.
    #[test]
    fn the_updater_requires_the_signed_version_and_never_goes_back() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater = &config["plugins"]["updater"];
        assert_eq!(updater["requireSignedVersion"], true);
        assert_eq!(updater["allowDowngrades"], false);
        assert_eq!(config["bundle"]["windows"]["allowDowngrades"], false);
        assert_eq!(config["bundle"]["createUpdaterArtifacts"], true);
    }
}

/// The updater's fetch, end to end: Tauri's own updater, set up by
/// `updater_for` as the app sets it up, asks a server on this computer for a
/// release, downloads it and checks it. The settings test above proves only
/// what the configuration says; a setting the plugin ignored, or a check it
/// skipped, would pass it.
///
/// The fixtures in `fixtures/updater/` were signed by two disposable keys,
/// thrown away once they had signed (the README there says how); the release
/// key is never used here. Each "installer" is a few bytes, and is only
/// downloaded, never installed or run.
#[cfg(test)]
mod fetch {
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    use base64::Engine;
    use serde_json::json;
    use tauri::test::{mock_builder, mock_context, noop_assets, MockRuntime};
    use tauri::App;

    use super::*;

    const KEY: &str = include_str!("../fixtures/updater/release.pub");
    const NEW: &[u8] = include_bytes!("../fixtures/updater/installer-0.1.1");
    const NEW_SIGNED: &str = include_str!("../fixtures/updater/installer-0.1.1.sig");
    const NEW_SIGNED_BY_ANOTHER_KEY: &str =
        include_str!("../fixtures/updater/installer-0.1.1.other-key.sig");
    const OLD: &[u8] = include_bytes!("../fixtures/updater/installer-0.0.9");
    const OLD_SIGNED: &str = include_str!("../fixtures/updater/installer-0.0.9.sig");
    const OLD_SIGNED_WITHOUT_VERSION: &str =
        include_str!("../fixtures/updater/installer-0.0.9.unversioned.sig");

    /// Serve one release on 127.0.0.1, on a port the system picks: a
    /// `latest.json` announcing `version` with `signature`, and `installer`,
    /// the bytes a download gets. Returns the address of `latest.json`. The
    /// thread serves until the test process ends.
    fn serve(version: &str, signature: &str, installer: Vec<u8>) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let latest = json!({
            "version": version,
            "notes": "A fixture release.",
            "platforms": {
                "windows-x86_64": {"url": format!("{base}/installer"), "signature": signature.trim()},
            },
        })
        .to_string()
        .into_bytes();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut request = String::new();
                let _ = reader.read_line(&mut request);
                // The headers, up to the empty line that ends them: no request
                // here has a body.
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                }
                let (status, body) = match request.split(' ').nth(1) {
                    Some("/latest.json") => ("200 OK", latest.as_slice()),
                    Some("/installer") => ("200 OK", installer.as_slice()),
                    _ => ("404 Not Found", &[][..]),
                };
                let head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes()).and_then(|()| stream.write_all(body));
            }
        });
        format!("{base}/latest.json")
    }

    /// A Steve at 0.1.0 whose updater has the shipped settings but for three:
    /// the fixtures' key in place of the release key, the test's server in
    /// place of the project's, and plain http, which that server speaks on
    /// 127.0.0.1. The cases test the signature, which TLS does not replace.
    fn steve(endpoint: &str) -> App<MockRuntime> {
        let shipped: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let mut settings = shipped["plugins"]["updater"].clone();
        settings["pubkey"] = KEY.trim().into();
        settings["endpoints"] = json!([endpoint]);
        settings["dangerousInsecureTransportProtocol"] = true.into();
        let mut context = mock_context(noop_assets());
        context.config_mut().plugins.0.insert("updater".into(), settings);
        context.package_info_mut().version = Version::new(0, 1, 0);
        // The plugin is made as the app makes it (`lib.rs`), from the settings.
        mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .unwrap()
    }

    /// The release the updater offers, if any.
    fn check(app: &App<MockRuntime>, repair: bool) -> Option<Update> {
        // Without a proxy: a system proxy must not carry a test's request off
        // this computer.
        let updater = updater_for(app.handle(), repair).no_proxy().build().unwrap();
        tauri::async_runtime::block_on(updater.check()).unwrap()
    }

    /// The bytes of the update's download, once the updater has checked them.
    fn download(update: &Update) -> Result<Vec<u8>, Error> {
        tauri::async_runtime::block_on(update.download(|_, _| {}, || {}))
    }

    /// `signature`, claiming the key id of `key`. The id is a label in the
    /// signature, read before anything is checked; it proves nothing.
    fn with_key_id(signature: &str, key: &str) -> String {
        let b64 = base64::engine::general_purpose::STANDARD;
        let text = |encoded: &str| String::from_utf8(b64.decode(encoded.trim()).unwrap()).unwrap();
        // The second line of each: the algorithm (2 bytes), the key id (8),
        // then the signature or the key.
        let key_line = b64.decode(text(key).lines().nth(1).unwrap()).unwrap();
        let mut lines: Vec<String> = text(signature).lines().map(str::to_string).collect();
        let mut signed = b64.decode(&lines[1]).unwrap();
        signed[2..10].copy_from_slice(&key_line[2..10]);
        lines[1] = b64.encode(signed);
        b64.encode(lines.join("\n") + "\n")
    }

    /// The whole path works: without this, every refusal below could come
    /// from a server or a setting that refuses everything.
    #[test]
    fn a_newer_release_signed_by_the_built_in_key_is_offered_and_downloaded_as_served() {
        let app = steve(&serve("0.1.1", NEW_SIGNED, NEW.to_vec()));

        let update = check(&app, false).expect("0.1.1 is newer than 0.1.0");

        assert_eq!(update.version, "0.1.1");
        assert_eq!(download(&update).unwrap(), NEW);
    }

    /// Anyone can make a key and sign with it; only the key built into the
    /// app makes a release. Another key's signature is refused under its own
    /// key id, and under the built-in key's.
    #[test]
    fn the_same_release_signed_by_another_key_is_refused() {
        let app = steve(&serve("0.1.1", NEW_SIGNED_BY_ANOTHER_KEY, NEW.to_vec()));
        let update = check(&app, false).expect("offered: the signature is checked on download");
        let refused = download(&update).expect_err("another key's signature was accepted");
        assert!(
            matches!(refused, Error::Minisign(minisign_verify::Error::UnexpectedKeyId)),
            "{refused}"
        );

        let posing = with_key_id(NEW_SIGNED_BY_ANOTHER_KEY, KEY);
        let app = steve(&serve("0.1.1", &posing, NEW.to_vec()));
        let update = check(&app, false).unwrap();
        let refused = download(&update).expect_err("a signature claiming the key's id was accepted");
        assert!(
            matches!(refused, Error::Minisign(minisign_verify::Error::InvalidSignature)),
            "{refused}"
        );
    }

    /// A release with no signature at all is no release.
    #[test]
    fn a_release_signed_by_no_key_is_refused() {
        let app = steve(&serve("0.1.1", "", NEW.to_vec()));
        let update = check(&app, false).expect("offered: the signature is checked on download");

        let refused = download(&update).expect_err("an unsigned installer was accepted");

        assert!(
            matches!(refused, Error::Minisign(minisign_verify::Error::InvalidEncoding)),
            "{refused}"
        );
    }

    /// An installer changed on the server or on the way, after it was signed:
    /// the signature covers every byte.
    #[test]
    fn a_download_changed_after_signing_is_refused() {
        let mut changed = NEW.to_vec();
        changed[0] ^= 1;
        let app = steve(&serve("0.1.1", NEW_SIGNED, changed));
        let update = check(&app, false).unwrap();

        let refused = download(&update).expect_err("a changed installer was accepted");

        assert!(
            matches!(refused, Error::Minisign(minisign_verify::Error::InvalidSignature)),
            "{refused}"
        );
    }

    /// `latest.json` is not signed; only the installer is. Whoever serves it
    /// could pair a new number with an old release's installer and its
    /// genuine signature, and take Steve back to a version whose worker
    /// refuses the database. So the version signed must be the version
    /// announced. A signature that names its version is compared whatever
    /// the settings say; one that names none is refused only because
    /// `requireSignedVersion` is on.
    #[test]
    fn an_old_installer_announced_as_newer_is_refused() {
        let app = steve(&serve("0.1.1", OLD_SIGNED, OLD.to_vec()));
        let update = check(&app, false).expect("offered: the number announced is newer");
        let refused = download(&update).expect_err("an old installer passed as new");
        assert!(
            matches!(&refused, Error::SignedVersionMismatch { signed, announced }
                if signed == "0.0.9" && announced == "0.1.1"),
            "{refused}"
        );

        let app = steve(&serve("0.1.1", OLD_SIGNED_WITHOUT_VERSION, OLD.to_vec()));
        let update = check(&app, false).unwrap();
        let refused = download(&update).expect_err("a signature naming no version was accepted");
        assert!(matches!(refused, Error::MissingSignedVersion), "{refused}");
    }

    /// Core's migrations only go forward: an older release, genuine and
    /// signed, is not even offered, and a repair takes only this version.
    #[test]
    fn an_older_release_is_not_offered_even_for_a_repair() {
        let app = steve(&serve("0.0.9", OLD_SIGNED, OLD.to_vec()));

        assert!(check(&app, false).is_none());
        assert!(check(&app, true).is_none());
    }
}
