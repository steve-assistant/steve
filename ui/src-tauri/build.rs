//! Tauri's build, with the Windows manifest given to the linker.
//!
//! The manifest (`windows/app.manifest`) asks Windows for Common Controls 6.
//! Tauri's build puts it in a resource linked into the app's executable only,
//! so the lib's own test executable had none. That was harmless until a test
//! built a tauri app (the updater's fetch tests, in `src/update.rs`): the menu
//! and tray code it links imports functions that only Common Controls 6 has,
//! and Windows refused to start the test executable at all
//! (`STATUS_ENTRYPOINT_NOT_FOUND`), before any test ran.
//!
//! So the linker embeds the manifest in everything it links, the tests
//! included, and tauri's build adds none of its own: two would collide. A
//! tests-only link argument is not possible here, as cargo's apply only to a
//! `tests/` target, and the shell's tests live in the lib. This is tauri's
//! own recipe for its tests. The app's manifest says the same as before: the
//! same text, and `/MANIFESTUAC:NO`, so the linker adds no section of its own.

fn main() {
  let windows = std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows");
  let msvc = std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc");
  let mut attributes = tauri_build::Attributes::new();
  if windows && msvc {
    let manifest = std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap())
      .join("windows")
      .join("app.manifest");
    println!("cargo:rerun-if-changed={}", manifest.display());
    println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
    println!("cargo:rustc-link-arg=/MANIFESTINPUT:{}", manifest.display());
    println!("cargo:rustc-link-arg=/MANIFESTUAC:NO");
    attributes =
      attributes.windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
  }
  if let Err(error) = tauri_build::try_build(attributes) {
    // As `tauri_build::build` reports a failure.
    println!("{error:#}");
    std::process::exit(1);
  }
}
