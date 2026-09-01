#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Domestique Linux AppImage Builder
# ═══════════════════════════════════════════════════════════════
# Usage:  ./build_linux.sh
# Output: dist/Domestique-v<VERSION>-x86_64.AppImage
#
# The artifact carries pywebview's Qt/PySide6 backend, not GTK: QtWebEngine
# ships its own Chromium, so there is no host WebKitGTK to bundle, patch or
# track across the 4.0 -> 4.1 -> 6.0 churn. It costs ~272 MB and buys a
# download-and-run binary that is not tied to the Debian/Ubuntu family.
#
# Build host MUST be old enough for the reach we claim: build inside
# ubuntu:22.04 (glibc 2.35), never on whatever the runner happens to be.
# Building on 24.04 produces a binary 22.04 cannot load and buys nothing.
#
#   docker run --rm -v "$PWD":/src -w /src ubuntu:22.04 bash build_linux.sh
#
# The gates below exist because none of this is provable from a Mac: the
# glibc/GLIBCXX/CXXABI floors, the "no bundled graphics stack" assertion and
# the mechanical ldd host-dependency list are the only things standing between
# a green build and an AppImage that dies on a user's desktop.
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

# The developer works on macOS; fail immediately and usefully rather than
# part-building something unusable.
if [ "$(uname -s)" != "Linux" ]; then
    echo "✗ build_linux.sh must run on Linux (found: $(uname -s))."
    echo ""
    echo "  Build it in the pinned container instead:"
    echo "    docker run --rm -v \"\$PWD\":/src -w /src ubuntu:22.04 bash build_linux.sh"
    echo ""
    echo "  macOS DMG:   ./build_dmg.sh"
    echo "  Windows EXE: built in CI"
    exit 1
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
APP_NAME="Domestique"
DIST="dist/${APP_NAME}"
APPDIR="build/${APP_NAME}.AppDir"
# Frozen literal — the picker tests and the update-checker pin this exact
# shape. The v1.8.8 DMG incident came from a bare, versionless asset name.
ARTIFACT="dist/${APP_NAME}-v${VERSION}-x86_64.AppImage"

# Floors = what the OLDEST host we claim to support actually provides.
# Ubuntu 22.04 ships glibc 2.35 and GCC 12's libstdc++ (GLIBCXX_3.4.30 /
# CXXABI_1.3.13). Overridable ONLY so the gate itself can be sanity-checked
# by deliberately failing it once, e.g. GLIBC_FLOOR=2.17 ./build_linux.sh
GLIBC_FLOOR="${GLIBC_FLOOR:-2.35}"
GLIBCXX_FLOOR="${GLIBCXX_FLOOR:-3.4.30}"
CXXABI_FLOOR="${CXXABI_FLOOR:-1.3.13}"

echo "=== Domestique Linux AppImage Build (v${VERSION}) ==="

# appimagetool ships as an AppImage and mounts itself via FUSE. Containers and
# most CI have no FUSE, and the failure ("fuse: device not found") reads like a
# problem with OUR build rather than with the tool. Exporting this makes it
# extract-and-run instead; harmless on a desktop that does have FUSE.
export APPIMAGE_EXTRACT_AND_RUN="${APPIMAGE_EXTRACT_AND_RUN:-1}"

for tool in objdump ldd appimagetool; do
    if ! command -v "$tool" &> /dev/null; then
        echo "✗ Required tool missing: $tool"
        case "$tool" in
            objdump|ldd) echo "  apt-get install -y binutils libc-bin" ;;
            appimagetool)
                echo "  Download appimagetool-x86_64.AppImage from the"
                echo "  AppImage/appimagetool repo's 'continuous' release"
                echo "  github.com/AppImage/AppImageKit/releases, chmod +x it,"
                echo "  and put it on PATH as 'appimagetool'."
                ;;
        esac
        exit 1
    fi
done

# 1. Dependencies. PySide6 comes in via requirements.txt's sys_platform ==
# "linux" marker, so macOS/Windows resolve byte-identically to today.
echo "[1/9] Installing dependencies..."
pip3 install -r requirements.txt pyinstaller

# 2. Freeze. onedir (the spec's exclude_binaries + COLLECT shape) — NOT
# onefile: onefile re-extracts ~360 MB to /tmp on every launch.
# A dist/ left by a previous macOS or Windows build would otherwise survive a
# failed run and be measured by the version gate below — which is exactly how a
# 3.7.0 bundle got checked against a 3.8.0 repo.
rm -rf dist build/Domestique
echo "[2/9] Building with PyInstaller..."
# NOT piped into tail: the pipeline's exit status would be tail's, which is
# always 0, so `set -e` never saw a failed build — the script carried on and
# version-checked whatever happened to be in dist/ already.
set -o pipefail
pyinstaller packaging/domestique.spec --clean --noconfirm 2>&1 | tail -3
set +o pipefail
[ -x "${DIST}/${APP_NAME}" ] || { echo "✗ FATAL: ${DIST}/${APP_NAME} not produced"; exit 1; }

# 3. Version smoke-test — the bundled app must ship its own VERSION file with
# the right contents, else the running app misreports itself and the in-app
# updater shows a perpetual "upgrade" prompt against an artifact whose very
# filename embeds this number.
BUNDLED_VER_FILE="$(find "$DIST" -name VERSION -type f 2>/dev/null | head -1)"
BUNDLED_VER="$(tr -d '[:space:]' < "$BUNDLED_VER_FILE" 2>/dev/null)"
if [ -z "$BUNDLED_VER_FILE" ] || [ "$BUNDLED_VER" != "$VERSION" ]; then
    echo "✗ FATAL: bundled VERSION ('$BUNDLED_VER') != repo VERSION ('$VERSION')" >&2
    exit 1
fi
echo "[3/9] Version smoke-test OK — bundle reports $BUNDLED_VER"

# 4. Strip the host graphics/runtime libraries PyInstaller helpfully collected.
# It bundles libstdc++/libgcc_s/libgbm but EXCLUDES libGL/libEGL/libdrm, so the
# bundled halves and the host's halves get mixed at runtime — the canonical
# AppImage GPU crash, and worse here because §6 builds on an older host than
# the user runs. Take all of them from the host, consistently.
echo "[4/9] Stripping bundled graphics/runtime libraries..."
for lib in 'libstdc++.so.6*' 'libgcc_s.so.1*' 'libgbm.so.1*' 'libxshmfence.so.1*'; do
    find "$DIST" -type f -name "$lib" -print -delete
done

# 5. Symbol-version gate. Walk the WHOLE tree: under PyInstaller 6 the payload
# lives in _internal/, so the obvious `objdump -T dist/Domestique/*.so*` matches
# nothing and passes unconditionally. Each family is maxed and compared
# SEPARATELY — GLIBCXX_3.4.30 is not comparable to GLIBC_2.35.
echo "[5/9] Symbol-version gate (glibc >= floors of the oldest supported host)..."
SYMS="$(mktemp)"
trap 'rm -f "$SYMS"' EXIT
find "$DIST" -type f \( -name '*.so*' -o -perm -u+x \) -exec objdump -T {} + \
    > "$SYMS" 2>/dev/null || true
# A sweep that found nothing means the glob missed the tree — which is exactly
# how the plan's original gate passed unconditionally. An empty sweep is a
# broken gate, not a clean build.
if [ ! -s "$SYMS" ]; then
    echo "  \u2717 FATAL: symbol sweep produced no output \u2014 the gate is not looking at the binaries" >&2
    exit 1
fi

gate_family() {
    # $1 = symbol prefix (GLIBC_/GLIBCXX_/CXXABI_), $2 = floor version
    local prefix="$1" floor="$2" max highest
    max="$(grep -oE "${prefix}[0-9]+(\.[0-9]+)+" "$SYMS" | sed "s/^${prefix}//" \
           | sort -V | tail -1)"
    if [ -z "$max" ]; then
        # Reachable only for a family genuinely absent from every binary
        # (e.g. CXXABI_ in a pure-C tree). The empty-sweep case is fatal above.
        echo "  ${prefix%_}: none referenced"
        return 0
    fi
    # sort -V puts the larger last; if that is not the floor, we exceeded it.
    highest="$(printf '%s\n%s\n' "$max" "$floor" | sort -V | tail -1)"
    if [ "$highest" != "$floor" ]; then
        echo "  ✗ ${prefix%_}: needs $max, floor is $floor" >&2
        # Name the offenders — "something needs 2.38" is not actionable.
        find "$DIST" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
            | while IFS= read -r -d '' f; do
                objdump -T "$f" 2>/dev/null | grep -q "${prefix}${max}" \
                    && echo "      $f"
              done | head -10
        return 1
    fi
    echo "  ✓ ${prefix%_}: needs $max, floor is $floor"
}

GATE_FAIL=0
gate_family "GLIBC_"   "$GLIBC_FLOOR"   || GATE_FAIL=1
gate_family "GLIBCXX_" "$GLIBCXX_FLOOR" || GATE_FAIL=1
gate_family "CXXABI_"  "$CXXABI_FLOOR"  || GATE_FAIL=1
if [ "$GATE_FAIL" -ne 0 ]; then
    echo "✗ FATAL: the binary requires newer runtime symbols than the oldest" >&2
    echo "  host we promise to support. Build in ubuntu:22.04, or lower the" >&2
    echo "  promise — do not raise the floor to make this pass." >&2
    exit 1
fi

# 6. Assemble the AppDir. appimagetool requires an AppRun, exactly one
# .desktop in the root, and a root icon named EXACTLY the .desktop's Icon=
# value; the usr/share copies are what desktop environments read after
# integration.
echo "[6/9] Assembling AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons"
cp -a "$DIST/." "$APPDIR/usr/bin/"
cp -a assets/linux/hicolor "$APPDIR/usr/share/icons/"
cp assets/linux/domestique.png "$APPDIR/domestique.png"

cat > "$APPDIR/domestique.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Domestique
Comment=Cycling training planner, workout library and ride viewer
Exec=Domestique
Icon=domestique
Categories=Education;Sports;
Terminal=false
StartupWMClass=Domestique
DESKTOP
cp "$APPDIR/domestique.desktop" "$APPDIR/usr/share/applications/"

# A missing Chromium helper is a white window, not an error — find it now and
# bake the path in, rather than letting Qt's compiled-in guess miss inside a
# relocated AppDir.
QTWEP="$(find "$APPDIR/usr/bin" -type f -name QtWebEngineProcess | head -1)"
if [ -z "$QTWEP" ]; then
    echo "✗ FATAL: QtWebEngineProcess not in the bundle — the window would" >&2
    echo "  open blank. Check the PySide6.QtWebEngineCore hidden imports." >&2
    exit 1
fi
QTWEP_REL="${QTWEP#"$APPDIR"/}"
echo "  QtWebEngineProcess: $QTWEP_REL"

cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/sh
# AppRun is the SOLE owner of Qt environment for the shipped artifact:
# launcher.py sets no Qt variables and CI sets none of these, so the config a
# user runs is exactly the config the smoke test exercises. Every assignment
# below defers to a value the user or distro already set.
APPDIR="\$(dirname "\$(readlink -f "\$0")")"

# QtWebEngine's Chromium sandbox needs either a setuid helper or unprivileged
# user namespaces. An AppImage has neither reliably — Ubuntu 24.04's AppArmor
# policy denies the latter outright — and the render process then dies before
# painting anything.
export QTWEBENGINE_DISABLE_SANDBOX="\${QTWEBENGINE_DISABLE_SANDBOX:-1}"

# Chromium spawns its helper by absolute path; baked in at build time.
export QTWEBENGINEPROCESS_PATH="\${QTWEBENGINEPROCESS_PATH:-\$APPDIR/${QTWEP_REL}}"

# Qt 6 reads its DPI from the X session (Xft/DPI, then Xft.dpi) and falls back
# to 96 — it never looks at the panel the way Qt 5 did — so under XWayland one
# CSS pixel lands on one physical pixel. Measured: devicePixelRatio 1.0 in the
# Linux smoke screenshot against 2.0 in the macOS window, and every font size in
# the app is an absolute px, which is why the first Linux tester's verdict was
# "could really use a bigger font". launcher.py divides the window geometry back
# out, so the window keeps its size in physical pixels and only the contents
# grow. Deferring to a value the user already set, like every assignment here:
# QT_SCALE_FACTOR=1 restores the old rendering, and a larger value is the
# power-user escape hatch. Above ~1.4 the window stops holding its physical
# size — launcher.py's min_size clamps first.
export QT_SCALE_FACTOR="\${QT_SCALE_FACTOR:-1.25}"

# Deliberately NOT setting LD_LIBRARY_PATH. The PyInstaller bootloader sets it
# and stashes the caller's value in LD_LIBRARY_PATH_ORIG, which launcher.py
# restores before handing a URL to the host browser. Prepending AppDir paths
# here would poison that saved copy and break every browser we open.

exec "\$APPDIR/usr/bin/${APP_NAME}" "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# 7. Assert the graphics stack really is gone. Step 4 removes what PyInstaller
# collects today; this catches the day it starts collecting something new.
# Qt ships optional plugins we never load, and they drag in whole stacks: the
# GTK platform-theme plugin NEEDs gtk-3/gdk/pango/cairo/atk/gdk_pixbuf, print
# support NEEDs cups, and the multimedia plugins NEED pulse. Keeping them would
# make a Qt application depend on GTK3 at runtime — which is the dependency we
# picked Qt to escape. The app draws with the xcb platform plugin and prints
# nothing, so they are dead weight with a large dependency tail.
#
# The Wayland platform plugins go for the same reason: this release targets X11
# via xcb (Wayland-native is out of scope), and the plugins NEED
# libwayland-cursor/egl. Under a Wayland session the app still runs through
# XWayland, which is what the xcb plugin talks to.
echo "[6b/9] Pruning unused Qt plugins (GTK theme, print, multimedia)..."
_QTPLUG="$APPDIR/usr/bin/_internal/PySide6/Qt/plugins"
_QTROOT="$APPDIR/usr/bin/_internal/PySide6/Qt"
for _p in "plugins/platformthemes/libqgtk3.so" "plugins/printsupport" \
          "plugins/multimedia" "plugins/mediaservice" "plugins/texttospeech" \
          "qml/QtMultimedia" "qml/QtQuick3D/SpatialAudio" \
          "qml/QtQuick/VirtualKeyboard" "qml/QtTextToSpeech" \
          "lib/libQt6Multimedia.so.6" "lib/libQt6MultimediaQuick.so.6" \
          "lib/libQt6SpatialAudio.so.6" "lib/libQt6Quick3DSpatialAudio.so.6" \
          "lib/libQt6TextToSpeech.so.6" "plugins/imageformats/libqtiff.so" \
          "plugins/platforms/libqwayland.so" \
          "plugins/platforms/libqwayland-egl.so" \
          "plugins/platforms/libqwayland-generic.so" \
          "plugins/wayland-decoration-client" \
          "plugins/wayland-graphics-integration-client" \
          "plugins/wayland-shell-integration" \
          "qml/QtWayland" \
          "lib/libQt6WaylandClient.so.6" "lib/libQt6WlShellIntegration.so.6" \
          "lib/libQt6WaylandEglClientHwIntegration.so.6"; do
    if [ -e "$_QTROOT/$_p" ]; then
        echo "  - $_p"
        rm -rf "${_QTROOT:?}/$_p"
    fi
done

# 6c. Those plugins did not arrive alone. PyInstaller collects the whole NEEDED
# closure of everything it freezes, so libqtiff.so brought _internal/libtiff.so.5
# and libqgtk3.so brought _internal/libgdk_pixbuf-2.0.so.0 — and BOTH of those
# NEED libjpeg.so.8, so that came too. Deleting the plugin leaves its libraries
# behind with nothing in the bundle referencing them: Qt's own libqjpeg.so links
# libjpeg-turbo statically, and Pillow carries auditwheel copies under mangled
# names (libjpeg-31e2ca52.so.62.4.0), so no code path loses a JPEG decoder.
#
# They are worse than dead weight. Step 8 resolves a bundled orphan's NEEDED
# against the BUILD HOST, where jammy does have libjpeg.so.8, so a library we
# already ship got published as a host requirement and dpkg named it
# libjpeg-turbo8 — a name Debian does not have, and whose libjpeg62-turbo
# provides a DIFFERENT soname (libjpeg.so.62). The clean-distro job then failed
# on the apt name and reported "not found" for a file inside the AppImage.
#
# Exact filenames, never a libjpeg*/libtiff* glob: a glob takes Pillow's
# mangled auditwheel copies with it.
echo "[6c/9] Removing libraries orphaned by the plugin prune..."
_INT="$APPDIR/usr/bin/_internal"
for _o in "libtiff.so.5" "libgdk_pixbuf-2.0.so.0" "libjpeg.so.8"; do
    if [ -e "$_INT/$_o" ]; then
        echo "  - $_o"
        rm -f "$_INT/$_o"
    fi
done

# PyInstaller publishes every Qt library a second time as _internal/<soname>, a
# SYMLINK into PySide6/Qt/lib. Pruning the target leaves the link dangling, and
# step 8's "does it ship inside the AppDir?" exemption matches a dangling link
# by NAME — which is how a bundle missing libQt6Multimedia.so.6 shipped under a
# green build. Delete the links the prune just broke: a library that is gone
# must LOOK gone to the gate below.
find "$APPDIR" -xtype l -print -delete

echo "[7/9] Asserting no bundled graphics/runtime libraries..."
# Match real system SONAMEs (libfoo.so.N), not any file whose name merely
# starts with the same letters. The greedy globs flagged
# PySide6/Qt/plugins/wayland-graphics-integration-client/libdrm-egl-server.so
# — a Qt plugin that happens to be called libdrm-egl-server, not the system
# libdrm.so.2 this gate exists to keep out.
LEAKED="$(find "$APPDIR" -type f \( -name 'libGL.so.*' -o -name 'libGLX.so.*' \
    -o -name 'libEGL.so.*' -o -name 'libdrm.so.*' -o -name 'libgbm.so.*' \
    -o -name 'libstdc++.so.*' \
    -o -name 'libgcc_s.so.*' \) 2>/dev/null || true)"
if [ -n "$LEAKED" ]; then
    echo "✗ FATAL: bundled libraries that must come from the host:" >&2
    echo "$LEAKED" >&2
    echo "  Add them to the step-4 strip list — mixing bundled and host halves" >&2
    echo "  of the graphics stack crashes on the user's GPU driver." >&2
    exit 1
fi
echo "  ✓ none present"

# 8. Host dependencies, derived MECHANICALLY. Anything the AppDir needs that
# does NOT resolve inside the AppDir is a runtime requirement on the user's
# distro, and this list is the only honest source for the release notes and
# for CI's apt line. Never hand-maintained.
echo "[8/9] Deriving host dependencies via ldd..."
DEPS="dist/host-deps.txt"
LDD_OUT="$(find "$APPDIR" -type f \( -name '*.so*' -o -perm -u+x \) \
    -exec ldd {} + 2>/dev/null || true)"

# A NEEDED entry that ldd cannot resolve on the BUILD HOST is still fine if the
# library ships inside the AppDir — auditwheel-repaired wheels carry their own
# copies under mangled names (libquadmath-96973f99.so.0.0.0), and those are
# found at runtime via RPATH, not via the host's loader cache. Only entries
# absent from both the host and the bundle are genuinely missing.
MISSING="$(printf '%s\n' "$LDD_OUT" | awk '/=> not found/ {print $1}' | sort -u \
    | while read -r _lib; do
        [ -z "$_lib" ] && continue
        find "$APPDIR" -name "$_lib" -print -quit 2>/dev/null | grep -q . \
            || printf '%s\n' "$_lib"
      done)"
if [ -n "$MISSING" ]; then
    echo "✗ FATAL: NEEDED libraries unresolved on the build host:" >&2
    printf '%s\n' "$MISSING" >&2
    echo "  apt-get install the packages providing these in the build" >&2
    echo "  container AND declare them as host dependencies." >&2
    exit 1
fi

printf '%s\n' "$LDD_OUT" \
    | awk -v appdir="$(cd "$APPDIR" && pwd)/" \
        '$2 == "=>" && $3 ~ /^\// && index($3, appdir) != 1 {print $1}' \
    | sort -u > "$DEPS"
echo "  $(wc -l < "$DEPS" | tr -d ' ') host libraries required — see $DEPS"
if command -v dpkg &> /dev/null; then
    # Package names only exist on the Debian-family build container; the
    # soname list above is the portable, authoritative form.
    while IFS= read -r soname; do
        # Leading-slash arguments are looked up as exact paths and a bare
        # soname is not one; the wildcard makes dpkg treat it as a pattern.
        pkg="$(dpkg -S "*/$soname" 2>/dev/null | head -1 | cut -d: -f1)"
        [ -n "$pkg" ] && echo "$pkg"
    done < "$DEPS" | sort -u > "dist/host-deps-debian.txt"
    echo "  Debian/Ubuntu packages — see dist/host-deps-debian.txt"
fi

# 9. Wrap it. --no-appstream: we ship no AppStream metainfo, and appimagetool
# otherwise hard-depends on appstreamcli being installed.
echo "[9/9] Building AppImage..."
rm -f "$ARTIFACT"
ARCH=x86_64 appimagetool --no-appstream "$APPDIR" "$ARTIFACT"
[ -f "$ARTIFACT" ] || { echo "✗ FATAL: appimagetool produced no artifact"; exit 1; }
chmod +x "$ARTIFACT"

SIZE=$(du -h "$ARTIFACT" | cut -f1)
echo ""
echo "=== Build complete ==="
echo "AppImage: $ARTIFACT ($SIZE)"
echo "Host deps: $DEPS"
echo ""
echo "NOT verified by this script: that the window renders. Run the AppImage"
echo "on a real desktop before calling it released."
