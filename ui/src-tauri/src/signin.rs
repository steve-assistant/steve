//! The app's sign-in session, held here for all of Steve's windows.
//!
//! Once a passphrase is set, the app shows nothing until it signs in, and
//! an answer to a card needs the session the backend gave for it
//! (agent/worker/signin.py). A session held by one window's page would end
//! with that page, and the quick bar and the desk card would each need their
//! own. So the shell holds it, hands it to each window that asks, and ends
//! it for all of them:
//! - when Windows locks;
//! - after the laptop sleeps;
//! - after 30 minutes with no input since the sign-in, if the user turned
//!   that on (the `SIGN_OUT_IDLE` switch, off by default since 19 October
//!   2026);
//! - when the app quits.
//!
//! Each end is told to the backend, and every minute the backend is told the
//! app is still here; an app that stops saying so has its session lapse
//! there (`ALIVE_S`), whether it crashed or the laptop slept first. The
//! backend's answer says whether the user wants the idle sign-out, so the
//! switch is read where it is kept and a change counts within a minute.
//!
//! The lock ends a session when Windows goes from unlocked to locked, not
//! whenever Windows says it is locked: nobody signs in at a locked screen,
//! and a session made while the query says "locked" (an unattended test
//! machine) would otherwise end at once. The idle time counts from the
//! sign-in for the same reason, and because signing in is activity: Hello's
//! glance makes no input Windows counts.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager};

const POLL: Duration = Duration::from_secs(2);
/// How often the backend is told the app is still here, in polls: a minute.
const ALIVE_EVERY: u32 = 30;
pub(crate) const IDLE_MS: u64 = 30 * 60 * 1000;
/// Time the clock moved while the machine was suspended between two polls,
/// past which it slept: well over a poll's own jitter.
pub(crate) const SLEPT_MS: u64 = 10_000;

/// Why a session ended, told to the windows so the sign-in page can say.
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum Why {
    Locked,
    Slept,
    Idle,
    /// The backend no longer knows it: it restarted, or the app went quiet.
    Lapsed,
    /// Ended on purpose: Sign out, a refused answer, quitting.
    Ended,
}

struct Held {
    secret: String,
    since: Instant,
    was_locked: bool,
    /// Whether the user wants the idle sign-out, as the backend last said:
    /// off until it says otherwise, as the switch is.
    sign_out_idle: bool,
}

#[derive(Default)]
pub(crate) struct Session(Mutex<Option<Held>>);

/// Whether the session ends now, and why. Idle counts from the later of
/// the last input and the sign-in (`since_ms` ago): a sign-in is the user
/// there, even where Windows saw no input, as on a laptop driven remotely.
/// It ends a session only if the user turned the idle sign-out on.
pub(crate) fn ended(was_locked: bool, locked: bool, input_idle_ms: u64, since_ms: u64,
                    slept_ms: u64, sign_out_idle: bool) -> Option<Why> {
    let idle_ms = input_idle_ms.min(since_ms);
    if locked && !was_locked {
        Some(Why::Locked)
    } else if slept_ms >= SLEPT_MS {
        Some(Why::Slept)
    } else if sign_out_idle && idle_ms >= IDLE_MS {
        Some(Why::Idle)
    } else {
        None
    }
}

/// Milliseconds since the last keyboard or mouse input anywhere on the
/// desktop.
fn input_idle_ms() -> u64 {
    use windows_sys::Win32::System::SystemInformation::GetTickCount;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
    let mut info = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
    // SAFETY: a struct Windows fills, of the size it is told.
    if unsafe { GetLastInputInfo(&mut info) } == 0 {
        return 0;
    }
    // Both wrap every 49.7 days, together.
    u64::from(unsafe { GetTickCount() }.wrapping_sub(info.dwTime))
}

/// How long the machine has spent suspended since it started: the tick
/// count runs through sleep, the unbiased interrupt time does not.
fn asleep_ms() -> u64 {
    use windows_sys::Win32::System::SystemInformation::GetTickCount64;
    use windows_sys::Win32::System::WindowsProgramming::QueryUnbiasedInterruptTime;
    let mut unbiased = 0u64;
    // SAFETY: Windows writes one u64.
    unsafe {
        if QueryUnbiasedInterruptTime(&mut unbiased) == 0 {
            return 0;
        }
        GetTickCount64().saturating_sub(unbiased / 10_000)
    }
}

/// Watch for the ends, and keep the backend told.
pub(crate) fn watch(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut asleep = asleep_ms();
        let mut polls = 0u32;
        loop {
            std::thread::sleep(POLL);
            let now_asleep = asleep_ms();
            let slept = now_asleep.saturating_sub(asleep);
            asleep = now_asleep;
            let locked = crate::toast::locked();
            let verdict = {
                let held = app.state::<Session>();
                let mut held = held.0.lock().unwrap_or_else(|e| e.into_inner());
                let Some(session) = held.as_mut() else {
                    polls = 0;
                    continue;
                };
                let since = session.since.elapsed().as_millis() as u64;
                let why = ended(session.was_locked, locked, input_idle_ms(), since, slept,
                                session.sign_out_idle);
                session.was_locked = locked;
                why.map(|why| (why, None)).or_else(|| {
                    polls += 1;
                    (polls % ALIVE_EVERY == 0).then(|| (Why::Lapsed, Some(session.secret.clone())))
                })
            };
            match verdict {
                Some((why, None)) => end(&app, why),
                Some((_, Some(secret))) => match alive(&secret) {
                    (false, _) => end(&app, Why::Lapsed),
                    (true, Some(wanted)) => {
                        let held = app.state::<Session>();
                        let mut held = held.0.lock().unwrap_or_else(|e| e.into_inner());
                        if let Some(session) = held.as_mut() {
                            session.sign_out_idle = wanted;
                        }
                    }
                    (true, None) => {}
                },
                None => {}
            }
        }
    });
}

/// What the backend answers the keep-alive (`heard`).
fn alive(secret: &str) -> (bool, Option<bool>) {
    let body = serde_json::json!({ "session": secret }).to_string();
    let Ok(run) = crate::proven() else { return (true, None) };
    match crate::post(run.port, "/signin/alive", &body, Some(&run.token)) {
        Ok(answer) => heard(&answer),
        Err(_) => (true, None),
    }
}

/// Whether the backend still holds this session, and whether the user wants
/// the idle sign-out, `None` where it did not say. A backend that doesn't
/// answer is not taken for one that said no: it may be restarting, and says
/// so plainly once it is back.
pub(crate) fn heard(answer: &str) -> (bool, Option<bool>) {
    match serde_json::from_str::<serde_json::Value>(answer) {
        Ok(said) => (said["live"].as_bool() != Some(false), said["sign_out_idle"].as_bool()),
        Err(_) => (true, None),
    }
}

/// End the session for every window, and tell the backend.
pub(crate) fn end(app: &tauri::AppHandle, why: Why) {
    let taken = app.state::<Session>().0.lock().unwrap_or_else(|e| e.into_inner()).take();
    let Some(held) = taken else { return };
    // What was shown while signed in stays readable in Windows' history
    // after the sign-in ends (the step 11 audit, E2).
    let app_id = crate::toast::app_id(&app.config().identifier);
    if let Err(e) = app.state::<crate::toast::Shown>().clear(&app_id) {
        log::warn!("could not clear Steve's notifications: {e}");
    }
    if why != Why::Lapsed {
        let body = serde_json::json!({ "session": held.secret }).to_string();
        if let Ok(run) = crate::proven() {
            let _ = crate::post(run.port, "/signin/end", &body, Some(&run.token));
        }
    }
    let _ = app.emit("session", serde_json::json!({ "live": false, "why": why }));
}

fn app_window(window: &tauri::Window) -> Result<(), String> {
    if matches!(window.label(), "main" | "quickbar" | crate::deskcard::LABEL) {
        Ok(())
    } else {
        Err("only the app's own windows can ask for that".into())
    }
}

/// The session's secret, for an answer to a card; None while signed out.
#[tauri::command]
pub(crate) fn session_get(window: tauri::Window, held: tauri::State<'_, Session>) -> Result<Option<String>, String> {
    app_window(&window)?;
    Ok(held.0.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|h| h.secret.clone()))
}

/// Keep the session the main window's sign-in got, and tell every window.
#[tauri::command]
pub(crate) fn session_keep(
    app: tauri::AppHandle,
    window: tauri::Window,
    secret: String,
) -> Result<(), String> {
    crate::main_only(&window)?;
    let held = Held { secret, since: Instant::now(), was_locked: crate::toast::locked(),
                      sign_out_idle: false };
    *app.state::<Session>().0.lock().unwrap_or_else(|e| e.into_inner()) = Some(held);
    let _ = app.emit("session", serde_json::json!({ "live": true }));
    Ok(())
}

/// Sign out, or drop a session the backend refused.
#[tauri::command]
pub(crate) async fn session_end(app: tauri::AppHandle, window: tauri::Window) -> Result<(), String> {
    app_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || end(&app, Why::Ended))
        .await
        .map_err(|err| err.to_string())
}

/// Whether this laptop has Windows Hello set up for its user.
#[tauri::command]
pub(crate) async fn hello_available() -> bool {
    tauri::async_runtime::spawn_blocking(crate::hello::available).await.unwrap_or(false)
}

/// Make this install's Hello key, after the user's face, finger or PIN; its
/// public half, as base64 of its SubjectPublicKeyInfo, for the backend.
#[tauri::command]
pub(crate) async fn hello_enrol(window: tauri::Window, name: String) -> Result<String, String> {
    crate::main_only(&window)?;
    crate::hello::named(&name)?;
    let key = tauri::async_runtime::spawn_blocking(move || crate::hello::enrol(&name))
        .await
        .map_err(|err| err.to_string())??;
    Ok(base64_of(&key))
}

/// Sign the backend's challenge with this install's Hello key.
#[tauri::command]
pub(crate) async fn hello_sign(window: tauri::Window, name: String, message: String) -> Result<String, String> {
    crate::main_only(&window)?;
    crate::hello::named(&name)?;
    use base64::Engine;
    let message = base64::engine::general_purpose::STANDARD
        .decode(message)
        .map_err(|_| "the challenge is not base64".to_string())?;
    let signed = tauri::async_runtime::spawn_blocking(move || crate::hello::sign(&name, &message))
        .await
        .map_err(|err| err.to_string())??;
    Ok(base64_of(&signed))
}

fn base64_of(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOUR: u64 = 3_600_000;

    #[test]
    fn windows_locking_ends_the_session_but_a_lock_it_began_under_does_not() {
        assert_eq!(ended(false, true, 0, HOUR, 0, false), Some(Why::Locked));
        assert_eq!(ended(true, true, 0, HOUR, 0, false), None,
                   "signed in while the query said locked");
        assert_eq!(ended(true, false, 0, HOUR, 0, false), None);
    }

    #[test]
    fn thirty_idle_minutes_end_it_and_twenty_nine_do_not() {
        assert_eq!(ended(false, false, IDLE_MS - 60_000, HOUR, 0, true), None);
        assert_eq!(ended(false, false, IDLE_MS, HOUR, 0, true), Some(Why::Idle));
    }

    #[test]
    fn without_the_users_switch_no_idle_time_ends_it() {
        // Off by default since 19 October 2026: away for five hours at an
        // unlocked desk, still signed in.
        assert_eq!(ended(false, false, 5 * HOUR, 5 * HOUR, 0, false), None);
    }

    #[test]
    fn idle_counts_from_the_sign_in_when_windows_saw_no_input_since() {
        // Signed in a minute ago on a laptop whose last input was hours back.
        assert_eq!(ended(false, false, 5 * HOUR, 60_000, 0, true), None);
        assert_eq!(ended(false, false, 5 * HOUR, IDLE_MS, 0, true), Some(Why::Idle));
    }

    #[test]
    fn a_sleep_between_two_polls_ends_it_and_a_slow_poll_does_not() {
        assert_eq!(ended(false, false, 0, HOUR, 2_000, false), None);
        assert_eq!(ended(false, false, 0, HOUR, SLEPT_MS, false), Some(Why::Slept));
    }

    #[test]
    fn the_keep_alives_answer_says_the_switch_and_silence_changes_nothing() {
        assert_eq!(heard(r#"{"live": true, "sign_out_idle": true}"#), (true, Some(true)));
        assert_eq!(heard(r#"{"live": true, "sign_out_idle": false}"#), (true, Some(false)));
        assert_eq!(heard(r#"{"live": false}"#), (false, None));
        // An older backend, or none: still live, and the switch as it was.
        assert_eq!(heard(r#"{"live": true}"#), (true, None));
        assert_eq!(heard("Bad Gateway"), (true, None));
    }
}
