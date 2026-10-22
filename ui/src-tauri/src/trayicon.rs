//! The tray's icon: Steve's mark, drawn for the taskbar it sits on.
//!
//! The tray used to show the window's own icon, with a red dot painted over
//! it while something waited. Its navy disappeared on a dark taskbar. Above
//! 100 % scaling Windows blurred one large picture down to the tray's size.
//! And a backend that had stopped for good looked like one that was fine.
//!
//! Now `devtools/marks.py --tray` draws every picture ahead of time from the
//! logo: for a light taskbar and a dark one, for each state, at each size
//! Windows asks for. The app only picks one. A picture that a build lacks
//! fails the build, here, not the user's tray.

/// What the icon says, most urgent first when more than one is true.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Shown {
    /// The backend has stopped and will not be started again: faint, with
    /// outline diamonds (docs/design/presence.md, "The tray").
    Stopped,
    /// Notes wait for them: the red diamond badge.
    Waiting,
    Idle,
}

impl Shown {
    pub fn of(stopped: bool, waiting: u32) -> Shown {
        if stopped {
            Shown::Stopped
        } else if waiting > 0 {
            Shown::Waiting
        } else {
            Shown::Idle
        }
    }

    fn name(self) -> &'static str {
        match self {
            Shown::Stopped => "stopped",
            Shown::Waiting => "waiting",
            Shown::Idle => "idle",
        }
    }
}

macro_rules! drawn {
    ($($name:literal),* $(,)?) => {
        &[$(($name, include_bytes!(concat!("../icons/tray/", $name, ".png")) as &[u8])),*]
    };
}

/// Every picture, by `{theme}-{state}-{size}`: `paper` for a light taskbar,
/// `ink` for a dark one.
const PICTURES: &[(&str, &[u8])] = drawn![
    "paper-idle-16", "paper-idle-20", "paper-idle-24", "paper-idle-32",
    "paper-waiting-16", "paper-waiting-20", "paper-waiting-24", "paper-waiting-32",
    "paper-stopped-16", "paper-stopped-20", "paper-stopped-24", "paper-stopped-32",
    "ink-idle-16", "ink-idle-20", "ink-idle-24", "ink-idle-32",
    "ink-waiting-16", "ink-waiting-20", "ink-waiting-24", "ink-waiting-32",
    "ink-stopped-16", "ink-stopped-20", "ink-stopped-24", "ink-stopped-32",
];

/// The tray's icon is 16 pixels at 100 %, so the drawn size nearest to 16
/// times the display's scale. Above 175 % Windows shrinks the 32.
pub fn size_for(scale: f64) -> u32 {
    if scale < 1.125 {
        16
    } else if scale < 1.375 {
        20
    } else if scale < 1.625 {
        24
    } else {
        32
    }
}

/// The PNG for a taskbar, a state and a display's scale.
pub fn picture(light_taskbar: bool, shown: Shown, scale: f64) -> &'static [u8] {
    let theme = if light_taskbar { "paper" } else { "ink" };
    let name = format!("{theme}-{}-{}", shown.name(), size_for(scale));
    PICTURES
        .iter()
        .find(|(drawn, _)| *drawn == name)
        .map(|(_, png)| *png)
        .expect("every theme, state and size is drawn")
}

/// Whether the taskbar is light. Windows sets it apart from the apps'
/// colours ("Choose your default Windows mode"), and the window's theme
/// reports only the apps'. Before Windows 10's 1903 the value is missing,
/// and the taskbar was always dark.
#[cfg(windows)]
pub fn light_taskbar() -> bool {
    use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_DWORD};

    let wide = |text: &str| text.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let key = wide(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
    let value = wide("SystemUsesLightTheme");
    let mut data: u32 = 0;
    let mut size = std::mem::size_of::<u32>() as u32;
    // SAFETY: both names are NUL-terminated and outlive the call; `data` is
    // a DWORD and `size` says so.
    let status = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_DWORD,
            std::ptr::null_mut(),
            (&mut data as *mut u32).cast(),
            &mut size,
        )
    };
    status == 0 && data != 0
}

#[cfg(not(windows))]
pub fn light_taskbar() -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A picture the wrong size is scaled by Windows, and blurs; one that
    /// doesn't decode leaves the tray empty.
    #[test]
    fn every_picture_is_a_png_of_the_size_its_name_gives() {
        assert_eq!(PICTURES.len(), 2 * 3 * 4);
        for (name, png) in PICTURES {
            let size: u32 = name.rsplit('-').next().unwrap().parse().unwrap();
            let image = tauri::image::Image::from_bytes(png).unwrap();
            assert_eq!((image.width(), image.height()), (size, size), "{name}");
        }
    }

    /// Windows' small icon is 16, 20, 24 and 28 pixels at 100, 125, 150 and
    /// 175 %: each gets its own picture, or the next one up.
    #[test]
    fn each_display_scale_gets_the_picture_drawn_for_it() {
        let sizes: Vec<u32> = [1.0, 1.25, 1.5, 1.75, 2.0, 3.0].iter().map(|s| size_for(*s)).collect();
        assert_eq!(sizes, [16, 20, 24, 32, 32, 32]);
    }

    /// A backend that has stopped for good says so, even with notes waiting:
    /// they can't be answered until it's back.
    #[test]
    fn stopped_shows_over_waiting_and_waiting_over_idle() {
        assert_eq!(Shown::of(true, 3), Shown::Stopped);
        assert_eq!(Shown::of(false, 3), Shown::Waiting);
        assert_eq!(Shown::of(false, 0), Shown::Idle);
        assert_ne!(picture(true, Shown::Idle, 1.0), picture(false, Shown::Idle, 1.0));
        assert_ne!(picture(true, Shown::Waiting, 1.0), picture(true, Shown::Idle, 1.0));
    }
}
