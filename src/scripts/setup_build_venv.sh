#!/usr/bin/env bash
# v2.2.16 — create the isolated macOS BUILD venv used by build_dmg.sh.
#
# The macOS app must run on macOS 11/12 (Monterey). The SYSTEM Homebrew
# python@3.12 is compiled with a deployment target = the build host's OS (e.g.
# 15), so its libpython *strong*-links symbols introduced after macOS 11/12
# (e.g. _mkfifoat / _mknodat, added in macOS 13) → dlopen fails on Monterey
# ("Symbol not found: _mkfifoat"). vtool min-OS re-stamping CANNOT fix a strong
# symbol reference.
#
# Fix: build from python-build-standalone CPython 3.12 (deployment target 10.15).
# It is the SAME CPython 3.12 — same version, same stdlib, same behaviour — only
# compiled for an older target, so it *weak*-links the macOS-13 symbols (they
# bind to NULL on 12 and CPython guards them at runtime). This is the standard
# Python for distributing macOS apps; nothing about the app changes.
#
# numpy/scipy are then forced onto the OpenBLAS wheel variant (NOT Apple
# Accelerate, whose $NEWLAPACK symbols need macOS 13.3) at the SAME versions.
set -e
cd "$(dirname "$0")/.."

PBS_TAG="20260623"
PBS_PY="3.12.13"
PBS_ASSET="cpython-${PBS_PY}+${PBS_TAG}-x86_64-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
PBS_DIR=".python-build"   # gitignored

if [ ! -x "$PBS_DIR/python/bin/python3.12" ]; then
    echo "[venv] fetching python-build-standalone CPython ${PBS_PY} (target 10.15) ..."
    rm -rf "$PBS_DIR"; mkdir -p "$PBS_DIR"
    curl -fsSL "$PBS_URL" -o "$PBS_DIR/pbs.tar.gz"
    tar -xzf "$PBS_DIR/pbs.tar.gz" -C "$PBS_DIR"
    rm -f "$PBS_DIR/pbs.tar.gz"
fi
PBS_PYBIN="$PBS_DIR/python/bin/python3.12"

# Sanity: the standalone libpython must WEAK-link the macOS-13 symbols (not strong).
LIBPY=$(find "$PBS_DIR/python" -name "libpython3.12*.dylib" | head -1)
# nm -mu = UNDEFINED imports only (excludes CPython's own defined _probe_* fns).
# A macOS-13 import must be 'weak'; a plain 'external' (strong) ref fails on 12.
if nm -mu "$LIBPY" 2>/dev/null | grep -E "_mkfifoat|_mknodat" | grep -qv "weak"; then
    echo "[venv] ERROR: standalone libpython strong-links a macOS-13 symbol — would fail on Monterey" >&2
    exit 1
fi
echo "[venv] standalone libpython weak-links macOS-13 symbols (Monterey-safe)"

VENV=".venv-build"
echo "[venv] creating $VENV from standalone CPython ..."
rm -rf "$VENV"
"$PBS_PYBIN" -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip wheel
echo "[venv] installing app deps + pyinstaller ..."
"$VENV/bin/pip" install -q -r requirements.txt pyinstaller

# Force OpenBLAS numpy/scipy (macOS<=12 wheels) at the LAST-SHIPPED versions, so
# the only thing this Monterey fix changes is the Python build target + the
# Accelerate->OpenBLAS swap — NOT the numpy/scipy version (no behaviour drift).
NPV="2.4.2"
SPV="1.17.1"
echo "[venv] pinning numpy==$NPV scipy==$SPV to OpenBLAS (macosx_12) wheels ..."
TMP=$(mktemp -d)
"$VENV/bin/pip" download "numpy==$NPV" "scipy==$SPV" --only-binary :all: --no-deps \
    --platform macosx_12_0_x86_64 --python-version 312 --abi cp312 -d "$TMP"
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$TMP"/*.whl
rm -rf "$TMP"

BLAS=$("$VENV/bin/python" -c "import numpy;c=numpy.show_config('dicts');print(c['Build Dependencies']['blas']['name'])" 2>/dev/null || echo "?")
echo "[venv] numpy BLAS backend: $BLAS  (must NOT be 'accelerate')"
echo "$BLAS" | grep -qi accelerate && { echo "[venv] ERROR: numpy still on Accelerate" >&2; exit 1; }
echo "[venv] done — build venv ready (standalone CPython + OpenBLAS)."
