"""Status parsing for protocol-v1 textual frames and the USB GetLWStatus block."""

from __future__ import annotations

from dataclasses import asdict, dataclass

STATUS_FRAME_LEN = 64

STATES = {
    0x00: "idle", 0x01: "feeding", 0x02: "printing", 0x03: "data_sending",
    0x04: "feed_end", 0x05: "print_end", 0x06: "pick_and_print_printing",
    0x10: "demo_printing", 0x11: "device_feeding", 0x12: "device_printing",
    0x13: "firmware_updating", 0x20: "small_roll_waiting",
    0x22: "waiting_for_tape_removal", 0xFF: "unexpected_error",
}

ERRORS = {
    0x00: "no_error", 0x01: "cutter_error", 0x06: "no_tape_cartridge",
    0x15: "head_overheated", 0x20: "printer_cancel", 0x21: "cover_open",
    0x22: "low_voltage", 0x23: "power_off_cancel", 0x24: "tape_eject_error",
    0x30: "tape_feed_error", 0x40: "ink_ribbon_slack", 0x41: "ink_ribbon_short",
    0x42: "tape_end", 0x43: "cut_label_error", 0x44: "temperature_error",
    0x45: "insufficient_parameters",
}

# Raw cartridge 'TW' codes -> tape width in mm.
RAW_TW_MM = {
    0x0B: 4, 0x5B: 4, 0x01: 6, 0x51: 6, 0x02: 9, 0x52: 9, 0x03: 12, 0x53: 12,
    0x04: 18, 0x54: 18, 0x05: 24, 0x55: 24, 0x06: 36, 0x56: 36,
    0x11: 24, 0x12: 36, 0x21: 50, 0x23: 100, 0x07: 50, 0x57: 50,
}
CABLE_TW = {0x11, 0x12}

READY_STATES = {0x00, 0x04, 0x05}


@dataclass
class PrinterStatus:
    source: str
    state_code: int | None
    state: str
    error_code: int | None
    error: str
    tape_code: int | None
    tape_width_mm: int | None
    tape_kind_code: int | None = None
    is_cable_tape: bool = False
    fields: dict[str, int] | None = None
    raw_hex: str = ""

    @property
    def ready(self) -> bool:
        return (self.error_code in (None, 0)) and (self.state_code in READY_STATES)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        return d


def _field(text: str, key: str) -> int | None:
    i = text.rfind(key)
    if i < 0 or i + 5 > len(text):
        return None
    try:
        return int(text[i + 3 : i + 5], 16)
    except ValueError:
        return None


def parse_text_frame(frame: bytes) -> PrinterStatus:
    """Parse a 64-byte '@ST:00;ER:00;TW:03;...' frame."""
    if len(frame) != STATUS_FRAME_LEN or frame[:1] != b"@":
        raise ValueError(f"not a status frame: {frame[:16].hex(' ')}")
    text = frame.rstrip(b"\x00\xff").decode("ascii", errors="replace")
    keys = ("ST", "ER", "TW", "TR", "EI", "EJ", "TO", "IR", "RR", "WR", "RV", "CT", "OP")
    fields = {k: v for k in keys if (v := _field(text, k)) is not None}
    st, er, tw = fields.get("ST"), fields.get("ER"), fields.get("TW")
    return PrinterStatus(
        source="text_v1",
        state_code=st,
        state=STATES.get(st, f"unknown_{st:02x}") if st is not None else "unknown",
        error_code=er,
        error=ERRORS.get(er, f"error_{er:02x}") if er is not None else "unknown",
        tape_code=tw,
        tape_width_mm=RAW_TW_MM.get(tw) if tw is not None else None,
        tape_kind_code=fields.get("TR"),
        is_cable_tape=tw in CABLE_TW,
        fields=fields,
        raw_hex=frame.hex(" "),
    )


def find_text_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Return the last complete status frame in buf, plus leftover bytes."""
    found = None
    while True:
        i = buf.find(b"@ST")
        if i < 0:
            # keep a possible partial '@' at the tail
            j = buf.rfind(b"@")
            return found, (buf[j:] if j >= 0 else b"")
        if len(buf) - i < STATUS_FRAME_LEN:
            return found, buf[i:]
        found = buf[i : i + STATUS_FRAME_LEN]
        buf = buf[i + STATUS_FRAME_LEN :]


def parse_usb_lwstatus(block: bytes) -> PrinterStatus:
    """Parse the binary GetLWStatus (C1/01) control response.

    Byte 1 = activity (0 idle), byte 3 = tape code (same raw TW sequence).
    Only observed on LW-600P/LW-700; treat as provisional for the PX400.
    """
    if len(block) < 4:
        raise ValueError(f"short GetLWStatus block: {block.hex(' ')}")
    activity, tape = block[1], block[3]
    return PrinterStatus(
        source="usb_lwstatus",
        state_code=0x00 if activity == 0 else 0x02,
        state="idle" if activity == 0 else f"busy_{activity:02x}",
        error_code=None,
        error="unknown (not reported by GetLWStatus)",
        tape_code=tape,
        tape_width_mm=RAW_TW_MM.get(tape),
        is_cable_tape=tape in CABLE_TW,
        raw_hex=block.hex(" "),
    )
