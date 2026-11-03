"""Epson LabelWorks ESC/P-L2 ("ESCPL2") command encoding.

This is a clean implementation written from the published protocol research:

* tyalie/RE-epson-label-printer  - Android iLabel SDK analysis (capability levels)
* oxplot/nospero                  - LW-600P Bluetooth implementation
* igrbtn/LW700Print               - LW-700 USB implementation
* stoeppke/ha-epson-labelworks    - consolidated protocol notes (docs/PROTOCOL.md)

Nothing here is an official Epson specification.

Framing
-------
Control frame:  ESC '{' <len> <cmd> <params...> <sum> '}'
    len = len(params) + 3   (cmd + sum + '}')
    sum = (cmd + sum(params)) & 0xFF
Raster record:  ESC '.' 00 00 00 01 <dots u16le> <bitmap, MSB first>
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator

ESC = 0x1B
FORM_FEED = b"\x0c"


def frame(cmd: int | str, params: bytes = b"") -> bytes:
    """Build one framed control command."""
    if isinstance(cmd, str):
        cmd = ord(cmd)
    if not 0 <= cmd <= 0xFF:
        raise ValueError(f"command byte out of range: {cmd}")
    if len(params) > 0xFF - 3:
        raise ValueError("parameter block too long for one frame")
    checksum = (cmd + sum(params)) & 0xFF
    return bytes([ESC, 0x7B, len(params) + 3, cmd]) + params + bytes([checksum, 0x7D])


# --- fixed commands -------------------------------------------------------

def reset() -> bytes:
    """'!' Reset printer state."""
    return frame("!")


def print_end() -> bytes:
    """'@' Tape-feed / print-end (document state)."""
    return frame("@")


def job_init() -> bytes:
    """'{' Job initialisation signature (always 00 00 53 54)."""
    return frame(0x7B, b"\x00\x00\x53\x54")


def job_option_g() -> bytes:
    """'G' Unknown, hard-coded in every SDK job environment."""
    return frame("G")


def status_request_v1(enable: bool) -> bytes:
    """'Q' Enable/disable unsolicited textual status (Bluetooth protocol v1)."""
    return frame("Q", b"\x05\x00" if enable else b"\x00\x00")


def status_request_v2(enable: bool) -> bytes:
    """'I' Same as 'Q' for protocol-v2 printers (not used by the PX400)."""
    return frame("I", b"\x05\x00" if enable else b"\x00\x00")


def feed(cut: bool) -> bytes:
    """'+' Feed tape, optionally cutting."""
    return frame("+", b"\x01" if cut else b"\x00")


# --- parameterised commands ----------------------------------------------

# Cutter parameter blocks from the Android SDK.  "fast" (01) variants are the
# ones used by printers without a half-cutter (LW-600P / LW-PX400 class).
CUT_PARAMS = {
    ("each", "fast"): b"\x01\x01\x01\x01",
    ("job", "fast"): b"\x01\x00\x01\x01",
    ("each", "slow"): b"\x02\x02\x01\x01",
    ("job", "slow"): b"\x02\x00\x01\x01",
    ("none", "fast"): b"\x00\x00\x00\x00",
    ("none", "slow"): b"\x00\x00\x00\x00",
}


def cut_options(mode: str = "job", variant: str = "fast") -> bytes:
    """'C' Tape cut options. mode: each | job | none."""
    try:
        return frame("C", CUT_PARAMS[(mode, variant)])
    except KeyError:
        raise ValueError(f"unknown cut mode/variant {mode!r}/{variant!r}") from None


def density(level: int) -> bytes:
    """'D' Print density, -5..+5 (UI exposes -3..+3). Encoded as level + 5."""
    if not -5 <= level <= 5:
        raise ValueError("density must be between -5 and 5")
    return frame("D", bytes([level + 5]))


def page_length(feed_positions: int) -> bytes:
    """'L' Page length in feed positions (raster records), u32 LE."""
    return frame("L", struct.pack("<I", feed_positions))


def margin(dots: int) -> bytes:
    """'T' Margin in dots for the SDK level-1/3 path (u16 LE)."""
    return frame("T", struct.pack("<H", dots))


def option_h(value: int = 0x05) -> bytes:
    """'H' Unknown capability-level-3/4 option; the SDK always sends 05."""
    return frame("H", bytes([value]))


def print_speed(value: int) -> bytes:
    """'s' Print speed. Level 3: 0 = slow, 1 = high."""
    return frame("s", bytes([value]))


# --- raster --------------------------------------------------------------

def raster_record(dots: int, bitmap: bytes) -> bytes:
    """One raster strip across the tape."""
    expected = (dots + 7) // 8
    if len(bitmap) != expected:
        raise ValueError(f"bitmap is {len(bitmap)} bytes, expected {expected} for {dots} dots")
    return bytes([ESC, 0x2E, 0, 0, 0, 1]) + struct.pack("<H", dots) + bitmap


def pack_column(bits: Iterable[bool], dots: int) -> bytes:
    """Pack an iterable of booleans (True = black) MSB-first into dots bits."""
    out = bytearray((dots + 7) // 8)
    for i, black in enumerate(bits):
        if i >= dots:
            break
        if black:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def raster_records(columns: Iterable[bytes], dots: int) -> Iterator[bytes]:
    for col in columns:
        yield raster_record(dots, col)


def hexdump(data: bytes, limit: int = 256) -> str:
    shown = data[:limit].hex(" ")
    return shown + (f" … (+{len(data) - limit} bytes)" if len(data) > limit else "")
