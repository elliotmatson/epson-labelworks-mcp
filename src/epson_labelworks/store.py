"""Saved label designs (JSON files under LW_DATA_DIR/labels)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def _dir() -> Path:
    root = Path(os.environ.get("LW_DATA_DIR") or ("/data" if Path("/data").is_dir() else "./data"))
    d = root / "labels"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError("label name must contain letters or digits")
    return s[:80]


def save(name: str, params: dict, description: str | None = None) -> dict:
    entry = {"name": name, "slug": slug(name), "description": description or "",
             "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "params": params}
    (_dir() / f"{entry['slug']}.json").write_text(json.dumps(entry, indent=2))
    return entry


def get(name: str) -> dict:
    p = _dir() / f"{slug(name)}.json"
    if not p.exists():
        names = ", ".join(e["name"] for e in list_all()) or "none saved yet"
        raise ValueError(f"no saved label {name!r} (saved: {names})")
    return json.loads(p.read_text())


def list_all() -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def delete(name: str) -> bool:
    p = _dir() / f"{slug(name)}.json"
    if p.exists():
        p.unlink()
        return True
    return False
