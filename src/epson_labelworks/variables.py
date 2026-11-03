"""Placeholders, serial numbers and batch expansion.

Any string in a label may contain {name} or {name:format}:
  {n}          serial number (zero-padded to Serial.digits); {n:03} {n:A} (A, B, ... AA) {n:a}
  {i} {count}  1-based label index and batch size
  {date} {time} {datetime:%Y-%m-%d %H:%M}   current local time (strftime format)
  {field}      a value from the current record in `records`
Unknown placeholders are left untouched, so literal braces are safe.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

PLACEHOLDER = re.compile(r"\{(\w+)(?::([^{}]*))?\}")
SKIP_KEYS = {"image_base64", "type", "symbology", "font", "style", "border", "effect",
             "direction", "align", "v_align", "frame", "layout", "distribute", "ecc"}
MAX_LABELS = 200


def _alpha(n: int, upper: bool = True) -> str:
    if n < 1:
        return str(n)
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr((65 if upper else 97) + r) + s
    return s


def _now() -> datetime:
    tz = os.environ.get("LW_TZ") or os.environ.get("TZ")
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz))
        except Exception:
            pass
    return datetime.now().astimezone()


def _fmt(name: str, value: Any, spec: str | None, digits: int) -> str:
    if isinstance(value, datetime):
        default = {"date": "%Y-%m-%d", "time": "%H:%M", "datetime": "%Y-%m-%d %H:%M"}.get(name, "%c")
        return value.strftime(spec or default)
    if isinstance(value, int) and spec in ("A", "a"):
        return _alpha(value, spec == "A")
    if isinstance(value, int) and not spec and name == "n" and digits:
        return str(value).zfill(digits)
    try:
        return format(value, spec or "")
    except (ValueError, TypeError):
        return str(value)


def substitute(text: str, values: dict[str, Any], digits: int = 0) -> str:
    def rep(m: re.Match) -> str:
        name, spec = m.group(1), m.group(2)
        if name not in values:
            return m.group(0)
        return _fmt(name, values[name], spec, digits)

    return PLACEHOLDER.sub(rep, text)


def apply(obj: Any, values: dict[str, Any], digits: int = 0, key: str | None = None) -> Any:
    if key in SKIP_KEYS:
        return obj
    if isinstance(obj, str):
        return substitute(obj, values, digits)
    if isinstance(obj, list):
        return [apply(v, values, digits) for v in obj]
    if isinstance(obj, dict):
        return {k: apply(v, values, digits, k) for k, v in obj.items()}
    return obj


def placeholders(obj: Any) -> list[str]:
    found: list[str] = []

    def walk(o, key=None):
        if key in SKIP_KEYS:
            return
        if isinstance(o, str):
            found.extend(m.group(1) for m in PLACEHOLDER.finditer(o))
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)

    walk(obj)
    return sorted(set(found))


def expand(design: dict, records: list[dict] | None = None, serial: dict | None = None) -> list[dict]:
    """One design dict per label, with placeholders filled in."""
    serial = serial or {}
    start, step = int(serial.get("start", 1)), int(serial.get("step", 1))
    digits = int(serial.get("digits", 0))
    count = len(records) if records else int(serial.get("count", 1))
    if count > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} labels per call (asked for {count})")
    now = _now()
    out = []
    for i in range(count):
        values: dict[str, Any] = {"date": now, "time": now, "datetime": now, "i": i + 1,
                                  "count": count, "n": start + i * step}
        if records:
            values.update({str(k): v for k, v in records[i].items()})
        out.append(apply(design, values, digits))
    return out
