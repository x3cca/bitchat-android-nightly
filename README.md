# bitchat Android nightly

> [!WARNING]
> **Unofficial** automated nightly builds of
> [permissionlesstech/bitchat-android](https://github.com/permissionlesstech/bitchat-android)
> `main`. Not endorsed by upstream; may be unstable.

## Install with Obtainium

- Repository URL: `https://github.com/x3cca/bitchat-android-nightly`
- Asset regex: `bitchat-nightly-universal\.apk`

The app ID is `com.bitchat.droid.nightly`, so it installs alongside the
official app with separate data and identity. Every release is signed with the
same key, so updates install normally (Obtainium or the in-app updater).

## How it works

This repo contains no app source, only:

- `.github/workflows/nightly.yml`: runs daily at 09:00 UTC (or by hand). It skips
  if upstream `main` is already released, otherwise checks out upstream, applies
  the overlay, builds a release APK, signs it, and publishes a GitHub release.
  It keeps the newest 30 releases.
- `nightly/apply.sh`: renames the app, swaps in gradient icons (`nightly/res`),
  makes signature permissions and broadcast actions unique, and points the
  in-app updater at this repo.
- `nightly/nightly.init.gradle`: sets the application ID and version at build
  time without editing upstream's Gradle files.

Test the overlay locally:

```sh
git clone https://github.com/permissionlesstech/bitchat-android /tmp/up
nightly/apply.sh /tmp/up && git -C /tmp/up diff --stat
```

Signing uses the secrets `NIGHTLY_KEYSTORE_B64`, `NIGHTLY_KEYSTORE_PASSWORD`,
`NIGHTLY_KEY_ALIAS`, and `NIGHTLY_KEY_PASSWORD`. The variable
`NIGHTLY_CERT_SHA256` pins the expected signer.
