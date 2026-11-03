"""Job building, status polling and the print workflow."""

from __future__ import annotations

import time
from dataclasses import dataclass

from PIL import Image

from . import protocol as P
from .profiles import Profile
from .render import to_columns
from .status import (PrinterStatus, find_text_frame, parse_text_frame,
                     parse_usb_lwstatus)
from .transport import FileTransport, Transport, TransportError, UsbTransport


class PrinterError(RuntimeError):
    pass


class PrinterStalled(PrinterError):
    pass


STATUS_WRITE_TIMEOUT = 3.0   # a status request is 9 bytes; a healthy printer takes it at once
JOB_WRITE_TIMEOUT = 20.0
STALL_HINT = ("The printer stopped accepting data (stalled). The server reset its USB port; "
              "if the next print also stalls, power-cycle the printer.")


@dataclass
class JobSettings:
    tape_mm: int
    cut: str = "job"          # job | each | none
    density: int = 0          # -5..5
    speed: int = 1            # level 3: 0 slow, 1 high
    margin_mm: float | None = None


def build_job(img: Image.Image, profile: Profile, s: JobSettings) -> bytes:
    """Complete byte stream for one label (one page)."""
    dots = profile.dots_for(s.tape_mm)
    margin_mm = profile.default_margin_mm if s.margin_mm is None else s.margin_mm
    margin_dots = profile.mm_to_dots(margin_mm)
    cols = to_columns(img, dots, profile.flip_across)
    blank = bytes((dots + 7) // 8)
    feed_positions = len(cols) + 2 * margin_dots

    out = bytearray()
    out += P.reset()
    if profile.status_protocol == "v1":
        out += P.status_request_v1(True)
    # job environment
    out += P.print_end() + P.job_init() + P.cut_options(s.cut, profile.cut_variant)
    out += P.density(s.density) + P.job_option_g()
    # page environment
    if profile.status_protocol == "v1":
        out += P.status_request_v1(True)
    out += P.page_length(feed_positions) + P.margin(margin_dots)
    if profile.capability_level in (3, 4):
        out += P.option_h(0x05) + P.print_speed(s.speed)
    elif profile.capability_level == 2:
        raise PrinterError("capability level 2 page commands (O/W/t) are not implemented")
    # raster
    for _ in range(margin_dots):
        out += P.raster_record(dots, blank)
    for c in cols:
        out += P.raster_record(dots, c)
    for _ in range(margin_dots):
        out += P.raster_record(dots, blank)
    out += P.FORM_FEED
    out += P.print_end()
    return bytes(out)


class LabelPrinter:
    def __init__(self, transport: Transport, profile: Profile):
        self.t = transport
        self.profile = profile
        self._usb_text_status: bool | None = None  # learned on first USB status read

    # --- status ------------------------------------------------------------
    def read_status(self, timeout: float = 3.0, request: bool = True) -> PrinterStatus | None:
        """Ask for (and read) a status report. Must be called with the transport open."""
        if isinstance(self.t, FileTransport):
            return None
        if isinstance(self.t, UsbTransport):
            if self._usb_text_status is not False:
                text = self._read_text_status(min(timeout, 1.5), request)
                self._usb_text_status = text is not None
                if text:
                    return text
            block = self.t.get_lwstatus()
            return parse_usb_lwstatus(block) if block else None
        return self._read_text_status(timeout, request)

    def _read_text_status(self, timeout: float, request: bool) -> PrinterStatus | None:
        if request:
            self.t.write(P.status_request_v1(True), timeout=STATUS_WRITE_TIMEOUT)
        buf = b""
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            buf += self.t.read(256, timeout=0.3)
            frame, buf = find_text_frame(buf)
            if frame:
                latest = parse_text_frame(frame)
                return latest
        return latest

    def status(self) -> PrinterStatus | None:
        with self.t:
            try:
                return self.read_status()
            finally:
                if not isinstance(self.t, FileTransport):
                    try:
                        self.t.write(P.status_request_v1(False), timeout=STATUS_WRITE_TIMEOUT)
                    except TransportError:
                        pass

    # --- printing ----------------------------------------------------------
    def print_image(self, img: Image.Image, settings: JobSettings, *, check_tape: bool = True,
                    wait_timeout: float = 60.0) -> dict:
        job = build_job(img, self.profile, settings)
        result: dict = {"bytes": len(job), "tape_mm": settings.tape_mm,
                        "length_mm": round(self.profile.dots_to_mm(img.width + 2 * self.profile.mm_to_dots(
                            self.profile.default_margin_mm if settings.margin_mm is None else settings.margin_mm)), 1)}
        with self.t:
            if isinstance(self.t, FileTransport):
                self.t.write(job)
            else:
                try:
                    pre = self.read_status()
                except TransportError as e:
                    self.t.reset()
                    raise PrinterStalled(f"{e}. {STALL_HINT}") from None
                result["preflight"] = pre.to_dict() if pre else None
                if pre is not None:
                    if pre.error_code not in (None, 0):
                        raise PrinterError(f"printer reports error: {pre.error}")
                    if pre.state_code not in (None, 0x00, 0x04, 0x05):
                        raise PrinterError(f"printer busy: {pre.state}")
                    if check_tape and pre.tape_width_mm and pre.tape_width_mm != settings.tape_mm:
                        raise PrinterError(
                            f"loaded tape is {pre.tape_width_mm} mm but label is for {settings.tape_mm} mm")
                try:
                    self.t.write(job, timeout=JOB_WRITE_TIMEOUT)
                    result["final"] = self._wait_done(wait_timeout)
                except (PrinterStalled, TransportError) as e:
                    self.t.reset()
                    raise PrinterStalled(f"{e}. {STALL_HINT}") from None
                try:
                    self.t.write(P.status_request_v1(False), timeout=STATUS_WRITE_TIMEOUT)
                except TransportError:
                    pass
        if isinstance(self.t, FileTransport) and self.t.last_path:
            result["job_file"] = str(self.t.last_path)
        return result

    def _wait_done(self, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        last: PrinterStatus | None = None
        saw_busy = False
        failures = 0
        while time.monotonic() < deadline:
            try:
                st = self.read_status(timeout=2.0, request=True)
                failures = 0
            except TransportError as e:
                failures += 1
                if failures >= 3:
                    raise PrinterStalled(f"no response to status requests ({e})") from None
                continue
            if st is None:
                continue
            last = st
            if st.error_code not in (None, 0):
                raise PrinterError(f"printer error during job: {st.error}")
            if st.state_code == 0x05:
                return st.to_dict()
            if st.state_code in (0x01, 0x02, 0x03):
                saw_busy = True
            elif st.source == "usb_lwstatus" and st.state_code == 0 and saw_busy:
                return st.to_dict()
            time.sleep(0.4)
        if last is None:
            return None  # no status channel; assume fire-and-forget
        raise PrinterStalled(f"timed out after {timeout:.0f}s waiting for print end "
                             f"(last state: {last.state})")


def probe(transport: Transport) -> dict:
    """Read-only identification. Sends nothing that prints, feeds or cuts."""
    out: dict = {"transport": transport.describe()}
    try:
        with transport:
            if isinstance(transport, UsbTransport):
                out["usb"] = transport.info().__dict__
            if isinstance(transport, FileTransport):
                out["note"] = "dry-run transport; nothing to probe"
                return out
            transport.write(P.status_request_v1(True), timeout=STATUS_WRITE_TIMEOUT)
            raw = b""
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                raw += transport.read(256, timeout=0.3)
                if len(raw) >= 128:
                    break
            transport.write(P.status_request_v1(False), timeout=STATUS_WRITE_TIMEOUT)
            out["status_raw_hex"] = raw.hex(" ") if raw else None
            frame, _ = find_text_frame(raw)
            out["status"] = parse_text_frame(frame).to_dict() if frame else None
            if not frame:
                out["note"] = ("No protocol-v1 text status received. Over USB the binary "
                               "GetLWStatus block above may be the only status channel.")
    except TransportError as e:
        out["error"] = str(e)
    return out
