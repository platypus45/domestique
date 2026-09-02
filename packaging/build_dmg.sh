#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Domestique DMG Builder — Standard Operating Procedure
# ═══════════════════════════════════════════════════════════════
# Usage: ./build_dmg.sh
# Output: ~/Desktop/Domestique.dmg
#
# Notarization flow (when .notarize.env is present and complete):
#
#   1. PyInstaller builds dist/Domestique.app.
#   2. Resolve signing identity (existing keychain entry, else .p12).
#   3. Strip PyInstaller's ad-hoc signatures from every nested binary.
#   4. Sign nested .dylib / .so depth-first with Developer ID identity.
#   5. Submit .app to Apple notarytool, staple ticket DIRECTLY ON .app.
#   6. Build DMG containing the stapled .app.
#   7. Sign DMG, submit to notarytool, staple DMG ticket too.
#
# Critical: stapling the .app means the notarization ticket TRAVELS
# with the .app when extracted from the DMG. Without that step the
# extracted .app on /Applications/ fails Gatekeeper despite the DMG
# being notarized.
#
# Ad-hoc fallback when .notarize.env is missing/incomplete.
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")/.."  # scripts live in packaging/; build from the repo root

# v2.2.15 — Monterey (macOS 12) compatibility (see IP_MONTEREY_COMPAT.md):
#   * MACOSX_DEPLOYMENT_TARGET=11.0 so anything PyInstaller compiles targets 11.
#   * Build from the isolated .venv-build (OpenBLAS numpy/scipy — NOT the system
#     Homebrew Python's Accelerate build, which links $NEWLAPACK = macOS 13.3+).
#   * Step [3a] re-stamps every bundled Mach-O's min-OS to 11.0 + a proof gate
#     ABORTS before signing if anything still needs > macOS 12.
export MACOSX_DEPLOYMENT_TARGET=11.0
MACOS_MIN="11.0"
PYINSTALLER="pyinstaller"
if [ ! -x ".venv-build/bin/pyinstaller" ]; then
    echo "  Build venv missing — creating it (OpenBLAS numpy/scipy)..."
    bash src/scripts/setup_build_venv.sh
fi
if [ -x ".venv-build/bin/pyinstaller" ]; then
    PYINSTALLER=".venv-build/bin/pyinstaller"
    echo "  Using isolated build venv (.venv-build) for OpenBLAS numpy/scipy"
fi

DMG_NAME="Domestique"
DMG_PATH="$HOME/Desktop/${DMG_NAME}.dmg"
STAGING="/tmp/dmg_staging"
RW_DMG="/tmp/${DMG_NAME}_rw.dmg"
APP_ZIP="/tmp/${DMG_NAME}.app.zip"
ICON_PNG="assets/icon.png"
ICON_ICNS="assets/icon.icns"
ENTITLEMENTS="packaging/entitlements.plist"

NOTARIZE_MODE="adhoc"
if [ -f .notarize.env ]; then
    # shellcheck disable=SC1091
    source .notarize.env
    if [ -n "$NOTARIZE_IDENTITY" ] && [ -n "$NOTARIZE_APPLE_ID" ] \
       && [ -n "$NOTARIZE_APP_PASSWORD" ] && [ -n "$NOTARIZE_TEAM_ID" ]; then
        NOTARIZE_MODE="notarize"
    fi
fi

echo "=== Domestique DMG Build (mode: $NOTARIZE_MODE) ==="

# Pre-flight: detach any stale Domestique volumes left behind by prior
# failed builds. hdiutil's Finder-layout step at [7/9] silently exits with
# "Write Permissions Error (-61)" if /Volumes/Domestique* is already
# mounted — usually because a previous run crashed before detaching.
# Repeated failures leave /Volumes/Domestique 1, /Volumes/Domestique 2, ...
# until the build can never finish without manual recovery.
for mp in $(ls -d "/Volumes/${DMG_NAME}"* 2>/dev/null); do
    echo "  Detaching stale volume: $mp"
    hdiutil detach "$mp" -force >/dev/null 2>&1 || true
done

# 1. Build with PyInstaller
echo "[1/9] Building app with PyInstaller..."
"$PYINSTALLER" packaging/domestique.spec --clean --noconfirm 2>&1 | tail -3

# 1b. Version smoke-test — the bundled app MUST ship its own VERSION file with the
# right contents, else the running app reports "0.0.0" and the in-app updater shows
# a perpetual "upgrade" prompt. Fail the build before notarize/upload if it regresses.
REPO_VER="$(tr -d '[:space:]' < VERSION)"
BUNDLED_VER_FILE="$(find dist/Domestique.app -name VERSION -path '*Resources*' 2>/dev/null | head -1)"
[ -z "$BUNDLED_VER_FILE" ] && BUNDLED_VER_FILE="$(find dist/Domestique.app -name VERSION 2>/dev/null | head -1)"
BUNDLED_VER="$(tr -d '[:space:]' < "$BUNDLED_VER_FILE" 2>/dev/null)"
if [ -z "$BUNDLED_VER_FILE" ] || [ "$BUNDLED_VER" != "$REPO_VER" ]; then
    echo "FATAL: bundled VERSION ('$BUNDLED_VER') != repo VERSION ('$REPO_VER') — app would misreport its version. Aborting build." >&2
    exit 1
fi
echo "[1b/9] Version smoke-test OK — bundle reports $BUNDLED_VER"

# 1b2. Linux-IP D4 — the bundled workout library must survive packaging. A
# bundle that loses workouts/ (or its hidden classification/facts sidecars)
# still builds, boots and serves /api/version, then silently degrades every
# plan to endurance-only ("Z2 Steady" everywhere — the v3.11.0 Linux report
# class, same family as the v3.3.0 frozen-install storm).
BUNDLED_WK_DIR="$(find dist/Domestique.app -type d -name workouts -path '*Resources*' 2>/dev/null | head -1)"
[ -z "$BUNDLED_WK_DIR" ] && BUNDLED_WK_DIR="$(find dist/Domestique.app -type d -name workouts 2>/dev/null | head -1)"
if [ -z "$BUNDLED_WK_DIR" ]; then
    echo "FATAL: no workouts/ directory inside the bundle — plans would degrade to endurance-only. Aborting build." >&2
    exit 1
fi
ZWO_N="$(find "$BUNDLED_WK_DIR" -maxdepth 1 -name '*.zwo' | wc -l | tr -d ' ')"
if [ "$ZWO_N" -lt 4000 ]; then
    echo "FATAL: bundled workout library holds $ZWO_N .zwo files (<4000). Aborting build." >&2
    exit 1
fi
for SIDE in .content_classification.json .workout_facts.json .library_index.json; do
    if [ ! -f "$BUNDLED_WK_DIR/$SIDE" ]; then
        echo "FATAL: $SIDE missing from the bundled workouts/ — classification/facts would fail closed and empty the HIT pools. Aborting build." >&2
        exit 1
    fi
done
echo "[1b2/9] Library smoke-test OK — $ZWO_N workouts + sidecars bundled"

# 1b3. OAuth-secret gate (parity with the Windows CI check): the spec bundles
# the gitignored .oauth.env only when it exists at build time — a checkout
# without it (fresh clone, WORKTREE) silently ships a DMG whose intervals.icu
# OAuth sign-in fails with an "exchange" error. Refuse instead.
BUNDLED_OAUTH="$(find dist/Domestique.app -name '.oauth.env' 2>/dev/null | head -1)"
if [ -z "$BUNDLED_OAUTH" ] || ! grep -q 'ICU_OAUTH_CLIENT_SECRET=..*' "$BUNDLED_OAUTH"; then
    echo "FATAL: .oauth.env missing or empty in the bundle — OAuth sign-in would ship broken. Put .oauth.env at the repo root and rebuild." >&2
    exit 1
fi
echo "[1b3/9] OAuth-secret gate OK — client secret bundled"

# 1b4. Frozen-module gate: fit_tool resolves FIT message classes through
# message_factory, which imports all 90 message modules — reachable only via
# a lazy import PyInstaller's static scan never followed. Every frozen build
# through v3.11.1 lacked device_info_message, so a real Garmin FIT (always
# carries device_info) failed to parse. Assert the module is in the archive.
PYZ_FILE="$(find dist/Domestique.app -name 'PYZ-00.pyz' | head -1)"
if [ -z "$PYZ_FILE" ] || ! .venv-build/bin/pyi-archive_viewer -l "$PYZ_FILE" 2>/dev/null | grep -q "fit_tool.profile.messages.device_info_message"; then
    echo "FATAL: fit_tool.profile.messages.device_info_message not in the frozen archive — FIT import would fail on real rides." >&2
    exit 1
fi
echo "[1b4/9] FIT-message gate OK — fit_tool message modules bundled"

# 1c. HR-mode FIT smoke (v2.5.0 P1.5) — the desktop save path builds FIT bytes
# in-process (launcher.JsApi save_fit bridge), a path the web tests can't reach.
# Prove the bundled code produces HEART_RATE-target steps for view='hr' before
# notarize/upload; a regression here would silently ship power-only FITs to
# HR-mode riders using the native save dialog.
# python3.12 explicitly: the system python3 (3.9) can't import app.py
# (PEP 604 unions in FastAPI annotations evaluate at import time).
# The workout is CHOSEN FROM THE LIBRARY, never named literally. A hardcoded
# filename made this smoke test fail the whole build the first time the library
# was renamed — and it failed for the one reason a smoke test must not, a stale
# fixture rather than a real defect in the thing under test. Any threshold file
# exercises the same code path.
FIT_SMOKE="$(python3.12 - <<'PYEOF'
import sys
sys.path.insert(0, "src")
try:
    import app
    from pathlib import Path
    picks = sorted(Path("src/workouts").glob("threshold_*.zwo"))
    if not picks:
        raise SystemExit("no threshold workout in the library to smoke-test")
    data = app.build_fit_workout_bytes("z2", 56, "smoke",
                                       picks[0].name, view="hr")
    import fitparse
    steps = list(fitparse.FitFile(data).get_messages("workout_step"))
    hr = sum(1 for m in steps
             for f in m.fields if f.name == "target_type" and f.value == "heart_rate")
    print("OK" if hr >= 1 else f"NO_HR_STEPS ({len(steps)} steps)")
except Exception as e:  # noqa: BLE001
    print(f"ERROR {e}")
PYEOF
)"
if [ "$FIT_SMOKE" != "OK" ]; then
    echo "FATAL: HR-mode FIT smoke failed: $FIT_SMOKE — the desktop FIT save would ship broken for HR riders. Aborting build." >&2
    exit 1
fi
echo "[1c/9] HR-mode FIT smoke OK — view='hr' emits HEART_RATE steps"

if [ "$NOTARIZE_MODE" = "notarize" ]; then
    # 2. Resolve signing identity.
    #
    # Use the user's login keychain if the identity is already there
    # (typical local-dev case). Fall back to importing the .p12 into
    # a temp keychain when no existing identity (CI runner with fresh
    # login keychain).
    echo "[2/9] Resolving Developer ID Application identity..."
    if security find-identity -v -p codesigning 2>&1 | grep -q "$NOTARIZE_IDENTITY"; then
        echo "✓ Identity found in existing keychain — skipping .p12 import"
    elif [ -n "$NOTARIZE_P12_PATH" ] && [ -f "$NOTARIZE_P12_PATH" ]; then
        echo "  Identity missing → importing $NOTARIZE_P12_PATH"
        BUILD_KEYCHAIN="/tmp/domestique_build.keychain"
        KEYCHAIN_PASS="$(openssl rand -base64 24)"
        security delete-keychain "$BUILD_KEYCHAIN" 2>/dev/null || true
        security create-keychain -p "$KEYCHAIN_PASS" "$BUILD_KEYCHAIN"
        security set-keychain-settings -lut 21600 "$BUILD_KEYCHAIN"
        security unlock-keychain -p "$KEYCHAIN_PASS" "$BUILD_KEYCHAIN"
        security import "$NOTARIZE_P12_PATH" -k "$BUILD_KEYCHAIN" \
            -P "$NOTARIZE_P12_PASSWORD" -T /usr/bin/codesign -A
        security list-keychains -d user -s "$BUILD_KEYCHAIN" $(security list-keychains -d user | tr -d '"')
        security set-key-partition-list -S apple-tool:,apple:,codesign: \
            -s -k "$KEYCHAIN_PASS" "$BUILD_KEYCHAIN" >/dev/null
        cleanup_keychain() {
            security delete-keychain "$BUILD_KEYCHAIN" 2>/dev/null || true
        }
        trap cleanup_keychain EXIT
    else
        echo "✗ ERROR: identity not in any keychain AND no .p12 file at:"
        echo "  $NOTARIZE_P12_PATH"
        echo "  Either re-export the cert via Keychain Access (My"
        echo "  Certificates → right-click → Export → .p12) and put it"
        echo "  back at the path in .notarize.env, OR re-import the"
        echo "  cert into your login keychain."
        exit 1
    fi

    # 3. Strip PyInstaller's ad-hoc inner signatures so our --force
    # resign replaces them cleanly. v1.8.5 fix: detect Mach-O files by
    # CONTENT (`file ... : Mach-O`) instead of extension, so framework
    # binaries like Python.framework/Versions/3.12/Python (no extension)
    # also get re-signed. The previous *.dylib / *.so glob missed those
    # → notarization rejected with "binary is not signed with a valid
    # Developer ID certificate".
    # v1.8.5 fix: enumerate Mach-O binaries via null-delimited
    # find → while-read loop. Previous attempt used xargs which choked
    # ("command line cannot be assembled, too long") because the
    # PyInstaller bundle has thousands of files and xargs couldn't
    # batch them. Without xargs success, the resign loop silently did
    # nothing, leaving libssl/libcrypto/libsqlite/libmpdec/_cffi_backend
    # unsigned → notarization rejected.
    BUNDLE="dist/${DMG_NAME}.app"
    MACHO_LIST_FILE="/tmp/${DMG_NAME}_macho_list.txt"
    > "$MACHO_LIST_FILE"
    echo "[3/9] Enumerating Mach-O binaries by content..."
    find "$BUNDLE" -type f -print0 | while IFS= read -r -d '' f; do
        if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then
            echo "$f" >> "$MACHO_LIST_FILE"
        fi
    done
    MACHO_COUNT=$(wc -l < "$MACHO_LIST_FILE" | tr -d ' ')
    echo "  Found $MACHO_COUNT Mach-O binaries"

    # [3a/9] v2.2.15 — re-stamp every bundled Mach-O's minimum-OS to 11.0 so dyld
    # loads them on macOS 11/12. Homebrew Python/openssl + some wheels stamp the
    # BUILD host's OS (14/15/26) as the minimum even though the code uses only
    # macOS-11-available symbols (verified: IP_MONTEREY_COMPAT.md). Done BEFORE
    # signing — it invalidates signatures, which the [4/9] resign repairs.
    echo "[3a/9] Re-stamping min-OS -> ${MACOS_MIN} on all Mach-O..."
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        sdk=$(vtool -show-build "$f" 2>/dev/null | awk '/sdk/{print $2; exit}')
        vtool -set-build-version macos "$MACOS_MIN" "${sdk:-$MACOS_MIN}" \
            -replace -output "$f" "$f" >/dev/null 2>&1 || true
    done < "$MACHO_LIST_FILE"

    # PROOF GATE — abort before signing/notarizing if anything still needs
    # > macOS 12, or any $NEWLAPACK (macOS-13.3 Accelerate) symbol slipped in
    # (i.e. numpy/scipy not on the OpenBLAS wheels).
    echo "[3a/9] Verifying Monterey compatibility..."
    BAD_MINOS=$(while IFS= read -r f; do
        [ -n "$f" ] || continue
        mo=$(vtool -show-build "$f" 2>/dev/null | awk '/minos/{print $2; exit}')
        [ -z "$mo" ] && continue
        major=${mo%%.*}
        if [ "${major:-0}" -gt 12 ] 2>/dev/null; then echo "$mo  $f"; fi
    done < "$MACHO_LIST_FILE")
    NEWLAPACK=$(while IFS= read -r f; do
        [ -n "$f" ] || continue
        nm -mu "$f" 2>/dev/null | grep -c NEWLAPACK
    done < "$MACHO_LIST_FILE" | awk '{s+=$1} END{print s+0}')
    # STRONG (non-weak) refs to macOS-13 symbols = a hard dlopen failure on
    # Monterey (this was the _mkfifoat bug). Weak refs are fine (bind to NULL +
    # guarded at runtime). Catches a Homebrew-built Python/extension slipping in.
    STRONG13=$(while IFS= read -r f; do
        [ -n "$f" ] || continue
        # nm -mu = UNDEFINED imports only (skip defined _probe_* fns). A macOS-13
        # symbol must be a 'weak' import; a strong (non-weak) one fails on 12.
        nm -mu "$f" 2>/dev/null | grep -E "_mkfifoat|_mknodat" | grep -v "weak" \
            | sed "s#\$#  ${f}#"
    done < "$MACHO_LIST_FILE")
    if [ -n "$BAD_MINOS" ]; then
        echo "  ✗ FAIL — binaries still require > macOS 12:"; echo "$BAD_MINOS" | head; exit 1
    fi
    if [ "$NEWLAPACK" -ne 0 ]; then
        echo "  ✗ FAIL — $NEWLAPACK \$NEWLAPACK (macOS-13.3) symbol refs remain; numpy/scipy not on OpenBLAS"; exit 1
    fi
    if [ -n "$STRONG13" ]; then
        echo "  ✗ FAIL — STRONG refs to macOS-13 symbols (won't load on Monterey):"; echo "$STRONG13" | head; exit 1
    fi
    echo "  ✓ all Mach-O minos <= 12, zero \$NEWLAPACK, no strong macOS-13 refs — Monterey-compatible"

    echo "[3b/9] Stripping PyInstaller's ad-hoc inner signatures..."
    while IFS= read -r f; do
        [ -n "$f" ] && codesign --remove-signature "$f" 2>/dev/null || true
    done < "$MACHO_LIST_FILE"
    codesign --remove-signature "$BUNDLE" 2>/dev/null || true

    # 4. Sign every Mach-O depth-first with Developer ID + hardened
    # runtime + entitlements + secure timestamp. Use a sort by path
    # length (deepest first) so framework binaries sign before their
    # parent framework bundles.
    echo "[4/9] Codesigning every Mach-O binary depth-first..."
    awk '{ print length, $0 }' "$MACHO_LIST_FILE" | sort -rn | cut -d' ' -f2- | \
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        codesign --force --options runtime --timestamp \
            --entitlements "$ENTITLEMENTS" \
            --sign "$NOTARIZE_IDENTITY" "$f" 2>&1 | tail -1
    done
    # Sign framework bundles (they're directories — not in MACHO list).
    find "$BUNDLE" -depth -name '*.framework' -type d | while IFS= read -r fw; do
        codesign --force --options runtime --timestamp \
            --entitlements "$ENTITLEMENTS" \
            --sign "$NOTARIZE_IDENTITY" "$fw" 2>&1 | tail -1
    done
    # Finally seal the main app bundle.
    codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" \
        --sign "$NOTARIZE_IDENTITY" "$BUNDLE"
    codesign --verify --deep --strict --verbose=2 "$BUNDLE" 2>&1 | tail -3

    # 5. Notarize the .app + staple ticket onto it.
    echo "[5/9] Zipping .app and submitting to Apple notarytool (5-30 min)..."
    rm -f "$APP_ZIP"
    ditto -c -k --keepParent "dist/${DMG_NAME}.app" "$APP_ZIP"
    xcrun notarytool submit "$APP_ZIP" \
        --apple-id "$NOTARIZE_APPLE_ID" \
        --team-id "$NOTARIZE_TEAM_ID" \
        --password "$NOTARIZE_APP_PASSWORD" \
        --wait
    echo "[5b/9] Stapling notarization ticket onto .app..."
    xcrun stapler staple "dist/${DMG_NAME}.app"
    xcrun stapler validate "dist/${DMG_NAME}.app" && echo "✓ .app ticket stapled and valid"
else
    # Ad-hoc fallback (v1.8.4 behaviour, no Gatekeeper-clean download).
    echo "[2-5/9] Ad-hoc codesigning .app bundle..."
    codesign --force --deep --sign - "dist/${DMG_NAME}.app" 2>&1 | tail -3
    codesign --verify --deep --strict "dist/${DMG_NAME}.app" && echo "✓ Ad-hoc signature verified" || echo "⚠ Signature verification failed"
fi

# 6. Stage DMG contents.
echo "[6/9] Staging DMG contents..."
rm -rf "$STAGING" "$RW_DMG" "$DMG_PATH"
mkdir -p "$STAGING"
cp -R "dist/${DMG_NAME}.app" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
cp "$ICON_ICNS" "$STAGING/.VolumeIcon.icns"
SetFile -a C "$STAGING"

# 7. Build the DMG.
echo "[7/9] Creating DMG with custom Finder layout..."
hdiutil create -volname "$DMG_NAME" -srcfolder "$STAGING" -ov -format UDRW "$RW_DMG" > /dev/null
hdiutil attach "$RW_DMG" -readwrite -noverify > /dev/null 2>&1

# v1.8.23 — the Finder icon-layout is COSMETIC (icon positions + window size).
# It needs Automation (Apple-events→Finder) TCC permission, which a DETACHED /
# background build process does not have → fails with "-1743 Not authorised to
# send Apple events to Finder" and, when run with `set -e`, aborts the whole
# build at [7/9] after the .app is already signed + stapled. Make it NON-FATAL:
# if the layout can't be applied, the DMG still ships (just with default icon
# arrangement). The functional content + notarization are unaffected.
osascript <<EOF 2>/dev/null || echo "  ⚠ Finder layout skipped (no Automation permission — cosmetic only)"
tell application "Finder"
    tell disk "${DMG_NAME}"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 200, 720, 520}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 128
        set position of item "${DMG_NAME}.app" of container window to {130, 150}
        set position of item "Applications" of container window to {390, 150}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
EOF

SetFile -a C "/Volumes/${DMG_NAME}" 2>/dev/null || true
sync
sleep 2
hdiutil detach "/Volumes/${DMG_NAME}" -force > /dev/null 2>&1 || true

# 8. Compress + sign + notarize the DMG container itself.
echo "[8/9] Compressing DMG..."
hdiutil convert "$RW_DMG" -format UDZO -o "$DMG_PATH" > /dev/null 2>&1

if [ "$NOTARIZE_MODE" = "notarize" ]; then
    codesign --force --sign "$NOTARIZE_IDENTITY" "$DMG_PATH" 2>&1 | tail -2

    echo "[8b/9] Submitting DMG to Apple notarytool (5-30 min)..."
    xcrun notarytool submit "$DMG_PATH" \
        --apple-id "$NOTARIZE_APPLE_ID" \
        --team-id "$NOTARIZE_TEAM_ID" \
        --password "$NOTARIZE_APP_PASSWORD" \
        --wait

    echo "[8c/9] Stapling notarization ticket onto DMG..."
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH" && echo "✓ DMG ticket stapled and valid"
else
    codesign --force --sign - "$DMG_PATH" 2>&1 | tail -2 || true
fi

# 9. Custom icon on the .dmg file.
echo "[9/9] Setting custom icon on DMG file..."
if command -v fileicon &> /dev/null; then
    fileicon set "$DMG_PATH" "$ICON_PNG" > /dev/null 2>&1
    echo "✓ Custom icon set via fileicon"
else
    echo "⚠ fileicon not installed (npm install -g fileicon) — DMG will have generic icon"
fi

# v1.8.16 — reclaim the ~570 MB of intermediate staging this build created
# (dmg_staging ~217M, _rw.dmg ~273M, .app.zip ~81M). Left uncleaned these
# accumulated across releases and filled the disk mid-session. The final
# compressed DMG at $DMG_PATH is all we keep; everything else is scratch.
rm -rf "$STAGING" "$RW_DMG" "$APP_ZIP" 2>/dev/null || true

SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo ""
echo "=== Build complete ==="
echo "DMG:  $DMG_PATH ($SIZE)"
echo "Mode: $NOTARIZE_MODE"
if [ "$NOTARIZE_MODE" = "notarize" ]; then
    echo "  ✓ .app stapled with notarization ticket (survives extraction)"
    echo "  ✓ DMG stapled with notarization ticket"
    echo "  ✓ Downloaded DMG opens with zero Gatekeeper prompts"
fi
