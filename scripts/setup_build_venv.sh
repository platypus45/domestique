#!/usr/bin/env bash
# v2.2.15 — create the isolated macOS BUILD venv used by build_dmg.sh.
#
# Why a dedicated venv (not the system Homebrew Python): the macOS app must run
# on macOS 11/12 (Monterey). Two host-specific problems otherwise leak into the
# bundle and break older macOS (see IP_MONTEREY_COMPAT.md):
#   1. Homebrew's python@3.12 + openssl stamp the BUILD host's OS as the minimum
#      (min-OS 15/26). build_dmg.sh re-stamps those to 11.0 (the code only uses
#      macOS-11-available symbols), so the venv interpreter inheriting them is OK.
#   2. numpy/scipy installed on a recent macOS pull the Apple Accelerate wheel,
#      which links $NEWLAPACK/$ILP64 symbols introduced in macOS 13.3 — absent on
#      Monterey. We force the OpenBLAS wheel variant (macOS<=12) at the SAME
#      versions, so there are no $NEWLAPACK symbols and the math is unchanged.
#
# Idempotent-ish: recreates .venv-build from scratch each run.
set -e
cd "$(dirname "$0")/.."

VENV=".venv-build"
echo "[venv] creating $VENV ..."
rm -rf "$VENV"
python3.12 -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip wheel
echo "[venv] installing app deps + pyinstaller ..."
"$VENV/bin/pip" install -q -r requirements.txt pyinstaller

# Force OpenBLAS numpy/scipy (macOS<=12 wheels) at the resolved versions so the
# Accelerate $NEWLAPACK (macOS-13.3+) dependency never enters the bundle.
NPV=$("$VENV/bin/python" -c "import numpy;print(numpy.__version__)")
SPV=$("$VENV/bin/python" -c "import scipy;print(scipy.__version__)")
echo "[venv] swapping numpy==$NPV scipy==$SPV to OpenBLAS (macosx_12) wheels ..."
TMP=$(mktemp -d)
"$VENV/bin/pip" download "numpy==$NPV" "scipy==$SPV" --only-binary :all: --no-deps \
    --platform macosx_12_0_x86_64 --python-version 312 --abi cp312 -d "$TMP"
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$TMP"/*.whl
rm -rf "$TMP"

BLAS=$("$VENV/bin/python" -c "import numpy;c=numpy.show_config('dicts');print(c['Build Dependencies']['blas']['name'])" 2>/dev/null || echo "?")
echo "[venv] numpy BLAS backend: $BLAS  (must NOT be 'accelerate')"
if echo "$BLAS" | grep -qi accelerate; then
    echo "[venv] ERROR: numpy still on Accelerate — Monterey build would fail" >&2
    exit 1
fi
echo "[venv] done."
