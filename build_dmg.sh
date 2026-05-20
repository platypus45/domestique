#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Domestique DMG Builder — Standard Operating Procedure
# ═══════════════════════════════════════════════════════════════
# Usage: ./build_dmg.sh
# Output: ~/Desktop/Domestique.dmg
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

DMG_NAME="Domestique"
DMG_PATH="$HOME/Desktop/${DMG_NAME}.dmg"
STAGING="/tmp/dmg_staging"
RW_DMG="/tmp/${DMG_NAME}_rw.dmg"
ICON_PNG="assets/icon.png"
ICON_ICNS="assets/icon.icns"

echo "=== Domestique DMG Build ==="

# 1. Build with PyInstaller
echo "[1/6] Building app with PyInstaller..."
pyinstaller domestique.spec --clean --noconfirm 2>&1 | tail -3

# 1b. Ad-hoc codesign the .app bundle. Without ANY signature, downloaded
# DMGs trigger macOS Gatekeeper's "Domestique is damaged and can't be
# opened. You should move it to the Bin." dialog. With an ad-hoc
# signature (`-` identity), the dialog softens to "unidentified
# developer" which is more bypassable.
#
# IMPORTANT: do NOT add `--options runtime` to the ad-hoc step.
# Hardened runtime enforces library validation — every loaded .dylib
# / .framework must share the binary's Team ID. Ad-hoc signing has
# NO Team ID. PyInstaller-bundled apps load ~150 nested libs at
# startup; with hardened runtime + ad-hoc, the dyld loader fails on
# the first nested framework with:
#     "code signature in <Python.framework> not valid for use in
#      process: mapping process and mapped file (non-platform) have
#      different Team IDs"
# and the app silently refuses to launch (no Console error, no UI).
# Hardened runtime is REQUIRED for notarization but ONLY when a real
# Developer ID cert is in play. When this script switches to Developer
# ID signing (v1.8.5+ once the cert lands), re-add `--options runtime`
# alongside `--sign "Developer ID Application: ..."`.
#
# `--deep` signs nested frameworks (PyInstaller bundles ~150
# dylibs/.so). `--force` overwrites prior signatures.
echo "[2/6] Ad-hoc codesigning .app bundle (no hardened runtime — ad-hoc has no Team ID)..."
codesign --force --deep --sign - "dist/${DMG_NAME}.app" 2>&1 | tail -3
codesign --verify --deep --strict "dist/${DMG_NAME}.app" && echo "✓ Ad-hoc signature verified" || echo "⚠ Signature verification failed"

# 2. Stage DMG contents
echo "[3/6] Staging DMG contents..."
rm -rf "$STAGING" "$RW_DMG" "$DMG_PATH"
mkdir -p "$STAGING"
cp -R "dist/${DMG_NAME}.app" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
cp "$ICON_ICNS" "$STAGING/.VolumeIcon.icns"
SetFile -a C "$STAGING"

# 3. Create read-write DMG and set Finder view
echo "[4/6] Creating DMG with custom Finder layout..."
hdiutil create -volname "$DMG_NAME" -srcfolder "$STAGING" -ov -format UDRW "$RW_DMG" > /dev/null
hdiutil attach "$RW_DMG" -readwrite -noverify > /dev/null 2>&1

osascript <<EOF
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

SetFile -a C "/Volumes/${DMG_NAME}"
sync
sleep 2
hdiutil detach "/Volumes/${DMG_NAME}" -force > /dev/null 2>&1

# 4. Convert to compressed DMG
echo "[5/6] Compressing DMG..."
hdiutil convert "$RW_DMG" -format UDZO -o "$DMG_PATH" > /dev/null 2>&1

# 5b. Ad-hoc codesign the DMG itself so the disk image carries a
# signature in addition to the bundle inside it. macOS uses this to
# decide whether to attach quarantine flags transitively.
codesign --force --sign - "$DMG_PATH" 2>&1 | tail -2 || true

# 5. Set custom icon on the .dmg file itself
echo "[6/6] Setting custom icon on DMG file..."
if command -v fileicon &> /dev/null; then
    fileicon set "$DMG_PATH" "$ICON_PNG" > /dev/null 2>&1
    echo "✓ Custom icon set via fileicon"
else
    echo "⚠ fileicon not installed (npm install -g fileicon) — DMG will have generic icon"
fi

# Done
SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo ""
echo "=== Build complete ==="
echo "DMG: $DMG_PATH ($SIZE)"
echo "  • Compact window (520×320)"
echo "  • 128px icons with Applications shortcut"
echo "  • Custom app icon on .dmg file"
