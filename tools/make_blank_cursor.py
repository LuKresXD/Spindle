#!/usr/bin/env python3
"""Write an Xcursor theme whose every cursor is a fully transparent 1x1 image.

The kiosk compositor (cage/wlroots) draws its default cursor even when the only
input device is a touch panel. Pointing XCURSOR_THEME at this theme hides it:

    python3 tools/make_blank_cursor.py ~/.icons/blank
    # spindle-kiosk.service: Environment=XCURSOR_PATH=/home/luka/.icons XCURSOR_THEME=blank
"""
import struct
import sys
from pathlib import Path

NAMES = ("default", "left_ptr", "arrow", "pointer", "hand2", "text", "xterm", "crosshair",
         "wait", "watch", "progress", "move", "grab", "grabbing", "not-allowed")


def xcursor_blank(size: int = 16) -> bytes:
    """One image chunk: size x size, all pixels transparent, hotspot 0,0."""
    image_type, nominal = 0xFFFD0002, size
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc_pos = 16 + 12
    toc = struct.pack("<III", image_type, nominal, toc_pos)
    chunk = struct.pack("<IIIIIIIII", 36, image_type, nominal, 1, size, size, 0, 0, 0)
    pixels = bytes(4 * size * size)
    return header + toc + chunk + pixels


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".icons" / "blank")
    cursors = root / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    data = xcursor_blank()
    for name in NAMES:
        (cursors / name).write_bytes(data)
    (root / "index.theme").write_text("[Icon Theme]\nName=blank\nComment=invisible cursor\n")
    (root / "cursor.theme").write_text("[Icon Theme]\nInherits=blank\n")
    print(f"wrote {len(NAMES)} invisible cursors to {cursors}")


if __name__ == "__main__":
    main()
