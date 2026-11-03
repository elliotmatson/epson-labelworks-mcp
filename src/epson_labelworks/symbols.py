"""Symbols: Material Design Icons (Pictogrammers, Apache 2.0) plus any Unicode character.

The icon font and a trimmed copy of its metadata (names, aliases, tags) ship in
package data, so symbol lookup works offline. Names are the MDI names without
the "mdi-" prefix, e.g. "lan", "server", "flash-alert", "fire-extinguisher".
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from importlib import resources

from PIL import Image, ImageDraw

from . import fonts as F

DATA = resources.files("epson_labelworks") / "assets"
ICON_FONT = str(DATA / "materialdesignicons-webfont.ttf")

# Label-maker style shortcuts -> MDI names (a few of Epson's symbol categories).
SHORTCUTS = {
    "warning": "alert", "caution": "alert-outline", "danger": "alert-octagon",
    "high-voltage": "flash-alert", "electric": "flash", "ground": "⏚",
    "fire-extinguisher": "fire-extinguisher", "first-aid": "medical-bag", "exit": "exit-run",
    "no-entry": "minus-circle", "no-smoking": "smoking-off", "recycle": "recycle",
    "fragile": "glass-fragile", "this-side-up": "arrow-up-bold", "phone": "phone",
    "network": "lan", "ethernet": "ethernet", "wifi": "wifi", "power": "power-plug",
    "fuse": "fuse", "breaker": "electric-switch", "lightbulb": "lightbulb-outline",
    "speaker": "speaker", "microphone": "microphone", "camera": "cctv", "server": "server",
    "computer": "monitor", "printer": "printer", "check": "check-bold", "cross": "close-thick",
    "star": "star", "heart": "heart", "home": "home", "key": "key", "lock": "lock",
    "arrow-left": "arrow-left-bold", "arrow-right": "arrow-right-bold",
    "arrow-up": "arrow-up-bold", "arrow-down": "arrow-down-bold",
}


@lru_cache(maxsize=1)
def _meta() -> dict[str, tuple[str, list[str], list[str]]]:
    raw = gzip.decompress((DATA / "mdi-meta.json.gz").read_bytes())
    return {name: (cp, aliases, tags) for name, cp, aliases, tags in json.loads(raw)}


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, str]:
    idx = {}
    for name, (_, aliases, _) in _meta().items():
        for a in aliases:
            idx.setdefault(a, name)
    return idx


def lookup(name: str) -> tuple[str, str]:
    """Return (glyph, font path) for an icon name or literal Unicode character(s)."""
    key = name.strip()
    low = key.lower().removeprefix("mdi:").removeprefix("mdi-").replace(" ", "-").replace("_", "-")
    meta = _meta()
    low = SHORTCUTS.get(low, low)
    if low not in meta:
        low = _alias_index().get(low, low)
    if low in meta:
        return chr(int(meta[low][0], 16)), ICON_FONT
    lit = low if not low.isascii() else key
    if lit and len(lit) <= 3 and not lit.isascii():  # literal symbol such as "⚠" or "☎"
        path = F.fallback_for(lit[0]) or F.resolve(None).path
        return lit, path
    near = search(low, limit=5)
    hint = f" Did you mean: {', '.join(n['name'] for n in near)}?" if near else ""
    raise ValueError(f"unknown symbol {name!r}.{hint} Use search_symbols to find icon names.")


def search(query: str, limit: int = 30) -> list[dict]:
    q = query.strip().lower().replace(" ", "-")
    words = [w for w in q.replace("-", " ").split() if w]
    scored = []
    for name, (_, aliases, tags) in _meta().items():
        hay_names = [name, *aliases]
        if q in hay_names:
            score = 0
        elif any(n.startswith(q) for n in hay_names):
            score = 1
        elif all(any(w in n for n in hay_names) for w in words):
            score = 2
        elif words and all(any(w in t.lower() for t in tags) or any(w in n for n in hay_names)
                           for w in words):
            score = 3
        else:
            continue
        scored.append((score, len(name), name, aliases, tags))
    for short, target in SHORTCUTS.items():
        if q and q in short:
            scored.append((0 if q == short else 1, 0, target, [short], ["shortcut"]))
    scored.sort()
    seen, out = set(), []
    for _, _, name, aliases, tags in scored:
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "aliases": aliases[:4], "tags": tags[:3]})
        if len(out) >= limit:
            break
    return out


def render_symbol(name: str, height: int, size: int | None = None) -> Image.Image:
    """Glyph centred in a box `height` dots tall, sized to `size` dots (default: fill)."""
    glyph, path = lookup(name)
    target = max(4, min(height, size or height))
    font = F.get_font(path, target)
    l, t, r, b = font.getbbox(glyph)
    w, h = max(1, r - l), max(1, b - t)
    # Scale so the inked glyph (not the em box) is `target` tall.
    if h != target:
        font = F.get_font(path, max(4, round(target * target / h)))
        l, t, r, b = font.getbbox(glyph)
        w, h = max(1, r - l), max(1, b - t)
        if h > height:
            font = F.get_font(path, max(4, round(font.size * height / h)))
            l, t, r, b = font.getbbox(glyph)
            w, h = max(1, r - l), max(1, b - t)
    img = Image.new("L", (w, height), 255)
    ImageDraw.Draw(img).text((-l, (height - h) // 2 - t), glyph, font=font, fill=0)
    return img
