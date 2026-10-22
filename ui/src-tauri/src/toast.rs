//! A Windows toast for each message Steve sends the user (the outbox's push,
//! `agent/outbox.py`), and a click on it opens the inbox.
//!
//! Until step 4 the app showed nothing it was not asked for but a red dot
//! for a waiting card: with Signal down, or never set up, a reminder went
//! nowhere at all. The outbox now keeps every message, and this says one
//! has come.
//!
//! **What it shows.** The text while Windows is unlocked; while it is
//! locked, only what kind of message it is ("Steve: a reminder"), since the
//! lock screen shows toasts to whoever is at the desk. The lock state is
//! asked as each toast is made (`locked`), not watched: a toast is the one
//! moment it matters. When Windows cannot say, it is taken as locked.
//!
//! **WinRT directly,** not tauri-plugin-notification: on Windows the plugin
//! does not report a click, and a click is how the user gets from the toast
//! to the message. Silent: the phone is the buzz, and a chime at the desk
//! for every message would be a second one.
//!
//! **How long it stays.** Windows keeps a toast in the Notification Centre
//! for days, outside Steve's folder, where its text can be read after the
//! sign-in ends (the step 11 audit, E2). Each toast is in Steve's own group
//! (`GROUP`) and expires after a day (`LIFE_S`); ending the sign-in, and
//! uninstalling (`--clear-notifications`), remove the group (`clear`).
//! The group, not every toast under the app id: a build from a checkout
//! borrows PowerShell's, and others' toasts are not Steve's to remove.
//!
//! **Whose name it carries.** Windows shows a toast only for an app it
//! knows by its AppUserModelID. An installed Steve is known by its Start
//! menu entry (step 8); a build run from a checkout is not, so it borrows
//! PowerShell's, and its toasts carry PowerShell's name and icon.

use std::collections::VecDeque;
use std::sync::Mutex;

use windows::core::{Interface, HSTRING};
use windows::Data::Xml::Dom::XmlDocument;
use windows::Foundation::{DateTime, IReference, PropertyValue, TypedEventHandler};
use windows::UI::Notifications::{ToastNotification, ToastNotificationManager};

/// PowerShell's AppUserModelID, which every Windows has registered.
const POWERSHELL: &str =
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe";

/// The group every toast of Steve's is shown in, and removed by.
pub const GROUP: &str = "steve";

/// How long Windows keeps a toast: a day.
pub const LIFE_S: u64 = 24 * 60 * 60;

/// Seconds from 1601, Windows' epoch, to 1970, Unix's.
const EPOCH_GAP_S: u64 = 11_644_473_600;

/// When a toast made at `now_s` (Unix seconds) expires, as WinRT's
/// `DateTime`: hundreds of nanoseconds since 1601.
pub fn expires(now_s: u64) -> i64 {
    ((now_s + LIFE_S + EPOCH_GAP_S) * 10_000_000) as i64
}

/// The toast for `lines`, made at `now_s`: in Steve's group, expiring a
/// day later. Made, not shown.
fn make(lines: &[String], now_s: u64) -> windows::core::Result<ToastNotification> {
    let content = XmlDocument::new()?;
    content.LoadXml(&HSTRING::from(xml(lines)))?;
    let toast = ToastNotification::CreateToastNotification(&content)?;
    toast.SetGroup(&HSTRING::from(GROUP))?;
    let until = PropertyValue::CreateDateTime(DateTime { UniversalTime: expires(now_s) })?
        .cast::<IReference<DateTime>>()?;
    toast.SetExpirationTime(&until)?;
    Ok(toast)
}

/// Remove every toast in Steve's group from the Notification Centre.
pub fn clear(app_id: &str) -> windows::core::Result<()> {
    ToastNotificationManager::History()?
        .RemoveGroupWithId(&HSTRING::from(GROUP), &HSTRING::from(app_id))
}

/// The id toasts are shown under: the app's own once installed, else
/// PowerShell's (see the module's doc).
pub fn app_id(identifier: &str) -> String {
    if cfg!(debug_assertions) {
        POWERSHELL.to_string()
    } else {
        identifier.to_string()
    }
}

/// What a message of this kind is called on the lock screen.
fn kind_words(kind: &str) -> &'static str {
    match kind {
        "reminder" => "a reminder",
        "briefing" => "your briefing",
        "card" => "something waiting for your yes",
        "job" => "a finished task",
        "reply" => "a reply",
        _ => "a message",
    }
}

/// The toast's lines: "Steve", then, unless hidden, the message with the
/// word the journal gives its kind (docs/design/presence.md, Windows
/// toasts): "Needs you: send the email to Clara?". A title per kind put
/// the same words in a second place, and no word at all on a reply.
/// Hidden is Windows locked, or the app signed out (`notify`).
pub fn lines(kind: &str, text: &str, hidden: bool) -> Vec<String> {
    if hidden {
        return vec![format!("Steve: {}", kind_words(kind))];
    }
    let said = text.trim();
    let line = match kind {
        "card" => format!("Needs you: {said}"),
        "reminder" => format!("Now: {said}"),
        "briefing" => format!("Briefing: {said}"),
        "job" => format!("Done: {said}"),
        _ => said.to_string(),
    };
    vec!["Steve".to_string(), line]
}

/// The toast's XML, every line escaped: a message is text, never markup.
pub fn xml(lines: &[String]) -> String {
    let texts: String = lines.iter().map(|line| format!("<text>{}</text>", escape(line))).collect();
    format!(
        "<toast><visual><binding template=\"ToastGeneric\">{texts}</binding></visual>\
         <audio silent=\"true\"/></toast>"
    )
}

fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            // XML 1.0 has no place for most control characters; a toast
            // with one is refused whole.
            c if (c as u32) < 0x20 && !matches!(c, '\n' | '\r' | '\t') => {}
            c => out.push(c),
        }
    }
    out
}

/// Whether this session is locked, or Windows cannot say.
pub fn locked() -> bool {
    use windows_sys::Win32::System::RemoteDesktop::{
        WTSFreeMemory, WTSQuerySessionInformationW, WTSSessionInfoEx, WTSINFOEXW,
        WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTS_SESSIONSTATE_UNLOCK,
    };
    let mut buffer: windows_sys::core::PWSTR = std::ptr::null_mut();
    let mut bytes = 0u32;
    // SAFETY: the buffer Windows allocates is read only as the struct it
    // documents for this class, only when it is that size, and freed once.
    unsafe {
        let asked = WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx, &mut buffer,
            &mut bytes,
        );
        if asked == 0 || buffer.is_null() {
            return true;
        }
        let unlocked = bytes as usize >= std::mem::size_of::<WTSINFOEXW>() && {
            let info = &*(buffer as *const WTSINFOEXW);
            info.Level == 1
                && info.Data.WTSInfoExLevel1.SessionFlags == WTS_SESSIONSTATE_UNLOCK as i32
        };
        WTSFreeMemory(buffer as *mut core::ffi::c_void);
        !unlocked
    }
}

/// Toasts shown and not yet let go: a toast's click is reported only while
/// something holds it.
#[derive(Default)]
pub struct Shown(Mutex<VecDeque<ToastNotification>>);

/// How many toasts are held for their clicks; older ones are in the
/// Notification Centre, where a click still shows them but opens nothing.
const KEEP: usize = 20;

impl Shown {
    /// Show a toast, calling `on_click` if the user clicks it.
    pub fn show(
        &self,
        app_id: &str,
        lines: &[String],
        on_click: impl Fn() + Send + 'static,
    ) -> windows::core::Result<()> {
        let now_s = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        let toast = make(lines, now_s)?;
        toast.Activated(&TypedEventHandler::new(move |_, _| {
            on_click();
            Ok(())
        }))?;
        ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(app_id))?
            .Show(&toast)?;
        let mut held = self.0.lock().unwrap_or_else(|e| e.into_inner());
        held.push_back(toast);
        while held.len() > KEEP {
            held.pop_front();
        }
        Ok(())
    }

    /// Clear Steve's toasts, and let go of those held for a click.
    pub fn clear(&self, app_id: &str) -> windows::core::Result<()> {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).clear();
        clear(app_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_locked_screen_shows_only_the_kind() {
        let shown = lines("reminder", "Call the dentist about the crown", true);
        assert_eq!(shown, vec!["Steve: a reminder".to_string()]);
        assert!(!shown.concat().contains("dentist"));
    }

    #[test]
    fn an_unlocked_screen_shows_the_message() {
        assert_eq!(
            lines("card", "Send the email to Clara? YES MAPLE", false),
            vec!["Steve".to_string(), "Needs you: Send the email to Clara? YES MAPLE".to_string()]
        );
    }

    #[test]
    fn a_message_is_text_never_markup() {
        let made = xml(&["<b>&</b> \"x\" \u{7}".to_string()]);
        assert!(made.contains("<text>&lt;b&gt;&amp;&lt;/b&gt; &quot;x&quot; </text>"));
    }

    #[test]
    fn a_toast_expires_a_day_after_it_was_made() {
        // 1 January 2026, 00:00 UTC: 1,767,225,600 s after 1970, and
        // 13,411,699,200 s after 1601.
        assert_eq!(expires(1_767_225_600), (13_411_699_200 + 86_400) * 10_000_000);
    }

    #[test]
    fn a_toast_is_made_in_steves_group_with_its_expiry() {
        // Made only: nothing is shown, and Windows' history is not touched.
        let toast = make(&lines("job", "Filed the receipts", false), 1_767_225_600).unwrap();
        assert_eq!(toast.Group().unwrap(), GROUP);
        assert_eq!(toast.ExpirationTime().unwrap().Value().unwrap().UniversalTime,
                   expires(1_767_225_600));
    }

    #[test]
    fn a_toast_is_silent() {
        assert!(xml(&["x".to_string()]).contains("<audio silent=\"true\"/>"));
    }
}
