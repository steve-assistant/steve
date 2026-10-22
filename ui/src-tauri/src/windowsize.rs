//! The main window's size, kept across starts.
//!
//! The window opened at 1100 x 760 every time, whatever size the user had
//! made it: someone who widened it for the journal, or narrowed it beside
//! their mail, did it again at every start. Tauri's window-state plugin
//! keeps a size, but in Windows' folder for the app's identifier, outside
//! Steve's data folder: removing Steve's data would leave it behind, and
//! every app on the identifier (a test's, a second install) would share
//! one size. So it is `window.json` in the data folder.
//!
//! Only the size, and whether the window was maximised: a position kept
//! from a monitor no longer plugged in would open it off every screen.
//! A file from a newer Steve is read as none and written over; a window's
//! size isn't worth keeping through a downgrade.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{LogicalSize, Manager};

/// The file's shape: every settings file says which (core/settingsfile.py).
const VERSION: u32 = 1;
/// A drag resizes many times a second; the size is written once it rests.
const SETTLE: Duration = Duration::from_millis(400);
/// The window's smallest, as tauri.conf.json's minWidth and minHeight.
const SMALLEST: (f64, f64) = (420.0, 560.0);

/// The size in logical pixels, so it means the same at another scale.
#[derive(Serialize, Deserialize, Debug, PartialEq, Clone, Copy)]
pub(crate) struct Kept {
    version: u32,
    width: f64,
    height: f64,
    maximized: bool,
}

/// What the window is now, measured: its size, maximised, minimised.
pub(crate) struct Now {
    pub width: f64,
    pub height: f64,
    pub maximized: bool,
    pub minimized: bool,
}

pub(crate) fn path(data: &Path) -> PathBuf {
    data.join("window.json")
}

/// The size kept, fitted to the screen it opens on (logical pixels, the
/// taskbar left out), or None: nothing kept, or a file not understood.
pub(crate) fn read(file: &Path, screen: Option<(f64, f64)>) -> Option<Kept> {
    let kept: Kept = serde_json::from_slice(&std::fs::read(file).ok()?).ok()?;
    if kept.version != VERSION || !kept.width.is_finite() || !kept.height.is_finite() {
        return None;
    }
    let (widest, tallest) = screen.unwrap_or((f64::MAX, f64::MAX));
    Some(Kept {
        width: kept.width.min(widest).max(SMALLEST.0),
        height: kept.height.min(tallest).max(SMALLEST.1),
        ..kept
    })
}

/// What to keep for the window as it is now, or None to keep what was.
/// Maximised, the size kept stays the one before: un-maximised at the
/// next start, the window goes back to it, not to the whole screen.
pub(crate) fn next(before: Option<Kept>, now: &Now) -> Option<Kept> {
    if now.minimized {
        return None; // minimised, Windows reports a size of nothing
    }
    let (width, height) = match (now.maximized, before) {
        (true, Some(before)) => (before.width, before.height),
        _ => (now.width, now.height),
    };
    Some(Kept { version: VERSION, width, height, maximized: now.maximized })
}

/// Written whole and renamed into place: a laptop that loses power
/// mid-write leaves the size before, not half a file. The desk card's
/// place is written so too (deskcard.rs).
pub(crate) fn write<T: Serialize>(file: &Path, kept: &T) -> std::io::Result<()> {
    let fresh = file.with_extension("json.new");
    std::fs::write(&fresh, serde_json::to_vec(kept)?)?;
    std::fs::rename(&fresh, file)
}

/// The app's place for the size, and a count of resizes, so only the
/// last of a drag writes.
pub(crate) struct Keeper {
    file: PathBuf,
    resizes: Arc<AtomicU64>,
}

impl Keeper {
    pub(crate) fn new(data: &Path) -> Self {
        Keeper { file: path(data), resizes: Arc::new(AtomicU64::new(0)) }
    }
}

/// Before the main window first shows: the size it was left at.
pub(crate) fn restore(window: &tauri::WebviewWindow, keeper: &Keeper) {
    let monitor = window.current_monitor().ok().flatten()
        .or_else(|| window.primary_monitor().ok().flatten());
    let screen = monitor.map(|m| {
        let area = m.work_area().size.to_logical::<f64>(m.scale_factor());
        (area.width, area.height)
    });
    let Some(kept) = read(&keeper.file, screen) else { return };
    let _ = window.set_size(LogicalSize::new(kept.width, kept.height));
    let _ = window.center();
    if kept.maximized {
        let _ = window.maximize();
    }
}

/// On each resize of the main window: kept once it has rested.
pub(crate) fn resized(window: &tauri::Window) {
    let Some(keeper) = window.app_handle().try_state::<Keeper>() else { return };
    let this = keeper.resizes.fetch_add(1, Ordering::SeqCst) + 1;
    let (resizes, file, window) = (keeper.resizes.clone(), keeper.file.clone(), window.clone());
    std::thread::spawn(move || {
        std::thread::sleep(SETTLE);
        if resizes.load(Ordering::SeqCst) != this {
            return; // a later resize will write
        }
        let Ok(scale) = window.scale_factor() else { return };
        let Ok(size) = window.inner_size() else { return };
        let size = size.to_logical::<f64>(scale);
        let now = Now {
            width: size.width,
            height: size.height,
            maximized: window.is_maximized().unwrap_or(false),
            minimized: window.is_minimized().unwrap_or(false),
        };
        if let Some(kept) = next(read(&file, None), &now) {
            if let Err(err) = write(&file, &kept) {
                log::warn!("couldn't keep the window's size in {}: {err}", file.display());
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target").join("test-scratch")
            .join(name);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn now(width: f64, height: f64, maximized: bool, minimized: bool) -> Now {
        Now { width, height, maximized, minimized }
    }

    #[test]
    fn a_size_written_is_read_back_as_it_was() {
        let file = path(&scratch("windowsize"));
        let kept = next(None, &now(900.0, 700.0, false, false)).unwrap();
        write(&file, &kept).unwrap();
        assert_eq!(read(&file, Some((1920.0, 1040.0))), Some(kept));
    }

    #[test]
    fn a_size_from_a_bigger_screen_is_fitted_to_this_one_and_never_below_the_smallest() {
        let file = path(&scratch("windowsize-fit"));
        write(&file, &next(None, &now(2400.0, 1500.0, false, false)).unwrap()).unwrap();
        let fitted = read(&file, Some((1280.0, 680.0))).unwrap();
        assert_eq!((fitted.width, fitted.height), (1280.0, 680.0));
        let tiny = read(&file, Some((300.0, 300.0))).unwrap();
        assert_eq!((tiny.width, tiny.height), SMALLEST);
    }

    #[test]
    fn maximised_the_size_before_is_kept_and_minimised_nothing_is() {
        let before = next(None, &now(900.0, 700.0, false, false));
        let maximized = next(before, &now(1920.0, 1040.0, true, false)).unwrap();
        assert_eq!((maximized.width, maximized.height, maximized.maximized),
                   (900.0, 700.0, true));
        assert_eq!(next(before, &now(0.0, 0.0, false, true)), None);
    }

    #[test]
    fn a_file_not_understood_is_read_as_none_kept() {
        let file = path(&scratch("windowsize-bad"));
        for body in ["", "{}", "not json",
                     r#"{"version":2,"width":900.0,"height":700.0,"maximized":false}"#] {
            std::fs::write(&file, body).unwrap();
            assert_eq!(read(&file, None), None, "{body}");
        }
    }
}
