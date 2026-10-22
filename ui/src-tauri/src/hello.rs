//! Windows Hello: a key Windows makes for this install, and uses only after
//! the user's face, finger or PIN (core/hello.py checks what it signs).
//!
//! WinRT's KeyCredentialManager. Its calls wait on the user, so each runs
//! on a blocking thread, never the main one, whose message loop the prompt
//! needs. A desktop app's Hello prompt can open behind the app's window;
//! `to_front` brings it forward, as other desktop apps using Hello do.

use windows::core::HSTRING;
use windows::Security::Credentials::{
    KeyCredentialCreationOption, KeyCredentialManager, KeyCredentialStatus,
};
use windows::Security::Cryptography::Core::CryptographicPublicKeyBlobType;
use windows::Security::Cryptography::CryptographicBuffer;
use windows::Storage::Streams::IBuffer;

/// Only a name core/identity.hello_key makes: `steve.<install>.hello`. The
/// page passes the backend's name along, and a name of another program's key
/// would replace that key at enrolment.
pub(crate) fn named(name: &str) -> Result<(), String> {
    let middle = name.strip_prefix("steve.").and_then(|rest| rest.strip_suffix(".hello"));
    match middle {
        Some(install) if !install.is_empty() && install.len() <= 16
            && install.chars().all(|c| c.is_ascii_alphanumeric()) => Ok(()),
        _ => Err("that is not the name of Steve's Hello key".into()),
    }
}

pub(crate) fn available() -> bool {
    KeyCredentialManager::IsSupportedAsync().and_then(|asked| asked.get()).unwrap_or(false)
}

fn said(status: KeyCredentialStatus) -> String {
    match status {
        KeyCredentialStatus::UserCanceled => "cancelled".into(),
        KeyCredentialStatus::NotFound => {
            "Windows Hello's key for Steve is gone: set Hello up again in Settings".into()
        }
        KeyCredentialStatus::UserPrefersPassword => "you chose the passphrase".into(),
        KeyCredentialStatus::SecurityDeviceLocked => {
            "Windows Hello is locked after too many tries: use the passphrase".into()
        }
        _ => "Windows Hello didn't answer: use the passphrase".into(),
    }
}

fn bytes(buffer: &IBuffer) -> Result<Vec<u8>, String> {
    let mut out = windows::core::Array::<u8>::new();
    CryptographicBuffer::CopyToByteArray(buffer, &mut out).map_err(|err| err.message())?;
    Ok(out.to_vec())
}

/// Make the key, replacing one of the same name; its public half.
pub(crate) fn enrol(name: &str) -> Result<Vec<u8>, String> {
    let asked = KeyCredentialManager::RequestCreateAsync(
        &HSTRING::from(name),
        KeyCredentialCreationOption::ReplaceExisting,
    )
    .map_err(|err| err.message())?;
    to_front();
    let made = asked.get().map_err(|err| err.message())?;
    let status = made.Status().map_err(|err| err.message())?;
    if status != KeyCredentialStatus::Success {
        return Err(said(status));
    }
    let key = made
        .Credential()
        .and_then(|c| {
            c.RetrievePublicKeyWithBlobType(CryptographicPublicKeyBlobType::X509SubjectPublicKeyInfo)
        })
        .map_err(|err| err.message())?;
    bytes(&key)
}

/// Delete the key, if Windows holds it: uninstall's, when the data goes
/// (core/uninstall.py, the review of steps 7 to 9, finding 12). Left, it
/// stayed in the user's Hello container, one more with each reinstall.
/// Opening a key asks nothing of the user; only signing does. Whether a key
/// is there is asked first, so an install that never set Hello up has
/// nothing to fail on.
pub(crate) fn forget(name: &str) -> Result<bool, String> {
    named(name)?;
    let name = HSTRING::from(name);
    let opened = KeyCredentialManager::OpenAsync(&name)
        .and_then(|asked| asked.get())
        .map_err(|err| err.message())?;
    match opened.Status().map_err(|err| err.message())? {
        KeyCredentialStatus::NotFound => return Ok(false),
        KeyCredentialStatus::Success => {}
        status => return Err(said(status)),
    }
    KeyCredentialManager::DeleteAsync(&name)
        .and_then(|asked| asked.get())
        .map_err(|err| err.message())?;
    Ok(true)
}

/// Sign `data` with the key, after the user's face, finger or PIN.
pub(crate) fn sign(name: &str, data: &[u8]) -> Result<Vec<u8>, String> {
    let opened = KeyCredentialManager::OpenAsync(&HSTRING::from(name))
        .and_then(|asked| asked.get())
        .map_err(|err| err.message())?;
    let status = opened.Status().map_err(|err| err.message())?;
    if status != KeyCredentialStatus::Success {
        return Err(said(status));
    }
    let buffer = CryptographicBuffer::CreateFromByteArray(data).map_err(|err| err.message())?;
    let asked = opened
        .Credential()
        .and_then(|c| c.RequestSignAsync(&buffer))
        .map_err(|err| err.message())?;
    to_front();
    let signed = asked.get().map_err(|err| err.message())?;
    let status = signed.Status().map_err(|err| err.message())?;
    if status != KeyCredentialStatus::Success {
        return Err(said(status));
    }
    bytes(&signed.Result().map_err(|err| err.message())?)
}

/// Bring Windows' Hello prompt in front of the app, once it opens.
fn to_front() {
    use windows_sys::Win32::UI::WindowsAndMessaging::{FindWindowW, SetForegroundWindow};
    std::thread::spawn(|| {
        let class: Vec<u16> = "Credential Dialog Xaml Host\0".encode_utf16().collect();
        for _ in 0..30 {
            // SAFETY: a NUL-terminated class name, and a window handle used
            // only as Windows gave it.
            let found = unsafe { FindWindowW(class.as_ptr(), std::ptr::null()) };
            if !found.is_null() {
                unsafe { SetForegroundWindow(found) };
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_name_of_steves_own_key_is_used() {
        assert!(named("steve.ab12cd34.hello").is_ok());
        for other in ["other.ab12cd34.hello", "steve..hello", "steve.ab12/../x.hello",
                      "steve.ab12cd34.passkey", "Passport.ab12cd34"] {
            assert!(named(other).is_err(), "{other}");
        }
    }

    /// Uninstall hands the app a name: one not Steve's must never reach
    /// Windows, where it would delete another program's key.
    #[test]
    fn only_a_name_of_steves_own_key_is_forgotten() {
        for other in ["other.ab12cd34.hello", "steve..hello", "Passport.ab12cd34"] {
            assert_eq!(forget(other), Err("that is not the name of Steve's Hello key".into()));
        }
    }
}
