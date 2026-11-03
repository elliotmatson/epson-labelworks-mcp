"""LabelDesign -> 1-bit-ready bitmap (width = length along tape, height = printable dots)."""

from __future__ import annotations

import base64
import io
import math

from PIL import Image, ImageDraw, ImageOps

from . import barcodes as BC
from . import frames as FR
from . import symbols as SY
from . import textrender as TR
from .model import (BarcodeBlock, DividerBlock, ImageBlock, LabelDesign, QRBlock, SpacerBlock,
                    SymbolBlock, TableBlock, TextBlock)
from .profiles import Profile

MAX_LENGTH_MM = 1000


def _blank(w: int, h: int) -> Image.Image:
    return Image.new("L", (max(1, int(w)), max(1, int(h))), 255)


def _ink_bbox(img: Image.Image):
    return Image.eval(img.convert("L"), lambda v: 255 - v).getbbox()


class Ctx:
    def __init__(self, design: LabelDesign, profile: Profile):
        self.d = design
        self.p = profile
        self.base = TR.Style().merged(design)

    def dots(self, mm: float | None) -> int | None:
        return None if mm is None else max(1, self.p.mm_to_dots(mm))


# --- blocks -----------------------------------------------------------------------

def _text(b: TextBlock, h: int, c: Ctx, max_width: int | None = None) -> Image.Image:
    d = c.d
    style = c.base.merged(b)
    align = b.align or d.align
    markup = d.markup if b.markup is None else b.markup
    if b.lines:
        texts = [(ln.text, ln) for ln in b.lines]
    else:
        texts = [(t, None) for t in (b.text or "").replace("\\n", "\n").split("\n")]
    lines = TR.make_lines(texts, style, align, markup)
    return TR.render_text_block(lines, h, direction=b.direction or d.direction,
                                spacing=d.line_spacing if b.line_spacing is None else b.line_spacing,
                                v_align=b.v_align or d.v_align, width=c.dots(b.length_mm),
                                max_width=max_width)


def _image(b: ImageBlock, h: int) -> Image.Image:
    try:
        src = Image.open(io.BytesIO(base64.b64decode(b.image_base64)))
    except Exception as e:
        raise ValueError(f"image_base64 is not a readable image: {e}") from None
    src = ImageOps.exif_transpose(src)
    if src.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
        src = Image.alpha_composite(bg, src.convert("RGBA"))
    gray = src.convert("L")
    target = max(1, round(h * b.size_pct / 100))
    w = max(1, round(gray.width * target / gray.height))
    gray = gray.resize((w, target), Image.LANCZOS)
    if b.dither:
        mono = gray.convert("1", dither=Image.FLOYDSTEINBERG).convert("L")
    else:
        mono = gray.point(lambda v: 0 if v < b.threshold else 255)
    out = _blank(w, h)
    out.paste(mono, (0, (h - target) // 2))
    return out


def _table(b: TableBlock, h: int, c: Ctx) -> Image.Image:
    rows = [list(r) for r in b.rows if r is not None]
    if not rows:
        raise ValueError("table has no rows")
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    nrows = len(rows)
    bd = b.border_dots
    pad = max(1, h // 30)
    rh = (h - (nrows + 1) * bd) // nrows
    if rh < 8:
        raise ValueError(f"{nrows} table rows do not fit across the tape")
    style = c.base.merged(b)
    markup = c.d.markup

    def cell_img(text: str, width: int | None) -> Image.Image:
        if not text.strip():
            return _blank(width or 1, rh - 2 * pad)
        lines = TR.make_lines([(t, None) for t in text.replace("\\n", "\n").split("\n")],
                              style, b.align, markup)
        return TR.render_text_block(lines, rh - 2 * pad, spacing=c.d.line_spacing,
                                    width=width)

    if b.column_lengths_mm:
        widths = [c.dots(b.column_lengths_mm[i % len(b.column_lengths_mm)]) for i in range(ncols)]
    elif b.cell_length_mm:
        widths = [c.dots(b.cell_length_mm)] * ncols
    else:
        widths = [0] * ncols
        for r in rows:
            for i, t in enumerate(r):
                widths[i] = max(widths[i], cell_img(t, None).width + 2 * pad + 2)
    widths = [w - bd for w in widths] if (b.column_lengths_mm or b.cell_length_mm) else widths
    total_w = sum(widths) + (ncols + 1) * bd
    out = _blank(total_w, h)
    d = ImageDraw.Draw(out)
    xs = [bd]
    for w in widths:
        xs.append(xs[-1] + w + bd)
    ys = [bd + r * (rh + bd) for r in range(nrows)]
    for ri, r in enumerate(rows):
        for ci, t in enumerate(r):
            x0, y0, w = xs[ci], ys[ri], widths[ci]
            img = cell_img(t, max(1, w - 2 * pad))
            if img.width > w - 2 * pad:
                raise ValueError(f"cell {t!r} does not fit in {round(c.p.dots_to_mm(w), 1)} mm")
            out.paste(img, (x0 + pad, y0 + pad))
            if (b.invert_first_row and ri == 0) or (b.invert_first_column and ci == 0):
                region = out.crop((x0, y0, x0 + w, y0 + rh))
                out.paste(ImageOps.invert(region), (x0, y0))
    last_y = h - 1
    outer = b.border in ("grid", "outer")
    if b.border in ("grid", "inner", "columns") or outer:
        for i, x in enumerate(xs):
            edge = i in (0, len(xs) - 1)
            if (edge and (outer or b.border == "columns")) or (not edge and b.border in ("grid", "inner", "columns")):
                d.rectangle([x - bd, 0, x - 1, last_y], fill=0)
    if b.border in ("grid", "inner", "rows") or outer:
        lines_y = [0] + [y + rh for y in ys]
        for i, y in enumerate(lines_y):
            edge = i in (0, len(lines_y) - 1)
            if (edge and (outer or b.border == "rows")) or (not edge and b.border in ("grid", "inner", "rows")):
                d.rectangle([0, y, total_w - 1, y + bd - 1], fill=0)
    return out


def render_block(b, h: int, c: Ctx) -> Image.Image:
    if isinstance(b, TextBlock):
        img = _text(b, h, c)
    elif isinstance(b, SymbolBlock):
        img = SY.render_symbol(b.name, h, size=round(h * b.size_pct / 100))
    elif isinstance(b, QRBlock):
        img = BC.render_matrix("qr", b.data, h, size=round(h * b.size_pct / 100), ecc=b.ecc)
    elif isinstance(b, BarcodeBlock):
        if b.symbology in BC.MATRIX:
            img = BC.render_matrix(b.symbology, b.data, h, size=round(h * b.size_pct / 100), ecc=b.ecc)
        else:
            img = BC.render_linear(b.symbology, b.data, h, module=b.module_dots,
                                   show_text=b.show_text, checksum=b.checksum,
                                   max_width=c.dots(b.length_mm))
    elif isinstance(b, ImageBlock):
        img = _image(b, h)
    elif isinstance(b, DividerBlock):
        return FR.render_divider(h, c.dots(b.length_mm), b.style, b.thickness, b.coverage_pct)
    elif isinstance(b, SpacerBlock):
        return _blank(c.dots(b.length_mm), h)
    elif isinstance(b, TableBlock):
        img = _table(b, h, c)
    else:  # pragma: no cover
        raise ValueError(f"unknown block {b!r}")
    fixed = c.dots(getattr(b, "length_mm", None))
    if fixed and not isinstance(b, (TextBlock,)):
        if img.width > fixed:
            raise ValueError(f"{b.type} block is {round(c.p.dots_to_mm(img.width), 1)} mm long but "
                             f"length_mm is {b.length_mm}")
        canvas = _blank(fixed, h)
        canvas.paste(img, ((fixed - img.width) // 2, 0))
        img = canvas
    if getattr(b, "invert", False):
        pad = max(2, h // 12)
        canvas = _blank(img.width + 2 * pad, h)
        canvas.paste(img, (pad, 0))
        img = ImageOps.invert(canvas)
    return img


def _row(blocks, h: int, c: Ctx, gap: int) -> list[Image.Image]:
    if not blocks:
        raise ValueError("the label is empty: give text, qr, barcode, symbol, image, cells or blocks")
    return [render_block(b, h, c) for b in blocks]


def _compose(parts: list[Image.Image], h: int, gap: int, length: int | None,
             distribute: str) -> Image.Image:
    natural = sum(p.width for p in parts) + gap * (len(parts) - 1)
    if length is None:
        length, distribute = natural, "start"
    if natural > length:
        raise ValueError(f"content needs {natural} dots but the label only has {length}; "
                         "shorten it, use smaller text or drop length_mm")
    out = _blank(length, h)
    spare = length - natural
    if distribute == "spread" and len(parts) > 1:
        g = gap + spare / (len(parts) - 1)
        x = 0.0
    else:
        g = gap
        x = {"start": 0, "end": spare}.get(distribute, spare / 2)
    for p in parts:
        out.paste(p, (round(x), 0))
        x += p.width + g
    return out


def _shrink_to(parts: list[Image.Image], blocks, h: int, c: Ctx, gap: int, length: int) -> list[Image.Image]:
    """Re-render auto-sized text blocks narrower so the row fits `length` (Epson's auto-shrink)."""
    natural = sum(p.width for p in parts) + gap * (len(parts) - 1)
    if natural <= length:
        return parts
    idx = [i for i, b in enumerate(blocks) if isinstance(b, TextBlock) and not b.length_mm]
    if not idx:
        return parts
    text_w = sum(parts[i].width for i in idx)
    avail = length - (natural - text_w)
    if avail < 8 * len(idx):
        return parts
    out = list(parts)
    for i in idx:
        mw = max(8, int(avail * parts[i].width / text_w))
        try:
            out[i] = _text(blocks[i], h, c, max_width=mw)
        except ValueError:
            return parts
    return out


def _framed(content_parts: list[Image.Image], c: Ctx, h: int, length: int | None,
            blocks=None) -> Image.Image:
    d = c.d
    t = d.frame_dots or FR.FRAME_DEFAULT_DOTS.get(d.frame, 2)
    ix, iy = FR.frame_insets(d.frame, t, h)
    gap = c.dots(d.gap_mm) if d.gap_mm else 0
    inner_len = None if length is None else length - 2 * ix
    if inner_len is not None and inner_len <= 0:
        raise ValueError("label length is shorter than its frame")
    if inner_len is not None and blocks is not None:
        content_parts = _shrink_to(content_parts, blocks, h - 2 * iy, c, gap, inner_len)
    row = _compose(content_parts, h - 2 * iy, gap, inner_len, d.distribute)
    canvas = _blank(row.width + 2 * ix, h)
    canvas.paste(row, (ix, iy))
    FR.draw_frame(canvas, d.frame, t)
    return canvas


def content_height(design: LabelDesign, h: int) -> int:
    t = design.frame_dots or FR.FRAME_DEFAULT_DOTS.get(design.frame, 2)
    return h - 2 * FR.frame_insets(design.frame, t, h)[1]


def build(design: LabelDesign, profile: Profile, tape_mm: int) -> tuple[Image.Image, int]:
    """(content bitmap, margin dots). The bitmap excludes the blank lead/trail margins."""
    c = Ctx(design, profile)
    h = profile.dots_for(tape_mm)
    margin_mm = design.margin_mm
    if margin_mm is None:
        margin_mm = 1.0 if design.layout == "cable_wrap" else profile.default_margin_mm
    margin = profile.mm_to_dots(margin_mm)
    ch = content_height(design, h)
    if ch < 8:
        raise ValueError("frame leaves no room for content on this tape")
    gap = c.dots(design.gap_mm) if design.gap_mm else 0

    def total_to_content(mm: float | None) -> int | None:
        if mm is None:
            return None
        if mm > MAX_LENGTH_MM:
            raise ValueError(f"length_mm is limited to {MAX_LENGTH_MM}")
        dots = profile.mm_to_dots(mm) - 2 * margin
        if dots <= 0:
            raise ValueError("length_mm is shorter than the two margins")
        return dots

    if design.layout == "cable_wrap":
        if not design.cable_diameter_mm:
            raise ValueError("cable_wrap needs cable_diameter_mm")
        blocks = [b.model_copy(update={"direction": "rotated_left"})
                  if isinstance(b, TextBlock) and b.direction is None else b for b in design.blocks]
        unit = _compose(_row(blocks, ch, c, gap), ch, gap, None, "start")
        total = total_to_content(design.length_mm) or max(
            unit.width, profile.mm_to_dots(math.pi * design.cable_diameter_mm * design.wraps) - 2 * margin)
        k = max(1, total // (unit.width + gap))
        seg = total / k
        row = _blank(total, ch)
        for i in range(k):
            row.paste(unit, (round(i * seg + (seg - unit.width) / 2), 0))
        t = design.frame_dots or FR.FRAME_DEFAULT_DOTS.get(design.frame, 2)
        ix, iy = FR.frame_insets(design.frame, t, h)
        img = _blank(row.width + 2 * ix, h)
        img.paste(row, (ix, iy))
        FR.draw_frame(img, design.frame, t)
    elif design.layout == "cable_flag":
        if not design.cable_diameter_mm:
            raise ValueError("cable_flag needs cable_diameter_mm")
        flag_len = c.dots(design.flag_length_mm)
        flag = _framed(_row(design.blocks, ch, c, gap), c, h, flag_len, design.blocks)
        wrap = profile.mm_to_dots(math.pi * design.cable_diameter_mm)
        img = _blank(flag.width * 2 + wrap, h)
        img.paste(flag, (0, 0))
        img.paste(flag.rotate(180), (flag.width + wrap, 0))
        dd = ImageDraw.Draw(img)  # small fold guides at the wrap boundaries
        for x in (flag.width + 1, flag.width + wrap - 2):
            for y in (0, h - 3):
                dd.rectangle([x, y, x, y + 2], fill=0)
    else:
        length = total_to_content(design.length_mm)
        img = _framed(_row(design.blocks, ch, c, gap), c, h, length, design.blocks)
        if design.min_length_mm and length is None:
            want = total_to_content(design.min_length_mm)
            if want and img.width < want:
                pad = _blank(want, h)
                x = {"left": 0, "right": want - img.width}.get(design.align, (want - img.width) // 2)
                if design.frame != "none":  # grow the frame instead of padding outside it
                    img = _framed(_row(design.blocks, ch, c, gap), c, h, want)
                else:
                    pad.paste(img, (x, 0))
                    img = pad
    if design.invert:
        img = ImageOps.invert(img.convert("L"))
    if design.mirror:
        img = ImageOps.mirror(img)
    return img, margin


# --- the simple (flat) API ----------------------------------------------------------

def design_from_simple(p: dict) -> LabelDesign:
    """Turn the flat tool arguments into a LabelDesign.

    Order along the tape: image, symbol, QR | divider | table (cells), text, barcode.
    """
    from .model import LabelDesign as LD

    p = {k: v for k, v in p.items() if v is not None}
    blocks = p.pop("blocks", None)
    content_keys = ("text", "qr", "barcode", "barcode_type", "barcode_text", "symbol",
                    "image_base64", "cells", "cell_length_mm", "cell_border", "divider")
    content = {k: p.pop(k) for k in content_keys if k in p}
    if not blocks:
        lead, rest = [], []
        if content.get("image_base64"):
            lead.append({"type": "image", "image_base64": content["image_base64"]})
        if content.get("symbol"):
            lead.append({"type": "symbol", "name": content["symbol"]})
        if content.get("qr"):
            lead.append({"type": "qr", "data": content["qr"]})
        if content.get("cells"):
            rest.append({"type": "table", "rows": [content["cells"]],
                         "cell_length_mm": content.get("cell_length_mm"),
                         "border": content.get("cell_border", "columns")})
        if content.get("text"):
            rest.append({"type": "text", "text": content["text"]})
        if content.get("barcode"):
            rest.append({"type": "barcode", "data": content["barcode"],
                         "symbology": content.get("barcode_type", "code128"),
                         "show_text": content.get("barcode_text", True)})
        div = content.get("divider")
        if div and div != "none" and lead and rest:
            blocks = lead + [{"type": "divider", "style": div}] + rest
        elif div and div != "none" and len(rest) > 1:
            blocks = rest[:1] + [{"type": "divider", "style": div}] + rest[1:]
        else:
            blocks = lead + rest
    return LD.model_validate({**p, "blocks": blocks})
