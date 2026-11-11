#!/usr/bin/env bash
# Turn a clean upstream checkout into the bitchat nightly variant.
# Usage: APP_ID=... RELEASE_REPO=owner/repo nightly/apply.sh <upstream-checkout>
# (Application ID and version are set at build time by nightly.init.gradle.)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$1"
: "${APP_ID:=com.bitchat.droid.nightly}"
: "${RELEASE_REPO:=x3cca/bitchat-android-nightly}"
main=app/src/main

# Icons with the nightly gradient background.
cp -r "$here/res/." "$main/res/"

# Launcher name in every locale.
sed -i 's#<string name="app_name">bitchat</string>#<string name="app_name">bitchat nightly</string>#' \
  "$main"/res/values*/strings.xml

# Signature permissions and broadcast actions must be unique per app, or the
# nightly can't be installed next to the official app.
grep -rlE 'com\.bitchat\.android\.(permission\.|ACTION_)' "$main" | while read -r file; do
  sed -i -E "s#com\.bitchat\.android\.(permission\.|ACTION_)#${APP_ID}.\1#g" "$file"
done

# In-app updater: look for nightly releases here instead of upstream's.
grep -rl 'permissionlesstech/bitchat-android' "$main/java" | while read -r file; do
  sed -i -e "s#permissionlesstech/bitchat-android/releases#${RELEASE_REPO}/releases#g" \
         -e 's#bitchat-android-universal\.apk#bitchat-nightly-universal.apk#g' "$file"
done
