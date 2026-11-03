"""Text rendering: fonts, per-line styles, inline markup, effects and text direction."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from PIL import Image, ImageDraw

from . import fonts as F
from . import symbols as S

STYLE_FIELDS = ("font", "font_size", "scale", "bold", "italic", "underline", "strikethrough",
                "effect", "width_pct", "letter_spacing")
DESCENDERS = set("gjpqyQ,;()[]{}|$@_µ")


@dataclass(frozen=True)
class Style:
    font: str | None = None
    font_size: int | None = None
    scale: float = 1.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    effect: str = "none"
    width_pct: int = 100
    letter_spacing: int = 0

    def merged(self, obj) -> "Style":
        """Overlay the non-None TextStyle fields of a pydantic object or dict."""
        get = obj.get if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))
        upd = {k: get(k) for k in STYLE_FIELDS if get(k) is not None}
        return replace(self, **upd) if upd else self


@dataclass
class Run:
    text: str = ""
    icon: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False


_TOKEN = re.compile(r"\\(.)|\*\*|__|~~|\*|\[\[([^\]]+)\]\]")


def parse_markup(line: str, enabled: bool) -> list[Run]:
    if not enabled:
        return [Run(line)]
    runs: list[Run] = []
    flags = {"**": False, "*": False, "__": False, "~~": False}
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            runs.append(Run(buf, bold=flags["**"], italic=flags["*"], underline=flags["__"],
                            strike=flags["~~"]))
            buf = ""

    pos = 0
    for m in _TOKEN.finditer(line):
        buf += line[pos:m.start()]
        pos = m.end()
        if m.group(1) is not None:
            buf += m.group(1)
        elif m.group(2):
            flush()
            runs.append(Run(icon=m.group(2).strip(), bold=flags["**"]))
        else:
            flush()
            flags[m.group(0)] = not flags[m.group(0)]
    buf += line[pos:]
    flush()
    return runs or [Run("")]


@dataclass
class Metrics:
    cap_top: int    # negative: dots above the baseline
    bottom: int     # dots below the baseline
    path: str

    @property
    def height(self) -> int:
        return self.bottom - self.cap_top

    @property
    def cap(self) -> int:
        return -self.cap_top


def metrics(style: Style, size: int, descenders: bool = True, text: str = "") -> Metrics:
    face = F.resolve(style.font, style.bold, style.italic)
    font = F.get_font(face.path, size)
    top = font.getbbox("Hdlfk", anchor="ls")[1]
    bottom = font.getbbox("gjpqy", anchor="ls")[3] if descenders else max(
        1, font.getbbox("H", anchor="ls")[3])
    if text.strip():  # make room for this line's actual ink (swashes, accents)
        try:
            tb = font.getbbox(text, anchor="ls")
            top, bottom = min(top, tb[1]), max(bottom, tb[3])
        except Exception:
            pass
    return Metrics(top - 1, bottom + 1, face.path)


def _stroke(size: int) -> int:
    return max(1, round(size / 22))


def render_line(runs: list[Run], style: Style, size: int, m: Metrics) -> Image.Image:
    """One line, baseline at y = m.cap, height m.height. Width as wide as needed."""
    parts: list[Image.Image] = []
    for run in runs:
        rs = replace(style, bold=style.bold or run.bold, italic=style.italic or run.italic,
                     underline=style.underline or run.underline,
                     strikethrough=style.strikethrough or run.strike)
        parts.append(_render_run(run, rs, size, m))
    width = sum(p.width for p in parts)
    out = Image.new("L", (max(1, width), m.height), 255)
    x = 0
    for p in parts:
        out.paste(p, (x, 0))
        x += p.width
    if style.width_pct != 100 and out.width > 1:
        out = out.resize((max(1, round(out.width * style.width_pct / 100)), out.height),
                         Image.LANCZOS)
    return out


def _render_run(run: Run, st: Style, size: int, m: Metrics) -> Image.Image:
    base = m.cap
    if run.icon:
        icon = S.render_symbol(run.icon, max(4, m.height), size=max(4, round(m.cap * 1.05)))
        pad = max(1, size // 10)
        img = Image.new("L", (icon.width + 2 * pad, m.height), 255)
        # icon image is m.height tall with the glyph centred; shift so it sits on the baseline
        bbox = Image.eval(icon, lambda v: 255 - v).getbbox() or (0, 0, icon.width, icon.height)
        img.paste(icon, (pad, base - bbox[3]))
        return img
    face = F.resolve(st.font, st.bold, st.italic)
    sw = _stroke(size)
    fake_bold = sw if face.fake_bold else 0
    outline = st.effect in ("outline", "outline_shadow")
    shadow = st.effect in ("shadow", "outline_shadow")
    off = max(1, size // 14) if shadow else 0
    segs = _segments(run.text, face.path, st.bold)
    est = int(sum(F.get_font(p, size).getlength(t) for t, p in segs)
              + len(run.text) * max(0, st.letter_spacing) + 4 * sw + off + size)
    pad = sw + 1 + size // 3   # room for swashes / negative side bearings, trimmed below
    img = Image.new("L", (max(1, est + 2 * pad), m.height), 255)
    d = ImageDraw.Draw(img)

    def draw(dx: int, dy: int, main: bool) -> int:
        x = pad
        for text, path in segs:
            font = F.get_font(path, size)
            chunks = list(text) if st.letter_spacing else [text]
            for ch in chunks:
                kw: dict = {"font": font, "anchor": "ls"}
                if not main:      # drop shadow: solid, slightly fattened
                    kw.update(fill=0, stroke_width=sw + fake_bold, stroke_fill=0)
                elif outline or shadow:  # hollow letters
                    kw.update(fill=255, stroke_width=sw + fake_bold, stroke_fill=0)
                else:
                    kw.update(fill=0)
                    if fake_bold:
                        kw.update(stroke_width=fake_bold, stroke_fill=0)
                d.text((x + dx, base + dy), ch, **kw)
                x += round(font.getlength(ch)) + (st.letter_spacing if st.letter_spacing else 0)
        return x

    if shadow:
        draw(off, off, False)
    end = draw(0, 0, True)
    width = end + sw + 1 + off
    lt = max(1, size // 14)
    if st.underline:
        y = min(m.height - lt, base + max(2, m.bottom // 2))
        d.rectangle([pad, y, end - 1, y + lt - 1], fill=0)
    if st.strikethrough:
        y = base - m.cap * 2 // 5
        d.rectangle([pad, y - lt // 2, end - 1, y - lt // 2 + lt - 1], fill=0)
    ink = Image.eval(img, lambda v: 255 - v).getbbox()
    left = max(0, min(pad - sw - 1, ink[0] if ink else pad))
    right = max(width, ink[2] + 1) if ink else width
    img = img.crop((left, 0, max(left + 1, right), m.height))
    if face.fake_italic:
        shear = 0.2
        extra = int(m.height * shear) + 1
        wide = Image.new("L", (img.width + extra, img.height), 255)
        wide.paste(img, (extra, 0))
        img = wide.transform(wide.size, Image.AFFINE, (1, shear, -shear * 0, 0, 1, 0),
                             resample=Image.BICUBIC, fillcolor=255)
        bbox = Image.eval(img, lambda v: 255 - v).getbbox()
        if bbox:
            img = img.crop((max(0, bbox[0] - 1), 0, min(img.width, bbox[2] + 1), img.height))
    return img


def _segments(text: str, primary: str, bold: bool) -> list[tuple[str, str]]:
    """Split text into (substring, font path) using a fallback font for missing glyphs."""
    out: list[tuple[str, str]] = []
    for ch in text:
        path = primary if F.covers(primary, ch) else (F.fallback_for(ch, bold) or primary)
        if out and out[-1][1] == path:
            out[-1] = (out[-1][0] + ch, path)
        else:
            out.append((ch, path))
    return out or [("", primary)]


# --- blocks -------------------------------------------------------------------

@dataclass
class LineSpec:
    runs: list[Run]
    style: Style
    align: str
    plain: str


def _place(lines_imgs: list[tuple[Image.Image, str]], width: int, height: int, v_align: str,
           gap: int) -> Image.Image:
    total = sum(i.height for i, _ in lines_imgs) + gap * (len(lines_imgs) - 1)
    out = Image.new("L", (max(1, width), height), 255)
    y = {"top": 0, "bottom": height - total}.get(v_align, (height - total) // 2)
    for img, align in lines_imgs:
        x = {"left": 0, "right": width - img.width}.get(align, (width - img.width) // 2)
        out.paste(img, (x, y))
        y += img.height + gap
    return out


def _layout_horizontal(lines: list[LineSpec], height: int, max_width: int | None,
                       spacing: float, v_align: str, width: int | None) -> Image.Image:
    single = len(lines) == 1
    desc = not single or any(c in DESCENDERS for c in lines[0].plain)

    def build(base_size: int):
        imgs, total, widest = [], 0, 0
        for ln in lines:
            size = ln.style.font_size or max(4, round(base_size * ln.style.scale))
            m = metrics(ln.style, size, descenders=desc, text=ln.plain)
            img = render_line(ln.runs, ln.style, size, m)
            imgs.append((img, ln.align))
            total += img.height
            widest = max(widest, img.width)
        gap = round(spacing * (total / max(1, len(lines))))
        total += gap * (len(lines) - 1)
        return imgs, total, widest, gap

    auto = any(ln.style.font_size is None for ln in lines)
    if auto:
        lo, hi, best = 4, height * 2, None
        while lo <= hi:
            mid = (lo + hi) // 2
            res = build(mid)
            if res[1] <= height and (max_width is None or res[2] <= max_width):
                best, lo = res, mid + 1
            else:
                hi = mid - 1
        if best is None:
            raise ValueError(f"{len(lines)} line(s) do not fit in {height} dots"
                             + (f" x {max_width} dots" if max_width else "")
                             + "; use fewer lines, shorter text or wider tape")
        imgs, total, widest, gap = best
    else:
        imgs, total, widest, gap = build(0)
        if total > height:
            raise ValueError(f"text needs {total} dots but only {height} fit across the tape; "
                             "reduce font_size or use wider tape")
        if max_width is not None and widest > max_width:
            raise ValueError(f"text is {widest} dots long but the block is {max_width}; "
                             "reduce font_size or lengthen the block")
    return _place(imgs, width or widest, height, v_align, gap)


def _layout_vertical(lines: list[LineSpec], height: int, max_width: int | None,
                     v_align: str, width: int | None) -> Image.Image:
    """Characters stacked across the tape; each line becomes a column."""
    def build(base: int):
        cols = []
        for ln in lines:
            size = ln.style.font_size or max(4, round(base * ln.style.scale))
            m = metrics(ln.style, size, descenders=False)
            cells = [render_line([Run(ch)], ln.style, size, m) for ch in ln.plain if ch != " "]
            cw = max((c.width for c in cells), default=1)
            col_h = sum(c.height for c in cells)
            cols.append((cells, cw, col_h))
        total_w = sum(c[1] for c in cols) + max(0, len(cols) - 1) * 4
        return cols, total_w, max(c[2] for c in cols)

    lo, hi, best = 4, height, None
    if all(ln.style.font_size for ln in lines):
        best = build(0)
    else:
        while lo <= hi:
            mid = (lo + hi) // 2
            res = build(mid)
            if res[2] <= height and (max_width is None or res[1] <= max_width):
                best, lo = res, mid + 1
            else:
                hi = mid - 1
    if best is None or best[2] > height:
        raise ValueError("vertical text does not fit; use fewer characters or wider tape")
    cols, total_w, _ = best
    out = Image.new("L", (max(1, width or total_w), height), 255)
    x = (out.width - total_w) // 2
    for cells, cw, col_h in cols:
        y = {"top": 0, "bottom": height - col_h}.get(v_align, (height - col_h) // 2)
        for c in cells:
            out.paste(c, (x + (cw - c.width) // 2, y))
            y += c.height
        x += cw + 4
    return out


def render_text_block(lines: list[LineSpec], height: int, *, direction: str = "horizontal",
                      spacing: float = 0.1, v_align: str = "center",
                      width: int | None = None, max_width: int | None = None) -> Image.Image:
    """Render lines into a block `height` dots tall (and `width` long if fixed)."""
    if direction == "vertical":
        return _layout_vertical(lines, height, width or max_width, v_align, width)
    if direction in ("rotated_left", "rotated_right"):
        # Lay out with the tape height as the line length, then turn it.
        img = _layout_horizontal(lines, height=width or max_width or height * 6, max_width=height,
                                 spacing=spacing, v_align="center", width=height)
        if not width:  # trim the unused length
            bbox = Image.eval(img, lambda v: 255 - v).getbbox()
            if bbox:
                img = img.crop((0, max(0, bbox[1] - 1), img.width, min(img.height, bbox[3] + 1)))
        img = img.rotate(90 if direction == "rotated_left" else -90, expand=True, fillcolor=255)
        if img.height != height:
            canvas = Image.new("L", (img.width, height), 255)
            canvas.paste(img, (0, (height - img.height) // 2))
            img = canvas
        return img
    return _layout_horizontal(lines, height, width or max_width, spacing, v_align, width)


def make_lines(texts: list[tuple[str, object | None]], base: Style, align: str,
               markup: bool) -> list[LineSpec]:
    """texts: (line text, per-line style object or None)."""
    out = []
    for text, obj in texts:
        st = base.merged(obj) if obj is not None else base
        al = getattr(obj, "align", None) or align
        out.append(LineSpec(parse_markup(text, markup), st, al,
                            re.sub(r"\[\[[^\]]+\]\]|\*\*|__|~~|\*", "", text) if markup else text))
    return out
