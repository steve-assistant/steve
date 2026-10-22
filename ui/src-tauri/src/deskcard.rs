//! The desk card's window (docs/design/presence.md, The desk card): where
//! it sits, and when it shows.
//!
//! - **Showing never takes the focus.** A card that opened on its own for a
//!   note while the user typed a mail would take their next keys. After a
//!   window's first showing, Tauri shows it with SW_SHOW, which activates
//!   it; so the card is shown and hidden here, by its handle, with
//!   SW_SHOWNOACTIVATE, and its window is unfocusable (tauri.conf.json).
//!   A click may take the focus: what a click opens takes it anyway.
//! - **It grows away from its corner.** Grown as a window grows, from its
//!   top-left, a card sitting in the screen's bottom-right corner would
//!   open off the screen. Its place is the corner of the screen it is
//!   nearest and its distance in from there, so the tile stays put and the
//!   card opens towards the middle.
//! - **Its place is kept per monitor,** as that distance in from the
//!   monitor's work area. A point on the whole desktop would land off every
//!   screen once the monitor it was on is unplugged.
//! - **It hides when it shouldn't show:** while Windows says the user is
//!   busy (a full-screen app, a game, a presentation) or the session is
//!   locked, and for the time the user chose. Asked every two seconds:
//!   Windows sends no event when a full-screen app starts.
//!
//! `deskcard.json` in the data folder keeps the places and the hiding, as
//! `window.json` keeps the main window's size (windowsize.rs).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager};

/// The file's shape: every settings file says which (core/settingsfile.py).
const VERSION: u32 = 1;
/// The design's distance in from the screen's edges, to the tile.
const MARGIN: f64 = 16.0;
/// The window's transparent edge around the tile and card, where their
/// shadow and the note's edge are drawn (deskcard.css `--desk-pad`).
const PAD: f64 = 16.0;
/// Let go this close to an edge, the card goes to the edge's MARGIN.
const SNAP: f64 = 48.0;
/// How often Windows is asked whether the user is busy, or locked.
const POLL: Duration = Duration::from_secs(2);
pub(crate) const LABEL: &str = "deskcard";

#[derive(Serialize, Deserialize, Debug, PartialEq, Clone, Copy)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum Corner {
    TopLeft,
    TopRight,
    BottomLeft,
    BottomRight,
}

impl Corner {
    fn of(right: bool, bottom: bool) -> Corner {
        match (right, bottom) {
            (false, false) => Corner::TopLeft,
            (true, false) => Corner::TopRight,
            (false, true) => Corner::BottomLeft,
            (true, true) => Corner::BottomRight,
        }
    }

    fn right(self) -> bool {
        matches!(self, Corner::TopRight | Corner::BottomRight)
    }

    fn bottom(self) -> bool {
        matches!(self, Corner::BottomLeft | Corner::BottomRight)
    }
}

/// Where the window sits on one monitor: the screen's corner it is
/// nearest, and its distance in from that corner's two edges, in logical
/// pixels, so it means the same at another scale.
#[derive(Serialize, Deserialize, Debug, PartialEq, Clone, Copy)]
pub(crate) struct Place {
    corner: Corner,
    dx: f64,
    dy: f64,
}

/// Where it sits until it is moved: the bottom-right, by the tray.
pub(crate) const FIRST: Place = Place { corner: Corner::BottomRight, dx: MARGIN - PAD, dy: MARGIN - PAD };

/// A rectangle in logical pixels: a monitor's work area, or the window.
#[derive(Debug, PartialEq, Clone, Copy)]
pub(crate) struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Clone, Default)]
pub(crate) struct Kept {
    version: u32,
    /// By the monitor's name ("\\.\DISPLAY1").
    #[serde(default)]
    places: BTreeMap<String, Place>,
    /// The monitor it was last put on, to go back to while it is there.
    #[serde(default)]
    last: Option<String>,
    /// Hidden until then (milliseconds since 1970), by its menu's Hide.
    #[serde(default)]
    hidden_until: Option<u64>,
}

/// The place the window is let go at: the corner its middle is nearest,
/// and a distance under SNAP (or off the screen) made the edge's.
pub(crate) fn place_of(window: Rect, area: Rect) -> Place {
    let right = window.x + window.w / 2.0 > area.x + area.w / 2.0;
    let bottom = window.y + window.h / 2.0 > area.y + area.h / 2.0;
    let dx = if right { area.x + area.w - (window.x + window.w) } else { window.x - area.x };
    let dy = if bottom { area.y + area.h - (window.y + window.h) } else { window.y - area.y };
    let snap = |d: f64| if d < SNAP { MARGIN - PAD } else { d };
    Place { corner: Corner::of(right, bottom), dx: snap(dx), dy: snap(dy) }
}

/// The window at a place and size: its corner held, so it grows towards
/// the middle, and kept on the screen, whatever screen the place was
/// kept from.
pub(crate) fn rect_at(place: Place, w: f64, h: f64, area: Rect) -> Rect {
    let x = if place.corner.right() { area.x + area.w - place.dx - w } else { area.x + place.dx };
    let y = if place.corner.bottom() { area.y + area.h - place.dy - h } else { area.y + place.dy };
    let fit = |v: f64, start: f64, room: f64, size: f64| v.min(start + room - size).max(start);
    Rect { x: fit(x, area.x, area.w, w), y: fit(y, area.y, area.h, h), w, h }
}

/// Whether the card shows: wanted (setup done and the setting on), and
/// not hidden by the user, a busy screen or the lock.
pub(crate) fn shown(want: bool, now_ms: u64, hidden_until: Option<u64>, busy: bool, locked: bool) -> bool {
    want && !busy && !locked && hidden_until.is_none_or(|until| now_ms >= until)
}

/// Windows' notification state, as the card reads it: not present (a
/// screen saver, a hung full-screen app), busy (a full-screen app), a
/// game, or presentation mode all hide it. Quiet hours and Focus don't:
/// they hold back toasts, and the card is the quiet way to see things.
pub(crate) fn busy(state: i32) -> bool {
    use windows_sys::Win32::UI::Shell::{
        QUNS_BUSY, QUNS_NOT_PRESENT, QUNS_PRESENTATION_MODE, QUNS_RUNNING_D3D_FULL_SCREEN,
    };
    matches!(state, QUNS_NOT_PRESENT | QUNS_BUSY | QUNS_RUNNING_D3D_FULL_SCREEN | QUNS_PRESENTATION_MODE)
}

fn windows_busy() -> bool {
    use windows_sys::Win32::UI::Shell::SHQueryUserNotificationState;
    let mut state = 0;
    // SAFETY: one out-parameter, written by the call.
    let asked = unsafe { SHQueryUserNotificationState(&mut state) };
    asked == 0 && busy(state)
}

pub(crate) fn path(data: &Path) -> PathBuf {
    data.join("deskcard.json")
}

/// What was kept, or nothing: no file, or one from another version. A
/// card's place isn't worth keeping through a downgrade.
pub(crate) fn read(file: &Path) -> Kept {
    std::fs::read(file)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Kept>(&bytes).ok())
        .filter(|kept| kept.version == VERSION)
        .unwrap_or(Kept { version: VERSION, ..Kept::default() })
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64)
}

/// A drag: where the pointer and the window were when it began.
struct Drag {
    pointer: (f64, f64),
    window: (i32, i32),
}

struct State {
    kept: Kept,
    /// Setup is done and the user wants the card (its page says).
    want: bool,
    showing: bool,
    /// The page's size, logical, padding included.
    size: (f64, f64),
    drag: Option<Drag>,
}

/// The card's state, managed by the app.
pub(crate) struct Card {
    file: PathBuf,
    state: Mutex<State>,
}

impl Card {
    pub(crate) fn new(data: &Path) -> Self {
        let file = path(data);
        let kept = read(&file);
        Card {
            file,
            state: Mutex::new(State { kept, want: false, showing: false, size: (88.0, 88.0), drag: None }),
        }
    }

    fn save(&self, kept: &Kept) {
        if let Err(err) = crate::windowsize::write(&self.file, kept) {
            log::warn!("couldn't keep the desk card's place in {}: {err}", self.file.display());
        }
    }
}

/// Every POLL, for as long as the app runs: shown or hidden as it should
/// be. Started once, at setup.
pub(crate) fn watch(app: tauri::AppHandle) {
    std::thread::spawn(move || loop {
        apply(&app);
        std::thread::sleep(POLL);
    });
}

/// Shown or hidden now, as it should be. The flip is decided under the
/// lock, so two callers never both flip; and done on the main thread with
/// the lock let go, since showing a window waits on that thread.
pub(crate) fn apply(app: &tauri::AppHandle) {
    let Some(card) = app.try_state::<Card>() else { return };
    let Some(window) = app.get_webview_window(LABEL) else { return };
    let visible = window.hwnd().is_ok_and(|hwnd| {
        use windows_sys::Win32::UI::WindowsAndMessaging::IsWindowVisible;
        // SAFETY: reads the live window's style, which any thread may.
        unsafe { IsWindowVisible(hwnd.0 as _) != 0 }
    });
    let on = {
        let mut state = card.state.lock().unwrap_or_else(|e| e.into_inner());
        let on = shown(state.want, now_ms(), state.kept.hidden_until, windows_busy(), crate::toast::locked());
        if settled(on, state.showing, visible) {
            return;
        }
        state.showing = on;
        on
    };
    let _ = app.run_on_main_thread(move || {
        use windows_sys::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_HIDE, SW_SHOWNOACTIVATE};
        let Ok(hwnd) = window.hwnd() else { return };
        // SAFETY: the handle is this live window's, on its own thread.
        unsafe { ShowWindow(hwnd.0 as _, if on { SW_SHOWNOACTIVATE } else { SW_HIDE }) };
    });
    let _ = app.emit_to(LABEL, "deskcard-showing", on);
}

/// Whether the card is as it should be: last shown or hidden so, and so on
/// the screen. Not by the last flip alone: the card is shown by a raw
/// ShowWindow, so as never to take the focus, and tao never hears of it.
/// At tao's next change to the window (its page turning "on top" on or
/// off) tao rewrote the window's style from its own record and hid it
/// (window_state.rs `apply_diff`), and with the flip still on, nothing
/// showed it again until the app restarted.
fn settled(on: bool, showing: bool, visible: bool) -> bool {
    on == showing && on == visible
}

/// Whether the card is on the screen now: while it is, updates and
/// results make no toast (App.tsx).
pub(crate) fn showing(app: &tauri::AppHandle) -> bool {
    app.try_state::<Card>()
        .is_some_and(|card| card.state.lock().unwrap_or_else(|e| e.into_inner()).showing)
}

fn card_only(window: &tauri::Window) -> Result<(), String> {
    if window.label() == LABEL { Ok(()) } else { Err("for the desk card only".into()) }
}

/// The monitor the card belongs on, and its work area in logical pixels:
/// the one it was last put on while that is plugged in, else the main one.
fn monitor(window: &tauri::WebviewWindow, kept: &Kept) -> Option<(String, Rect, f64)> {
    let monitors = window.available_monitors().ok()?;
    let named = |m: &tauri::Monitor| m.name().cloned().unwrap_or_default();
    let chosen = kept
        .last
        .as_ref()
        .and_then(|last| monitors.iter().find(|m| &named(m) == last).cloned())
        .or_else(|| window.primary_monitor().ok().flatten())
        .or_else(|| monitors.first().cloned())?;
    Some((named(&chosen), area_of(&chosen), chosen.scale_factor()))
}

fn area_of(monitor: &tauri::Monitor) -> Rect {
    let scale = monitor.scale_factor();
    let area = monitor.work_area();
    Rect {
        x: area.position.x as f64 / scale,
        y: area.position.y as f64 / scale,
        w: area.size.width as f64 / scale,
        h: area.size.height as f64 / scale,
    }
}

/// The window put at its place on its monitor, at the page's size, in one
/// move. Moved and then sized, as two calls, a card in the bottom-right
/// corner opening on a hover first jumped up and left at its small size,
/// away from the pointer: the pointer left, the card closed, the window
/// shrank back under the pointer, and the card opened again, over and over
/// (a review of the running app, 18 October 2026).
fn place(window: &tauri::WebviewWindow, state: &State) -> Option<Corner> {
    let (name, area, scale) = monitor(window, &state.kept)?;
    let at = state.kept.places.get(&name).copied().unwrap_or(FIRST);
    let rect = rect_at(at, state.size.0, state.size.1, area);
    let px = |v: f64| (v * scale).round() as i32;
    let (x, y, w, h) = (px(rect.x), px(rect.y), px(rect.w), px(rect.h));
    use windows_sys::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_NOACTIVATE, SWP_NOZORDER};
    if let Ok(hwnd) = window.hwnd() {
        // SAFETY: the handle is this live window's; SetWindowPos from another
        // thread waits for the window's own thread to make the move.
        unsafe { SetWindowPos(hwnd.0 as _, std::ptr::null_mut(), x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE) };
    }
    Some(at.corner)
}

/// The card's page says whether it is wanted: setup done and the setting
/// on (`desk_card`), and on top of other windows or not
/// (`desk_card_on_top`).
#[tauri::command]
pub(crate) fn deskcard_want(app: tauri::AppHandle, window: tauri::Window, on: bool, on_top: bool)
    -> Result<(), String> {
    card_only(&window)?;
    let card = app.try_state::<Card>().ok_or("no desk card")?;
    card.state.lock().unwrap_or_else(|e| e.into_inner()).want = on;
    let _ = window.set_always_on_top(on_top);
    apply(&app);
    Ok(())
}

/// The page's size changed (the tile opened into the card, or a line
/// came): the window takes it, its corner held. Says which corner, so the
/// page lays the tile out in it.
#[tauri::command]
pub(crate) fn deskcard_size(app: tauri::AppHandle, window: tauri::Window, width: f64, height: f64)
    -> Result<Option<Corner>, String> {
    card_only(&window)?;
    let card = app.try_state::<Card>().ok_or("no desk card")?;
    let webview = app.get_webview_window(LABEL).ok_or("no desk card window")?;
    let mut state = card.state.lock().unwrap_or_else(|e| e.into_inner());
    if !(width.is_finite() && height.is_finite() && width > 0.0 && height > 0.0) {
        return Err("not a size".into());
    }
    state.size = (width.min(480.0), height.min(320.0));
    if state.drag.is_some() {
        return Ok(None); // put at its place when the drag ends
    }
    Ok(place(&webview, &state))
}

/// A drag of the card, the page's pointer events driving it: "start",
/// "move" while the button is down, "end" on its release. The window
/// follows the pointer as Windows reports it, not the page's own
/// coordinates, which move with the window. Let go, it snaps and is kept
/// for that monitor.
#[tauri::command]
pub(crate) fn deskcard_drag(app: tauri::AppHandle, window: tauri::Window, phase: String)
    -> Result<Option<Corner>, String> {
    card_only(&window)?;
    let card = app.try_state::<Card>().ok_or("no desk card")?;
    let webview = app.get_webview_window(LABEL).ok_or("no desk card window")?;
    let pointer = app.cursor_position().map_err(|e| e.to_string())?;
    let mut state = card.state.lock().unwrap_or_else(|e| e.into_inner());
    match phase.as_str() {
        "start" => {
            let at = webview.outer_position().map_err(|e| e.to_string())?;
            state.drag = Some(Drag { pointer: (pointer.x, pointer.y), window: (at.x, at.y) });
            Ok(None)
        }
        "move" => {
            if let Some(drag) = &state.drag {
                let x = drag.window.0 + (pointer.x - drag.pointer.0).round() as i32;
                let y = drag.window.1 + (pointer.y - drag.pointer.1).round() as i32;
                let _ = webview.set_position(tauri::PhysicalPosition::new(x, y));
            }
            Ok(None)
        }
        "end" => {
            if state.drag.take().is_none() {
                return Ok(None);
            }
            let Some(monitor) = webview.current_monitor().ok().flatten() else { return Ok(None) };
            let (Ok(at), Ok(size)) = (webview.outer_position(), webview.outer_size()) else {
                return Ok(None);
            };
            let scale = monitor.scale_factor();
            let rect = Rect {
                x: at.x as f64 / scale,
                y: at.y as f64 / scale,
                w: size.width as f64 / scale,
                h: size.height as f64 / scale,
            };
            let name = monitor.name().cloned().unwrap_or_default();
            let kept_at = place_of(rect, area_of(&monitor));
            state.kept.places.insert(name.clone(), kept_at);
            state.kept.last = Some(name);
            card.save(&state.kept);
            Ok(place(&webview, &state))
        }
        _ => Err("start, move or end".into()),
    }
}

/// Hidden until then (milliseconds since 1970): the page works out "an
/// hour" and "tomorrow", where the day's turn is known.
#[tauri::command]
pub(crate) fn deskcard_hide(app: tauri::AppHandle, window: tauri::Window, until: u64)
    -> Result<(), String> {
    card_only(&window)?;
    let card = app.try_state::<Card>().ok_or("no desk card")?;
    {
        let mut state = card.state.lock().unwrap_or_else(|e| e.into_inner());
        state.kept.hidden_until = Some(until);
        card.save(&state.kept);
    }
    apply(&app);
    Ok(())
}

/// The tray's "Show the desk card": no longer hidden for a while, and its
/// page told, which turns the setting on if it was off.
pub(crate) fn show_again(app: &tauri::AppHandle) {
    if let Some(card) = app.try_state::<Card>() {
        let mut state = card.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.kept.hidden_until.take().is_some() {
            card.save(&state.kept);
        }
    }
    let _ = app.emit_to(LABEL, "deskcard-show", ());
    apply(app);
}

#[tauri::command]
pub(crate) fn deskcard_showing(app: tauri::AppHandle) -> bool {
    showing(&app)
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect { x: 0.0, y: 0.0, w: 1920.0, h: 1040.0 };

    #[test]
    fn a_card_hidden_behind_its_back_is_shown_again_and_one_shown_so_is_hidden() {
        // Wanted, last shown, and on the screen: nothing to do.
        assert!(settled(true, true, true));
        assert!(settled(false, false, false));
        // Wanted and last shown, but tao hid it: shown again.
        assert!(!settled(true, true, false));
        // Not wanted and last hidden, but something showed it: hidden again.
        assert!(!settled(false, false, true));
        // A flip either way.
        assert!(!settled(true, false, false));
        assert!(!settled(false, true, true));
    }

    #[test]
    fn a_card_let_go_near_a_corner_snaps_to_it_sixteen_pixels_in() {
        // Its tile 16px in means the window, with its 16px edge, at the
        // work area's edge.
        let window = Rect { x: 1920.0 - 88.0 - 30.0, y: 1040.0 - 88.0 - 5.0, w: 88.0, h: 88.0 };
        assert_eq!(
            place_of(window, AREA),
            Place { corner: Corner::BottomRight, dx: MARGIN - PAD, dy: MARGIN - PAD }
        );
    }

    #[test]
    fn a_card_let_go_away_from_the_edges_stays_where_it_was_put() {
        let window = Rect { x: 300.0, y: 200.0, w: 88.0, h: 88.0 };
        let at = place_of(window, AREA);
        assert_eq!(at, Place { corner: Corner::TopLeft, dx: 300.0, dy: 200.0 });
        assert_eq!(rect_at(at, 88.0, 88.0, AREA), window);
    }

    #[test]
    fn a_card_dragged_partly_off_the_screen_comes_back_to_the_edge() {
        let window = Rect { x: -40.0, y: 500.0, w: 88.0, h: 88.0 };
        assert_eq!(place_of(window, AREA), Place { corner: Corner::BottomLeft, dx: 0.0, dy: 452.0 });
    }

    #[test]
    fn the_card_opens_towards_the_middle_its_tile_staying_where_it_was() {
        // In the bottom-right corner, the tile's 88 grows into the card's
        // 352 x 120: its right and bottom edges stay, so it grows up and
        // left, not off the screen.
        let tile = rect_at(FIRST, 88.0, 88.0, AREA);
        let card = rect_at(FIRST, 352.0, 120.0, AREA);
        assert_eq!((tile.x + tile.w, tile.y + tile.h), (card.x + card.w, card.y + card.h));
        assert_eq!((card.x + card.w, card.y + card.h), (1920.0, 1040.0));
        // In the top-left, it grows right and down.
        let at = Place { corner: Corner::TopLeft, dx: 0.0, dy: 0.0 };
        assert_eq!(rect_at(at, 352.0, 120.0, AREA), Rect { x: 0.0, y: 0.0, w: 352.0, h: 120.0 });
    }

    #[test]
    fn a_place_kept_from_a_bigger_screen_still_lands_on_this_one() {
        let far = Place { corner: Corner::TopLeft, dx: 2400.0, dy: 1300.0 };
        let rect = rect_at(far, 88.0, 88.0, AREA);
        assert!(rect.x + rect.w <= AREA.w && rect.y + rect.h <= AREA.h, "{rect:?}");
    }

    #[test]
    fn a_second_monitors_area_is_measured_from_its_own_corner() {
        let right = Rect { x: 1920.0, y: 0.0, w: 1280.0, h: 680.0 };
        let window = rect_at(FIRST, 88.0, 88.0, right);
        assert_eq!((window.x + window.w, window.y + window.h), (3200.0, 680.0));
        assert_eq!(place_of(window, right), FIRST);
    }

    #[test]
    fn the_card_shows_only_wanted_unhidden_and_with_the_screen_free() {
        assert!(shown(true, 1_000, None, false, false));
        assert!(!shown(false, 1_000, None, false, false), "setting off, or setup not done");
        assert!(!shown(true, 1_000, Some(2_000), false, false), "hidden for an hour");
        assert!(shown(true, 2_000, Some(2_000), false, false), "the hour is up");
        assert!(!shown(true, 1_000, None, true, false), "a full-screen app");
        assert!(!shown(true, 1_000, None, false, true), "Windows locked");
    }

    #[test]
    fn a_full_screen_app_a_game_and_a_presentation_hide_it_but_quiet_hours_dont() {
        use windows_sys::Win32::UI::Shell::{
            QUNS_ACCEPTS_NOTIFICATIONS, QUNS_APP, QUNS_BUSY, QUNS_NOT_PRESENT,
            QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME, QUNS_RUNNING_D3D_FULL_SCREEN,
        };
        for state in [QUNS_NOT_PRESENT, QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE] {
            assert!(busy(state), "{state}");
        }
        for state in [QUNS_ACCEPTS_NOTIFICATIONS, QUNS_QUIET_TIME, QUNS_APP] {
            assert!(!busy(state), "{state}");
        }
    }

    #[test]
    fn its_place_and_its_hiding_survive_a_restart_and_a_strange_file_is_none() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target").join("test-scratch")
            .join("deskcard");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let file = path(&dir);
        let mut kept = read(&file);
        assert_eq!(kept, Kept { version: VERSION, ..Kept::default() });
        kept.places.insert("\\\\.\\DISPLAY2".into(), Place { corner: Corner::TopRight, dx: 40.0, dy: 0.0 });
        kept.last = Some("\\\\.\\DISPLAY2".into());
        kept.hidden_until = Some(1_800_000_000_000);
        crate::windowsize::write(&file, &kept).unwrap();
        assert_eq!(read(&file), kept);
        std::fs::write(&file, br#"{"version": 99, "places": {}}"#).unwrap();
        assert_eq!(read(&file), Kept { version: VERSION, ..Kept::default() });
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
