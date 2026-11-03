"""Command-line entrypoint: `labelworks probe | status | preview | print | pattern | serve`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _transport_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("transport (overrides LW_* env)")
    g.add_argument("--transport", choices=["rfcomm", "serial", "usb", "file"])
    g.add_argument("--bt", metavar="MAC", help="Bluetooth address (implies --transport rfcomm)")
    g.add_argument("--channel", type=int, help="RFCOMM channel (default 1)")
    g.add_argument("--serial", metavar="PATH", help="serial device, e.g. /dev/rfcomm0")
    g.add_argument("--usb-pid", metavar="HEX")
    g.add_argument("--profile", default=None)


def _label_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("text", nargs="?", help="label text; use \\n for line breaks")
    p.add_argument("--qr")
    p.add_argument("--image", type=Path, help="PNG/JPEG to print")
    p.add_argument("--tape", type=int, help="tape width mm (default: detect, else LW_DEFAULT_TAPE_MM)")
    p.add_argument("--font-size", type=int)
    p.add_argument("--bold", action="store_true")
    p.add_argument("--align", default="center", choices=["left", "center", "right"])
    p.add_argument("--min-length", type=float, metavar="MM")
    p.add_argument("--invert", action="store_true")
    p.add_argument("--preview", type=Path, metavar="PNG", help="also write a preview PNG")


def _apply_env(a) -> None:
    if a.bt:
        os.environ["LW_TRANSPORT"], os.environ["LW_BT_ADDRESS"] = "rfcomm", a.bt
    if a.channel:
        os.environ["LW_BT_CHANNEL"] = str(a.channel)
    if a.serial:
        os.environ["LW_TRANSPORT"], os.environ["LW_SERIAL_PATH"] = "serial", a.serial
    if a.usb_pid:
        os.environ["LW_TRANSPORT"], os.environ["LW_USB_PID"] = "usb", a.usb_pid
    if a.transport:
        os.environ["LW_TRANSPORT"] = a.transport
    if a.profile:
        os.environ["LW_PROFILE"] = a.profile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="epson_labelworks", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, helptext in [("probe", "read-only identification"), ("status", "read printer status")]:
        _transport_args(sub.add_parser(name, help=helptext))

    pv = sub.add_parser("preview", help="render a label to PNG (no printer needed)")
    _transport_args(pv)
    _label_args(pv)

    pr = sub.add_parser("print", help="print a label")
    _transport_args(pr)
    _label_args(pr)
    pr.add_argument("--copies", type=int, default=1)
    pr.add_argument("--no-cut", action="store_true")
    pr.add_argument("--density", type=int, default=0)
    pr.add_argument("--dry-run", metavar="DIR", help="write job bytes to DIR instead of printing")
    pr.add_argument("--yes", action="store_true", help="allow printing on an unverified profile")

    pt = sub.add_parser("pattern", help="print the calibration wedge")
    _transport_args(pt)
    pt.add_argument("--tape", type=int)
    pt.add_argument("--dry-run", metavar="DIR")
    pt.add_argument("--preview", type=Path)
    pt.add_argument("--yes", action="store_true")

    sv = sub.add_parser("serve", help="run the MCP server")
    _transport_args(sv)
    sv.add_argument("--stdio", action="store_true", help="stdio instead of streamable HTTP")

    a = ap.parse_args(argv)
    _apply_env(a)

    # imports after env is set: server reads env at import time
    from .label import LabelSpec, build_image
    from .printer import JobSettings, LabelPrinter, PrinterError, probe
    from .profiles import get_profile
    from .render import preview_png
    from .transport import FileTransport, TransportError, from_env

    profile = get_profile(os.environ.get("LW_PROFILE", "lw-px400"))

    def out(obj) -> None:
        print(json.dumps(obj, indent=2, default=str))

    try:
        if a.cmd == "serve":
            from .server import serve
            serve("stdio" if a.stdio else "http")
            return 0
        if a.cmd == "probe":
            out(probe(from_env()))
            return 0
        if a.cmd == "status":
            st = LabelPrinter(from_env(), profile).status()
            out(st.to_dict() if st else {"status": None})
            return 0

        def tape_for(t: int | None) -> int:
            if t:
                return t
            tr = from_env()
            if not isinstance(tr, FileTransport) and a.cmd != "preview":
                st = LabelPrinter(tr, profile).status()
                if st and st.tape_width_mm:
                    return st.tape_width_mm
            return int(os.environ.get("LW_DEFAULT_TAPE_MM", "12"))

        if a.cmd == "pattern":
            spec = LabelSpec(test_pattern=True)
        else:
            spec = LabelSpec(text=a.text, qr=a.qr, font_size=a.font_size, bold=a.bold,
                             align=a.align, min_length_mm=a.min_length, invert=a.invert,
                             image_base64=(__import__("base64").b64encode(a.image.read_bytes()).decode()
                                           if a.image else None))
        tape = tape_for(a.tape)
        img = build_image(spec, profile, tape)
        if a.preview or a.cmd == "preview":
            dest = a.preview or Path("preview.png")
            dest.write_bytes(preview_png(img, profile, tape, profile.mm_to_dots(profile.default_margin_mm)))
            print(f"preview: {dest} ({img.width}x{img.height} dots, tape {tape} mm)", file=sys.stderr)
        if a.cmd == "preview":
            return 0

        if not a.dry_run and not profile.verified and not a.yes:
            print(f"{profile.model} profile is unverified; re-run with --yes to print for real "
                  "(or --dry-run DIR to inspect bytes).", file=sys.stderr)
            return 2
        tr = FileTransport(a.dry_run) if a.dry_run else from_env()
        settings = JobSettings(tape_mm=tape,
                               cut="none" if getattr(a, "no_cut", False) else "job",
                               density=getattr(a, "density", 0))
        p = LabelPrinter(tr, profile)
        for _ in range(getattr(a, "copies", 1)):
            out(p.print_image(img, settings))
        return 0
    except (PrinterError, TransportError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
