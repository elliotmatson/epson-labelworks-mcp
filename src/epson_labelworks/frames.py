"""Label frames (borders around the whole label) and divider lines."""

from __future__ import annotations

from PIL import Image, ImageDraw

# How far content must stay inside each frame style, in multiples of the line thickness,
# plus fixed padding. (left/right inset, top/bottom inset)
FRAME_DEFAULT_DOTS = {"line": 2, "thick": 4, "double": 2, "rounded": 2, "dashed": 2, "dotted": 2,
                      "bracket": 3, "arrow": 2, "notch": 2, "hazard": 2, "shadow": 2, "corners": 3}


def frame_insets(style: str, t: int, height: int) -> tuple[int, int]:
    """(along-tape inset, across-tape inset) the content must respect."""
    pad = max(2, height // 24)
    if style == "none":
        return 0, 0
    if style == "double":
        return 3 * t + pad, 3 * t + pad
    if style == "hazard":
        band = max(4, height // 7)
        return band + pad, band + pad
    if style == "shadow":
        return 2 * t + pad, 2 * t + pad
    if style in ("arrow", "notch"):
        return height // 2 + pad, t + pad
    if style == "bracket":
        return t + pad + 2, t + pad
    if style == "corners":
        return t + pad, t + pad
    return t + pad, t + pad


def _dash_h(d, x0, x1, y, t, on, off):
    x = x0
    while x <= x1:
        d.rectangle([x, y, min(x + on - 1, x1), y + t - 1], fill=0)
        x += on + off


def _dash_v(d, x, y0, y1, t, on, off):
    y = y0
    while y <= y1:
        d.rectangle([x, y, x + t - 1, min(y + on - 1, y1)], fill=0)
        y += on + off


def draw_frame(img: Image.Image, style: str, t: int) -> None:
    if style == "none":
        return
    d = ImageDraw.Draw(img)
    w, h = img.size
    r = w - 1
    b = h - 1
    if style in ("line", "thick"):
        d.rectangle([0, 0, r, b], outline=0, width=t)
    elif style == "double":
        d.rectangle([0, 0, r, b], outline=0, width=t)
        d.rectangle([2 * t, 2 * t, r - 2 * t, b - 2 * t], outline=0, width=t)
    elif style == "rounded":
        d.rounded_rectangle([0, 0, r, b], radius=max(4, h // 4), outline=0, width=t)
    elif style in ("dashed", "dotted"):
        on, off = (max(6, 3 * t), max(4, 2 * t)) if style == "dashed" else (t, t + 1)
        for y in (0, b - t + 1):
            _dash_h(d, 0, r, y, t, on, off)
        for x in (0, r - t + 1):
            _dash_v(d, x, 0, b, t, on, off)
    elif style == "bracket":
        arm = max(4, h // 6)
        for x, sgn in ((0, 1), (r - t + 1, -1)):
            d.rectangle([x, 0, x + t - 1, b], fill=0)
            x2 = x + sgn * arm
            d.rectangle([min(x, x2), 0, max(x, x2) + t - 1, t - 1], fill=0)
            d.rectangle([min(x, x2), b - t + 1, max(x, x2) + t - 1, b], fill=0)
    elif style == "corners":
        arm = max(6, h // 4)
        for cx, sx in ((0, 1), (r, -1)):
            for cy, sy in ((0, 1), (b, -1)):
                xs = sorted([cx, cx + sx * arm])
                ys = sorted([cy, cy + sy * (t - 1)])
                d.rectangle([xs[0], ys[0], xs[1], ys[1]], fill=0)
                xs = sorted([cx, cx + sx * (t - 1)])
                ys = sorted([cy, cy + sy * arm])
                d.rectangle([xs[0], ys[0], xs[1], ys[1]], fill=0)
    elif style in ("arrow", "notch"):
        mid = h // 2
        tip = mid
        if style == "arrow":  # pointed ends
            pts = [(tip, 0), (r - tip, 0), (r, mid), (r - tip, b), (tip, b), (0, mid)]
        else:  # flag / ribbon notches cut into the ends
            pts = [(0, 0), (r, 0), (r - tip, mid), (r, b), (0, b), (tip, mid)]
        d.polygon(pts, outline=0, width=t)
    elif style == "hazard":
        band = max(4, h // 7)
        stripe = max(4, band)
        for y0 in (0, h - band):
            band_img = Image.new("L", (w, band), 255)
            bd = ImageDraw.Draw(band_img)
            for x in range(-band, w + band, 2 * stripe):
                bd.polygon([(x, band), (x + stripe, band), (x + stripe + band, 0), (x + band, 0)], fill=0)
            img.paste(band_img, (0, y0))
        d.rectangle([0, 0, r, b], outline=0, width=max(1, t // 2))
    elif style == "shadow":
        d.rectangle([0, 0, r - t, b - t], outline=0, width=t)
        d.rectangle([t, b - t + 1, r, b], fill=0)
        d.rectangle([r - t + 1, t, r, b], fill=0)


def render_divider(height: int, length: int, style: str, thickness: int, coverage_pct: int) -> Image.Image:
    img = Image.new("L", (max(1, length), height), 255)
    d = ImageDraw.Draw(img)
    span = max(1, round(height * coverage_pct / 100))
    y0 = (height - span) // 2
    y1 = y0 + span - 1
    t = thickness * 2 if style == "thick" else thickness
    x = (length - t) // 2
    if style == "double":
        g = max(2, thickness)
        x = (length - 2 * thickness - g) // 2
        d.rectangle([x, y0, x + thickness - 1, y1], fill=0)
        d.rectangle([x + thickness + g, y0, x + 2 * thickness + g - 1, y1], fill=0)
    elif style == "dashed":
        _dash_v(d, x, y0, y1, t, max(4, 3 * t), max(3, 2 * t))
    elif style == "dotted":
        _dash_v(d, x, y0, y1, t, t, t + 1)
    else:
        d.rectangle([x, y0, x + t - 1, y1], fill=0)
    return img
