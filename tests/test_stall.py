import asyncio
import json
import threading
import time

import pytest
from PIL import Image

from epson_labelworks import protocol as P
from epson_labelworks.printer import JobSettings, LabelPrinter, PrinterStalled
from epson_labelworks.profiles import LW_PX400
from epson_labelworks.transport import Transport, TransportError

IDLE = b"@BDC ST\r\nST:00;ER:00;WR:00;RV:00;TW:03;TR:00;CT:00;OP:00;EI:00;\xff"
END = IDLE.replace(b"ST:00", b"ST:05")


class FakePrinter(Transport):
    """Answers status requests; can be told to stop accepting data after the job."""

    name = "fake"

    def __init__(self, stall_after_job=False, print_seconds=0.0):
        self.stall_after_job = stall_after_job
        self.print_seconds = print_seconds
        self.got_job = None
        self.resets = 0
        self.pending = b""

    def open(self):
        pass

    def close(self):
        pass

    def write(self, data, timeout=None):
        if self.got_job and self.stall_after_job:
            raise TransportError(f"printer did not accept data within {timeout:.0f}s")
        if len(data) > 64:
            self.got_job = time.monotonic()
            return
        if data == P.status_request_v1(True):
            done = self.got_job and time.monotonic() - self.got_job >= self.print_seconds
            self.pending += END if done else IDLE.replace(b"ST:00", b"ST:01")
            if not self.got_job:
                self.pending = self.pending.replace(b"ST:01", b"ST:00")

    def read(self, size=256, timeout=0.5):
        out, self.pending = self.pending[:size], self.pending[size:]
        return out

    def reset(self):
        self.resets += 1
        return True

    def describe(self):
        return {"transport": "fake"}


def label():
    return Image.new("L", (40, 72), 255)


def test_normal_print_finishes():
    t = FakePrinter()
    res = LabelPrinter(t, LW_PX400).print_image(label(), JobSettings(tape_mm=12))
    assert res["final"]["state"] == "print_end"


def test_stall_is_detected_quickly_and_resets_usb():
    t = FakePrinter(stall_after_job=True)
    start = time.monotonic()
    with pytest.raises(PrinterStalled, match="stopped accepting data"):
        LabelPrinter(t, LW_PX400).print_image(label(), JobSettings(tape_mm=12))
    assert time.monotonic() - start < 5
    assert t.resets == 1


def test_server_returns_while_printing(monkeypatch, tmp_path):
    monkeypatch.setenv("LW_DATA_DIR", str(tmp_path))
    from epson_labelworks import server as S

    fake = FakePrinter(print_seconds=1.5)
    monkeypatch.setattr(S, "PRINT_WAIT_S", 0.3)
    monkeypatch.setattr(S, "_printer", lambda dry_run: LabelPrinter(fake, LW_PX400))
    monkeypatch.setattr(S, "_job", None)

    async def go():
        r = await S.mcp.call_tool("print_label", {"text": "Hi", "tape_mm": 12})
        info = json.loads(r.content[1].text)
        assert info["state"] == "printing" and "note" in info
        busy = await S.mcp.call_tool("print_label", {"text": "Again", "tape_mm": 12})
        assert "busy" in busy.content[0].text
        st = json.loads((await S.mcp.call_tool("printer_status", {})).content[0].text)
        assert st["busy"] is True
        S._job.done.wait(5)
        assert S._job.summary()["state"] == "done"

    asyncio.run(go())
    assert not any(t.name.startswith("print-") and t.is_alive() for t in threading.enumerate())
