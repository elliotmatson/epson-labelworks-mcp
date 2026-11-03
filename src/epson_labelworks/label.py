"""High-level label description -> bitmap. Shared by preview and print."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from PIL import Image

from . import render as R
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

    def validate(self) -> None:
        if not (self.text or self.qr or self.image_base64 or self.test_pattern):
            raise ValueError("give at least one of text, qr, image_base64")


def build_image(spec: LabelSpec, profile: Profile, tape_mm: int) -> Image.Image:
    spec.validate()
    h = profile.dots_for(tape_mm)
    gap = profile.mm_to_dots(2)
    if spec.test_pattern:
        return R.test_pattern(h)
    parts: list[Image.Image] = []
    if spec.image_base64:
        parts.append(R.render_image(base64.b64decode(spec.image_base64), h, invert=False))
    if spec.qr:
        parts.append(R.render_qr(spec.qr, h))
    if spec.text:
        parts.append(R.render_text(spec.text.replace("\\n", "\n"), h, spec.font_size,
                                   spec.bold, spec.align))
    img = R.hstack(parts, gap, h)
    if spec.min_length_mm:
        img = R.pad_length(img, profile.mm_to_dots(spec.min_length_mm), spec.align)
    if spec.invert:
        from PIL import ImageOps
        img = ImageOps.invert(img.convert("L"))
    return img
