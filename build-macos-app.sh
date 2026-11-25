#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build a self-contained Apple Silicon (arm64) .app bundle and a zip/DMG.
# The Python runtime is bundled, so the finished app needs no Homebrew, pip,
# Xcode or system Python. Run this script on macOS/arm64 for a signed DMG, or
# anywhere for the portable .app.zip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${BUILD_DIR:-$HERE/build/macos}"
OUTPUT_DIR="${OUTPUT_DIR:-$HERE}"
VERSION="${VERSION:-$(cat "$HERE/VERSION" 2>/dev/null || echo 0.0.0)}"
APP="$BUILD/HifiMagic.app"
CONTENTS="$APP/Contents"
RESOURCES="$CONTENTS/Resources"
PYTHON_ROOT="$BUILD/python"

ZIP="$OUTPUT_DIR/HifiMagic-${VERSION}-apple-silicon.zip"
DMG="$OUTPUT_DIR/HifiMagic-${VERSION}-apple-silicon.dmg"

# python-build-standalone release used for the embedded runtime.
PY_TAG="${PY_TAG:-20261123}"
PY_ASSET="${PY_ASSET:-cpython-3.11.18+${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz}"
PY_URL="${PY_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${PY_ASSET}}"
CACHE="$BUILD/$PY_ASSET"

mkdir -p "$BUILD" "$OUTPUT_DIR"

download() {
  local url="$1" file="$2"
  if [[ ! -s "$file" ]]; then
    echo "Downloading $(basename "$file")..."
    curl -fL --retry 2 --max-time 600 "$url" -o "$file"
  fi
}

download "$PY_URL" "$CACHE"

if [[ ! -x "$PYTHON_ROOT/bin/python3.11" ]]; then
  echo "Extracting Python runtime..."
  rm -rf "$PYTHON_ROOT"
  mkdir -p "$PYTHON_ROOT"
  tar -xzf "$CACHE" -C "$PYTHON_ROOT" --strip-components=1
fi

echo "Assembling HifiMagic.app..."
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$RESOURCES/hifimagic/web"

cp -a "$PYTHON_ROOT/." "$RESOURCES/python/"
# Bundle every top-level app module so new files cannot be forgotten here.
cp "$HERE"/*.py "$RESOURCES/hifimagic/"
cp "$HERE/VERSION" "$RESOURCES/hifimagic/VERSION"
cp "$HERE/LICENSE" "$RESOURCES/hifimagic/LICENSE"
cp "$HERE/web/index.html" "$HERE/web/app.js" "$HERE/web/styles.css" "$HERE/web/logo.png" "$RESOURCES/hifimagic/web/"

# Use a real .icns icon when building on macOS; keep a PNG fallback elsewhere.
ICON_FILE="icon.png"
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$BUILD/hifimagic.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$HERE/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z "$((size * 2))" "$((size * 2))" "$HERE/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RESOURCES/icon.icns"
  ICON_FILE="icon.icns"
else
  cp "$HERE/icon.png" "$RESOURCES/icon.png"
fi

cat > "$CONTENTS/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>HifiMagic</string>
  <key>CFBundleIdentifier</key>
  <string>org.iris3th.hifimagic</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>HifiMagic Media Manager</string>
  <key>CFBundleDisplayName</key>
  <string>HifiMagic Media Manager</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>CFBundleIconFile</key>
  <string>$ICON_FILE</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSHumanReadableCopyright</key>
  <string>(c) iris3th</string>
</dict>
</plist>
EOF

cat > "$CONTENTS/MacOS/HifiMagic" <<'EOF'
#!/bin/sh
# Locate the bundle contents regardless of where the .app has been copied.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
CONTENTS="$(cd "$HERE/.." && pwd)"
PYTHON="$CONTENTS/Resources/python/bin/python3.11"
APP="$CONTENTS/Resources/hifimagic/hifimagic.py"
if [ ! -x "$PYTHON" ]; then
  echo "HifiMagic: bundled Python runtime is missing or not executable" >&2
  exit 1
fi
# Ship a subtle Support link by default; users can override or hide it.
if [ "${HIFIMAGIC_SUPPORT_URL+x}" = "" ]; then
  HIFIMAGIC_SUPPORT_URL="https://buy.stripe.com/bJebIU5en43t2EY43Dawo00"
fi
export HIFIMAGIC_SUPPORT_URL
exec "$PYTHON" "$APP" "$@"
EOF
chmod +x "$CONTENTS/MacOS/HifiMagic"
chmod -R a+rX "$APP"

echo "Creating $(basename "$ZIP")..."
rm -f "$ZIP"
if command -v ditto >/dev/null 2>&1; then
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
else
  (cd "$BUILD" && python3 -m zipfile -c "$ZIP" HifiMagic.app)
fi

if command -v hdiutil >/dev/null 2>&1; then
  echo "Creating $(basename "$DMG")..."
  rm -f "$DMG"
  hdiutil create -volname "HifiMagic" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null
else
  echo "Skipping DMG: hdiutil is only available on macOS."
  DMG=""
fi

echo "Done: $ZIP"
if [[ -n "$DMG" ]]; then
  echo "      $DMG"
fi
