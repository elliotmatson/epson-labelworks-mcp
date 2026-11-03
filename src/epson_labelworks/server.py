"""MCP server exposing an Epson LabelWorks printer (LW-PX400 first)."""

from __future__ import annotations

import hmac
import io
import logging
import os
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from pydantic import ValidationError

from . import __version__, fonts, store, symbols
from .barcodes import SYMBOLOGIES
from .layout import build, design_from_simple
from .model import (Align, Block, Direction, Effect, FrameStyle, LabelDesign, Serial, Symbology,
                    VAlign)
from .printer import JobSettings, LabelPrinter, PrinterError, probe
from .profiles import get_profile
from .render import preview_png, test_pattern
from .transport import FileTransport, TransportError, from_env
from .variables import expand, placeholders

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
        "tape and cannot be undone. Designs support fonts (list_fonts), symbols (search_symbols), "
        "1D/2D barcodes, frames, dividers, tables and patch-panel cells, cable wraps and flags, "
        "serial numbers and batches from records, and saved templates (save_label)."
    ),
)

TapeMM = Annotated[int | None, Field(description="Tape width in mm. Omit to auto-detect from the loaded cartridge.")]
Opt = None  # every design parameter defaults to "unset" so saved labels can be overridden


def _p(annotation, description: str):
    return Annotated[annotation | None, Field(description=description)]


# Flat design parameters shared by preview_label, print_label and save_label.
DESIGN_PARAMS: list[tuple[str, object]] = [
    ("text", _p(str, "Label text. \\n starts a new line. Placeholders like {n}, {date} or {field} "
                     "are filled from serial/records.")),
    ("qr", _p(str, "QR code data (URL, asset ID...), placed left of the text.")),
    ("barcode", _p(str, "Barcode data, placed right of the text.")),
    ("barcode_type", _p(Symbology, "Barcode symbology (default code128).")),
    ("barcode_text", _p(bool, "Print human-readable digits under 1D barcodes (default true).")),
    ("symbol", _p(str, "Icon placed at the left: a Material Design icon name (search_symbols), a "
                       "shortcut such as warning, high-voltage, network, fire-extinguisher, or a "
                       "Unicode character like ⚠.")),
    ("image_base64", _p(str, "Base64 PNG/JPEG placed at the left, scaled to the tape and dithered.")),
    ("cells", _p(list[str], "Patch-panel / terminal-block row: one cell per port, separated by lines.")),
    ("cell_length_mm", _p(float, "Length of each cell, i.e. the port pitch (e.g. 19.05/24 for a 24-port "
                                  "1U panel ~ 18.4 mm). Omit to fit the text.")),
    ("divider", _p(Literal["none", "solid", "thick", "double", "dashed", "dotted"],
                   "Divider line between the QR/symbol/image and the text.")),
    ("blocks", _p(list[Block], "Advanced layout: an ordered list of blocks along the tape (text with "
                               "per-line styles, symbol, barcode, qr, image, divider, spacer, table). "
                               "When given, the simple content fields above are ignored.")),
    ("font", _p(str, "Font family or alias: sans (default), serif, mono, courier, helvetica, arial, "
                     "times, narrow, sans-condensed, gothic, bookman, schoolbook, palatino, script. "
                     "list_fonts shows every installed family.")),
    ("font_size", _p(int, "Font size in dots (180 dpi; 2.5 dots ~ 1 pt). Omit to auto-fit the tape.")),
    ("bold", _p(bool, "Bold.")),
    ("italic", _p(bool, "Italic.")),
    ("underline", _p(bool, "Underline.")),
    ("strikethrough", _p(bool, "Strike through.")),
    ("effect", _p(Effect, "outline = hollow letters; shadow = hollow letters with a drop shadow.")),
    ("width_pct", _p(int, "Character width %: 50-90 condensed, 110-200 expanded (default 100).")),
    ("letter_spacing", _p(int, "Extra dots between characters.")),
    ("align", _p(Align, "Line alignment (default center).")),
    ("v_align", _p(VAlign, "Vertical alignment across the tape (default center).")),
    ("direction", _p(Direction, "horizontal (default); vertical = characters stacked; "
                                "rotated_left/right = text turned 90 deg to read across the tape.")),
    ("line_spacing", _p(float, "Extra gap between lines as a fraction of line height (default 0.1).")),
    ("markup", _p(bool, "Enable inline markup in text: **bold**, *italic*, __underline__, ~~strike~~, "
                        "[[icon-name]] for an inline symbol.")),
    ("frame", _p(FrameStyle, "Border around the label.")),
    ("length_mm", _p(float, "Fixed total label length incl. margins; text shrinks to fit.")),
    ("min_length_mm", _p(float, "Minimum total label length; content is padded.")),
    ("margin_mm", _p(float, "Blank tape before and after the content (default 2; lower saves tape).")),
    ("gap_mm", _p(float, "Space between blocks (default 2).")),
    ("distribute", _p(Literal["start", "center", "end", "spread"],
                      "How blocks sit inside a fixed length (default center).")),
    ("invert", _p(bool, "White on black.")),
    ("mirror", _p(bool, "Mirror image (for clear tape stuck on from behind glass).")),
    ("layout", _p(Literal["standard", "cable_wrap", "cable_flag"],
                  "cable_wrap: text turned and repeated around a cable of cable_diameter_mm; "
                  "cable_flag: two flags joined by a blank wrap section that goes around the cable.")),
    ("cable_diameter_mm", _p(float, "Cable diameter for cable_wrap / cable_flag.")),
    ("flag_length_mm", _p(float, "Length of each flag for cable_flag (default: fit the content).")),
]
BATCH_PARAMS: list[tuple[str, object]] = [
    ("records", _p(list[dict[str, str]], "Batch: one label per record; {field} placeholders are "
                                          "replaced with the record's values.")),
    ("serial", _p(Serial, "Batch numbering: {n} counts from start by step, count labels (with no "
                          "records), zero-padded to digits. {n:A} gives letters.")),
    ("saved", _p(str, "Name of a saved label to use as the design; any other design parameter "
                      "given overrides it.")),
]
DESIGN_KEYS = [k for k, _ in DESIGN_PARAMS]


def _signature(params, defaults=None):
    import inspect

    defaults = defaults or {}
    return inspect.Signature([
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann,
                          default=defaults.get(name, None)) for name, ann in params])


def tool_with(params, **tool_kw):
    """Register fn(**kwargs) as a tool whose schema is built from `params`."""
    def deco(fn):
        fn.__signature__ = _signature(params)
        fn.__annotations__ = {n: a for n, a in params}
        fn.__annotations__["return"] = list
        return mcp.tool(**tool_kw)(fn)
    return deco


def _design_dicts(kw: dict) -> list[dict]:
    """Merge saved label + overrides, then expand serial/records into one dict per label."""
    params: dict = {}
    serial = kw.get("serial")
    records = kw.get("records")
    if kw.get("saved"):
        entry = store.get(kw["saved"])
        params.update(entry["params"])
        serial = serial or entry["params"].get("serial")
        records = records or entry["params"].get("records")
    params.update({k: kw[k] for k in DESIGN_KEYS if kw.get(k) is not None})
    params.pop("serial", None)
    params.pop("records", None)
    if hasattr(serial, "model_dump"):
        serial = serial.model_dump()
    design = design_from_simple(_jsonable(params)).model_dump(mode="json")
    return expand(design, records, serial)


def _jsonable(params: dict) -> dict:
    return {k: ([b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b
                 for b in v] if isinstance(v, list) else v) for k, v in params.items()}


def _render_all(kw: dict, tape_mm: int) -> list[tuple[object, int, LabelDesign]]:
    out = []
    for i, dd in enumerate(_design_dicts(kw)):
        design = LabelDesign.model_validate(dd)
        try:
            img, margin = build(design, PROFILE, tape_mm)
        except ValueError as e:
            raise ValueError(f"label {i + 1}: {e}" if i else str(e)) from None
        out.append((img, margin, design))
    return out


def _sheet(rendered, tape_mm: int, limit: int = 8) -> tuple[bytes, list[float]]:
    from PIL import Image as PILImage

    pngs = [preview_png(img, PROFILE, tape_mm, m) for img, m, _ in rendered[:limit]]
    lengths = [round(PROFILE.dots_to_mm(img.width + 2 * m), 1) for img, m, _ in rendered]
    if len(pngs) == 1:
        return pngs[0], lengths
    ims = [PILImage.open(io.BytesIO(p)) for p in pngs]
    gap = 12
    sheet = PILImage.new("RGB", (max(i.width for i in ims), sum(i.height for i in ims) + gap * (len(ims) - 1)),
                         (255, 255, 255))
    y = 0
    for im in ims:
        sheet.paste(im, (0, y))
        y += im.height + gap
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue(), lengths


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


READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
PRINT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


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
        "barcode_types": list(SYMBOLOGIES),
        "frames": list(FrameStyle.__args__),
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


@mcp.tool(title="List fonts", annotations=READ)
def list_fonts() -> dict:
    """Installed font families with their styles and the friendly aliases that map to them."""
    return {"default": fonts.find_family(None).name, "aliases": sorted(fonts.ALIASES),
            "families": fonts.list_fonts()}


@mcp.tool(title="Search symbols", annotations=READ)
def search_symbols(query: Annotated[str, Field(description="Words such as 'network', 'fire', 'arrow left'.")],
                   limit: Annotated[int, Field(ge=1, le=100)] = 30) -> dict:
    """Find icon names (7,000+ Material Design Icons) for the symbol parameter, symbol blocks and [[icon]] markup."""
    return {"results": symbols.search(query, limit),
            "shortcuts": sorted(symbols.SHORTCUTS) if not query.strip() else None}


@tool_with(DESIGN_PARAMS + BATCH_PARAMS + [("tape_mm", TapeMM)],
           title="Preview label", annotations=READ, structured_output=False)
async def preview_label(**kw) -> list:
    """Render the label(s) exactly as they would print and return a PNG. Prints nothing.

    Simple labels: text, qr, barcode, symbol, image, cells. Styling: font, size, bold/italic/
    underline, effect, width, frame, divider, invert, mirror. Layouts: fixed length, cable wrap,
    cable flag, patch panel (cells), tables and multi-block designs (blocks). Batches: serial
    numbering or records with {placeholders}; the preview shows up to 8 labels.
    """
    tape, how = await _detect_tape(kw.get("tape_mm"))
    try:
        rendered = _render_all(kw, tape)
        png, lengths = _sheet(rendered, tape)
    except (ValueError, ValidationError) as e:
        return [f"Cannot render: {e}"]
    info = {"tape_mm": tape, "tape_source": how, "labels": len(rendered),
            "length_mm": lengths[0] if len(lengths) == 1 else lengths}
    if len(rendered) > 8:
        info["note"] = f"preview shows the first 8 of {len(rendered)} labels"
    return [Image(data=png, format="png"), info]


PRINT_PARAMS = [
    ("copies", Annotated[int, Field(ge=1, le=50, description="Copies of each label.")]),
    ("cut", Annotated[Literal["each", "chain", "none"],
                      Field(description="each = cut after every label; chain = labels joined, one cut "
                                        "at the end; none = no cut.")]),
    ("density", Annotated[int, Field(ge=-3, le=3, description="Print darkness.")]),
    ("speed", Annotated[Literal["high", "slow"], Field(description="slow can help on heat-shrink tube "
                                                                   "and dense images.")]),
    ("dry_run", Annotated[bool, Field(description="Write the job bytes to a file instead of printing.")]),
]
PRINT_DEFAULTS = {"copies": 1, "cut": "each", "density": 0, "speed": "high", "dry_run": False}


def _print_tool(fn):
    import inspect

    params = DESIGN_PARAMS + BATCH_PARAMS + [("tape_mm", TapeMM)] + PRINT_PARAMS
    fn.__signature__ = inspect.Signature(
        [inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, annotation=a,
                           default=PRINT_DEFAULTS.get(n)) for n, a in params])
    fn.__annotations__ = {n: a for n, a in params} | {"return": list}
    return mcp.tool(title="Print label", annotations=PRINT, structured_output=False)(fn)


@_print_tool
async def print_label(**kw) -> list:
    """Print the label(s). Takes the same design parameters as preview_label. Consumes tape; cannot be undone."""
    kw = {**PRINT_DEFAULTS, **{k: v for k, v in kw.items() if v is not None}}
    dry_run = kw["dry_run"]
    if not dry_run and not (PROFILE.verified or ALLOW_UNVERIFIED):
        return [f"Printing is disabled: the {PROFILE.model} profile is not hardware-verified. "
                "Run probe_printer and a dry_run first, then set LW_ALLOW_UNVERIFIED=1 on the server."]
    tape, how = await _detect_tape(kw.get("tape_mm"))
    try:
        rendered = _render_all(kw, tape)
        png, lengths = _sheet(rendered, tape)
    except (ValueError, ValidationError) as e:
        return [f"Cannot render: {e}"]
    jobs = [(img, m) for img, m, _ in rendered for _ in range(kw["copies"])]
    results = []
    try:
        for i, (img, margin) in enumerate(jobs):
            last = i == len(jobs) - 1
            cut = {"each": "job", "none": "none"}.get(kw["cut"], "job" if last else "none")
            settings = JobSettings(tape_mm=tape, cut=cut, density=kw["density"],
                                   speed=1 if kw["speed"] == "high" else 0,
                                   margin_mm=PROFILE.dots_to_mm(margin))
            async with _lock:
                results.append(await anyio.to_thread.run_sync(
                    lambda: _printer(dry_run).print_image(img, settings)))
    except (PrinterError, TransportError, OSError) as e:
        return [Image(data=png, format="png"),
                {"printed": len(results), "requested": len(jobs), "error": str(e)}]
    return [Image(data=png, format="png"),
            {"printed": len(results), "dry_run": dry_run, "tape_mm": tape, "tape_source": how,
             "length_mm": lengths[0] if len(lengths) == 1 else lengths,
             "jobs": results if len(results) <= 5 else results[-1:]}]


SAVE_PARAMS = [("name", Annotated[str, Field(description="Name to save under (overwrites).")]),
               ("description", _p(str, "What the label is for."))] + DESIGN_PARAMS + BATCH_PARAMS[:2]


@tool_with(SAVE_PARAMS, title="Save label design", annotations=WRITE, structured_output=False)
async def save_label(**kw) -> list:
    """Save a label design (and optional serial/records defaults) by name for reuse.

    Use {placeholders} in the text to make a template; fill them later with records or serial in
    preview_label / print_label via the `saved` parameter. Returns a preview.
    """
    name = kw.pop("name")
    description = kw.pop("description", None)
    params = {k: v for k, v in kw.items() if v is not None}
    if hasattr(params.get("serial"), "model_dump"):
        params["serial"] = params["serial"].model_dump()
    params = _jsonable(params)
    try:
        design_from_simple({k: v for k, v in params.items() if k in DESIGN_KEYS})
    except (ValueError, ValidationError) as e:
        return [f"Not saved: {e}"]
    entry = store.save(name, params, description)
    result: list = [{"saved": entry["name"], "placeholders": placeholders(params)}]
    try:
        tape, _ = await _detect_tape(None)
        png, _ = _sheet(_render_all({"saved": name}, tape), tape, limit=1)
        result.insert(0, Image(data=png, format="png"))
    except (ValueError, ValidationError) as e:
        result.append(f"Saved, but the preview failed: {e}")
    return result


@mcp.tool(title="List saved labels", annotations=READ)
def list_saved_labels() -> dict:
    """Saved label designs with their descriptions, placeholders and parameters."""
    out = []
    for e in store.list_all():
        params = {k: (v[:40] + "…" if k == "image_base64" and isinstance(v, str) else v)
                  for k, v in e["params"].items()}
        out.append({"name": e["name"], "description": e.get("description", ""),
                    "saved_at": e.get("saved_at"), "placeholders": placeholders(e["params"]),
                    "params": params})
    return {"labels": out}


@mcp.tool(title="Delete saved label", annotations=ToolAnnotations(destructive_hint=True, read_only_hint=False))
def delete_saved_label(name: str) -> dict:
    """Delete a saved label design."""
    return {"deleted": store.delete(name), "name": name}


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
    img = test_pattern(PROFILE.dots_for(tape))
    margin = PROFILE.mm_to_dots(PROFILE.default_margin_mm)
    png = preview_png(img, PROFILE, tape, margin)
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

    def __init__(self, app, token: str | None, header: str | None = None):
        self.app, self.token = app, token
        self.header = (header or os.environ.get("LW_AUTH_HEADER") or "x-api-key").lower().encode()

    # A static token is not OAuth. Answering 401 + WWW-Authenticate (especially on
    # the /.well-known discovery paths MCP clients probe first) makes gateways such
    # as Obot conclude the server needs an OAuth login, which then propagates to
    # every connector the gateway serves. So: discovery paths get a plain 404 and a
    # missing/wrong token gets 403 with no auth challenge.

    @staticmethod
    async def _plain(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/healthz":
                return await self._plain(send, 200, b"ok")
            if path.startswith("/.well-known/"):
                return await self._plain(send, 404, b"not found")
            if self.token and not self._authorized(scope):
                names = sorted(k.decode() for k, _ in scope.get("headers") or []
                               if k.lower() in (b"authorization", self.header))
                log.warning("rejected %s %s: no valid token (auth headers present: %s)",
                            scope.get("method"), path, ", ".join(names) or "none")
                return await self._plain(send, 403, b"forbidden: missing or invalid token")
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        """Token in `Authorization: Bearer <t>` or in the LW_AUTH_HEADER header.

        Gateways like Obot may replace the Authorization header with their own
        credentials, so a dedicated header (default X-API-Key) is also accepted.
        """
        want = self.token.encode()
        for k, v in scope.get("headers") or []:
            k = k.lower()
            if k == b"authorization" and hmac.compare_digest(v, b"Bearer " + want):
                return True
            if k == self.header and hmac.compare_digest(v.strip(), want):
                return True
        return False


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
