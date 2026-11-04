#!/usr/bin/env bash
# Build dist/Mirror Watch.app, the native menu-bar mirror app.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE="$ROOT/macos/MirrorWatch"
APP="$ROOT/dist/Mirror Watch.app"
CONFIGURATION="${CONFIGURATION:-release}"

echo "==> Building Mirror Watch ($CONFIGURATION)"
swift build --package-path "$PACKAGE" -c "$CONFIGURATION"
BIN_DIR="$(swift build --package-path "$PACKAGE" -c "$CONFIGURATION" --show-bin-path)"
BINARY="$BIN_DIR/MirrorWatch"

if [ ! -x "$BINARY" ]; then
  echo "error: built binary not found at $BINARY" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BINARY" "$APP/Contents/MacOS/MirrorWatch"
cp "$PACKAGE/Info.plist" "$APP/Contents/Info.plist"
chmod +x "$APP/Contents/MacOS/MirrorWatch"

/usr/bin/codesign --force --sign - --timestamp=none "$APP" >/dev/null

echo "==> $APP"
lipo -archs "$APP/Contents/MacOS/MirrorWatch"
"$APP/Contents/MacOS/MirrorWatch" --selftest
echo "Mirror Watch app built. Open it with: open \"$APP\""
