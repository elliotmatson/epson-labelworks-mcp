"""MCP server exposing an Epson LabelWorks printer (LW-PX400 first)."""

from __future__ import annotations

import io
import logging
import os
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .label import LabelSpec, build_image
from .printer import JobSettings, LabelPrinter, PrinterError, probe
from .profiles import get_profile
from .render import preview_png
from .transport import FileTransport, TransportError, from_env

log = logging.getLogger("epson_labelworks")

PROFILE = get_profile(os.environ.get("LW_PROFILE", "lw-px400"))
ALLOW_UNVERIFIED = os.environ.get("LW_ALLOW_UNVERIFIED", "0").lower() in ("1", "true", "yes")
DRY_RUN_DIR = os.environ.get("LW_DRY_RUN_DIR", "./jobs")
DEFAULT_TAPE = int(os.environ.get("LW_DEFAULT_TAPE_MM", "12"))
_lock = anyio.Lock()

mcp = MCPServer(
    name="labelworks",
    title="Epson LabelWorks label printer",
    version=__version__,
    instructions=(
        f"Prints labels on an Epson {PROFILE.model} ({PROFILE.dpi} dpi, tapes "
        f"{', '.join(map(str, PROFILE.tape_widths_mm))} mm). Call preview_label first and show "
        "the image to the user before print_label unless they have already approved the exact "
        "content. Leave tape_mm unset to use the loaded cartridge's width. Each print consumes "
        "tape and cannot be undone."
    ),
)

TapeMM = Annotated[int | None, Field(description="Tape width in mm. Omit to auto-detect from the loaded cartridge.")]
Text = Annotated[str | None, Field(description="Label text. Use \\n for multiple lines; font auto-fits the tape.")]
QR = Annotated[str | None, Field(description="Data for a QR code placed left of the text (URL, asset ID…).")]
ImageB64 = Annotated[str | None, Field(description="Base64 PNG/JPEG, scaled to the tape height and dithered.")]
FontSize = Annotated[int | None, Field(description="Font size in dots (180 dpi). Omit to fit the tape.", ge=6, le=400)]
MinLen = Annotated[float | None, Field(description="Minimum printed content length in mm (pads around content).", ge=0, le=500)]
Align = Literal["left", "center", "right"]


def _spec(text, qr, image_base64, font_size, bold, align, min_length_mm, invert) -> LabelSpec:
    return LabelSpec(text=text, qr=qr, image_base64=image_base64, font_size=font_size,
                     bold=bold, align=align, min_length_mm=min_length_mm, invert=invert)


def _printer(dry_run: bool) -> LabelPrinter:
    t = FileTransport(DRY_RUN_DIR) if dry_run else from_env()
    return LabelPrinter(t, PROFILE)


async def _detect_tape(tape_mm: int | None) -> tuple[int, str]:
    if tape_mm:
        return tape_mm, "requested"
    t = from_env()
    if isinstance(t, FileTransport):
        return DEFAULT_TAPE, "default (dry-run transport)"
    try:
        async with _lock:
            st = await anyio.to_thread.run_sync(LabelPrinter(t, PROFILE).status)
    except (TransportError, OSError) as e:
        log.warning("tape auto-detect failed: %s", e)
        return DEFAULT_TAPE, f"default (printer unreachable: {e})"
    if st and st.tape_width_mm in PROFILE.tape_widths_mm:
        return st.tape_width_mm, "detected"
    return DEFAULT_TAPE, "default (tape not reported)"


def _render(spec: LabelSpec, tape_mm: int):
    img = build_image(spec, PROFILE, tape_mm)
    margin = PROFILE.mm_to_dots(PROFILE.default_margin_mm)
    png = preview_png(img, PROFILE, tape_mm, margin)
    length_mm = round(PROFILE.dots_to_mm(img.width + 2 * margin), 1)
    return img, png, length_mm


READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
PRINT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


@mcp.tool(title="Printer info", annotations=READ)
def printer_info() -> dict:
    """Model profile, transport configuration and whether real printing is enabled."""
    return {
        "model": PROFILE.model,
        "profile": PROFILE.key,
        "capability_level": PROFILE.capability_level,
        "dpi": PROFILE.dpi,
        "tape_widths_mm": list(PROFILE.tape_widths_mm),
        "printable_dots": PROFILE.printable_dots,
        "hardware_verified": PROFILE.verified,
        "printing_enabled": PROFILE.verified or ALLOW_UNVERIFIED,
        "transport": from_env().describe(),
        "notes": list(PROFILE.notes),
    }


@mcp.tool(title="Printer status", annotations=READ)
async def printer_status() -> dict:
    """Current state, error and loaded tape width as reported by the printer."""
    try:
        async with _lock:
            st = await anyio.to_thread.run_sync(_printer(False).status)
    except (TransportError, OSError) as e:
        return {"reachable": False, "error": str(e)}
    if st is None:
        return {"reachable": True, "status": None, "note": "transport has no status channel"}
    return {"reachable": True, **st.to_dict()}


@mcp.tool(title="Probe printer (read-only)", annotations=READ)
async def probe_printer() -> dict:
    """Identify the printer without printing: USB descriptors / IEEE-1284 ID and raw status frame."""
    async with _lock:
        return await anyio.to_thread.run_sync(probe, from_env())


@mcp.tool(title="Preview label", annotations=READ, structured_output=False)
async def preview_label(text: Text = None, qr: QR = None, image_base64: ImageB64 = None,
                        tape_mm: TapeMM = None, font_size: FontSize = None, bold: bool = False,
                        align: Align = "center", min_length_mm: MinLen = None,
                        invert: bool = False) -> list:
    """Render the label exactly as it would print and return a PNG preview. Prints nothing."""
    tape, how = await _detect_tape(tape_mm)
    try:
        _, png, length = _render(_spec(text, qr, image_base64, font_size, bold, align, min_length_mm, invert), tape)
    except ValueError as e:
        return [f"Cannot render: {e}"]
    return [Image(data=png, format="png"),
            {"tape_mm": tape, "tape_source": how, "length_mm": length}]


@mcp.tool(title="Print label", annotations=PRINT, structured_output=False)
async def print_label(text: Text = None, qr: QR = None, image_base64: ImageB64 = None,
                      tape_mm: TapeMM = None, font_size: FontSize = None, bold: bool = False,
                      align: Align = "center", min_length_mm: MinLen = None, invert: bool = False,
                      copies: Annotated[int, Field(ge=1, le=50)] = 1,
                      cut: Annotated[Literal["cut", "no_cut"], Field(description="Full-cut after each label.")] = "cut",
                      density: Annotated[int, Field(ge=-3, le=3)] = 0,
                      dry_run: Annotated[bool, Field(description="Write the job bytes to a file instead of printing.")] = False) -> list:
    """Print a label (text, QR code and/or image). Consumes tape; cannot be undone."""
    if not dry_run and not (PROFILE.verified or ALLOW_UNVERIFIED):
        return [f"Printing is disabled: the {PROFILE.model} profile is not hardware-verified. "
                "Run probe_printer and a dry_run first, then set LW_ALLOW_UNVERIFIED=1 on the server."]
    tape, how = await _detect_tape(tape_mm)
    try:
        img, png, length = _render(_spec(text, qr, image_base64, font_size, bold, align, min_length_mm, invert), tape)
    except ValueError as e:
        return [f"Cannot render: {e}"]
    settings = JobSettings(tape_mm=tape, cut="job" if cut == "cut" else "none", density=density)
    results = []
    try:
        for _ in range(copies):
            async with _lock:
                results.append(await anyio.to_thread.run_sync(
                    lambda: _printer(dry_run).print_image(img, settings)))
    except (PrinterError, TransportError, OSError) as e:
        return [Image(data=png, format="png"),
                {"printed": len(results), "requested": copies, "error": str(e)}]
    return [Image(data=png, format="png"),
            {"printed": len(results), "dry_run": dry_run, "tape_mm": tape, "tape_source": how,
             "length_mm": length, "jobs": results}]


@mcp.tool(title="Print calibration pattern", annotations=PRINT, structured_output=False)
async def print_test_pattern(tape_mm: TapeMM = None,
                             dry_run: Annotated[bool, Field(description="Write bytes to a file instead of printing.")] = False) -> list:
    """Print a wedge that shows how many head dots print and which tape edge is row 0.

    Measure the wedge on the printed label: its longest step is the real printable
    height. Use this when bringing up a new model or tape width.
    """
    if not dry_run and not (PROFILE.verified or ALLOW_UNVERIFIED):
        return ["Printing is disabled until LW_ALLOW_UNVERIFIED=1 is set on the server."]
    tape, how = await _detect_tape(tape_mm)
    spec = LabelSpec(test_pattern=True)
    img, png, length = _render(spec, tape)
    async with _lock:
        try:
            res = await anyio.to_thread.run_sync(
                lambda: _printer(dry_run).print_image(img, JobSettings(tape_mm=tape)))
        except (PrinterError, TransportError, OSError) as e:
            return [Image(data=png, format="png"), {"error": str(e)}]
    return [Image(data=png, format="png"),
            {"tape_mm": tape, "tape_source": how, "dots_sent": PROFILE.dots_for(tape), **res}]


# --- HTTP entrypoint ---------------------------------------------------------

class BearerAuth:
    """Optional static bearer token (LW_MCP_TOKEN). /healthz is always open."""

    def __init__(self, app, token: str | None):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") == "/healthz":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"ok"})
                return
            if self.token:
                headers = dict(scope.get("headers") or [])
                if headers.get(b"authorization", b"") != f"Bearer {self.token}".encode():
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain"),
                                            (b"www-authenticate", b"Bearer")]})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
        await self.app(scope, receive, send)


def http_app():
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in os.environ.get("LW_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)
                if hosts else TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app = mcp.streamable_http_app(transport_security=security, stateless_http=True,
                                  host=os.environ.get("LW_HOST", "0.0.0.0"))
    return BearerAuth(app, os.environ.get("LW_MCP_TOKEN") or None)


def serve(transport: str = "http") -> None:
    logging.basicConfig(level=os.environ.get("LW_LOG_LEVEL", "INFO"))
    if transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn

    uvicorn.run(http_app(), host=os.environ.get("LW_HOST", "0.0.0.0"),
                port=int(os.environ.get("LW_PORT", "8765")), log_level="info")


def _png_bytes(img) -> bytes:  # used by the CLI
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
