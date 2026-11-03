"""1D and 2D barcodes rendered at whole-dot module widths (no resampling).

1D: Code 128, Code 39, EAN-13, EAN-8, UPC-A, ITF, Codabar, GS1-128, EAN-14.
2D: QR, Data Matrix, PDF417.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from . import fonts as F

LINEAR = {
    "code128": "code128", "code39": "code39", "ean13": "ean13", "ean8": "ean8",
    "upca": "upca", "itf": "itf", "codabar": "codabar", "gs1_128": "gs1_128", "ean14": "ean14",
}
MATRIX = ("qr", "datamatrix", "pdf417")
SYMBOLOGIES = tuple(LINEAR) + MATRIX


def _blank(w: int, h: int) -> Image.Image:
    return Image.new("L", (max(1, w), max(1, h)), 255)


def linear_modules(symbology: str, data: str, checksum: bool = True) -> tuple[str, str]:
    """('1010…' module string, human-readable text) for a 1D symbology."""
    import barcode
    from barcode.errors import BarcodeError

    name = LINEAR[symbology]
    kwargs = {}
    if symbology == "code39":
        kwargs["add_checksum"] = checksum
    elif symbology == "codabar":
        if data[:1].upper() not in "ABCD":
            data = f"A{data}A"
    try:
        code = barcode.get_barcode_class(name)(data, **kwargs)
        modules = code.build()[0]
        text = code.get_fullcode()
    except (BarcodeError, ValueError, TypeError, KeyError) as e:
        raise ValueError(f"{symbology}: cannot encode {data!r}: {e}") from None
    return modules, text


def render_linear(symbology: str, data: str, height: int, module: int | None = None,
                  show_text: bool = True, quiet: int = 10, checksum: bool = True,
                  max_width: int | None = None, font: str | None = "mono") -> Image.Image:
    modules, human = linear_modules(symbology, data, checksum)
    m = module or 2
    if max_width and not module:
        while m > 1 and (len(modules) + 2 * quiet) * m > max_width:
            m -= 1
    width = (len(modules) + 2 * quiet) * m
    text_h = 0
    tfont = None
    if show_text and height >= 30:
        text_h = max(8, min(22, height // 4))
        tfont = F.get_font(F.resolve(font).path, text_h)
        asc, desc = tfont.getmetrics()
        text_h = asc + desc
    bar_h = height - text_h
    if bar_h < 10:
        raise ValueError("barcode needs at least 10 dots of bar height; use wider tape or hide the text")
    img = _blank(width, height)
    d = ImageDraw.Draw(img)
    x = quiet * m
    run = 0
    for i, bit in enumerate(modules + "0"):
        if bit == "1":
            run += 1
            continue
        if run:
            d.rectangle([x + (i - run) * m, 0, x + i * m - 1, bar_h - 1], fill=0)
            run = 0
    if tfont:
        tw = tfont.getlength(human)
        if tw > width:
            tfont = F.get_font(tfont.path, max(6, int(tfont.size * width / tw)))
            tw = tfont.getlength(human)
        d.text(((width - tw) / 2, bar_h), human, font=tfont, fill=0)
    return img


def _scale_matrix(matrix: list[list[int]], height: int, quiet: int, size: int | None) -> Image.Image:
    rows, cols = len(matrix), len(matrix[0])
    target = min(height, size or height)
    box = target // (rows + 2 * quiet)
    if box < 1:
        raise ValueError(f"code needs {rows + 2 * quiet} rows of dots but only {target} fit; "
                         "shorten the data or use wider tape")
    w = (cols + 2 * quiet) * box
    img = _blank(w, height)
    d = ImageDraw.Draw(img)
    ox, oy = quiet * box, (height - rows * box) // 2
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            if v:
                d.rectangle([ox + c * box, oy + r * box, ox + (c + 1) * box - 1,
                             oy + (r + 1) * box - 1], fill=0)
    return img


def qr_matrix(data: str, ecc: str = "M") -> list[list[int]]:
    import qrcode
    from qrcode import constants as C

    level = {"L": C.ERROR_CORRECT_L, "M": C.ERROR_CORRECT_M, "Q": C.ERROR_CORRECT_Q,
             "H": C.ERROR_CORRECT_H}[ecc.upper()]
    qr = qrcode.QRCode(border=0, error_correction=level)
    qr.add_data(data)
    qr.make(fit=True)
    return [[1 if v else 0 for v in row] for row in qr.get_matrix()]


def datamatrix_matrix(data: str) -> list[list[int]]:
    from ppf.datamatrix import DataMatrix

    try:
        return [list(r) for r in DataMatrix(data).matrix]
    except Exception as e:  # library raises plain exceptions for unencodable data
        raise ValueError(f"datamatrix: cannot encode {data!r}: {e}") from None


def pdf417_image(data: str, height: int, size: int | None, quiet: int) -> Image.Image:
    from pdf417gen import encode, render_image

    target = min(height, size or height)
    best = None
    for cols in range(1, 31):  # fewest columns whose rows still fit
        try:
            codes = encode(data, columns=cols, security_level=2)
        except ValueError:
            continue
        rows = len(codes)
        for ratio in (3, 2):
            if rows * ratio <= target - 2 * quiet:
                best = (codes, ratio)
                break
        if best:
            break
    if not best:
        raise ValueError("pdf417: data too long for this tape; use QR or Data Matrix")
    codes, ratio = best
    img = render_image(codes, scale=1, ratio=ratio, padding=0).convert("L")
    out = _blank(img.width + 2 * quiet, height)
    out.paste(img, (quiet, (height - img.height) // 2))
    return out


def render_matrix(symbology: str, data: str, height: int, size: int | None = None,
                  ecc: str = "M", quiet: int = 1) -> Image.Image:
    if symbology == "qr":
        return _scale_matrix(qr_matrix(data, ecc), height, quiet, size)
    if symbology == "datamatrix":
        return _scale_matrix(datamatrix_matrix(data), height, quiet, size)
    if symbology == "pdf417":
        return pdf417_image(data, height, size, max(quiet, 2))
    raise ValueError(f"unknown 2D symbology {symbology!r}")
