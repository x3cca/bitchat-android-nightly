#!/usr/bin/env python3
"""Apply the deterministic bitchat nightly overlay to a clean upstream worktree."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path


APP_NAME = "bitchat nightly"
APPLICATION_ID = "com.bitchat.droid.nightly"
APK_NAME = "bitchat-nightly-universal.apk"
FORCE_FINISH_ACTION = f"{APPLICATION_ID}.ACTION_FORCE_FINISH"
FORCE_FINISH_PERMISSION = f"{APPLICATION_ID}.permission.FORCE_FINISH"
START_COLOR = (0x16, 0x0A, 0x2B)
END_COLOR = (0x07, 0x2A, 0x4A)
REQUIRED_DENSITIES = {"mipmap-mdpi", "mipmap-hdpi", "mipmap-xhdpi", "mipmap-xxhdpi", "mipmap-xxxhdpi"}
BACKGROUND_XML = """<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:aapt="http://schemas.android.com/aapt"
    android:viewportWidth="135.4667"
    android:viewportHeight="135.4667"
    android:width="108dp"
    android:height="108dp">
    <path android:pathData="M-0.2617076 -0.1145289H135.7284V135.5812H-0.2617076V-0.1145289Z">
        <aapt:attr name="android:fillColor">
            <gradient
                android:type="linear"
                android:startX="0"
                android:startY="0"
                android:endX="135.4667"
                android:endY="135.4667"
                android:startColor="#160A2B"
                android:endColor="#072A4A" />
        </aapt:attr>
    </path>
</vector>
"""


class BrandingError(RuntimeError):
    pass


def _read(path: Path) -> str:
    if not path.is_file():
        raise BrandingError(f"required file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _write_if_changed(path: Path, content: str | bytes) -> None:
    existing = path.read_bytes() if path.exists() else None
    desired = content.encode("utf-8") if isinstance(content, str) else content
    if existing != desired:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(desired)


def _replace_once(text: str, old: str, new: str, description: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise BrandingError(f"expected exactly one {description}; found {count}")
    return text.replace(old, new, 1)


def _replace_regex_once(
    text: str,
    pattern: str,
    replacement: str,
    description: str,
    *,
    already_applied: str | None = None,
) -> str:
    if already_applied is not None and already_applied in text:
        return text
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise BrandingError(f"expected exactly one {description}; found {count}")
    return updated


def _insert_marker_block(text: str, start: str, end: str, block: str, anchor: str, description: str) -> str:
    start_count = text.count(start)
    end_count = text.count(end)
    rendered = f"{start}\n{block.rstrip()}\n{end}\n"
    if start_count or end_count:
        if start_count != 1 or end_count != 1:
            raise BrandingError(f"malformed marker block for {description}")
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
        return pattern.sub(rendered, text, count=1)
    if text.count(anchor) != 1:
        raise BrandingError(f"expected exactly one anchor for {description}")
    return text.replace(anchor, rendered + "\n" + anchor, 1)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_rgba_png(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrandingError(f"not a PNG: {path}")
    offset = 8
    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    while offset < len(data):
        if offset + 12 > len(data):
            raise BrandingError(f"truncated PNG: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break
    if None in (width, height, bit_depth, color_type, interlace):
        raise BrandingError(f"PNG has no IHDR: {path}")
    if bit_depth != 8 or color_type != 6 or interlace != 0:
        raise BrandingError(f"expected a non-interlaced 8-bit RGBA PNG: {path}")
    raw = zlib.decompress(bytes(compressed))
    stride = width * 4
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise BrandingError(f"unexpected decompressed PNG size: {path}")
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        encoded = raw[cursor : cursor + stride]
        cursor += stride
        prior = rows[-1] if rows else bytearray(stride)
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - 4] if index >= 4 else 0
            up = prior[index]
            upper_left = prior[index - 4] if index >= 4 else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + up
            elif filter_type == 3:
                decoded = value + ((left + up) // 2)
            elif filter_type == 4:
                decoded = value + _paeth(left, up, upper_left)
            else:
                raise BrandingError(f"unsupported PNG filter {filter_type}: {path}")
            row[index] = decoded & 0xFF
        rows.append(row)
    pixels = [tuple(row[index : index + 4]) for row in rows for index in range(0, stride, 4)]
    return width, height, pixels  # type: ignore[return-value]


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def write_rgba_png(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    if len(pixels) != width * height:
        raise BrandingError("pixel count does not match PNG dimensions")
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for pixel in pixels[y * width : (y + 1) * width]:
            rows.extend(pixel)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def gradient_pixels(width: int, height: int) -> list[tuple[int, int, int, int]]:
    pixels: list[tuple[int, int, int, int]] = []
    denominator = max(1, (width - 1) + (height - 1))
    for y in range(height):
        for x in range(width):
            numerator = x + y
            rgb = tuple(
                (start * (denominator - numerator) + end * numerator + denominator // 2) // denominator
                for start, end in zip(START_COLOR, END_COLOR)
            )
            pixels.append((rgb[0], rgb[1], rgb[2], 255))
    return pixels


def _legacy_icon_pixels(
    width: int,
    height: int,
    legacy_source: list[tuple[int, int, int, int]],
    foreground_width: int,
    foreground_height: int,
    foreground_source: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Composite the unchanged upstream raster foreground over the gradient."""
    gradient = gradient_pixels(width, height)
    x0, x1 = int(foreground_width * 0.25), int(foreground_width * 0.75)
    y0, y1 = int(foreground_height * 0.30), int(foreground_height * 0.70)
    outside_luma = [
        max(r, g, b)
        for index, (r, g, b, alpha) in enumerate(foreground_source)
        if alpha
        and not (
            x0 <= index % foreground_width <= x1
            and y0 <= index // foreground_width <= y1
        )
    ]
    if not outside_luma:
        raise BrandingError("could not sample the upstream legacy icon background")
    threshold = min(160, max(outside_luma) + 12)
    output: list[tuple[int, int, int, int]] = []
    glyph_pixels = 0
    for index, (legacy_pixel, gradient_pixel) in enumerate(zip(legacy_source, gradient)):
        target_x, target_y = index % width, index // width
        source_x = min(foreground_width - 1, (target_x * foreground_width) // width)
        source_y = min(foreground_height - 1, (target_y * foreground_height) // height)
        red, green, blue, _ = foreground_source[source_y * foreground_width + source_x]
        source_alpha = legacy_pixel[3]
        luminance = max(red, green, blue)
        coverage = max(0, min(255, ((luminance - threshold) * 255) // max(1, 255 - threshold)))
        if coverage:
            glyph_pixels += 1
        out_rgb = tuple((channel * (255 - coverage) + 255 * coverage + 127) // 255 for channel in gradient_pixel[:3])
        output.append((out_rgb[0], out_rgb[1], out_rgb[2], source_alpha))
    if glyph_pixels < max(10, (width * height) // 200):
        raise BrandingError("could not recover the existing white glyph from the legacy icon")
    return output


def update_app_names(worktree: Path) -> None:
    resource_root = worktree / "app/src/main/res"
    if not resource_root.is_dir():
        raise BrandingError("phone resource directory is missing")
    candidates = sorted(resource_root.glob("values*/strings.xml"))
    updated = 0
    pattern = re.compile(
        r"(<string\b(?=[^>]*\bname\s*=\s*['\"]app_name['\"])[^>]*>)(.*?)(</string>)",
        re.DOTALL,
    )
    for path in candidates:
        text = _read(path)
        root = ET.fromstring(text)
        matches = [element for element in root.findall("string") if element.attrib.get("name") == "app_name"]
        if not matches:
            continue
        if len(matches) != 1:
            raise BrandingError(f"expected one app_name in {path}; found {len(matches)}")
        rewritten, count = pattern.subn(lambda match: match.group(1) + APP_NAME + match.group(3), text)
        if count != 1:
            raise BrandingError(f"could not safely rewrite app_name in {path}")
        ET.fromstring(rewritten)
        _write_if_changed(path, rewritten)
        updated += 1
    if updated == 0 or not (resource_root / "values/strings.xml").is_file():
        raise BrandingError("no phone app_name resources were updated")

    wear_strings = worktree / "wear/src/main/res/values/strings.xml"
    if (worktree / "wear").is_dir():
        text = _read(wear_strings)
        root = ET.fromstring(text)
        matches = [element for element in root.findall("string") if element.attrib.get("name") == "app_name"]
        if len(matches) != 1:
            raise BrandingError("Wear module exists but does not have exactly one app_name resource")
        rewritten, count = pattern.subn(lambda match: match.group(1) + APP_NAME + match.group(3), text)
        if count != 1:
            raise BrandingError("could not safely rewrite Wear app_name")
        _write_if_changed(wear_strings, rewritten)


def update_icons(worktree: Path) -> None:
    resource_root = worktree / "app/src/main/res"
    background = resource_root / "drawable/ic_launcher_background.xml"
    foreground_paths = [
        resource_root / "drawable/ic_launcher_foreground.xml",
        resource_root / "drawable/ic_launcher_monochrome.xml",
    ]
    foreground_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in foreground_paths if path.is_file()}
    if len(foreground_hashes) != len(foreground_paths):
        raise BrandingError("required adaptive/themed launcher foreground resources are missing")

    for adaptive_name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        adaptive = resource_root / "mipmap-anydpi-v26" / adaptive_name
        text = _read(adaptive)
        required_refs = (
            '@drawable/ic_launcher_background',
            '@drawable/ic_launcher_foreground',
            '@drawable/ic_launcher_monochrome',
        )
        if not all(reference in text for reference in required_refs):
            raise BrandingError(f"adaptive icon no longer has the expected resources: {adaptive}")
    _write_if_changed(background, BACKGROUND_XML)

    density_dirs = {path.name: path for path in resource_root.glob("mipmap-*") if path.is_dir() and path.name != "mipmap-anydpi-v26"}
    missing = REQUIRED_DENSITIES - density_dirs.keys()
    if missing:
        raise BrandingError(f"required legacy launcher densities are missing: {', '.join(sorted(missing))}")
    for density_name, density_dir in sorted(density_dirs.items()):
        launcher = density_dir / "ic_launcher.png"
        adaptive_back = density_dir / "ic_launcher_adaptive_back.png"
        adaptive_fore = density_dir / "ic_launcher_adaptive_fore.png"
        if not all(path.is_file() for path in (launcher, adaptive_back, adaptive_fore)):
            if density_name in REQUIRED_DENSITIES:
                raise BrandingError(f"required launcher raster is missing in {density_dir}")
            continue
        launcher_width, launcher_height, launcher_source = read_rgba_png(launcher)
        back_width, back_height, _ = read_rgba_png(adaptive_back)
        foreground_width, foreground_height, foreground_source = read_rgba_png(adaptive_fore)
        _write_if_changed(
            adaptive_back,
            write_rgba_png(back_width, back_height, gradient_pixels(back_width, back_height)),
        )
        _write_if_changed(
            launcher,
            write_rgba_png(
                launcher_width,
                launcher_height,
                _legacy_icon_pixels(
                    launcher_width,
                    launcher_height,
                    launcher_source,
                    foreground_width,
                    foreground_height,
                    foreground_source,
                ),
            ),
        )

    for path, expected_hash in foreground_hashes.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise BrandingError(f"launcher foreground changed unexpectedly: {path}")


def update_build_config(worktree: Path, version_name: str, version_code: int) -> None:
    path = worktree / "app/build.gradle.kts"
    text = _read(path)
    text, name_count = re.subn(r'(?m)^(\s*versionName\s*=\s*)"[^"]+"\s*$', rf'\1"{version_name}"', text)
    text, code_count = re.subn(r"(?m)^(\s*versionCode\s*=\s*)\d+\s*$", rf"\g<1>{version_code}", text)
    if name_count != 1 or code_count != 1:
        raise BrandingError(f"expected one phone versionName/versionCode; found {name_count}/{code_count}")

    repository_block = """val nightlyReleaseRepository = providers
    .environmentVariable("BITCHAT_GITHUB_RELEASE_REPOSITORY")
    .orElse(providers.environmentVariable("GITHUB_REPOSITORY"))
    .getOrElse("")
    .trim()
require(nightlyReleaseRepository.matches(Regex("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"))) {
    "GITHUB_REPOSITORY or BITCHAT_GITHUB_RELEASE_REPOSITORY must be owner/repository"
}"""
    text = _insert_marker_block(
        text,
        "// BEGIN BITCHAT NIGHTLY RELEASE REPOSITORY",
        "// END BITCHAT NIGHTLY RELEASE REPOSITORY",
        repository_block,
        "android {",
        "nightly release repository provider",
    )
    build_config_block = """        buildConfigField(
            "String",
            "GITHUB_RELEASE_REPOSITORY",
            "\\\"$nightlyReleaseRepository\\\""
        )"""
    text = _insert_marker_block(
        text,
        "        // BEGIN BITCHAT NIGHTLY BUILD CONFIG",
        "        // END BITCHAT NIGHTLY BUILD CONFIG",
        build_config_block,
        "        testInstrumentationRunner = \"androidx.test.runner.AndroidJUnitRunner\"",
        "nightly BuildConfig field",
    )
    text = _replace_once(
        text,
        "            isEnable = enableSplits",
        "            isEnable = enableSplits &&\n                providers.environmentVariable(\"BITCHAT_NIGHTLY_UNIVERSAL_ONLY\").orNull != \"1\"",
        "ABI split toggle",
    )
    text = _replace_once(
        text,
        'applicationId = "com.bitchat.droid"',
        f'applicationId = "{APPLICATION_ID}"',
        "nightly application ID",
    )
    _write_if_changed(path, text)


def update_internal_broadcast_identifiers(worktree: Path) -> None:
    manifest_path = worktree / "app/src/main/AndroidManifest.xml"
    manifest = _read(manifest_path)
    old_permission = 'android:name="com.bitchat.android.permission.FORCE_FINISH"'
    new_permission = f'android:name="{FORCE_FINISH_PERMISSION}"'
    old_count = manifest.count(old_permission)
    new_count = manifest.count(new_permission)
    if old_count == 2 and new_count == 0:
        manifest = manifest.replace(old_permission, new_permission)
    elif old_count != 0 or new_count != 2:
        raise BrandingError(
            "expected exactly two original or two branded force-finish permission declarations"
        )
    _write_if_changed(manifest_path, manifest)

    constants_path = worktree / "app/src/main/java/com/bitchat/android/util/AppConstants.kt"
    constants = _read(constants_path)
    constants = _replace_once(
        constants,
        'const val ACTION_FORCE_FINISH: String = "com.bitchat.android.ACTION_FORCE_FINISH"',
        f'const val ACTION_FORCE_FINISH: String = "{FORCE_FINISH_ACTION}"',
        "force-finish action",
    )
    constants = _replace_once(
        constants,
        'const val PERMISSION_FORCE_FINISH: String = "com.bitchat.android.permission.FORCE_FINISH"',
        f'const val PERMISSION_FORCE_FINISH: String = "{FORCE_FINISH_PERMISSION}"',
        "force-finish permission constant",
    )
    _write_if_changed(constants_path, constants)


def update_release_client(worktree: Path) -> None:
    path = worktree / "app/src/main/java/com/bitchat/android/util/GitHubReleaseClient.kt"
    text = _read(path)
    text = _replace_regex_once(
        text,
        r'^(import com\.bitchat\.android\.)',
        r'import com.bitchat.android.BuildConfig\n\1',
        "BuildConfig import",
        already_applied="import com.bitchat.android.BuildConfig\n",
    )
    nightly_api_url = (
        '        private val GITHUB_API_URL =\n'
        '            "https://api.github.com/repos/${BuildConfig.GITHUB_RELEASE_REPOSITORY}/releases/latest"'
    )
    text = _replace_regex_once(
        text,
        r'^\s{8}private const val GITHUB_API_URL\s*=\s*(?:\n\s*)?'
        r'"https://api\.github\.com/repos/permissionlesstech/bitchat-android/releases/latest"$',
        nightly_api_url,
        "GitHub release API URL",
        already_applied=nightly_api_url,
    )
    nightly_source_url = (
        '            latestApkUrl =\n'
        '                "https://github.com/${BuildConfig.GITHUB_RELEASE_REPOSITORY}/releases/latest/" +\n'
        f'                "download/{APK_NAME}"'
    )
    text = _replace_regex_once(
        text,
        r'^\s{12}latestApkUrl\s*=\s*'
        r'"https://github\.com/permissionlesstech/bitchat-android/releases/latest/"\s*\+\s*\n\s*'
        r'"download/bitchat-android-universal\.apk"$',
        nightly_source_url,
        "GitHub release download URL",
        already_applied=nightly_source_url,
    )
    text = _replace_once(text, '    private const val USER_AGENT = "BitChat-Android"', '    private const val USER_AGENT = "bitchat-nightly-Android"', "GitHub user agent")
    text = _replace_regex_once(
        text,
        r'^(\s{8}private const val CACHE_TTL_MILLIS\s*=\s*[^\n]+)$',
        f'        private const val NIGHTLY_APK_NAME = "{APK_NAME}"\n'
        r'\1',
        "nightly asset-name constant",
        already_applied=f'        private const val NIGHTLY_APK_NAME = "{APK_NAME}"',
    )
    nightly_asset_check = (
        '                if (name == NIGHTLY_APK_NAME &&\n'
        '                    url.startsWith("https://")\n'
        '                ) {'
    )
    text = _replace_regex_once(
        text,
        r'^\s{16}if \(name\.contains\("universal", ignoreCase = true\) &&\s*'
        r'name\.endsWith\("\.apk"(?:, ignoreCase = true)?\)'
        r'(?:\s*&&\s*url\.startsWith\("https://"\))?\s*\) \{',
        nightly_asset_check,
        "universal APK selection",
        already_applied=nightly_asset_check,
    )
    _write_if_changed(path, text)

    test_path = worktree / "app/src/test/kotlin/com/bitchat/android/util/GitHubReleaseClientTest.kt"
    test_text = _read(test_path).replace("bitchat-android-universal.apk", APK_NAME)
    _write_if_changed(test_path, test_text)


def write_metadata(
    worktree: Path,
    version_name: str,
    version_code: int,
    repository: str,
    upstream_commit: str,
    cert_sha256: str,
) -> None:
    metadata = {
        "schemaVersion": 1,
        "branding": APP_NAME,
        "applicationId": APPLICATION_ID,
        "releaseRepository": repository,
        "releaseAsset": APK_NAME,
        "signingCertificateSha256": cert_sha256.lower(),
        "upstreamCommit": upstream_commit.lower(),
        "versionCode": version_code,
        "versionName": version_name,
    }
    _write_if_changed(worktree / "NIGHTLY_BUILD_METADATA.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")


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
    if not (worktree / ".git").exists():
        raise BrandingError(f"not a Git worktree: {worktree}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise BrandingError("repository must be owner/repository")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", args.upstream_commit):
        raise BrandingError("upstream commit must be a full Git object ID")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.cert_sha256.replace(":", "")):
        raise BrandingError("certificate fingerprint must be SHA-256")
    if args.version_code <= 0 or args.version_code > 2_100_000_000:
        raise BrandingError("version code is outside Android's supported range")
    if "-nightly." not in args.version_name:
        raise BrandingError("version name does not have a nightly suffix")

    update_app_names(worktree)
    update_icons(worktree)
    update_build_config(worktree, args.version_name, args.version_code)
    update_internal_broadcast_identifiers(worktree)
    update_release_client(worktree)
    write_metadata(
        worktree,
        args.version_name,
        args.version_code,
        args.repository,
        args.upstream_commit,
        args.cert_sha256.replace(":", ""),
    )

    validator = Path(__file__).with_name("validate_branding.py")
    subprocess.run(
        [
            sys.executable,
            str(validator),
            "--worktree",
            str(worktree),
            "--version-name",
            args.version_name,
            "--version-code",
            str(args.version_code),
            "--repository",
            args.repository,
            "--upstream-commit",
            args.upstream_commit,
            "--cert-sha256",
            args.cert_sha256,
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrandingError as error:
        print(f"branding error: {error}", file=sys.stderr)
        raise SystemExit(1)
