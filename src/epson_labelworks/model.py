"""Label description: the data model shared by preview, print, batch and saved labels.

Geometry: the label runs along the tape (its *length*); the tape's printable
height is fixed by the cartridge. A label is a row of *blocks* laid out left to
right along the tape, optionally inside a frame.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .barcodes import LINEAR

Align = Literal["left", "center", "right"]
VAlign = Literal["top", "center", "bottom"]
Effect = Literal["none", "outline", "shadow", "outline_shadow"]
Direction = Literal["horizontal", "vertical", "rotated_left", "rotated_right"]
FrameStyle = Literal["none", "line", "thick", "double", "rounded", "dashed", "dotted", "bracket",
                     "arrow", "notch", "hazard", "shadow", "corners"]
DividerStyle = Literal["solid", "thick", "double", "dashed", "dotted"]
Linear = Literal[tuple(LINEAR)]  # type: ignore[valid-type]
Symbology = Literal[tuple(LINEAR) + ("qr", "datamatrix", "pdf417")]  # type: ignore[valid-type]
Border = Literal["grid", "outer", "inner", "columns", "rows", "none"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextStyle(_M):
    """Character formatting; unset fields inherit from the label (same meaning as the flat parameters)."""
    font: str | None = None
    font_size: int | None = Field(None, ge=4, le=400)
    scale: float | None = Field(None, gt=0, le=10, description="Relative size among auto-fitted lines.")
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    effect: Effect | None = None
    width_pct: int | None = Field(None, ge=25, le=400)
    letter_spacing: int | None = Field(None, ge=-10, le=100)


class Line(TextStyle):
    text: str
    align: Align | None = None


class TextBlock(TextStyle):
    type: Literal["text"] = "text"
    text: str | None = None
    lines: list[Line] | None = Field(None, description="Per-line text and style, instead of text.")
    align: Align | None = None
    v_align: VAlign | None = None
    direction: Direction | None = None
    line_spacing: float | None = Field(None, ge=0, le=2)
    markup: bool | None = None
    length_mm: float | None = Field(None, gt=0, description="Fixed block length; text shrinks to fit.")
    invert: bool = False


class SymbolBlock(_M):
    type: Literal["symbol"] = "symbol"
    name: str = Field(description="Material Design icon name (see search_symbols), a shortcut like "
                      "warning/high-voltage/network, or a Unicode character such as ⚠.")
    size_pct: int = Field(100, ge=10, le=100)
    length_mm: float | None = Field(None, gt=0)
    invert: bool = False


class BarcodeBlock(_M):
    type: Literal["barcode"] = "barcode"
    data: str
    symbology: Symbology = "code128"
    show_text: bool = Field(True, description="Human-readable digits under 1D barcodes.")
    module_dots: int | None = Field(None, ge=1, le=8, description="Narrow bar width in dots (default 2 "
                                    "= 0.28 mm).")
    size_pct: int = Field(100, ge=10, le=100, description="Height of 2D codes as % of the tape.")
    ecc: Literal["L", "M", "Q", "H"] = Field("M", description="QR error correction.")
    checksum: bool = Field(True, description="Code 39 check character.")
    length_mm: float | None = Field(None, gt=0)
    invert: bool = False


class QRBlock(_M):
    type: Literal["qr"] = "qr"
    data: str
    ecc: Literal["L", "M", "Q", "H"] = "M"
    size_pct: int = Field(100, ge=10, le=100)
    length_mm: float | None = Field(None, gt=0)
    invert: bool = False


class ImageBlock(_M):
    type: Literal["image"] = "image"
    image_base64: str = Field(description="Base64 PNG/JPEG/GIF/BMP.")
    size_pct: int = Field(100, ge=10, le=100)
    dither: bool = Field(True, description="Floyd-Steinberg dithering; false = hard threshold (logos).")
    threshold: int = Field(128, ge=1, le=254)
    length_mm: float | None = Field(None, gt=0)
    invert: bool = False


class DividerBlock(_M):
    type: Literal["divider"] = "divider"
    style: DividerStyle = "solid"
    thickness: int = Field(2, ge=1, le=12, description="Line thickness in dots.")
    coverage_pct: int = Field(90, ge=10, le=100, description="Line height as % of the tape.")
    length_mm: float = Field(2.0, ge=0.3, description="Space the divider takes along the tape.")


class SpacerBlock(_M):
    type: Literal["spacer"] = "spacer"
    length_mm: float = Field(gt=0)


class TableBlock(TextStyle):
    type: Literal["table"] = "table"
    rows: list[list[str]] = Field(description="Cells; rows stack across the tape, columns run "
                                  "along it. One row = patch-panel / terminal-block style.")
    cell_length_mm: float | None = Field(None, gt=0, description="Length of every column (e.g. port "
                                         "pitch). Omit to size columns to their content.")
    column_lengths_mm: list[float] | None = None
    border: Border = "grid"
    border_dots: int = Field(2, ge=1, le=6)
    align: Align = "center"
    invert_first_row: bool = False
    invert_first_column: bool = False


Block = Annotated[Union[TextBlock, SymbolBlock, BarcodeBlock, QRBlock, ImageBlock, DividerBlock,
                        SpacerBlock, TableBlock], Field(discriminator="type")]


class Serial(_M):
    start: int = 1
    step: int = 1
    count: int = Field(1, ge=1, le=200)
    digits: int = Field(0, ge=0, le=12, description="Zero-pad {n} to this many digits.")


class LabelDesign(TextStyle):
    """Everything about a label except the tape and print settings."""
    blocks: list[Block] = Field(default_factory=list)
    align: Align = "center"
    v_align: VAlign = "center"
    direction: Direction = "horizontal"
    line_spacing: float = 0.1
    markup: bool = False
    frame: FrameStyle = "none"
    frame_dots: int | None = Field(None, ge=1, le=8)
    length_mm: float | None = None
    min_length_mm: float | None = None
    margin_mm: float | None = None
    gap_mm: float = 2.0
    distribute: Literal["start", "center", "end", "spread"] = "center"
    invert: bool = False
    mirror: bool = False
    layout: Literal["standard", "cable_wrap", "cable_flag"] = "standard"
    cable_diameter_mm: float | None = None
    flag_length_mm: float | None = None
    wraps: float = 1.0
