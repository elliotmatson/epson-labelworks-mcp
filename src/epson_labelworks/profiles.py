"""Per-model printer profiles.

Only framing and raster encoding are shared across LabelWorks models. Everything
else (command sequence, cutter bytes, meaning of 'T', head width, status format)
is model specific, so it lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Printable head dots per tape width at 180 dpi, from oxplot/nospero (LW-600P
# class). 12 mm = 72 dots is hardware-confirmed on the LW-700; the rest are the
# SDK/implementation values and should be calibrated with print_test_pattern.
DOTS_180DPI = {4: 22, 6: 36, 9: 54, 12: 72, 18: 108, 24: 128}


@dataclass(frozen=True)
class Profile:
    key: str
    model: str
    capability_level: int
    dpi: int
    tape_widths_mm: tuple[int, ...]
    printable_dots: dict[int, int]
    status_protocol: str  # "v1" (textual '@ST:..' over Q) or "v2"
    cut_variant: str = "fast"
    has_half_cut: bool = False
    # Blank feed added before/after content, in mm. T carries the same value.
    default_margin_mm: float = 2.0
    # nospero / ha-epson-labelworks invert the across-tape axis when packing.
    flip_across: bool = True
    verified: bool = False
    usb_model_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def dots_for(self, tape_mm: int) -> int:
        try:
            return self.printable_dots[tape_mm]
        except KeyError:
            raise ValueError(
                f"{self.model} does not support {tape_mm} mm tape "
                f"(supported: {', '.join(map(str, self.tape_widths_mm))})"
            ) from None

    def mm_to_dots(self, mm: float) -> int:
        return round(mm / 25.4 * self.dpi)

    def dots_to_mm(self, dots: int) -> float:
        return dots / self.dpi * 25.4


LW_PX400 = Profile(
    key="lw-px400",
    model="LW-PX400",
    capability_level=3,
    dpi=180,
    tape_widths_mm=(4, 6, 9, 12, 18, 24),
    printable_dots=dict(DOTS_180DPI),
    status_protocol="v1",
    cut_variant="fast",
    has_half_cut=False,
    verified=False,
    usb_model_names=("LW-PX400", "LW-PX400 "),
    notes=(
        "Epson's Android SDK (LWPrintProductInformation) lists the LW-PX400 as "
        "capability level 3, 180 dpi, tape kinds 4-24 mm, no half cut, 8-byte status.",
        "Level 3 = level-1 job plus 'H 05' and 's <speed>' page commands.",
        "Not yet hardware-verified: run probe_printer and print_test_pattern first.",
    ),
)

LW_600P = Profile(
    key="lw-600p",
    model="LW-600P",
    capability_level=1,
    dpi=180,
    tape_widths_mm=(6, 9, 12, 18, 24),
    printable_dots={k: v for k, v in DOTS_180DPI.items() if k != 4},
    status_protocol="v1",
    verified=False,
    usb_model_names=("LW-600P",),
    notes=("Corroborated by nospero and ha-epson-labelworks.",),
)

PROFILES: dict[str, Profile] = {p.key: p for p in (LW_PX400, LW_600P)}


def get_profile(key: str) -> Profile:
    try:
        return PROFILES[key.lower()]
    except KeyError:
        raise ValueError(f"unknown profile {key!r}; known: {', '.join(PROFILES)}") from None
