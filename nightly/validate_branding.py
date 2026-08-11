#!/usr/bin/env python3
"""Validate all invariants expected from the deterministic nightly overlay."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from apply_branding import (
    APK_NAME,
    APPLICATION_ID,
    APP_NAME,
    END_COLOR,
    REQUIRED_DENSITIES,
    START_COLOR,
    FORCE_FINISH_ACTION,
    FORCE_FINISH_PERMISSION,
    BrandingError,
    gradient_pixels,
    read_rgba_png,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BrandingError(message)


def _read(path: Path) -> str:
    _require(path.is_file(), f"required file is missing: {path}")
    return path.read_text(encoding="utf-8")


def validate_strings(worktree: Path) -> None:
    paths = sorted((worktree / "app/src/main/res").glob("values*/strings.xml"))
    localized = 0
    for path in paths:
        root = ET.fromstring(_read(path))
        values = [element.text or "" for element in root.findall("string") if element.attrib.get("name") == "app_name"]
        if values:
            _require(values == [APP_NAME], f"unexpected app_name in {path}: {values}")
            localized += 1
    _require(localized > 1, "localized phone app_name resources were not found")
    wear = worktree / "wear/src/main/res/values/strings.xml"
    if wear.exists():
        root = ET.fromstring(_read(wear))
        values = [element.text or "" for element in root.findall("string") if element.attrib.get("name") == "app_name"]
        _require(values == [APP_NAME], "Wear app_name is not branded")


def validate_icons(worktree: Path) -> None:
    resources = worktree / "app/src/main/res"
    background = _read(resources / "drawable/ic_launcher_background.xml")
    _require('#160A2B' in background and '#072A4A' in background, "adaptive gradient colors are missing")
    _require('android:startX="0"' in background and 'android:endX="135.4667"' in background, "adaptive gradient direction is unexpected")
    foreground = _read(resources / "drawable/ic_launcher_foreground.xml")
    monochrome = _read(resources / "drawable/ic_launcher_monochrome.xml")
    _require('android:fillColor="#FFFFFF"' in foreground, "white adaptive foreground glyph is missing")
    _require("ic_launcher" not in monochrome or "pathData" in monochrome, "themed icon resource is malformed")
    for adaptive_name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        adaptive = _read(resources / "mipmap-anydpi-v26" / adaptive_name)
        for reference in (
            '@drawable/ic_launcher_background',
            '@drawable/ic_launcher_foreground',
            '@drawable/ic_launcher_monochrome',
        ):
            _require(reference in adaptive, f"{reference} is missing from {adaptive_name}")

    found_densities: set[str] = set()
    for directory in sorted(resources.glob("mipmap-*")):
        back = directory / "ic_launcher_adaptive_back.png"
        launcher = directory / "ic_launcher.png"
        foreground_png = directory / "ic_launcher_adaptive_fore.png"
        if not back.exists() and not launcher.exists() and not foreground_png.exists():
            continue
        _require(back.is_file() and launcher.is_file() and foreground_png.is_file(), f"incomplete launcher raster set: {directory}")
        found_densities.add(directory.name)
        width, height, pixels = read_rgba_png(back)
        _require(pixels == gradient_pixels(width, height), f"adaptive raster gradient is not deterministic: {back}")
        launcher_width, launcher_height, launcher_pixels = read_rgba_png(launcher)
        _require(any(max(pixel[:3]) > 220 and pixel[3] for pixel in launcher_pixels), f"white legacy glyph is missing: {launcher}")
        _require(launcher_width > 0 and launcher_height > 0, f"legacy launcher dimensions are invalid: {launcher}")
    _require(REQUIRED_DENSITIES.issubset(found_densities), "one or more required launcher densities are missing")


def validate_code(worktree: Path, version_name: str, version_code: int, repository: str) -> None:
    build = _read(worktree / "app/build.gradle.kts")
    _require(f'applicationId = "{APPLICATION_ID}"' in build, "nightly application ID is wrong")
    _require('applicationId = "com.bitchat.droid"' not in build, "official application ID is still configured")
    _require(re.search(rf'(?m)^\s*versionName\s*=\s*"{re.escape(version_name)}"\s*$', build) is not None, "version name is wrong")
    _require(re.search(rf"(?m)^\s*versionCode\s*=\s*{version_code}\s*$", build) is not None, "version code is wrong")
    _require('environmentVariable("GITHUB_REPOSITORY")' in build, "GITHUB_REPOSITORY is not injected at build time")
    _require('GITHUB_RELEASE_REPOSITORY' in build, "release repository BuildConfig field is missing")
    _require('BITCHAT_NIGHTLY_UNIVERSAL_ONLY' in build, "universal-only build switch is missing")
    _require('BITCHAT_GITHUB_RELEASE_CERT_SHA256' in build, "certificate pin build configuration is missing")

    client = _read(worktree / "app/src/main/java/com/bitchat/android/util/GitHubReleaseClient.kt")
    _require("BuildConfig.GITHUB_RELEASE_REPOSITORY" in client, "release client does not use injected repository")
    _require("permissionlesstech/bitchat-android/releases/latest" not in client, "release client still points at upstream")
    _require(f'private const val NIGHTLY_APK_NAME = "{APK_NAME}"' in client, "release client asset name is wrong")
    _require("if (name == NIGHTLY_APK_NAME" in client, "release client does not require the canonical nightly asset")

    manifest = _read(worktree / "app/src/main/AndroidManifest.xml")
    _require(manifest.count(f'android:name="{FORCE_FINISH_PERMISSION}"') == 2, "nightly force-finish permission is wrong")
    _require("com.bitchat.android.permission.FORCE_FINISH" not in manifest, "official force-finish permission still collides")

    constants = _read(worktree / "app/src/main/java/com/bitchat/android/util/AppConstants.kt")
    _require(f'const val ACTION_FORCE_FINISH: String = "{FORCE_FINISH_ACTION}"' in constants, "nightly force-finish action is wrong")
    _require(f'const val PERMISSION_FORCE_FINISH: String = "{FORCE_FINISH_PERMISSION}"' in constants, "nightly force-finish permission constant is wrong")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--version-code", required=True, type=int)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--cert-sha256", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worktree = args.worktree.resolve()
    validate_strings(worktree)
    validate_icons(worktree)
    validate_code(worktree, args.version_name, args.version_code, args.repository)
    metadata = json.loads(_read(worktree / "NIGHTLY_BUILD_METADATA.json"))
    expected = {
        "schemaVersion": 1,
        "branding": APP_NAME,
        "applicationId": APPLICATION_ID,
        "releaseRepository": args.repository,
        "releaseAsset": APK_NAME,
        "signingCertificateSha256": args.cert_sha256.replace(":", "").lower(),
        "upstreamCommit": args.upstream_commit.lower(),
        "versionCode": args.version_code,
        "versionName": args.version_name,
    }
    _require(metadata == expected, "NIGHTLY_BUILD_METADATA.json does not match build inputs")
    print(
        f"validated {APP_NAME} {args.version_name} ({args.version_code}) "
        f"for {args.repository}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrandingError as error:
        print(f"branding validation error: {error}", file=sys.stderr)
        raise SystemExit(1)
