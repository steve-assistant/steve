//! The window's title bar in the page's `desk` colour.
//!
//! The title bar stays native, so snap layouts and the system buttons work
//! as people expect (docs/design/journal.md, Layout). Left alone, Windows
//! paints it its own white or black: a band across the top that is neither
//! the cream page nor the navy one. Windows 11 lets a window set its
//! caption's colour and text (`DWMWA_CAPTION_COLOR`, `DWMWA_TEXT_COLOR`).
//!
//! The page says which theme it shows, since only it knows Settings'
//! choice (paper, ink, or Windows' own); a hook on Windows' theme change
//! here would be wrong whenever the user chose one. Windows can't read the
//! colour back, so the look is checked by eye (docs/plan/spikes/s9-app.md,
//! section 3); `tests/unit/ui/test_shell_screen.py` checks these colours
//! against `ui/src/tokens.css`.

use windows_sys::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR,
};

/// A colour as Windows takes it, 0x00BBGGRR.
const fn rgb(r: u8, g: u8, b: u8) -> u32 {
    (b as u32) << 16 | (g as u32) << 8 | r as u32
}

/// The caption's colour and its text's in each theme: `desk` and `ink`.
fn colours(theme: &str) -> Option<(u32, u32)> {
    match theme {
        "paper" => Some((rgb(0xf4, 0xec, 0xdc), rgb(0x0d, 0x1c, 0x34))),
        "ink" => Some((rgb(0x08, 0x13, 0x1f), rgb(0xf0, 0xec, 0xe3))),
        _ => None,
    }
}

/// Paint the main window's title bar for the theme the page shows.
#[tauri::command]
pub fn set_caption(window: tauri::Window, theme: String) -> Result<(), String> {
    crate::main_only(&window)?;
    let (caption, text) = colours(&theme).ok_or_else(|| format!("no theme {theme:?}"))?;
    let hwnd = window.hwnd().map_err(|err| err.to_string())?;
    for (attribute, value) in [(DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text)] {
        // SAFETY: the window is this process's own and alive for the call;
        // the value is a u32 on the stack, and its size is passed with it.
        let result = unsafe {
            DwmSetWindowAttribute(
                hwnd.0 as _,
                attribute as u32,
                &value as *const u32 as *const _,
                std::mem::size_of::<u32>() as u32,
            )
        };
        if result != 0 {
            return Err(format!("Windows refused the caption's colour: 0x{:08x}", result as u32));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_colour_is_packed_as_windows_reads_it() {
        assert_eq!(rgb(0xf4, 0xec, 0xdc), 0x00dcecf4);
    }

    #[test]
    fn only_the_two_themes_have_colours() {
        assert!(colours("paper").is_some() && colours("ink").is_some());
        assert!(colours("windows").is_none());
    }
}
