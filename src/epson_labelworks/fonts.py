"""Font discovery, friendly aliases and glyph fallback.

Fonts are found by scanning the usual system font directories plus LW_FONT_DIR
(default /data/fonts, so you can drop your own .ttf/.otf files into the data
volume). Families are matched case-insensitively, by alias ("courier",
"helvetica", "narrow"...), or by a unique substring.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

SYSTEM_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts"),
               "/System/Library/Fonts", "/Library/Fonts"]
EXTS = {".ttf", ".otf", ".ttc"}

# Friendly name -> family candidates, first installed one wins.
ALIASES: dict[str, list[str]] = {
    "sans": ["DejaVu Sans", "Liberation Sans", "FreeSans", "Nimbus Sans"],
    "sans-condensed": ["DejaVu Sans Condensed", "Nimbus Sans Narrow", "Liberation Sans Narrow"],
    "narrow": ["Nimbus Sans Narrow", "Liberation Sans Narrow", "DejaVu Sans Condensed"],
    "serif": ["DejaVu Serif", "Liberation Serif", "Nimbus Roman", "FreeSerif"],
    "serif-condensed": ["DejaVu Serif Condensed"],
    "mono": ["DejaVu Sans Mono", "Liberation Mono", "Cousine", "FreeMono"],
    "courier": ["Nimbus Mono PS", "FreeMono", "Courier 10 Pitch", "Liberation Mono"],
    "typewriter": ["Nimbus Mono PS", "FreeMono", "Courier 10 Pitch"],
    "helvetica": ["Nimbus Sans", "Liberation Sans", "FreeSans", "Arimo"],
    "arial": ["Liberation Sans", "Arimo", "Nimbus Sans", "FreeSans"],
    "times": ["Nimbus Roman", "Liberation Serif", "Tinos", "FreeSerif"],
    "gothic": ["URW Gothic"],
    "avant-garde": ["URW Gothic"],
    "bookman": ["URW Bookman"],
    "schoolbook": ["C059"],
    "century": ["C059"],
    "palatino": ["P052"],
    "script": ["Z003"],
    "chancery": ["Z003"],
}
DEFAULT_FAMILY = "sans"
FALLBACK_FAMILIES = ["DejaVu Sans", "FreeSans", "FreeSerif", "Noto Sans Symbols2",
                     "Noto Sans Symbols", "DejaVu Sans Mono", "FreeMono"]

BOLD_WORDS = ("bold", "demi", "black", "heavy")
ITALIC_WORDS = ("italic", "oblique", "slanted")
WIDTH_WORDS = ("condensed", "narrow", "semicondensed", "expanded", "extended", "compressed")
# Lower is better. Regular faces first, then near-regular weights; exact Bold before Black etc.
REGULAR_RANK = ["regular", "book", "roman", "normal", "medium", "light", "extralight", "thin"]
BOLD_RANK = ["bold", "demi", "heavy", "black"]


@dataclass
class Face:
    family: str
    path: str
    bold: bool
    italic: bool
    fake_bold: bool = False
    fake_italic: bool = False


@dataclass
class Family:
    name: str
    styles: dict[tuple[bool, bool], tuple[str, str]] = field(default_factory=dict)  # (b,i) -> (path, style)


def font_dirs() -> list[str]:
    extra = [d for d in os.environ.get("LW_FONT_DIR", "/data/fonts").split(os.pathsep) if d]
    return extra + SYSTEM_DIRS


_lock = threading.Lock()
_families: dict[str, Family] | None = None


def _classify(style: str) -> tuple[str, bool, bool, int]:
    """(width suffix, bold, italic, preference rank) for a style name."""
    words = style.lower().replace("-", " ").split()
    width = " ".join(w.capitalize() for w in words if w in WIDTH_WORDS)
    rest = [w for w in words if w not in WIDTH_WORDS]
    s = " ".join(rest)
    bold = any(w in s for w in BOLD_WORDS) and "semibold" not in rest
    italic = any(w in s for w in ITALIC_WORDS)
    if bold:
        rank = 0 if rest and "bold" in rest and not any(x in s for x in ("extra", "ultra", "semi")) else \
            next((i + 1 for i, w in enumerate(BOLD_RANK) if w in s), 9)
    else:
        core = [w for w in rest if w not in ITALIC_WORDS]
        rank = 0 if not core else min(
            (REGULAR_RANK.index(w) if w in REGULAR_RANK else 20) for w in core)
    return width, bold, italic, rank


def _scan() -> dict[str, Family]:
    fams: dict[str, Family] = {}
    ranks: dict[tuple[str, bool, bool], int] = {}
    seen: set[str] = set()
    for d in font_dirs():
        root = Path(d)
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() not in EXTS or not p.is_file():
                continue
            real = str(p.resolve())
            if real in seen:
                continue
            seen.add(real)
            try:
                fam, style = ImageFont.truetype(real, 12).getname()
            except Exception:  # bitmap/colour fonts, broken files
                continue
            if not fam:
                continue
            width, bold, italic, rank = _classify(style or "")
            if width and not fam.lower().endswith(width.lower()):
                fam = f"{fam} {width}"
            key = (fam.lower(), bold, italic)
            f = fams.setdefault(fam.lower(), Family(fam))
            if key not in ranks or rank < ranks[key]:
                ranks[key] = rank
                f.styles[(bold, italic)] = (real, style)
    return fams


def families() -> dict[str, Family]:
    global _families
    with _lock:
        if _families is None:
            _families = _scan()
        return _families


def rescan() -> None:
    global _families
    with _lock:
        _families = None
    get_font.cache_clear()


def find_family(name: str | None) -> Family:
    fams = families()
    if not fams:
        raise ValueError("no fonts installed on the server")
    want = (name or DEFAULT_FAMILY).strip().lower()
    if want in fams:
        return fams[want]
    for cand in ALIASES.get(want, []):
        if cand.lower() in fams:
            return fams[cand.lower()]
    hits = [f for k, f in fams.items() if want in k]
    if len(hits) == 1:
        return hits[0]
    if hits:
        exact_start = [f for f in hits if f.name.lower().startswith(want)]
        if exact_start:
            return min(exact_start, key=lambda f: len(f.name))
        return min(hits, key=lambda f: len(f.name))
    if name is None:  # default alias missing: take anything
        return next(iter(fams.values()))
    raise ValueError(f"unknown font {name!r}. Call list_fonts for the installed families "
                     f"(aliases: {', '.join(sorted(ALIASES))}).")


def resolve(name: str | None, bold: bool = False, italic: bool = False) -> Face:
    fam = find_family(name)
    for b, i in [(bold, italic), (bold, False), (False, italic), (False, False)]:
        if (b, i) in fam.styles:
            path = fam.styles[(b, i)][0]
            return Face(fam.name, path, bold, italic, fake_bold=bold and not b,
                        fake_italic=italic and not i)
    path = next(iter(fam.styles.values()))[0]
    return Face(fam.name, path, bold, italic, fake_bold=bold, fake_italic=italic)


@lru_cache(maxsize=512)
def get_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, max(1, int(size)))


@lru_cache(maxsize=128)
def _cmap(path: str) -> frozenset[int]:
    try:
        from fontTools.ttLib import TTFont

        tt = TTFont(path, fontNumber=0, lazy=True)
        return frozenset((tt.getBestCmap() or {}).keys())
    except Exception:
        return frozenset()


def covers(path: str, ch: str) -> bool:
    if ch.isspace():
        return True
    cm = _cmap(path)
    return not cm or ord(ch) in cm


def fallback_for(ch: str, bold: bool = False) -> str | None:
    fams = families()
    for name in FALLBACK_FAMILIES:
        f = fams.get(name.lower())
        if not f:
            continue
        for b in (bold, False):
            for (fb, fi), (path, _) in f.styles.items():
                if fb == b and not fi and covers(path, ch):
                    return path
    return None


def list_fonts() -> list[dict]:
    fams = families()
    rev: dict[str, list[str]] = {}
    for alias in ALIASES:
        try:
            rev.setdefault(find_family(alias).name, []).append(alias)
        except ValueError:
            pass
    out = []
    for f in sorted(fams.values(), key=lambda f: f.name.lower()):
        styles = sorted({("bold " if b else "") + ("italic" if i else "") or "regular"
                         for b, i in f.styles})
        out.append({"family": f.name, "styles": [s.strip() for s in styles],
                    "aliases": sorted(rev.get(f.name, []))})
    return out
