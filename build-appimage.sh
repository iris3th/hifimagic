#!/usr/bin/env bash
# Build a single-file AppImage that contains its own Python 3.11 runtime.
# Requires only curl, tar and coreutils on the build machine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
APPDIR="$BUILD/AppDir"
OUTPUT="$HERE/StreamMagic-CXN100-x86_64.AppImage"

PY_VERSION="3.11.17"
PY_URL="https://github.com/niess/python-appimage/releases/download/python3.11/python${PY_VERSION}-cp311-cp311-manylinux2014_x86_64.AppImage"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

mkdir -p "$BUILD"

download() {
  local url="$1" file="$2"
  if [[ ! -s "$file" ]]; then
    echo "Downloading $(basename "$file")..."
    curl -fL --retry 2 --max-time 300 "$url" -o "$file"
  fi
}

download "$PY_URL" "$BUILD/python.AppImage"
download "$TOOL_URL" "$BUILD/appimagetool.AppImage"
chmod +x "$BUILD/python.AppImage" "$BUILD/appimagetool.AppImage"

if [[ ! -d "$BUILD/pyextract/squashfs-root/opt/python3.11" ]]; then
  echo "Extracting Python runtime..."
  mkdir -p "$BUILD/pyextract"
  (cd "$BUILD/pyextract" && ../python.AppImage --appimage-extract >/dev/null)
fi
if [[ ! -x "$BUILD/toolextract/squashfs-root/usr/bin/appimagetool" ]]; then
  echo "Extracting appimagetool..."
  mkdir -p "$BUILD/toolextract"
  (cd "$BUILD/toolextract" && ../appimagetool.AppImage --appimage-extract >/dev/null)
fi

echo "Assembling AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/streammagic/web"
cp -a "$BUILD/pyextract/squashfs-root/." "$APPDIR/"
rm -f "$APPDIR/python.png" "$APPDIR/python3.11.17.desktop"
cp "$HERE/cxn100.py" "$HERE/smoip.py" "$HERE/server.py" "$APPDIR/usr/share/streammagic/"
cp "$HERE/web/index.html" "$HERE/web/app.js" "$HERE/web/styles.css" "$APPDIR/usr/share/streammagic/web/"
cp "$HERE/icon.png" "$APPDIR/streammagic.png"
rm -f "$APPDIR/.DirIcon"
cp "$HERE/icon.png" "$APPDIR/.DirIcon"

cat > "$APPDIR/streammagic.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=StreamMagic CXN100
Comment=Controller for Cambridge Audio network players
Exec=AppRun
Icon=streammagic
Categories=AudioVideo;Audio;
Terminal=false
EOF

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# The AppImage runtime may invoke us from usr/bin/AppRun, so walk upwards
# until we find the bundled Python runtime.
if [ -n "${APPDIR:-}" ] && [ -x "$APPDIR/opt/python3.11/bin/python3.11" ]; then
  HERE="$APPDIR"
else
  HERE="$(dirname "$(readlink -f "$0")")"
  while [ ! -x "$HERE/opt/python3.11/bin/python3.11" ] && [ "$HERE" != "/" ]; do
    HERE="$(dirname "$HERE")"
  done
fi
export APPDIR="$HERE"
export TCL_LIBRARY="$HERE/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$HERE/usr/share/tcltk/tcl8.6"
export TKPATH="$TK_LIBRARY"
export SSL_CERT_FILE="$HERE/opt/_internal/certs.pem"
exec "$HERE/opt/python3.11/bin/python3.11" "$HERE/usr/share/streammagic/cxn100.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"
chmod -R a+rX "$APPDIR"

echo "Creating AppImage..."
rm -f "$OUTPUT"
PATH="$BUILD/toolextract/squashfs-root/usr/bin:$PATH" \
  "$BUILD/toolextract/squashfs-root/usr/bin/appimagetool" --no-appstream "$APPDIR" "$OUTPUT"
chmod +x "$OUTPUT"
echo "Done: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
