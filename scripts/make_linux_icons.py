#!/usr/bin/env python3.12
"""Generate the freedesktop hicolor icon tree for the Linux AppImage.

An ELF has no embedded icon the way a .icns/.ico does, so the Linux artifact
carries its icon as loose PNGs: the AppDir's .desktop file names one icon, and
desktop environments resolve it out of a hicolor tree once the AppImage is
integrated. appimagetool additionally requires an icon at the AppDir ROOT named
exactly the `Icon=` value from the .desktop — hence `assets/linux/domestique.png`
alongside the tree.

Output (committed; the build script only copies):
    assets/linux/hicolor/<N>x<N>/apps/domestique.png   for N in SIZES
    assets/linux/domestique.png                        (the 256, AppDir root)

Usage:
    python3.12 scripts/make_linux_icons.py
"""
from __future__ import annotations

import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "assets", "icon.png")
OUT = os.path.join(ROOT, "assets", "linux")
ICON_NAME = "domestique.png"  # MUST equal the .desktop `Icon=` value

# freedesktop sizes a GNOME/KDE/XFCE panel actually looks for.
SIZES = [256, 128, 64, 48, 32, 24, 16]


def main() -> int:
    src = Image.open(SOURCE).convert("RGBA")
    print(f"Source: {SOURCE} ({src.width}x{src.height})")

    written = []
    for size in SIZES:
        # Upscaling would ship a blurrier icon than the source and is never
        # what the panel wants — skip instead.
        if size > src.width or size > src.height:
            print(f"  skip {size}x{size} — larger than source")
            continue
        icon = src.resize((size, size), Image.LANCZOS)
        dest_dir = os.path.join(OUT, "hicolor", f"{size}x{size}", "apps")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, ICON_NAME)
        icon.save(dest, "PNG", optimize=True)
        written.append((size, dest))
        print(f"  {size}x{size} -> {os.path.relpath(dest, ROOT)}")

        if size == 256:
            root_icon = os.path.join(OUT, ICON_NAME)
            icon.save(root_icon, "PNG", optimize=True)
            print(f"  {size}x{size} -> {os.path.relpath(root_icon, ROOT)} (AppDir root)")

    if not written:
        print("FATAL: no icons written — source smaller than every target size")
        return 1
    print(f"Wrote {len(written)} icons.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
