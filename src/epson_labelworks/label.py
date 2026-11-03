"""Backwards-compatible simple label description (used by the CLI and tests).

The full model lives in model.py / layout.py; this maps the original flat fields onto it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from . import render as R
from .layout import build, design_from_simple
from .profiles import Profile


@dataclass
class LabelSpec:
    text: str | None = None
    qr: str | None = None
    image_base64: str | None = None
    font_size: int | None = None
    bold: bool = False
    align: str = "center"
    min_length_mm: float | None = None
    invert: bool = False
    test_pattern: bool = False
    extra: dict = field(default_factory=dict)  # any other design parameter (font, frame, blocks...)

    def validate(self) -> None:
        if not (self.text or self.qr or self.image_base64 or self.test_pattern
                or self.extra.get("blocks") or self.extra.get("barcode")
                or self.extra.get("symbol") or self.extra.get("cells")):
            raise ValueError("give at least one of text, qr, image_base64")

    def params(self) -> dict:
        return {"text": self.text, "qr": self.qr, "image_base64": self.image_base64,
                "font_size": self.font_size, "bold": self.bold or None, "align": self.align,
                "min_length_mm": self.min_length_mm, "invert": self.invert or None, **self.extra}


def build_image(spec: LabelSpec, profile: Profile, tape_mm: int) -> Image.Image:
    spec.validate()
    if spec.test_pattern:
        return R.test_pattern(profile.dots_for(tape_mm))
    img, _ = build(design_from_simple(spec.params()), profile, tape_mm)
    return img
