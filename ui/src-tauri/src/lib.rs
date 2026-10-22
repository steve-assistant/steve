//! Tauri shell for Steve.
//!
//! Deliberately thin. All agent logic lives in the Python supervisor; this
//! process is a window, a tray icon, a global hotkey, and four buttons the
//! agent cannot press for itself. Keeping it that way matters because the
//! Rust side is the one part the agent cannot rebuild and reload without a
//! full recompile.
//!
//! The maintainer, 4 October 2026: a desktop app that starts at login, hidden in the
//! tray, with a control panel. So: one instance only (a second launch brings
//! the first forward), started by Windows at login with `--hidden`, a red dot
//! on the tray icon while a card waits for the user, and the agent's own
//! start/stop/restart -- which have to live here, because a worker asked to
//! stop itself cannot then be asked to start again. Since step 4 the app also
//! owns the backend: it starts it, starts it again if it dies, and ends it
//! when the app ends (`backend.rs`).
//!
//! And a sign-in window (the maintainer, 4 October 2026, "a better way to save
//! browser logins"): logging in through the sandbox's live view meant a
//! picture three times a second, keys one round trip each, and no passkeys
//! or Windows Hello. So the site opens here, in a private window of its own
//! -- nothing else's cookies in it, nothing kept on disk -- where the user signs in
//! as in any browser, and its cookies for the login's domains go to the
//! vault through the worker (`agent/browser/live.py`, `import_login`). No
//! model is anywhere in this; the page gets no access to these commands
//! (remote pages have no IPC, and every command here answers only the main
//! window).

mod backend;
mod caption;
mod deskcard;
mod hello;
mod integrity;
mod signin;
mod toast;
mod trayicon;
mod update;
mod windowsize;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::{
    image::Image,
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::PageLoadEvent,
    Emitter, Manager, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_log::{Target, TargetKind};

/// The sign-in window's label: one at a time.
const SIGNIN: &str = "signin";

/// What the user typed into sign-in forms in the sign-in window, by the host of
/// the page the user typed it on: username and password, the way a browser's
/// password manager would offer to keep them. The maintainer, 4 October 2026: typing
/// them a second time into the app, after signing in, was work for nothing.
/// Cleared when the window goes.
#[derive(Default)]
struct Typed {
    /// The current window's key: a page cannot guess it, and a poll that
    /// belongs to a window since closed stops when it changes.
    key: String,
    by_host: HashMap<String, (String, String)>,
}

#[derive(Default)]
struct SigninState(Mutex<Typed>);

/// What the tray says: cards waiting, whether it is taking notes on what
/// the laptop plays, and whether the backend has stopped for good. Drawn
/// into its icon (`trayicon.rs`) and one tooltip. Hands-free has no line or
/// menu item: it is not offered in v0.1 (docs/plan/07-voice.md, item 15).
#[derive(Default)]
struct TrayText {
    waiting: u32,
    tapping: bool,
    stopped: bool,
}

impl TrayText {
    /// After the window starts, stops or asks after the backend: the tray
    /// says it has stopped exactly while it is failing or damaged. Only the
    /// watcher used to set this, and nothing cleared it, so a Start that
    /// brought the backend back left the tray saying "Steve has stopped"
    /// until the app quit. True when what the tray says changed.
    fn backend_is(&mut self, gone: bool) -> bool {
        let changed = self.stopped != gone;
        self.stopped = gone;
        changed
    }
}

#[derive(Default)]
struct TrayState(Mutex<TrayText>);

/// The tray menu's "Take notes on what's playing" (a call, a video).
struct TapItem(CheckMenuItem<tauri::Wry>);

/// Runs first in every page of the sign-in window (the top frame only):
/// notes what the user types -- trusted keystrokes only, so a page's own script
/// cannot plant a value -- in a closure, and leaves behind only a function
/// that reads it, which no page can replace.
fn capture_script(key: &str) -> String {
    r#"(() => {
  if (window.top !== window) return;
  const NAME = "__steve_@@TOKEN@@";
  if (Object.prototype.hasOwnProperty.call(window, NAME)) return;
  const seen = { u: "", p: "" };
  Object.defineProperty(window, NAME, {
    value: () => ({ u: seen.u, p: seen.p }),
    enumerable: false, configurable: false, writable: false,
  });
  const userish = /user|login|email|netid|account|ident/i;
  document.addEventListener("input", (event) => {
    const el = event.target;
    if (!event.isTrusted || !(el instanceof HTMLInputElement)) return;
    const value = el.value || "";
    if (el.type === "password") { if (value) seen.p = value; return; }
    if (!["text", "email", "tel", ""].includes(el.type) || !value.trim()) return;
    const hint = [el.name, el.id, el.autocomplete, el.getAttribute("aria-label")].join(" ");
    if (userish.test(hint) || (el.form && el.form.querySelector("input[type=password]"))) {
      seen.u = value.trim();
    }
  }, true);
})();"#
        .replace("@@TOKEN@@", key)
}

/// Read back what the page saw typed, with the page's own host -- so a
/// value is put down to the site it was really typed on, whatever a page
/// says about itself.
fn poll_script(key: &str) -> String {
    r#"(() => { try {
  const read = window["__steve_@@TOKEN@@"];
  const v = typeof read === "function" ? read() : null;
  return { h: location.hostname, u: v ? v.u : "", p: v ? v.p : "" };
} catch (e) { return null; } })()"#
        .replace("@@TOKEN@@", key)
}

fn host_of(url: &Url) -> String {
    url.host_str().unwrap_or_default().to_lowercase()
}

/// Every 300 ms while this window is open, ask it what it has seen typed.
fn watch_typing(app: tauri::AppHandle, key: String) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(300));
        let state = app.state::<SigninState>();
        if state.0.lock().map(|t| t.key != key).unwrap_or(true) {
            return;
        }
        let Some(signin) = app.get_webview_window(SIGNIN) else { return };
        let Ok(url) = signin.url() else { continue };
        let host = host_of(&url);
        let app2 = app.clone();
        let key2 = key.clone();
        let _ = signin.eval_with_callback(poll_script(&key), move |json| {
            let Ok(seen) = serde_json::from_str::<serde_json::Value>(&json) else { return };
            let said_host = seen["h"].as_str().unwrap_or_default().to_lowercase();
            let (u, p) = (seen["u"].as_str().unwrap_or_default(), seen["p"].as_str().unwrap_or_default());
            if said_host != host || (u.is_empty() && p.is_empty()) {
                return;
            }
            let state = app2.state::<SigninState>();
            let Ok(mut typed) = state.0.lock() else { return };
            if typed.key != key2 {
                return;
            }
            let entry = typed.by_host.entry(host.clone()).or_default();
            if !u.is_empty() {
                entry.0 = u.to_string();
            }
            if !p.is_empty() {
                entry.1 = p.to_string();
            }
        });
    });
}

/// The sign-in window's page, cookies (HTTP-only included, in Playwright's
/// storage form) and what the user typed, by host. Never on the main thread:
/// WebView2 deadlocks reading cookies there.
fn collect(app: &tauri::AppHandle, signin: &tauri::WebviewWindow) -> Result<serde_json::Value, String> {
    let url = signin.url().map_err(|err| err.to_string())?;
    let cookies = signin.cookies().map_err(|err| err.to_string())?;
    let list: Vec<serde_json::Value> = cookies
        .iter()
        .map(|c| {
            serde_json::json!({
                "name": c.name(),
                "value": c.value(),
                // `domain()` (and `domain_raw()`) strip the leading dot WebView2
                // reports for a domain cookie, so every cookie arrives here looking
                // host-only. agent/browser/live.py `_cookie` puts the dot back; a
                // true host-only flag would need WebView2's cookie manager read
                // directly (7 October 2026: LinkedIn's login broke on this).
                "domain": c.domain().unwrap_or_default(),
                "path": c.path().unwrap_or("/"),
                "expires": c.expires_datetime().map(|t| t.unix_timestamp()).unwrap_or(-1),
                "httpOnly": c.http_only().unwrap_or(false),
                "secure": c.secure().unwrap_or(false),
                "sameSite": c.same_site().map(|s| format!("{s:?}")).unwrap_or_else(|| "Lax".into()),
            })
        })
        .collect();
    let typed: Vec<serde_json::Value> = app
        .state::<SigninState>()
        .0
        .lock()
        .map(|t| {
            t.by_host
                .iter()
                .map(|(host, (u, p))| serde_json::json!({ "host": host, "username": u, "password": p }))
                .collect()
        })
        .unwrap_or_default();
    Ok(serde_json::json!({ "url": url.as_str(), "cookies": list, "typed": typed }))
}

fn forget_typing(app: &tauri::AppHandle) {
    if let Ok(mut typed) = app.state::<SigninState>().0.lock() {
        typed.key.clear();
        typed.by_host.clear();
    }
}

/// Commands answer the control panel's own window only.
fn main_only(window: &tauri::Window) -> Result<(), String> {
    if window.label() == "main" {
        Ok(())
    } else {
        Err("only the main window can ask for that".into())
    }
}

// Push-to-talk is registered from the frontend, not here.
//
// The binding is a user preference -- the first default collided with
// another app's global shortcut -- and a hardcoded one would mean a Rust
// rebuild to change it. The JS side registers whatever is configured and
// re-registers when it changes; this process only supplies the plugin.
//
// Note on Fn: `keyboard-types` defines an FN modifier, but Windows never
// reports it. Fn is resolved by the keyboard's own controller and produces
// no scancode, so "Fn+Something" registers successfully and then never
// fires. Bindings must use Ctrl/Alt/Shift/Super.

/// The argument Windows starts it with at login: stay in the tray.
const HIDDEN: &str = "--hidden";

/// Uninstall's argument, with a key's name: forget that Hello key and exit,
/// before anything of the app starts (core/uninstall.py).
const FORGET_HELLO: &str = "--forget-hello";

/// The exit code, when the app was started to forget a Hello key: 0 once
/// the key is gone, or was never there.
fn forget_hello(args: &[String], forget: impl Fn(&str) -> Result<bool, String>) -> Option<i32> {
    let at = args.iter().position(|arg| arg == FORGET_HELLO)?;
    Some(match args.get(at + 1).map(|name| forget(name)) {
        Some(Ok(_)) => 0,
        Some(Err(why)) => {
            eprintln!("{why}");
            1
        }
        None => 2,
    })
}

/// Uninstall's argument: remove Steve's toasts from the Notification
/// Centre and exit (toast.rs, core/uninstall.py).
const CLEAR_NOTIFICATIONS: &str = "--clear-notifications";

/// The app's identifier, as tauri.conf.json names it: read here before the
/// app's own config is, for `--clear-notifications`.
const IDENTIFIER: &str = "dev.steve.desktop";

/// The exit code, when the app was started to clear its toasts: 0 once
/// they are gone.
fn clear_notifications(args: &[String], clear: impl Fn() -> Result<(), String>) -> Option<i32> {
    args.iter().any(|arg| arg == CLEAR_NOTIFICATIONS).then(|| match clear() {
        Ok(()) => 0,
        Err(why) => {
            eprintln!("{why}");
            1
        }
    })
}

/// Bring the main window to the front, restoring it if it was minimised.
///
/// Not by force for an app given a data folder of its own (a test's, or a
/// second Steve beside the user's own). Where Windows refuses a process in
/// the background the foreground, `set_focus` takes it anyway by pressing
/// and releasing Alt (tao's `force_window_active`). A test's app did that
/// at each start: a keystroke into whatever the developer was using, whose
/// next keys and clicks then went to the test's app. Shown, the window
/// comes to the front where Windows allows it, and waits in the taskbar
/// where it doesn't (`behind_unless_in_front`). Shown by tao, not by a raw
/// `ShowWindow`: tao keeps its own record of what is visible, and rewrites
/// the window's style from it at its next change, hiding a window it never
/// showed.
fn focus_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        if one_per_identifier(std::env::var_os("STEVE_DATA_DIR")) {
            let _ = window.set_focus();
        } else {
            // After the show, on the window's own thread: from any other,
            // `show` is a message to it, still in its queue.
            let _ = app.run_on_main_thread(move || behind_unless_in_front(&window));
        }
    }
}

/// A window shown without the foreground goes behind the one in use.
/// Windows refused it the focus but still put it on top: a test's window
/// came up over whatever the developer was doing, in their way until they
/// minimised it, and minimised, its page asks for nothing, so the walk
/// failed (19 October). Behind, it waits in the taskbar. Not for a window
/// Windows let come to the front: the user asked for it.
fn behind_unless_in_front(window: &tauri::WebviewWindow) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, SetWindowPos, HWND_BOTTOM, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
    };
    let Ok(hwnd) = window.hwnd() else { return };
    // SAFETY: the handle is this live window's, on its own thread.
    unsafe {
        if GetForegroundWindow() != hwnd.0 as _ {
            SetWindowPos(hwnd.0 as _, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
    }
}

/// The quick bar (the maintainer, 5 October 2026): a box over whatever the user is doing,
/// on the one hotkey's tap, to type to the agent without opening the app;
/// tapped again while it has focus, it goes away. The main window registers
/// the hotkey (its binding is a setting) and asks for this.
#[tauri::command]
fn show_quickbar(app: tauri::AppHandle, window: tauri::Window) -> Result<(), String> {
    main_only(&window)?;
    let bar = app.get_webview_window("quickbar").ok_or("no quick bar window")?;
    if bar.is_visible().unwrap_or(false) && bar.is_focused().unwrap_or(false) {
        let _ = bar.hide();
        return Ok(());
    }
    place_quickbar(&app, &bar)
}

/// The hotkey held (`on`) or let go, or the tray's Talk: the quick bar shown
/// if it is hidden, and told to listen or to send what it heard. Its own
/// socket carries the turn, so the words heard and the answer show there
/// (ui/src/QuickBar.tsx), not in a window the user isn't looking at.
#[tauri::command]
fn quickbar_talk(app: tauri::AppHandle, window: tauri::Window, on: bool) -> Result<(), String> {
    main_only(&window)?;
    talk(&app, on)
}

fn talk(app: &tauri::AppHandle, on: bool) -> Result<(), String> {
    let bar = app.get_webview_window("quickbar").ok_or("no quick bar window")?;
    if on && !bar.is_visible().unwrap_or(false) {
        place_quickbar(app, &bar)?;
    }
    let _ = bar.emit_to("quickbar", "quickbar-talk", on);
    Ok(())
}

/// Shown on the monitor the pointer is on, centred, its top 28% of the way
/// down (docs/design/presence.md, Look), and focused.
fn place_quickbar(app: &tauri::AppHandle, bar: &tauri::WebviewWindow) -> Result<(), String> {
    if let (Ok(cursor), Ok(monitors)) = (app.cursor_position(), bar.available_monitors()) {
        let here = monitors.iter().find(|m| {
            let (p, s) = (m.position(), m.size());
            cursor.x >= p.x as f64
                && cursor.x < (p.x + s.width as i32) as f64
                && cursor.y >= p.y as f64
                && cursor.y < (p.y + s.height as i32) as f64
        });
        if let (Some(monitor), Ok(size)) = (here, bar.outer_size()) {
            let (p, s) = (monitor.position(), monitor.size());
            let x = p.x + (s.width as i32 - size.width as i32) / 2;
            let y = p.y + (s.height as i32) * 28 / 100;
            let _ = bar.set_position(tauri::PhysicalPosition::new(x, y));
        }
    }
    bar.show().map_err(|e| e.to_string())?;
    let _ = bar.set_focus();
    let _ = bar.emit("quickbar-shown", ());
    Ok(())
}

/// The quick bar's "open the app" (Ctrl+Enter): today's page, in front, at
/// the exchange just had. It only brought the window forward, on whatever
/// view it was left at.
#[tauri::command]
fn quickbar_open_app(app: tauri::AppHandle, window: tauri::Window) {
    if window.label() != "quickbar" {
        return;
    }
    if let Some(bar) = app.get_webview_window("quickbar") {
        let _ = bar.hide();
    }
    open_today(&app);
}

/// The desk card's click: the journal at today, in front.
#[tauri::command]
fn deskcard_open(app: tauri::AppHandle, window: tauri::Window) {
    if window.label() == deskcard::LABEL {
        open_today(&app);
    }
}

fn open_today(app: &tauri::AppHandle) {
    focus_main(app);
    let _ = app.emit_to("main", "open-inbox", ());
}

/// The card's note line: the quick bar, where the note is answered.
#[tauri::command]
fn deskcard_quickbar(app: tauri::AppHandle, window: tauri::Window) -> Result<(), String> {
    if window.label() != deskcard::LABEL {
        return Err("for the desk card only".into());
    }
    let bar = app.get_webview_window("quickbar").ok_or("no quick bar window")?;
    place_quickbar(&app, &bar)
}

/// Press and hold on the card: push-to-talk, as the hotkey's hold is.
#[tauri::command]
fn deskcard_talk(app: tauri::AppHandle, window: tauri::Window, on: bool) -> Result<(), String> {
    if window.label() != deskcard::LABEL {
        return Err("for the desk card only".into());
    }
    talk(&app, on)
}

/// The card's right-click (presence.md, What it does). Its Hide items go
/// back to the card's page, which works out the hour and tomorrow.
#[tauri::command]
fn deskcard_menu(app: tauri::AppHandle, window: tauri::Window) -> Result<(), String> {
    if window.label() != deskcard::LABEL {
        return Err("for the desk card only".into());
    }
    let menu = (|| {
        let item = |id: &str, text: &str| MenuItem::with_id(&app, id, text, true, None::<&str>);
        Menu::with_items(&app, &[
            &item("deskcard-talk", "Talk")?,
            &item("deskcard-open", "Open Steve")?,
            &PredefinedMenuItem::separator(&app)?,
            &item("deskcard-hour", "Hide for an hour")?,
            &item("deskcard-tomorrow", "Hide until tomorrow")?,
            &PredefinedMenuItem::separator(&app)?,
            &item("deskcard-settings", "Desk card settings")?,
        ])
    })()
    .map_err(|e| e.to_string())?;
    window.popup_menu(&menu).map_err(|e| e.to_string())
}

fn deskcard_menu_event(app: &tauri::AppHandle, id: &str) {
    match id {
        "deskcard-talk" => {
            let _ = talk(app, true);
        }
        "deskcard-open" => open_today(app),
        "deskcard-hour" => {
            let _ = app.emit_to(deskcard::LABEL, "deskcard-hide", "hour");
        }
        "deskcard-tomorrow" => {
            let _ = app.emit_to(deskcard::LABEL, "deskcard-hide", "tomorrow");
        }
        "deskcard-settings" => {
            focus_main(app);
            let _ = app.emit_to("main", "open-view", "settings");
        }
        _ => {}
    }
}

/// The checkout a developer's build runs from, fixed when the app is built
/// so it never goes looking for a Python to run: `STEVE_ROOT`, or in a debug
/// build the checkout it was built in. A release build without `STEVE_ROOT`
/// names none, and runs only the backend beside it (`backend::checkout`).
///
/// Until step 11 a release build fell back to the folder it was built in
/// too: every release named the builder's Windows account, and a copy with
/// no backend beside it ran the builder's checkout, if the computer had one
/// there (the audit's F7). `python -m devtools.package scan` checks a
/// release for the builder's folders.
fn repo_root() -> Option<PathBuf> {
    option_env!("STEVE_ROOT").map(PathBuf::from).or_else(built_in)
}

/// A debug build's checkout is the one it was built in.
#[cfg(debug_assertions)]
fn built_in() -> Option<PathBuf> {
    Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

/// A release build names no folder of the machine that built it.
#[cfg(not(debug_assertions))]
fn built_in() -> Option<PathBuf> {
    None
}

/// The folder the app runs from, which is also its resource folder on
/// Windows: the install's, or `target\<profile>` in a developer's build.
fn app_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(PathBuf::from))
        .unwrap_or_default()
}

/// The update key, as the updater's configuration holds it: it signs the
/// updates and the list of an install's files alike.
fn update_key(config: &tauri::Config) -> Option<String> {
    config.plugins.0.get("updater")?.get("pubkey")?.as_str().map(str::to_string)
}

/// Where Steve keeps its data, by the rule core/paths.py follows:
/// `STEVE_DATA_DIR` if set, the checkout's `data/` in a source tree,
/// `%LOCALAPPDATA%\Steve` when installed.
fn data_dir() -> PathBuf {
    data_dir_for(
        std::env::var_os("STEVE_DATA_DIR"),
        &app_dir(),
        repo_root().as_deref(),
        std::env::var_os("LOCALAPPDATA"),
    )
}

/// The rule, apart from the process's environment. An install is asked
/// first: built on a developer's machine, it still names a checkout that
/// holds a `pyproject.toml`, and its data went into that checkout.
fn data_dir_for(
    chosen: Option<std::ffi::OsString>,
    app: &std::path::Path,
    checkout: Option<&std::path::Path>,
    local: Option<std::ffi::OsString>,
) -> PathBuf {
    if let Some(chosen) = chosen.filter(|d| !d.is_empty()) {
        let chosen = PathBuf::from(chosen);
        return if chosen.is_absolute() {
            chosen
        } else {
            std::env::current_dir().unwrap_or_default().join(chosen)
        };
    }
    if let Some(checkout) = backend::checkout(app, checkout)
        .filter(|checkout| checkout.join("pyproject.toml").is_file())
    {
        return checkout.join("data");
    }
    local.map(PathBuf::from).unwrap_or_default().join("Steve")
}

/// Whether this app is the one Steve its identifier names: the one on the
/// data folder the rule finds, not one given a folder of its own.
fn one_per_identifier(chosen: Option<std::ffi::OsString>) -> bool {
    chosen.filter(|d| !d.is_empty()).is_none()
}

/// The run file the supervisor writes at each start (core/localapi.py).
#[derive(serde::Deserialize)]
pub(crate) struct RunFile {
    port: u16,
    token: String,
}

/// What the app's windows need to reach the backend.
#[derive(serde::Serialize)]
struct Backend {
    url: String,
    token: String,
}

/// HMAC-SHA256 (RFC 2104). Written out rather than a crate for it: sha2 is
/// already here, and the construction is the standard one, checked below
/// against RFC 4231.
fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    const BLOCK: usize = 64;
    let mut padded = [0u8; BLOCK];
    if key.len() > BLOCK {
        padded[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        padded[..key.len()].copy_from_slice(key);
    }
    let pad = |byte: u8| padded.iter().map(|k| k ^ byte).collect::<Vec<u8>>();
    let inner = Sha256::new().chain_update(pad(0x36)).chain_update(data).finalize();
    Sha256::new().chain_update(pad(0x5c)).chain_update(inner).finalize().into()
}

/// What the backend must answer to the nonce: core/localapi.proof.
fn proof(token: &str, nonce: &str, port: u16) -> String {
    hex::encode(hmac_sha256(token.as_bytes(), format!("{nonce}:{port}").as_bytes()))
}

/// Compared in constant time.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// One small POST to whatever listens on the port, read to its end: an
/// HTTP/1.1 request to a server that answers with a length and closes. With
/// the token only for a backend that has already proved itself.
pub(crate) fn post(port: u16, path: &str, body: &str, token: Option<&str>) -> Result<String, String> {
    use std::io::{Read, Write};
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = std::net::TcpStream::connect_timeout(&address, Duration::from_secs(3))
        .map_err(|_| "Steve's backend is not answering".to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let authorization = token.map(|token| format!("Authorization: Bearer {token}\r\n")).unwrap_or_default();
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n\
         {authorization}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|err| err.to_string())?;
    let mut answer = Vec::new();
    stream.take(64 * 1024).read_to_end(&mut answer).map_err(|err| err.to_string())?;
    let answer = String::from_utf8_lossy(&answer).to_string();
    match answer.split_once("\r\n\r\n") {
        Some((head, rest)) if head.starts_with("HTTP/1.1 200") => Ok(rest.to_string()),
        _ => Err(format!("the backend refused {path}")),
    }
}

/// Read the run file, and hand over its address and token only once what
/// listens on its port has proved it holds the token (core/localapi.py). A
/// program squatting on a port a stale run file names has no token to answer
/// with, and is sent nothing; one that relays the nonce to the real backend
/// gets the proof for the real backend's port, not its own.
pub(crate) fn proven() -> Result<RunFile, String> {
    let path = data_dir().join("run").join("backend.json");
    let text = std::fs::read_to_string(&path).map_err(|_| "Steve's backend is not running".to_string())?;
    let run: RunFile = serde_json::from_str(&text).map_err(|err| format!("the run file is unreadable: {err}"))?;
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|err| err.to_string())?;
    let nonce = hex::encode(bytes);
    let answer =
        post(run.port, "/_sup/prove", &serde_json::json!({ "nonce": nonce }).to_string(), None)?;
    let given: serde_json::Value = serde_json::from_str(&answer).map_err(|err| err.to_string())?;
    let expected = proof(&run.token, &nonce, run.port);
    if !same(given["proof"].as_str().unwrap_or_default().as_bytes(), expected.as_bytes()) {
        return Err(format!("what answers on port {} is not Steve's backend", run.port));
    }
    Ok(run)
}

fn proven_backend() -> Result<Backend, String> {
    proven().map(|run| Backend { url: format!("http://127.0.0.1:{}", run.port), token: run.token })
}

/// The backend's address and token, for the app's own windows: the main
/// window and the quick bar. Asked again whenever a request is refused, since
/// a backend that restarted has a new port and a new token.
#[tauri::command]
async fn backend(window: tauri::Window) -> Result<Backend, String> {
    if !matches!(window.label(), "main" | "quickbar" | deskcard::LABEL) {
        return Err("only the app's own windows can ask for that".into());
    }
    tauri::async_runtime::spawn_blocking(proven_backend)
        .await
        .map_err(|err| err.to_string())?
}

/// Start, stop, restart or ask after the backend the app owns (`backend.rs`):
/// four things a page in the window can ask for, and nothing else.
#[tauri::command]
async fn backend_control(
    app: tauri::AppHandle,
    window: tauri::Window,
    owned: tauri::State<'_, Arc<backend::Backend>>,
    action: String,
) -> Result<backend::Status, String> {
    main_only(&window)?;
    let owned = owned.inner().clone();
    let status = tauri::async_runtime::spawn_blocking(move || {
        match action.as_str() {
            "status" => {}
            "start" => owned.start(),
            "stop" => owned.stop(),
            "restart" => owned.restart(),
            other => return Err(format!("not something the app can do: {other}")),
        }
        Ok(owned.status())
    })
    .await
    .map_err(|err| err.to_string())??;
    let changed = app
        .try_state::<TrayState>()
        .and_then(|state| state.0.lock().ok().map(|mut text| text.backend_is(status.gone())));
    if changed == Some(true) {
        redraw_icon(&app);
        redraw_tooltip(&app);
    }
    Ok(status)
}

/// Quit: the backend asked to stop and given its time to drain, then the app
/// ends, and with it the job and anything still in it.
/// "Quit Steve" in the window's menu, as the tray's Quit does: the backend
/// stops with the app.
#[tauri::command]
fn quit(app: tauri::AppHandle, window: tauri::Window) -> Result<(), String> {
    main_only(&window)?;
    quit_app(&app);
    Ok(())
}

fn quit_app(app: &tauri::AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        signin::end(&app, signin::Why::Ended);
        app.state::<Arc<backend::Backend>>().stop();
        app.exit(0);
    });
}

/// The tray's icon for what it says now, the taskbar's theme and the
/// display's scale: drawn ahead of time, only picked here (`trayicon.rs`).
fn tray_icon(app: &tauri::AppHandle) -> Option<Image<'static>> {
    let shown = {
        let state = app.try_state::<TrayState>()?;
        let text = state.0.lock().ok()?;
        trayicon::Shown::of(text.stopped, text.waiting)
    };
    let scale = app.primary_monitor().ok().flatten().map_or(1.0, |m| m.scale_factor());
    Image::from_bytes(trayicon::picture(trayicon::light_taskbar(), shown, scale)).ok()
}

fn redraw_icon(app: &tauri::AppHandle) {
    let Some(tray) = app.tray_by_id("main") else { return };
    if let Some(icon) = tray_icon(app) {
        let _ = tray.set_icon(Some(icon));
    }
}

/// The backend has stopped and the app will not start it again: the tray
/// says so, in its icon and tooltip, as the window does.
fn backend_stopped(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<TrayState>() {
        if let Ok(mut text) = state.0.lock() {
            text.stopped = true;
        }
    }
    redraw_icon(app);
    redraw_tooltip(app);
}

/// How many cards wait for them, shown on the tray: a badge, and the count in
/// the tooltip. The window polls the queue; this only draws it.
#[tauri::command]
fn set_waiting(app: tauri::AppHandle, window: tauri::Window, count: u32) {
    if main_only(&window).is_err() {
        return;
    }
    if let Some(state) = app.try_state::<TrayState>() {
        if let Ok(mut text) = state.0.lock() {
            text.waiting = count;
        }
    }
    redraw_tooltip(&app);
    redraw_icon(&app);
}

fn redraw_tooltip(app: &tauri::AppHandle) {
    let Some(tray) = app.tray_by_id("main") else { return };
    let Some(state) = app.try_state::<TrayState>() else { return };
    let Ok(text) = state.0.lock() else { return };
    let mut tip = match text.waiting {
        _ if text.stopped => "Steve has stopped: open it to see why".to_string(),
        0 => "Steve".to_string(),
        1 => "Steve: 1 thing waiting for you".to_string(),
        n => format!("Steve: {n} things waiting for you"),
    };
    if text.tapping {
        tip.push_str("\nTaking notes on what's playing");
    }
    let _ = tray.set_tooltip(Some(tip));
}

/// Notes of what the laptop plays, on the tray: the menu's check and a
/// tooltip line. The window polls the worker; this only draws it.
/// A toast for a message from Steve, its text hidden while Windows is
/// locked (`toast.rs`) or while the app is signed out, which the window
/// says (`signed_out`): the socket stays open signed out, and a card's or
/// a reminder's text in a toast was read by whoever was at the desk. A
/// click brings the window forward on the inbox.
#[tauri::command]
fn notify(app: tauri::AppHandle, window: tauri::Window, kind: String, text: String,
          signed_out: bool) -> Result<(), String> {
    main_only(&window)?;
    let lines = toast::lines(&kind, &text, signed_out || toast::locked());
    let handle = app.clone();
    app.state::<toast::Shown>()
        .show(&toast::app_id(&app.config().identifier), &lines, move || {
            focus_main(&handle);
            let _ = handle.emit_to("main", "open-inbox", ());
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn set_tapping(app: tauri::AppHandle, window: tauri::Window, on: bool) {
    if main_only(&window).is_err() {
        return;
    }
    if let Some(tray_state) = app.try_state::<TrayState>() {
        if let Ok(mut text) = tray_state.0.lock() {
            text.tapping = on;
        }
    }
    if let Some(item) = app.try_state::<TapItem>() {
        let _ = item.0.set_checked(on);
    }
    redraw_tooltip(&app);
}

/// Open the site the user is saving a login for, in its own private window. Every
/// page it passes through is reported to the panel (`signin-navigated`),
/// so the login's domains are the ones the sign-in really used -- a school's
/// portal goes portal.example.edu, its single sign-on, then the course site
/// on another domain. What the user types into sign-in forms there is noted
/// (`capture_script`), and closing the window when they are done hands
/// everything to the panel to save
/// (`signin-finished`): nothing to type twice, nothing to press.
#[tauri::command]
async fn open_signin(app: tauri::AppHandle, window: tauri::Window, url: String) -> Result<(), String> {
    main_only(&window)?;
    let url = Url::parse(url.trim()).map_err(|err| format!("not an address: {err}"))?;
    let host = url.host_str().unwrap_or_default().to_string();
    if url.scheme() != "https" || !host.contains('.') {
        return Err("a sign-in page is an https address, like https://portal.example.com".into());
    }
    if let Some(old) = app.get_webview_window(SIGNIN) {
        let _ = old.destroy();
    }
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let key = format!("{:x}{:x}", nanos, std::process::id());
    if let Ok(mut typed) = app.state::<SigninState>().0.lock() {
        typed.key = key.clone();
        typed.by_host.clear();
    }
    let seen = app.clone();
    let loaded = app.clone();
    let signin = WebviewWindowBuilder::new(&app, SIGNIN, WebviewUrl::External(url))
        .title(format!("Sign in: {host} (close this window when you are signed in)"))
        .inner_size(1040.0, 800.0)
        // Private: nothing of their other windows in it, nothing left on disk.
        .incognito(true)
        .initialization_script(capture_script(&key))
        .on_navigation(move |to: &Url| {
            let web = matches!(to.scheme(), "https" | "http");
            if web {
                let _ = seen.emit_to("main", "signin-navigated", to.as_str().to_string());
            }
            web || to.scheme() == "about"
        })
        .on_page_load(move |_, payload| {
            if matches!(payload.event(), PageLoadEvent::Finished) {
                let _ = loaded.emit_to("main", "signin-page", payload.url().as_str().to_string());
            }
        })
        .build()
        .map_err(|err| format!("could not open the sign-in window: {err}"))?;
    let events = app.clone();
    let this = signin.clone();
    signin.on_window_event(move |event| match event {
        // Their closing it is "done": read what it holds -- on a thread of its
        // own, WebView2 deadlocks reading cookies in an event handler --
        // hand that to the panel, and only then let it go.
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            let app = events.clone();
            let signin = this.clone();
            std::thread::spawn(move || {
                let finished = collect(&app, &signin).unwrap_or_else(|err| serde_json::json!({ "error": err }));
                let _ = app.emit_to("main", "signin-finished", finished);
                let _ = signin.destroy();
                focus_main(&app);
            });
        }
        WindowEvent::Destroyed => {
            forget_typing(&events);
            let _ = events.emit_to("main", "signin-closed", ());
        }
        _ => {}
    });
    watch_typing(app.clone(), key);
    Ok(())
}

/// What the sign-in window holds now, for saving without closing it.
#[tauri::command]
async fn signin_cookies(app: tauri::AppHandle, window: tauri::Window) -> Result<serde_json::Value, String> {
    main_only(&window)?;
    let signin = app
        .get_webview_window(SIGNIN)
        .ok_or("the sign-in window is closed: open it and sign in first")?;
    // Off the main thread: WebView2 deadlocks reading cookies on it.
    tauri::async_runtime::spawn_blocking(move || collect(&app, &signin))
        .await
        .map_err(|err| err.to_string())?
}

/// Who the user has typed in as so far, by site -- never the password itself --
/// so the panel can say what it will keep.
#[tauri::command]
fn signin_typed(app: tauri::AppHandle, window: tauri::Window) -> Vec<serde_json::Value> {
    if main_only(&window).is_err() {
        return Vec::new();
    }
    app.state::<SigninState>()
        .0
        .lock()
        .map(|t| {
            t.by_host
                .iter()
                .map(|(host, (u, p))| {
                    serde_json::json!({ "host": host, "username": u, "has_password": !p.is_empty() })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Close the sign-in window without saving -- Cancel. Being private, it
/// takes its cookies with it; what the user typed is forgotten too.
#[tauri::command]
fn close_signin(app: tauri::AppHandle, window: tauri::Window) {
    if main_only(&window).is_ok() {
        forget_typing(&app);
        if let Some(signin) = app.get_webview_window(SIGNIN) {
            let _ = signin.destroy();
        }
    }
}

/// The longest link `open_link` hands on: a page's address, not a payload.
const LINK_MAX: usize = 2048;

/// A page the backend names -- a setup guide's step, the page that fixes an
/// account -- opened in the user's own browser, where they are signed in
/// and can see the address. Followed inside the app's window, it would
/// replace the app with a site that has the app's IPC in reach; the window
/// refuses that (`app_pages_only`), and the sign-in window is for logins the
/// vault keeps (`open_signin`), not for reading a help page. A sync command,
/// so it runs on the main thread, whose COM the shell's handlers need.
#[tauri::command]
fn open_link(window: tauri::Window, url: String) -> Result<(), String> {
    main_only(&window)?;
    let url = link_to_open(&url)?;
    shell_open(url.as_str())
}

/// The link as the shell will get it, or why not. Only an https page: a
/// `file:` or `ms-settings:` link, or a program's path, would have Windows
/// run something rather than show a page. The parser's own spelling is what
/// is passed on, percent-encoded, so no space or quote can split the
/// browser's command line.
fn link_to_open(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw.trim()).map_err(|err| format!("not an address: {err}"))?;
    if url.scheme() != "https" {
        return Err("only an https page opens from the app".into());
    }
    if url.host_str().map_or(true, str::is_empty) {
        return Err("that address names no site".into());
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("an address with a name or password in it is not opened".into());
    }
    if url.as_str().len() > LINK_MAX {
        return Err(format!("that address is longer than {LINK_MAX} characters"));
    }
    Ok(url)
}

/// Hand a page to Windows, which opens it in the user's default browser.
fn shell_open(url: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    let wide = |text: &str| text.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let (verb, file) = (wide("open"), wide(url));
    // SAFETY: both strings end in a NUL and outlive the call; the window,
    // parameters and directory may be null.
    let code = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            file.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    } as isize;
    // Above 32 is success; anything else is the reason (ShellExecuteW's documentation).
    if code > 32 {
        Ok(())
    } else {
        Err(format!("Windows could not open the page (error {code})"))
    }
}

/// Whether the app's own windows may go to `to`: the app's pages only. The
/// built app is served at http://tauri.localhost on Windows (https when asked
/// for, tauri://localhost elsewhere); `tauri dev`, from the dev server.
fn app_page(to: &Url, dev: Option<&Url>) -> bool {
    let built = match to.scheme() {
        "http" | "https" => to.host_str() == Some("tauri.localhost"),
        "tauri" => to.host_str() == Some("localhost"),
        _ => false,
    };
    built || dev.is_some_and(|dev| to.origin() == dev.origin())
}

/// "main" and "quickbar" never leave the app's pages. A link followed in
/// them -- an `<a href>`, a page setting `location` -- would load a site
/// there, as the app, with its IPC and the backend's token in reach; links
/// go to the user's browser instead (`open_link`). A plugin's hook, because
/// those windows come from tauri.conf.json, not a builder whose
/// `on_navigation` could be set. The sign-in window keeps its own rule.
fn app_pages_only<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri::plugin::Builder::new("app-pages-only")
        .on_navigation(|webview, to| {
            if !matches!(webview.label(), "main" | "quickbar" | deskcard::LABEL) {
                return true;
            }
            let dev = if tauri::is_dev() { webview.config().build.dev_url.as_ref() } else { None };
            let ours = app_page(to, dev);
            if !ours {
                // The origin only: the rest of an address can carry a token.
                log::warn!("kept the {} window from leaving for {}", webview.label(),
                           to.origin().ascii_serialization());
            }
            ours
        })
        .build()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let args: Vec<String> = std::env::args().collect();
    if let Some(code) = forget_hello(&args, hello::forget) {
        std::process::exit(code);
    }
    if let Some(code) = clear_notifications(&args, || {
        toast::clear(&toast::app_id(IDENTIFIER)).map_err(|e| e.to_string())
    }) {
        std::process::exit(code);
    }
    let mut builder = tauri::Builder::default();
    // First: a second launch -- the Start menu while the login one sits
    // in the tray -- brings that one forward instead of a second icon. Not
    // for an app given a data folder of its own (`STEVE_DATA_DIR`, as a
    // test's is): the plugin is keyed by the app's identifier, so that app
    // would hand itself to whichever Steve is running, and exit.
    //
    // Nor is starting with Windows: the Run key holds one value, "Steve",
    // so such an app turning it on would point the user's own Steve's
    // login at itself, and turning it off would delete theirs. A test of
    // the real app answering setup's "No" did just that. Without the
    // plugin the app says it can't (desktop.ts `startsAtLogin` is null).
    if one_per_identifier(std::env::var_os("STEVE_DATA_DIR")) {
        builder = builder
            .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
                focus_main(app);
            }))
            .plugin(tauri_plugin_autostart::init(
                tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                Some(vec![HIDDEN]),
            ));
    }
    builder
        // In the data folder, with everything the backend writes, not in
        // Windows' folder for the app's identifier: a test's app wrote its
        // lines into the developer's own log there (step 9's spikes).
        .plugin(
            tauri_plugin_log::Builder::new()
                .targets([
                    Target::new(TargetKind::Stdout),
                    Target::new(TargetKind::Folder {
                        path: data_dir().join("logs"),
                        file_name: Some("app".into()),
                    }),
                ])
                .build(),
        )
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        // Its own commands are not allowed to the window (capabilities/):
        // updates go through `update.rs`, which stops the backend first.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(app_pages_only())
        .manage(SigninState::default())
        .manage(TrayState::default())
        .manage(toast::Shown::default())
        .manage(update::Pending::default())
        .invoke_handler(tauri::generate_handler![
            backend_control,
            backend,
            update::update_check,
            update::update_install,
            set_waiting,
            notify,
            set_tapping,
            show_quickbar,
            quickbar_talk,
            quickbar_open_app,
            deskcard::deskcard_want,
            deskcard::deskcard_size,
            deskcard::deskcard_drag,
            deskcard::deskcard_hide,
            deskcard::deskcard_showing,
            deskcard_open,
            deskcard_quickbar,
            deskcard_talk,
            deskcard_menu,
            signin::session_get,
            signin::session_keep,
            signin::session_end,
            signin::hello_available,
            signin::hello_enrol,
            signin::hello_sign,
            open_signin,
            signin_cookies,
            signin_typed,
            close_signin,
            open_link,
            caption::set_caption,
            quit
        ])
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "Open Steve", true, None::<&str>)?;
            // Talk without holding a key: the quick bar listens until Enter
            // or Esc (QuickBar.tsx).
            let talk_item = MenuItem::with_id(app, "talk", "Talk", true, None::<&str>)?;
            // Notes of a call or a video, from the laptop's output: the
            // window switches it too, told by `tap-toggle`.
            let tap = CheckMenuItem::with_id(
                app, "tap", "Take notes on what\u{2019}s playing", true, false, None::<&str>,
            )?;
            // Back from "Hide", or turned on again (presence.md, The tray).
            let card = MenuItem::with_id(app, "deskcard", "Show the desk card", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &talk_item, &card, &tap, &quit])?;
            app.manage(TapItem(tap.clone()));

            TrayIconBuilder::with_id("main")
                .icon(tray_icon(app.handle()).ok_or("the tray's icon did not decode")?)
                .tooltip("Steve")
                .menu(&menu)
                // The menu is for the right button; a left click should just
                // open the window, which is what people actually expect.
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => focus_main(app),
                    "talk" => {
                        let _ = talk(app, true);
                    }
                    "tap" => {
                        let _ = app.emit("tap-toggle", ());
                    }
                    "deskcard" => deskcard::show_again(app),
                    "quit" => quit_app(app),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        focus_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // Start at login is the user's answer, on setup's page
            // (ui/src/Setup.tsx). It was turned on here at the first run,
            // with a marker so it happened once: a change to Windows' start-up
            // that no one was asked about.

            // The backend: started now, watched while the app runs, ended
            // with it. Started off this thread, since a backend already
            // running is asked to prove itself first.
            let launch = backend::Launch::find(
                &app_dir(),
                repo_root().as_deref(),
                std::env::var_os("STEVE_PYTHON").map(PathBuf::from),
            );
            let owned = backend::Backend::new(launch, data_dir())?;
            app.manage(owned.clone());
            let handle = app.handle().clone();
            owned.clone().watch(move |lines| {
                log::error!("the backend keeps stopping; the app has stopped starting it");
                backend_stopped(&handle);
                let _ = handle.emit("backend-failing", lines);
            });
            // An install's files are checked against the list its build
            // signed first (`integrity.rs`); a developer's build has none.
            let pubkey = update_key(&app.config());
            let version = app.package_info().version.to_string();
            let handle = app.handle().clone();
            std::thread::spawn(move || match backend::bundled(&app_dir()) {
                None if backend::checkout(&app_dir(), repo_root().as_deref()).is_some() => {
                    owned.start()
                }
                None => {
                    let why = backend::missing(&app_dir());
                    log::error!("Steve's files are damaged: {why}");
                    owned.refuse(why.clone());
                    backend_stopped(&handle);
                    let _ = handle.emit("backend-damaged", why);
                }
                Some(_) => integrity::start_checked(
                    &app_dir(),
                    pubkey.as_deref(),
                    &version,
                    &owned,
                    |why| {
                        backend_stopped(&handle);
                        let _ = handle.emit("backend-damaged", why);
                    },
                ),
            });

            // At the size the user left it, before it first shows.
            let keeper = windowsize::Keeper::new(&data_dir());
            if let Some(main) = app.get_webview_window("main") {
                windowsize::restore(&main, &keeper);
            }
            app.manage(keeper);

            // The desk card: hidden until its page says setup is done and
            // the user wants it, then shown while the screen is free.
            app.manage(deskcard::Card::new(&data_dir()));
            deskcard::watch(app.handle().clone());
            app.manage(signin::Session::default());
            signin::watch(app.handle().clone());
            app.on_menu_event(|app, event| deskcard_menu_event(app, event.id.as_ref()));

            // The window starts hidden; opened from the Start menu it shows,
            // started by Windows at login it stays in the tray.
            if !std::env::args().any(|arg| arg == HIDDEN) {
                focus_main(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides it rather than exiting. A personal
            // agent that dies when you hit X cannot answer you later, and the
            // tray icon is the only remaining affordance to bring it back.
            // The main window only: a sign-in window closes for real.
            if window.label() != "main" {
                return;
            }
            match event {
                WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                }
                // Windows switched between light and dark, or the display's
                // scale changed: the taskbar's icon may need another picture.
                WindowEvent::ThemeChanged(_) | WindowEvent::ScaleFactorChanged { .. } => {
                    redraw_icon(window.app_handle());
                }
                WindowEvent::Resized(_) => windowsize::resized(window),
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Steve");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Started again from the window, the backend runs, and a tray still
    /// saying "Steve has stopped" sent the user looking for a fault that
    /// was gone.
    #[test]
    fn the_tray_says_stopped_only_while_the_backend_is_failing_or_damaged() {
        let mut text = TrayText::default();
        assert!(text.backend_is(true), "the watcher's stop is drawn");
        assert!(text.stopped);
        assert!(!text.backend_is(true), "nothing to redraw");
        assert!(text.backend_is(false), "a start that brought it back is drawn");
        assert!(!text.stopped);
    }

    /// A test's app, on a data folder of its own, beside a Steve the
    /// developer is running: the plugin would have handed it over.
    #[test]
    fn only_the_app_on_the_usual_data_folder_hands_a_second_launch_to_the_first() {
        assert!(one_per_identifier(None));
        assert!(one_per_identifier(Some("".into())));
        assert!(!one_per_identifier(Some(r"C:\tests\data".into())));
    }

    /// RFC 4231, test case 2.
    #[test]
    fn the_hmac_is_rfc_4231s() {
        assert_eq!(
            hex::encode(hmac_sha256(b"Jefe", b"what do ya want for nothing?")),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    /// The backend's own answer for the same inputs: core/localapi.proof,
    /// pinned to the same value in tests/gate/test_local_token.py.
    #[test]
    fn the_proof_is_the_one_the_backend_computes() {
        let nonce = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";
        assert_eq!(
            proof(&"a".repeat(64), nonce, 51234),
            "90d161ef95c0e7706f8794584db2e69d7d0e0658fbb8af07662371a0b9439725"
        );
    }

    #[test]
    fn only_the_same_bytes_are_the_same() {
        assert!(same(b"abc", b"abc"));
        assert!(!same(b"abc", b"abd"));
        assert!(!same(b"abc", b"ab"));
    }

    /// The shell runs whatever it is given: a file or a settings page would
    /// be opened as readily as a help page.
    #[test]
    fn only_an_https_page_with_a_site_and_no_password_is_opened() {
        assert_eq!(
            link_to_open(" https://example.com/help?step=2#here ").unwrap().as_str(),
            "https://example.com/help?step=2#here"
        );
        for refused in [
            "http://example.com/",
            "file:///C:/Windows/System32/calc.exe",
            "ms-settings:privacy",
            "javascript:alert(1)",
            "C:\\Windows\\notepad.exe",
            "https://test:secret@example.com/",
            "https://test@example.com/",
            "https://",
            "",
        ] {
            assert!(link_to_open(refused).is_err(), "{refused} was let through");
        }
        let longest = format!("https://example.com/{}", "a".repeat(LINK_MAX - 20));
        assert_eq!(longest.len(), LINK_MAX);
        assert!(link_to_open(&longest).is_ok());
        assert!(link_to_open(&format!("{longest}a")).is_err());
    }

    /// A site loaded in the app's window would run as the app.
    #[test]
    fn the_app_s_windows_stay_on_the_app_s_own_pages() {
        let dev = Url::parse("http://localhost:5173").unwrap();
        let url = |text: &str| Url::parse(text).unwrap();
        for ours in ["http://tauri.localhost/", "http://tauri.localhost/index.html#quickbar",
                     "https://tauri.localhost/", "tauri://localhost/"] {
            assert!(app_page(&url(ours), None), "{ours} was refused");
        }
        assert!(app_page(&url("http://localhost:5173/src/main.tsx"), Some(&dev)));
        for theirs in ["https://example.com/", "http://tauri.localhost.example.com/",
                       "http://127.0.0.1:51234/", "http://localhost:5173/", "about:blank"] {
            assert!(!app_page(&url(theirs), None), "{theirs} was let in");
        }
        assert!(!app_page(&url("http://localhost:5174/"), Some(&dev)));
    }

    /// A release installed on the machine that built it: its baked-in
    /// checkout is still there, with a `pyproject.toml`.
    fn install_beside_a_checkout(name: &str) -> (PathBuf, PathBuf) {
        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-scratch")
            .join(name);
        let _ = std::fs::remove_dir_all(&base);
        let (app, checkout) = (base.join("Programs").join("Steve"), base.join("checkout"));
        std::fs::create_dir_all(app.join("backend").join("app")).unwrap();
        std::fs::create_dir_all(&checkout).unwrap();
        std::fs::write(checkout.join("pyproject.toml"), "").unwrap();
        (app, checkout)
    }

    #[test]
    fn an_installs_data_is_in_local_app_data_though_the_checkout_it_was_built_from_is_there() {
        let (app, checkout) = install_beside_a_checkout("data-installed");
        std::fs::write(app.join("backend").join("app").join("installed.json"), "{}").unwrap();
        let local = checkout.parent().unwrap().join("Local");

        let chosen = data_dir_for(None, &app, Some(&checkout), Some(local.clone().into()));

        assert_eq!(chosen, local.join("Steve"));
    }

    /// Its data stays where the install keeps it, for Repair to find; it
    /// went into the checkout of the machine that built it.
    #[test]
    fn an_install_that_lost_its_marker_still_keeps_its_data_in_local_app_data() {
        let (app, checkout) = install_beside_a_checkout("data-unmarked");
        std::fs::write(app.join("uninstall.exe"), "").unwrap();
        let local = checkout.parent().unwrap().join("Local");

        let chosen = data_dir_for(None, &app, Some(&checkout), Some(local.clone().into()));

        assert_eq!(chosen, local.join("Steve"));
    }

    /// Uninstall reads the exit code: a key that could not be deleted is
    /// reported, and only a start with the flag skips the app.
    #[test]
    fn started_to_forget_a_hello_key_the_app_forgets_that_key_and_says_how_it_went() {
        let args = |list: &[&str]| list.iter().map(|a| a.to_string()).collect::<Vec<_>>();
        let asked = std::cell::RefCell::new(Vec::new());
        let gone = |name: &str| {
            asked.borrow_mut().push(name.to_string());
            Ok(true)
        };
        assert_eq!(forget_hello(&args(&["Steve.exe", HIDDEN]), gone), None);
        assert_eq!(forget_hello(&args(&["Steve.exe", FORGET_HELLO, "steve.ab12cd34.hello"]), gone),
                   Some(0));
        assert_eq!(*asked.borrow(), ["steve.ab12cd34.hello"]);
        assert_eq!(forget_hello(&args(&["Steve.exe", FORGET_HELLO, "x"]), |_| Err("no".into())),
                   Some(1));
        assert_eq!(forget_hello(&args(&["Steve.exe", FORGET_HELLO]), gone), Some(2));
    }

    #[test]
    fn uninstall_clears_the_notifications_and_says_when_it_could_not() {
        let args = |list: &[&str]| list.iter().map(|a| a.to_string()).collect::<Vec<_>>();
        let asked = std::cell::Cell::new(0);
        let cleared = || {
            asked.set(asked.get() + 1);
            Ok(())
        };
        assert_eq!(clear_notifications(&args(&["Steve.exe", HIDDEN]), cleared), None);
        assert_eq!(asked.get(), 0);
        assert_eq!(clear_notifications(&args(&["Steve.exe", CLEAR_NOTIFICATIONS]), cleared),
                   Some(0));
        assert_eq!(asked.get(), 1);
        assert_eq!(clear_notifications(&args(&["Steve.exe", CLEAR_NOTIFICATIONS]),
                                       || Err("no".into())), Some(1));
    }

    #[test]
    fn the_identifier_read_before_the_config_is_the_configs() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(config["identifier"], IDENTIFIER);
    }

    /// A release build away from its install, on the machine that built
    /// it: it names no checkout, so its data is never kept in one.
    #[test]
    fn a_build_that_names_no_checkout_keeps_its_data_in_local_app_data() {
        let (app, checkout) = install_beside_a_checkout("data-unnamed");
        let local = checkout.parent().unwrap().join("Local");

        assert_eq!(data_dir_for(None, &app, None, Some(local.clone().into())), local.join("Steve"));
    }

    #[test]
    fn a_developers_build_keeps_its_data_in_its_checkout() {
        let (app, checkout) = install_beside_a_checkout("data-checkout");

        assert_eq!(data_dir_for(None, &app, Some(&checkout), None), checkout.join("data"));
    }

    #[test]
    fn a_chosen_data_folder_wins_in_an_install_too() {
        let (app, checkout) = install_beside_a_checkout("data-chosen");
        std::fs::write(app.join("backend").join("app").join("installed.json"), "{}").unwrap();
        let chosen = checkout.parent().unwrap().join("chosen");

        assert_eq!(data_dir_for(Some(chosen.clone().into()), &app, Some(&checkout), None), chosen);
    }
}
