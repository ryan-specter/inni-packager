#!/usr/bin/env bash
# Build dist/Mirror Manager.app as a universal2 bundle.
#
# PyInstaller needs a universal2 interpreter and stable loader paths, so this
# uses the system Python (or PYTHON=/path/to/python3).  It creates a build venv
# under .build/ and needs no administrator rights.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/.build"
VENV="$BUILD/venv-app"
PYTHON="${PYTHON:-/usr/bin/python3}"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON does not exist" >&2
  exit 1
fi

REAL_PYTHON="$("$PYTHON" -c 'import sys; print(sys.executable)')"
ARCHS="$(lipo -archs "$REAL_PYTHON" 2>/dev/null || true)"
case "$ARCHS" in
  *x86_64*arm64*|*arm64*x86_64*) ;;
  *)
    echo "error: $REAL_PYTHON is not universal2 (architectures: ${ARCHS:-unknown})." >&2
    echo "Set PYTHON to a universal2 interpreter, for example the python.org build." >&2
    exit 1
    ;;
esac

mkdir -p "$BUILD"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip pyinstaller

cd "$ROOT/packaging"
"$VENV/bin/python" -m PyInstaller \
  --clean \
  --noconfirm \
  --distpath "$ROOT/dist" \
  --workpath "$BUILD/pyinstaller" \
  mirror_manager.spec

APP="$ROOT/dist/Mirror Manager.app"
BINARY="$APP/Contents/MacOS/Mirror Manager"

echo "==> $APP"
lipo -archs "$BINARY"
"$BINARY" --selftest
echo "Universal2 app built and self-tested."
