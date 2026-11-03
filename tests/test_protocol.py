"""Byte-level tests against frames published in the reference research."""

import pytest
from PIL import Image, ImageDraw

from epson_labelworks import protocol as P
from epson_labelworks.label import LabelSpec, build_image
from epson_labelworks.printer import JobSettings, build_job
from epson_labelworks.profiles import LW_600P, LW_PX400
from epson_labelworks.render import to_columns
from epson_labelworks.status import find_text_frame, parse_text_frame

H = bytes.fromhex


@pytest.mark.parametrize("built,expected", [
    (P.density(0), "1b 7b 04 44 05 49 7d"),           # documented example
    (P.reset(), "1b 7b 03 21 21 7d"),
    (P.feed(False), "1b 7b 04 2b 00 2b 7d"),
    (P.feed(True), "1b 7b 04 2b 01 2c 7d"),
    (P.print_end(), "1b 7b 03 40 40 7d"),
    (P.job_option_g(), "1b 7b 03 47 47 7d"),
    (P.status_request_v1(True), "1b 7b 05 51 05 00 56 7d"),
    (P.status_request_v1(False), "1b 7b 05 51 00 00 51 7d"),
    (P.status_request_v2(True), "1b 7b 05 49 05 00 4e 7d"),
    (P.job_init(), "1b 7b 07 7b 00 00 53 54 22 7d"),
    (P.cut_options("job"), "1b 7b 07 43 01 00 01 01 46 7d"),
    (P.cut_options("none"), "1b 7b 07 43 00 00 00 00 43 7d"),
])
def test_fixed_frames(built, expected):
    assert built == H(expected)


def test_length_and_margin_encoding():
    assert P.page_length(0x0102) == H("1b 7b 07 4c 02 01 00 00 4f 7d")
    assert P.margin(14) == H("1b 7b 05 54 0e 00 62 7d")
    assert P.option_h() == H("1b 7b 04 48 05 4d 7d")
    assert P.print_speed(1) == H("1b 7b 04 73 01 74 7d")


def test_raster_record_matches_tyalie_example():
    # 72-dot strip from the protocol doc's letter 'h' example
    rec = P.raster_record(72, H("0001fffffffffe0000"))
    assert rec == H("1b 2e 00 00 00 01 48 00 00 01 ff ff ff ff fe 00 00")
    with pytest.raises(ValueError):
        P.raster_record(72, b"\x00")


def test_pack_column_msb_first():
    assert P.pack_column([True] + [False] * 7 + [True], 9) == H("80 80")


def _job_commands(job: bytes) -> list[str]:
    """Sequence of control-frame command letters, raster collapsed to 'R'."""
    seq, i = [], 0
    while i < len(job):
        if job[i] == 0x1B and job[i + 1] == 0x7B:
            seq.append(chr(job[i + 3]))
            i += 3 + job[i + 2]
        elif job[i] == 0x1B and job[i + 1] == 0x2E:
            dots = job[i + 6] | job[i + 7] << 8
            if not seq or seq[-1] != "R":
                seq.append("R")
            i += 8 + (dots + 7) // 8
        elif job[i] == 0x0C:
            seq.append("FF")
            i += 1
        else:
            raise AssertionError(f"unexpected byte {job[i]:02x} at {i}")
    return seq


def test_px400_job_is_level3_sequence():
    img = Image.new("L", (40, 72), 255)
    job = build_job(img, LW_PX400, JobSettings(tape_mm=12))
    assert _job_commands(job) == ["!", "Q", "@", "{", "C", "D", "G", "Q", "L", "T", "H", "s", "R", "FF", "@"]
    margin = LW_PX400.mm_to_dots(2.0)
    # L counts content + leading/trailing blank records
    assert P.page_length(40 + 2 * margin) in job


def test_600p_job_has_no_level3_commands():
    img = Image.new("L", (10, 72), 255)
    seq = _job_commands(build_job(img, LW_600P, JobSettings(tape_mm=12)))
    assert "H" not in seq and "s" not in seq


def test_flip_across_inverts_bit_order():
    img = Image.new("L", (1, 8), 255)
    img.putpixel((0, 0), 0)  # top pixel black
    assert to_columns(img, 8, flip_across=False) == [b"\x80"]
    assert to_columns(img, 8, flip_across=True) == [b"\x01"]


def test_status_frame_parse():
    text = b"@ST:00;ER:00;TW:55;TR:00;EI:00;EJ:00;TO:00;IR:00;RR:00;"
    frame = text + b"\x00" * (63 - len(text)) + b"\xff"
    st = parse_text_frame(frame)
    assert (st.state, st.error, st.tape_width_mm, st.ready) == ("idle", "no_error", 24, True)
    found, rest = find_text_frame(b"\x00\x00" + frame + b"@ST:0")
    assert found == frame and rest == b"@ST:0"


# Captured from a real LW-PX400 (USB bulk IN after 'Q enable'), 2026-11-02.
PX400_FRAME = b"@BDC ST\r\nST:00;ER:00;WR:00;RV:00;TW:03;TR:00;CT:00;OP:00;EI:00;\xff"


def test_px400_hardware_status_frame():
    assert len(PX400_FRAME) == 64
    st = parse_text_frame(PX400_FRAME)
    assert (st.state, st.error, st.tape_width_mm, st.ready) == ("idle", "no_error", 12, True)
    assert st.fields["WR"] == 0 and "BD" not in st.fields


def test_px400_stream_resync():
    stream = PX400_FRAME * 3 + b"@BDC ST\r"   # as read: 3 frames + a partial
    found, rest = find_text_frame(b"\x00@junk" + stream)
    assert found == PX400_FRAME and rest == b"@BDC ST\r"


def test_render_fits_every_tape():
    for mm in LW_PX400.tape_widths_mm:
        img = build_image(LabelSpec(text="Rack A\nPort 12", qr="GBC-0042" if mm >= 12 else None),
                          LW_PX400, mm)
        assert img.height == LW_PX400.dots_for(mm)
        assert len(build_job(img, LW_PX400, JobSettings(tape_mm=mm))) > 100


def test_unsupported_tape():
    with pytest.raises(ValueError):
        LW_PX400.dots_for(36)
