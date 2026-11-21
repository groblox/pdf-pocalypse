"""Text contrast repair (WCAG 1.4.3), measured the way PAC measures it: on the rendered page.

pdf-a11y's contrast scan assumes every page is white, so white text on a grey table header
(3.96:1) passes there and fails in PAC. Here each text span's colour is compared with the
colour actually behind it in a render. Where a pair fails, the coloured side moves just far
enough to pass, keeping its hue: a fill behind light text gets darker, a light text colour on
white gets darker. Colours are rewritten in the content streams (page and form XObjects), and
the result is checked again; a change that makes any other text worse is dropped.

Text over a background that is a picture (a table drawn as image strips with the text on top, as
in B30) gets the matching pixels of that picture darkened the same way: only pixels close to the
measured background colour, only in images under failing text.

Pairs are repaired, and also flagged, up to a margin above the threshold: PAC measures colours its
own way (CMYK greys especially), and a header that read 4.67:1 here could read under 4.5 there.

Not for drawings, where colour carries meaning (demolition vs. existing, hazards).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pikepdf
import pymupdf

NORMAL, LARGE = 4.5, 3.0
MARGIN = 0.35        # flag and repair up to this far above the threshold (4.85:1, 3.35:1 for large text)
TOLERANCE = 0.035    # how close (0–1 per channel) a content-stream colour must be to match


def _lum(rgb) -> float:
    def ch(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _threshold(span) -> float:
    bold = "bold" in span["font"].lower() or span["flags"] & 16
    return LARGE if span["size"] >= 18 or (bold and span["size"] >= 14) else NORMAL


def failing_pairs(path: Path) -> list[dict]:
    """Visible text spans below their contrast threshold: page, text, fg, bg (0–1 RGB)."""
    out = []
    with pymupdf.open(path) as doc:
        for page in doc:
            zoom = 2
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            bounds = pymupdf.IRect(0, 0, pix.width, pix.height)
            # the page's pixels as one array (reading them one by one from Python took 0.6 s a page)
            rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3].astype(np.int32)
            packed = rgb[..., 0] << 16 | rgb[..., 1] << 8 | rgb[..., 2]  # one integer per pixel: fast to count
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if not span["text"].strip() or span.get("alpha", 255) == 0:
                            continue
                        c = span["color"]
                        fg = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
                        # text positions are unrotated, the render isn't (landscape pages stored rotated)
                        box = pymupdf.IRect(pymupdf.Rect(span["bbox"]) * page.rotation_matrix * zoom) & bounds
                        if box.is_empty:
                            continue
                        colours, n = np.unique(packed[box.y0:box.y1, box.x0:box.x1], return_counts=True)
                        counts = Counter({(int(c) >> 16, int(c) >> 8 & 255, int(c) & 255): int(k)
                                          for c, k in zip(colours, n)})
                        bg = tuple(v / 255 for v in counts.most_common(1)[0][0])
                        # text that doesn't show (OCR layers, text under an image) isn't judged
                        near_fg = sum(n for px, n in counts.items()
                                      if max(abs(px[i] / 255 - fg[i]) for i in range(3)) < 0.15)
                        if near_fg < 0.03 * sum(counts.values()) or ratio(fg, bg) >= _threshold(span) + MARGIN:
                            continue
                        out.append({"page": page.number, "text": span["text"].strip()[:40], "fg": fg,
                                    "bg": bg, "need": _threshold(span) + MARGIN, "bbox": list(span["bbox"]),
                                    "fails": ratio(fg, bg) < _threshold(span)})
    return out


def _adjust(color, other, need: float, darken: bool):
    """Scale colour toward black (or white) until it reaches `need` against `other`."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        f = (lo + hi) / 2
        c = tuple(v * f for v in color) if darken else tuple(1 - (1 - v) * f for v in color)
        if ratio(c, other) >= need:
            lo = f
        else:
            hi = f
    return tuple(v * lo for v in color) if darken else tuple(1 - (1 - v) * lo for v in color)


def plan(pairs: list[dict]) -> dict[tuple, tuple]:
    """old colour → new colour (0–1 RGB), strongest change wins per colour. Either side may
    move (text colour away from its background, or a fill away from its text); the smaller
    change that passes is used, and a white page is never recoloured."""
    mapping: dict[tuple, tuple] = {}
    for p in pairs:
        fg, bg = p["fg"], p["bg"]
        options = [(fg, _adjust(fg, bg, p["need"], darken=_lum(fg) <= _lum(bg)), bg)]
        if _lum(bg) < 0.95:
            options.append((bg, _adjust(bg, fg, p["need"], darken=_lum(fg) > _lum(bg)), fg))
        options = [o for o in options if ratio(o[1], o[2]) >= p["need"] - 0.01]
        if not options:
            continue
        old, new, _ = min(options, key=lambda o: abs(_lum(o[1]) - _lum(o[0])))
        key = tuple(round(v, 3) for v in old)
        if key not in mapping or abs(_lum(new) - _lum(old)) > abs(_lum(mapping[key]) - _lum(old)):
            mapping[key] = new
    return mapping


def _as_rgb(operands) -> tuple | None:
    vals = [float(v) for v in operands]
    if len(vals) == 1:
        return (vals[0],) * 3
    if len(vals) == 3:
        return tuple(vals)
    if len(vals) == 4:
        c, m, y, k = vals
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _from_rgb(new, n: int, old_operands) -> list:
    if n == 1:
        return [pikepdf.Object.parse(f"{(sum(new) / 3):.4f}".encode())]
    if n == 3:
        return [pikepdf.Object.parse(f"{v:.4f}".encode()) for v in new]
    # CMYK: keep the ink mix, change the black to reach the new lightness
    c, m, y, k = (float(v) for v in old_operands)
    old_rgb = _as_rgb(old_operands)
    f = (_lum(new) / max(_lum(old_rgb), 1e-6)) ** (1 / 2.2)
    k2 = min(1.0, 1 - (1 - k) * f)
    return [pikepdf.Object.parse(f"{v:.4f}".encode()) for v in (c, m, y, k2)]


FILL_OPS = {"g", "rg", "k", "sc", "scn"}


def _rewrite(stream_owner, mapping) -> int:
    changed, ops = 0, []
    for ins in pikepdf.parse_content_stream(stream_owner):
        op = str(ins.operator)
        operands = list(ins.operands)
        if op in FILL_OPS and operands and all(isinstance(v, (int, float)) or
                                               type(v).__name__ == "Decimal" for v in operands):
            rgb = _as_rgb(operands)
            if rgb:
                for old, new in mapping.items():
                    if max(abs(rgb[i] - old[i]) for i in range(3)) <= TOLERANCE:
                        ins = pikepdf.ContentStreamInstruction(_from_rgb(new, len(operands), operands),
                                                               ins.operator)
                        changed += 1
                        break
        ops.append(ins)
    if changed:
        target = stream_owner.Contents if isinstance(stream_owner, pikepdf.Page) else stream_owner
        target.write(pikepdf.unparse_content_stream(ops))
    return changed


def _recolour_images(pdf: pikepdf.Pdf, src: Path, mapping, pairs: list[dict]) -> int:
    """Darken background pixels inside the images that sit under failing text, to the colours the
    content-stream fills get: every pixel within TOLERANCE of an old colour takes its new one (the
    darkest, if several match). A shift that kept the scan's noise left the lighter pixels short of
    the target (B30 stayed at 4.67:1). 8-bit RGB or grey images only. Returns how many changed."""
    import zlib

    import numpy as np
    import pymupdf
    from pikepdf import PdfImage
    targets: set[int] = set()
    with pymupdf.open(src) as doc:
        for p in pairs:
            area = pymupdf.Rect(p["bbox"])
            targets |= {i["xref"] for i in doc[p["page"]].get_image_info(xrefs=True)
                        if i.get("xref") and pymupdf.Rect(i["bbox"]).intersects(area)}
    changed = 0
    for xref in targets:
        obj = pdf.get_object((xref, 0))
        if obj.get("/ImageMask") or int(obj.get("/BitsPerComponent", 8)) != 8:
            continue
        try:
            pil = PdfImage(obj).as_pil_image()
        except Exception:
            continue
        if pil.mode not in ("RGB", "L"):
            continue
        a = np.asarray(pil.convert("RGB")).astype(int)
        out = a.copy()
        hit = np.zeros(a.shape[:2], bool)
        for old, new in sorted(mapping.items(), key=lambda kv: -_lum(kv[1])):  # darkest last, so it wins
            near = np.abs(a - np.array(old) * 255).max(axis=2) <= TOLERANCE * 255
            out[near] = np.round(np.array(new) * 255)
            hit |= near
        if not hit.any():
            continue
        out = out.clip(0, 255).astype(np.uint8)
        if pil.mode == "L":
            out = out[..., 0]
        obj.write(zlib.compress(out.tobytes()), filter=pikepdf.Name.FlateDecode)
        obj.ColorSpace = pikepdf.Name.DeviceGray if pil.mode == "L" else pikepdf.Name.DeviceRGB
        for key in ("/DecodeParms", "/Decode"):
            if key in obj:
                del obj[key]
        changed += 1
    return changed


def _apply(src: Path, dst: Path, mapping, pages: set[int], pairs: list[dict] = ()) -> int:
    changed = 0
    with pikepdf.open(src) as pdf:
        seen = set()
        for i in pages:
            page = pdf.pages[i]
            page.contents_coalesce()
            changed += _rewrite(page, mapping)
            stack = list(page.get("/Resources", {}).get("/XObject", {}).values())
            while stack:
                x = stack.pop()
                if x.objgen in seen or x.get("/Subtype") != "/Form":
                    continue
                seen.add(x.objgen)
                changed += _rewrite(x, mapping)
                stack.extend(x.get("/Resources", {}).get("/XObject", {}).values())
        changed += _recolour_images(pdf, src, mapping, list(pairs))
        pdf.save(dst)
    return changed


def fix_contrast(path: Path) -> dict:
    """Repair failing text contrast in place. Returns stats."""
    before = failing_pairs(path)
    if not before:
        return {"low_contrast_spans": 0}
    mapping = plan(before)
    pages = {p["page"] for p in before}
    tmp = path.with_suffix(".contrast.pdf")
    changed = _apply(path, tmp, mapping, pages, before)
    after = failing_pairs(tmp)
    worse = {(p["page"], p["text"]) for p in after} - {(p["page"], p["text"]) for p in before}
    if not changed or worse or len(after) >= len(before):
        tmp.unlink(missing_ok=True)
        return {"low_contrast_spans": len(before), "contrast_fixed": 0,
                "contrast_failing": sum(p["fails"] for p in before), "contrast_examples": _examples(before),
                "contrast_note": "not changed: no matching colour found or it made other text worse"}
    tmp.replace(path)
    return {"low_contrast_spans": len(before), "contrast_fixed": len(before) - len(after),
            "contrast_remaining": len(after), "contrast_failing": sum(p["fails"] for p in after),
            "contrast_examples": _examples(after), "colours_changed": len(mapping)}


def _examples(pairs: list[dict]) -> list[str]:
    """A few still-failing pairs, for the report: “Roofing” #FFFFFF on #7F7F7F (4.0:1), page 1."""
    hexc = lambda c: "#%02X%02X%02X" % tuple(round(v * 255) for v in c)  # noqa: E731
    seen, out = set(), []
    for p in pairs:
        key = (hexc(p["fg"]), hexc(p["bg"]))
        if p["fails"] and key not in seen:
            seen.add(key)
            out.append(f"“{p['text']}” {key[0]} on {key[1]} ({ratio(p['fg'], p['bg']):.1f}:1), page {p['page'] + 1}")
    return out[:4]
