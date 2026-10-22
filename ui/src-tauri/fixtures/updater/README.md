# The updater's fixtures

Read by the fetch tests in `src/update.rs` (`mod fetch`), which serve them on
127.0.0.1 to Tauri's own updater.

- `installer-0.1.1` and `installer-0.0.9`: two "installers", a line of text
  each, with no line ending, so that no checkout can change a byte the
  signatures cover. They are only downloaded, never run.
- `release.pub`: the public half of a disposable key, standing in for the
  update key built into the app.
- `installer-0.1.1.sig` and `installer-0.0.9.sig`: signed by that key, each
  naming its version.
- `installer-0.0.9.unversioned.sig`: signed by the same key without a
  version, as a release signed before the Tauri CLI recorded one would be.
- `installer-0.1.1.other-key.sig`: signed by a second disposable key, whose
  public half is not kept.

Made on 21 October 2026 with the Tauri CLI in `ui/node_modules` (2.11.5),
the keys in a scratch folder, with an empty password:

```
tauri signer generate --ci -p "" -w release.key
tauri signer generate --ci -p "" -w other.key
tauri signer sign -f release.key -p "" --app-version 0.1.1 installer-0.1.1
tauri signer sign -f release.key -p "" --app-version 0.0.9 installer-0.0.9
tauri signer sign -f release.key -p "" installer-0.0.9      # the .unversioned.sig
tauri signer sign -f other.key -p "" --app-version 0.1.1 installer-0.1.1   # the .other-key.sig
```

Both private keys were deleted once these were signed. No private key is
ever committed, even a disposable one. To change a fixture, make new keys
and sign them all again. The release key signs releases only, never these.
