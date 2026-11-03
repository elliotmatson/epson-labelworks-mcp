"""Label rendering with Pillow.

Coordinate convention: image width = label length along the tape (feed
direction); image height = printable head dots across the tape.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .profiles import Profile
from .protocol import pack_column

FONT_CANDIDATES = {
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
}
MONO_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def load_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    env = os.environ.get("LW_FONT_BOLD" if bold else "LW_FONT")
    paths = ([env] if env else []) + ([MONO_FONT] if mono else []) + FONT_CANDIDATES[bold]
    for p in paths:
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _blank(width: int, height: int) -> Image.Image:
    return Image.new("L", (max(1, width), height), 255)


def _line_box(font, line: str) -> tuple[int, int, int]:
    """(width, ascent-offset, height) of one line using font metrics."""
    l, t, r, b = font.getbbox(line or " ")
    return r - l, t, b - t


def test_pattern(height: int) -> Image.Image:
    """Calibration wedge: row y is black for x < y, framed, with a row-0 marker.

    The printed wedge shows how many head dots actually print (its length on
    tape) and which tape edge row 0 lands on (the marker side).
    """
    pad = 6
    img = _blank(height + 2 * pad + 90, height)
    d = ImageDraw.Draw(img)
    for y in range(height):
        d.line([(pad, y), (pad + y, y)], fill=0)
    d.rectangle([0, 0, img.width - 1, height - 1], outline=0)
    font = load_font(max(8, min(18, height // 5)), bold=True, mono=True)
    d.text((pad + height + 8, 2), "row0", font=font, fill=0)
    d.text((pad + height + 8, height - font.size - 3), f"{height}d", font=font, fill=0)
    return img


# --- raster conversion -------------------------------------------------------

def to_columns(img: Image.Image, dots: int, flip_across: bool) -> list[bytes]:
    """One packed bitmap per image column (= one raster record)."""
    if img.height != dots:
        raise ValueError(f"image is {img.height} dots tall; tape expects {dots}")
    bw = img.convert("L").point(lambda v: 0 if v < 128 else 255, mode="1")
    px = bw.load()
    cols = []
    rows = range(dots - 1, -1, -1) if flip_across else range(dots)
    for x in range(bw.width):
        cols.append(pack_column((px[x, y] == 0 for y in rows), dots))
    return cols


def preview_png(img: Image.Image, profile: Profile, tape_mm: int, margin_dots: int,
                scale: int = 3) -> bytes:
    """Picture of the label as it should come out: full tape width, margins shaded."""
    tape_dots = profile.mm_to_dots(tape_mm)
    printable = img.height
    length = img.width + 2 * margin_dots
    canvas = Image.new("RGB", (length, max(tape_dots, printable)), (250, 248, 236))
    d = ImageDraw.Draw(canvas)
    top = (canvas.height - printable) // 2
    for x0 in (0, length - margin_dots):
        if margin_dots:
            d.rectangle([x0, 0, x0 + margin_dots - 1, canvas.height - 1], fill=(232, 228, 210))
    canvas.paste(img.convert("RGB"), (margin_dots, top))
    d.rectangle([0, 0, canvas.width - 1, canvas.height - 1], outline=(150, 150, 150))
    canvas = canvas.resize((canvas.width * scale, canvas.height * scale), Image.NEAREST)
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()
