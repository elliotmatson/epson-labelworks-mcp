import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from epson_labelworks import barcodes as BC
from epson_labelworks import fonts as F
from epson_labelworks import symbols as SY
from epson_labelworks.layout import build, design_from_simple
from epson_labelworks.printer import JobSettings, build_job
from epson_labelworks.profiles import LW_PX400 as P
from epson_labelworks.variables import expand, placeholders, substitute


def render(tape=12, **params):
    img, margin = build(design_from_simple(params), P, tape)
    assert img.height == P.dots_for(tape)
    assert len(build_job(img, P, JobSettings(tape_mm=tape, margin_mm=P.dots_to_mm(margin)))) > 50
    return img


def ink(img):
    return Image.eval(img, lambda v: 255 - v).getbbox()


def length_mm(img, margin_mm=2.0):
    return P.dots_to_mm(img.width) + 2 * margin_mm


@pytest.mark.parametrize("tape", P.tape_widths_mm)
def test_text_and_frames_every_tape(tape):
    for frame in ("none", "line", "rounded", "double", "dashed", "hazard", "arrow", "notch",
                  "bracket", "corners", "shadow", "dotted", "thick"):
        if tape <= 6 and frame in ("double", "hazard"):
            continue
        render(tape, text="Rack A", frame=frame)


def test_styles_and_markup():
    plain = render(text="Label")
    for kw in ({"bold": True}, {"italic": True}, {"underline": True}, {"strikethrough": True},
               {"effect": "outline"}, {"effect": "shadow"}, {"letter_spacing": 6},
               {"font": "courier"}, {"font": "serif"}, {"font": "narrow"}, {"font": "script"}):
        render(text="Label", **kw)
    assert render(text="Label", width_pct=50).width < plain.width
    assert render(text="Label", width_pct=200).width > plain.width
    render(text="[[lan]] **Rack** *A* __12__ ~~x~~ ⚠", markup=True)


def test_every_symbology():
    for sym in BC.SYMBOLOGIES:
        data = {"ean13": "590123412345", "ean8": "9638507", "upca": "01234567890",
                "itf": "12345678", "ean14": "1234567890123", "gs1_128": "(01)09501101530003",
                "codabar": "40156"}.get(sym, "GBC-0042")
        render(18, barcode=data, barcode_type=sym)
    with pytest.raises(ValueError):
        render(barcode="ABC", barcode_type="ean13")


def test_linear_modules_are_whole_dots():
    img = BC.render_linear("code128", "GBC-0042", 72, module=2, show_text=False)
    modules, _ = BC.linear_modules("code128", "GBC-0042")
    assert img.width == (len(modules) + 20) * 2


def test_blocks_table_and_cells():
    img = render(cells=[str(i) for i in range(1, 7)], cell_length_mm=12.5, margin_mm=2)
    assert abs(P.dots_to_mm(img.width) - 6 * 12.5) < 1
    render(24, blocks=[{"type": "table", "rows": [["Circuit", "Panel"], ["AV-3", "LP-2B"]],
                        "invert_first_row": True},
                       {"type": "divider", "style": "double"},
                       {"type": "symbol", "name": "flash-alert"},
                       {"type": "spacer", "length_mm": 5},
                       {"type": "text", "lines": [{"text": "A", "bold": True, "scale": 1.5},
                                                  {"text": "b", "italic": True}]}])


def test_fixed_length_shrinks_text():
    img = render(text="A fairly long line of text", length_mm=40)
    assert abs(length_mm(img) - 40) < 0.3
    with pytest.raises(ValueError):
        render(qr="x" * 40, text="t", length_mm=5)


def test_min_length_and_directions():
    assert length_mm(render(text="X", min_length_mm=50)) == pytest.approx(50, abs=0.3)
    render(24, text="EXIT", direction="vertical")
    render(text="SDI 1\nCAM 2", direction="rotated_left")
    render(text="SDI 1", direction="rotated_right")


def test_cable_layouts():
    wrap, _ = build(design_from_simple({"text": "SDI 12", "layout": "cable_wrap",
                                        "cable_diameter_mm": 6}), P, 12)
    assert P.dots_to_mm(wrap.width) + 2 == pytest.approx(3.14159 * 6, abs=0.5)
    flag = render(text="Stage L", layout="cable_flag", cable_diameter_mm=6, flag_length_mm=25)
    left = flag.crop((0, 0, flag.width // 2, flag.height))
    right = flag.crop((flag.width - flag.width // 2, 0, flag.width, flag.height)).rotate(180)
    assert ink(left) and ink(right)
    with pytest.raises(ValueError):
        render(text="x", layout="cable_wrap")


def test_invert_and_mirror():
    a = render(text="AB")
    b = render(text="AB", mirror=True)
    assert a.transpose(Image.FLIP_LEFT_RIGHT).tobytes() == b.tobytes()
    inv = render(text="AB", invert=True)
    assert inv.getpixel((0, 0)) == 0


def test_image_block():
    src = Image.new("RGB", (40, 20), "white")
    for x in range(10, 30):
        for y in range(5, 15):
            src.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    render(image_base64=b64, text="logo")
    render(blocks=[{"type": "image", "image_base64": b64, "dither": False, "size_pct": 50}])


def test_fonts_and_symbols():
    assert F.find_family("sans")
    assert F.list_fonts()
    with pytest.raises(ValueError):
        F.find_family("definitely-not-a-font")
    for short, target in SY.SHORTCUTS.items():
        SY.lookup(short)  # every shortcut points at a real icon
    assert SY.search("ethernet")[0]["name"] == "ethernet"
    with pytest.raises(ValueError):
        SY.lookup("not-an-icon-zz")


def test_variables():
    assert substitute("Port {n:02} {missing} {{x}}", {"n": 3}) == "Port 03 {missing} {{x}}"
    out = expand({"blocks": [{"type": "text", "text": "{room}-{n:A}"}]},
                 records=[{"room": "B1"}, {"room": "B2"}], serial={"start": 1})
    assert [o["blocks"][0]["text"] for o in out] == ["B1-A", "B2-B"]
    out = expand({"text": "{n}"}, serial={"start": 8, "step": 2, "count": 3, "digits": 3})
    assert [o["text"] for o in out] == ["008", "010", "012"]
    assert placeholders({"text": "{a} {b:02}", "image_base64": "{c}"}) == ["a", "b"]
    with pytest.raises(ValueError):
        expand({"text": "x"}, serial={"count": 1000})


def test_server_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("LW_TRANSPORT", "file")
    monkeypatch.setenv("LW_DATA_DIR", str(tmp_path))
    from epson_labelworks import server as S

    monkeypatch.setattr(S, "DRY_RUN_DIR", str(tmp_path / "jobs"))

    async def go():
        names = {t.name for t in await S.mcp.list_tools()}
        assert {"preview_label", "print_label", "save_label", "list_fonts", "search_symbols",
                "list_saved_labels", "delete_saved_label"} <= names
        r = await S.mcp.call_tool("preview_label", {"text": "Port {n}", "serial": {"count": 3}})
        assert r.content[0].type == "image"
        assert json.loads(r.content[1].text)["labels"] == 3
        r = await S.mcp.call_tool("save_label", {"name": "Tag", "text": "ID {id}", "frame": "line"})
        assert r.content[0].type == "image"
        r = await S.mcp.call_tool("print_label", {"saved": "tag", "records": [{"id": "1"}, {"id": "2"}],
                                                  "bold": True, "dry_run": True, "cut": "chain"})
        info = json.loads(r.content[1].text)
        assert info["printed"] == 2
        r = await S.mcp.call_tool("preview_label", {"text": "x", "font": "nope"})
        assert "unknown font" in r.content[0].text
        r = await S.mcp.call_tool("delete_saved_label", {"name": "Tag"})
        assert json.loads(r.content[0].text)["deleted"] is True

    asyncio.run(go())
