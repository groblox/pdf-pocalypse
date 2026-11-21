"""Font repairs pdf-a11y doesn't make, run before tagging so the tagger, the
intent check and screen readers all see real text.

  - ToUnicode for Identity-H subsets of Microsoft core fonts that carry no cmap
    (glyph IDs follow the standard Macintosh glyph order, so "5HYLVHG" → "Revised")
  - ToUnicode for symbolic TrueType subsets of core text fonts (codes are cp1252)
  - ToUnicode for ZapfDingbats check-box glyphs in form-field appearances
  - drop /CIDSet (optional in PDF/UA, and often stale after subsetting → veraPDF 7.21.4.2)
  - embed metric-compatible system fonts where the producer embedded none (veraPDF 7.21.4.1)
  - remap Word's Private-Use-Area Symbol/Wingdings glyphs (bullet → U+F0B7) to real Unicode (WTPDF 8.4.3)
  - fill partial ToUnicode maps from the glyph order their known entries prove (ISOAdobe / Mac)
"""
from __future__ import annotations

import io
import re
from collections import Counter
from pathlib import Path

import pikepdf
from fontTools.agl import toUnicode
from fontTools.cffLib import CFFFontSet, cffStandardStrings
from fontTools.misc import psCharStrings
from fontTools.ttLib import TTFont
from fontTools.ttLib.standardGlyphOrder import standardGlyphOrder

CORE_TEXT_FONT = re.compile(
    r"(Arial|TimesNewRoman|Times New Roman|CourierNew|Courier New|Verdana|Georgia|Tahoma|"
    r"Calibri|Cambria|Garamond|BookAntiqua|Century|TrebuchetMS|Helvetica)", re.I)

# Check-box styles a form field can use (PDF 32000 12.7.4.2.3) and their Unicode symbols.
ZAPF_DINGBATS = {"3": 0x2713, "4": 0x2714, "5": 0x2715, "6": 0x2716, "7": 0x2717, "8": 0x2718,
                 "l": 0x25CF, "n": 0x25A0, "u": 0x25C6, "H": 0x2605}


def _cmap_stream(pdf: pikepdf.Pdf, mapping: dict[int, str], two_byte: bool) -> pikepdf.Stream:
    width = 4 if two_byte else 2
    items = sorted(mapping.items())
    blocks = []
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        body = "\n".join(f"<{code:0{width}X}> <{''.join(f'{ord(ch):04X}' for ch in text)}>"
                         for code, text in chunk)
        blocks.append(f"{len(chunk)} beginbfchar\n{body}\nendbfchar")
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        f"1 begincodespacerange\n<{'0' * width}> <{'F' * width}>\nendcodespacerange\n"
        + "\n".join(blocks) +
        "\nendcmap\nCMapName currentdict /CIDResource defineresource pop\nend\nend\n"
    )
    return pdf.make_stream(cmap.encode("ascii"))


def _mac_order_mapping() -> dict[int, str]:
    out = {}
    for gid, name in enumerate(standardGlyphOrder):
        uni = toUnicode(name)
        if uni and gid > 2:  # 0-2 are .notdef / .null / nonmarkingreturn
            out[gid] = uni
    return out


MAC_ORDER = _mac_order_mapping()


def _windows_symbol_mapping() -> dict[int, str]:
    """GID → Unicode for subsets of Windows SymbolMT (192 glyphs: .notdef/.null/CR, then
    Symbol-encoding codes 0x20–0x7E and 0xA1–0xFE in order). Code → Unicode comes from
    the glyph names in the macOS Symbol font's Mac Roman cmap."""
    try:
        sym = TTFont("/System/Library/Fonts/Symbol.ttf")
        by_code = next(t.cmap for t in sym["cmap"].tables if (t.platformID, t.platEncID) == (1, 0))
    except Exception:
        return {}
    out = {}
    for gid in range(3, 192):
        code = gid + 29 if gid <= 97 else gid + 63
        uni = toUnicode(by_code.get(code, ""))
        if uni:
            out[gid] = uni
    return out


WINDOWS_SYMBOL = _windows_symbol_mapping()


# Adobe's ISOAdobe glyph order (CFF standard strings 1–228). Subsets of Adobe's own
# fonts (Minion Pro, Myriad Pro…) keep these CIDs: CID 1 = space, 34 = A, ….
ISO_ADOBE = {cid: u for cid in range(1, 229) if (u := toUnicode(cffStandardStrings[cid]))}


def _cff_cids(descriptor) -> list[int] | None:
    """CIDs present in an embedded CID-keyed CFF program (from its cidNNNNN charset)."""
    ff = descriptor.get("/FontFile3") if descriptor is not None else None
    if ff is None:
        return None
    try:
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(ff.read_bytes()), None)
        top = cff[cff.fontNames[0]]
        if not hasattr(top, "ROS"):
            return None
        return [int(n[3:]) for n in top.charset if n.startswith("cid")]
    except Exception:
        return None


def _cid_widths(desc) -> dict[int, float]:
    """A CIDFont's /W array as {cid: width}."""
    out, w = {}, list(desc.get("/W") or [])
    i = 0
    while i < len(w):
        first = int(w[i])
        if isinstance(w[i + 1], pikepdf.Array):
            for k, v in enumerate(w[i + 1]):
                out[first + k] = float(v)
            i += 2
        else:
            for cid in range(first, int(w[i + 1]) + 1):
                out[cid] = float(w[i + 2])
            i += 3
    return out


# Operand count each first stack-clearing operator takes without a width (T2 charstrings).
_T2_ARGS = {"rmoveto": (2,), "hmoveto": (1,), "vmoveto": (1,), "endchar": (0, 4)}


def _sync_cff_widths(desc, stats: Counter) -> None:
    """PDF/UA 7.21.5 / WTPDF 8.4.5.6: an embedded CID CFF program's glyph widths must match
    the font's /W. Viewers advance text by /W, so the program's width is rewritten to /W's;
    outlines, hints and text positions stay as they are."""
    fd = desc.get("/FontDescriptor")
    ff = fd.get("/FontFile3") if fd is not None else None
    if ff is None:
        return
    try:
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(ff.read_bytes()), None)
        top = cff[cff.fontNames[0]]
        if not hasattr(top, "ROS") or list(getattr(top, "FontMatrix", [0.001]))[:1] != [0.001]:
            return
        dw = float(desc.get("/DW", 1000))
        pdf_widths = _cid_widths(desc)
        changed = 0
        for name in top.charset:
            if not name.startswith("cid"):
                continue
            cs = top.CharStrings[name]
            private = cs.private
            ext = psCharStrings.T2WidthExtractor(getattr(private, "Subrs", []), cs.globalSubrs,
                                                 private.nominalWidthX, private.defaultWidthX)
            ext.execute(cs)
            target = pdf_widths.get(int(name[3:]), dw)
            if abs(ext.width - target) <= 1:
                continue
            cs.decompile()
            prog = list(cs.program)
            op_at = next(i for i, t in enumerate(prog) if isinstance(t, str))
            op, n = prog[op_at], op_at
            has_width = n not in _T2_ARGS[op] if op in _T2_ARGS else n % 2 == 1
            if has_width:
                prog = prog[1:]
            if round(target) != private.defaultWidthX:
                prog.insert(0, round(target) - private.nominalWidthX)
            cs.program, cs.bytecode = prog, None
            changed += 1
        if changed:
            buf = io.BytesIO()
            cff.compile(buf, TTFont(recalcBBoxes=False))
            ff.write(buf.getvalue())
            stats["cff_widths_synced"] += changed
    except Exception:
        stats["cff_widths_unchecked"] += 1


def _agrees(order: dict[int, str], known: dict[int, str]) -> bool:
    """A glyph-order hypothesis is accepted only if it reproduces every usable known entry."""
    usable = {c: t for c, t in known.items() if t and t != "�"}
    return len(usable) >= 3 and all(order.get(c) == t for c, t in usable.items())


def _fill_partial_tounicode(pdf, font, desc, stats: Counter) -> None:
    """Producers sometimes write a ToUnicode covering only a few glyphs (e.g., lab reports:
    6 of ~45), leaving the rest unreadable. Fill the gaps from the glyph order the known
    entries prove the subset follows."""
    known = parse_tounicode(font.ToUnicode)
    fd = desc.get("/FontDescriptor")
    if desc.get("/Subtype") == "/CIDFontType0":
        codes = _cff_cids(fd) or []
    else:
        codes = list(range(1, _num_glyphs(fd) or 0))
    missing = [c for c in codes if c not in known]
    if not missing:
        return
    for label, order in (("iso_adobe", ISO_ADOBE), ("mac_order", MAC_ORDER)):
        if _agrees(order, known):
            merged = {c: order[c] for c in missing if c in order} | known
            font.ToUnicode = _cmap_stream(pdf, merged, two_byte=True)
            stats[f"tounicode_filled_{label}"] += 1
            return
    if desc.get("/Subtype") == "/CIDFontType0":  # the charset lists exactly the glyphs present
        stats["partial_tounicode_unresolved"] += 1


def parse_tounicode(stream) -> dict[int, str]:
    """code → text from a ToUnicode CMap (bfchar and both bfrange forms)."""
    data = stream.read_bytes().decode("latin-1")
    out: dict[int, str] = {}

    def text(hexstr: str) -> str:
        b = bytes.fromhex(hexstr)
        return b.decode("utf-16-be", errors="replace") if len(b) % 2 == 0 else b.decode("latin-1")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = text(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for i, dst in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    out[lo + i] = text(dst)
            else:
                start = bytes.fromhex(rest[1:-1])
                base = int.from_bytes(start, "big")
                for i in range(hi - lo + 1):
                    out[lo + i] = text((base + i).to_bytes(len(start), "big").hex())
    return out


LEGACY_PUA = {"\uf001": "fi", "\uf002": "fl"}  # Mac/Adobe ligature code points


def _fix_pua_ligatures(pdf: pikepdf.Pdf, font, stats: Counter) -> None:
    """ToUnicode entries that map to the old Mac/Adobe fi/fl private-use code points (WTPDF
    8.4.3 fails on PUA without ActualText) are rewritten to the letters themselves."""
    try:
        mapping = parse_tounicode(font.ToUnicode)
    except Exception:
        return
    if not any(t in LEGACY_PUA for t in mapping.values()):
        return
    font.ToUnicode = _cmap_stream(pdf, {c: LEGACY_PUA.get(t, t) for c, t in mapping.items()},
                                  two_byte=font.get("/Subtype") == "/Type0")
    stats["pua_ligatures_mapped"] += 1


def _symbol_code_to_unicode() -> dict[int, str]:
    try:
        sym = TTFont("/System/Library/Fonts/Symbol.ttf")
        by_code = next(t.cmap for t in sym["cmap"].tables if (t.platformID, t.platEncID) == (1, 0))
    except Exception:
        return {}
    return {code: u for code, name in by_code.items() if (u := toUnicode(name))}


SYMBOL_ENCODING = _symbol_code_to_unicode()


# Wingdings has no glyph names to look up; these are the bullets Word's list styles use.
WINGDINGS = {0x6C: "\u25CF", 0x6E: "\u25A0", 0x6F: "\u25A1", 0x71: "\u2751", 0x75: "\u25C6",
             0x76: "\u2756", 0xA7: "\u25AA", 0xA8: "\u25FB", 0xD8: "\u27A2", 0xFB: "\u2717",
             0xFC: "\u2713", 0xFE: "\u2611"}


def _pua_family(font, base: str) -> str | None:
    """"Symbol" / "Wingdings" by BaseFont, or by the embedded program's own name (Word exports
    them as e.g. "CIDFont+F10")."""
    for family in ("Symbol", "Wingdings"):
        if family in base:
            return family
    desc = font.DescendantFonts[0] if font.get("/Subtype") == "/Type0" else font
    fd = desc.get("/FontDescriptor")
    ff = fd.get("/FontFile2") if fd is not None else None
    if ff is None:
        return None
    try:
        name = TTFont(io.BytesIO(ff.read_bytes()), lazy=True)["name"].getDebugName(6) or ""
    except Exception:
        return None
    return next((f for f in ("Symbol", "Wingdings") if f in name), None)


def _fix_pua(pdf: pikepdf.Pdf, font, family: str, stats: Counter) -> None:
    """Word maps Symbol and Wingdings glyphs to the Private Use Area (bullet → U+F0B7, arrow
    bullet → U+F0D8); WTPDF 8.4.3 wants real Unicode. Rewrite F020–F0FF through the font's
    own encoding."""
    table = SYMBOL_ENCODING if family == "Symbol" else WINGDINGS
    mapping = parse_tounicode(font.ToUnicode)
    changed = False
    for code, txt in mapping.items():
        if len(txt) == 1 and 0xF020 <= ord(txt) <= 0xF0FF and (ord(txt) - 0xF000) in table:
            mapping[code] = table[ord(txt) - 0xF000]
            changed = True
    if changed:
        font.ToUnicode = _cmap_stream(pdf, mapping, two_byte=font.get("/Subtype") == "/Type0")
        stats[f"{family.lower()}_pua_remapped"] += 1

# Glyph names outside the Adobe Glyph List, identified by rendering them.
KNOWN_GLYPHS = {"NewspaperPi": {"c6050": "☐"}}  # ballot box on DCM Yes/No forms


def _embedded_cmap(descriptor) -> dict[int, int] | None:
    """Unicode cmap of the embedded TrueType program, as gid → codepoint."""
    ff = descriptor.get("/FontFile2") if descriptor is not None else None
    if ff is None:
        return None
    try:
        tt = TTFont(io.BytesIO(ff.read_bytes()), lazy=True)
        best = tt["cmap"].getBestCmap() if "cmap" in tt else None
        if not best or all(0xF000 <= c <= 0xF0FF for c in best):  # symbolic (3,0) cmaps don't count
            return None
        order = tt.getGlyphOrder()
        gid = {n: i for i, n in enumerate(order)}
        return {gid[name]: cp for cp, name in best.items() if name in gid}
    except Exception:
        return None


def _num_glyphs(descriptor) -> int | None:
    ff = descriptor.get("/FontFile2") if descriptor is not None else None
    try:
        return TTFont(io.BytesIO(ff.read_bytes()), lazy=True)["maxp"].numGlyphs if ff is not None else None
    except Exception:
        return None


def _fix_font(pdf: pikepdf.Pdf, font: pikepdf.Dictionary, stats: Counter) -> None:
    base = str(font.get("/BaseFont", ""))
    subtype = font.get("/Subtype")

    if "/ToUnicode" in font and (family := _pua_family(font, base)):
        _fix_pua(pdf, font, family, stats)
    elif "/ToUnicode" in font:
        _fix_pua_ligatures(pdf, font, stats)

    if subtype == "/Type0":
        for d in font.get("/DescendantFonts", []):
            fd = d.get("/FontDescriptor")
            if fd is not None and "/CIDSet" in fd:
                del fd["/CIDSet"]
                stats["cidset_removed"] += 1
            if d.get("/Subtype") == "/CIDFontType2" and "/CIDToGIDMap" not in d:
                d.CIDToGIDMap = pikepdf.Name.Identity  # the spec's default, made explicit (PDF/UA 7.21.3.2)
                stats["cidtogidmap_added"] += 1
            if d.get("/Subtype") == "/CIDFontType0":
                _sync_cff_widths(d, stats)
        if font.get("/Encoding") != "/Identity-H":
            return
        desc = font.DescendantFonts[0]
        if desc.get("/CIDToGIDMap", "/Identity") != "/Identity":
            stats["skipped_cidtogid"] += 1
            return
        if "/ToUnicode" in font:
            _fill_partial_tounicode(pdf, font, desc, stats)
            return
        cmap = _embedded_cmap(desc.get("/FontDescriptor"))
        if desc.get("/Subtype") == "/CIDFontType0" and re.search(r"(Minion|Myriad)Pro|Adobe", base) \
                and _cff_cids(desc.get("/FontDescriptor")):
            mapping = ISO_ADOBE
            stats["tounicode_iso_adobe"] += 1
        elif cmap:
            mapping = {g: chr(c) for g, c in cmap.items()}
            stats["tounicode_from_cmap"] += 1
        elif "Symbol" in base and _num_glyphs(desc.get("/FontDescriptor")) == 192 and WINDOWS_SYMBOL:
            mapping = WINDOWS_SYMBOL
            stats["tounicode_windows_symbol"] += 1
        elif CORE_TEXT_FONT.search(base):
            mapping = MAC_ORDER
            stats["tounicode_mac_order"] += 1
        else:
            stats["skipped_unknown_type0"] += 1
            return
        font.ToUnicode = _cmap_stream(pdf, mapping, two_byte=True)
        return

    fd = font.get("/FontDescriptor")
    if fd is not None and "/CIDSet" in fd:
        del fd["/CIDSet"]
        stats["cidset_removed"] += 1
    symbolic = fd is not None and int(fd.get("/Flags", 0)) & 4
    if subtype == "/TrueType" and not symbolic:  # PDF/UA 7.21.6-2: WinAnsi/MacRoman base required
        enc = font.get("/Encoding")
        if enc is None:
            font.Encoding = pikepdf.Name.WinAnsiEncoding
            stats["encoding_winansi_added"] += 1
        elif isinstance(enc, pikepdf.Dictionary) and "/BaseEncoding" not in enc:
            enc.BaseEncoding = pikepdf.Name.WinAnsiEncoding
            stats["base_encoding_winansi_added"] += 1
    if "/ToUnicode" in font:
        return
    enc = font.get("/Encoding")
    if isinstance(enc, pikepdf.Dictionary) and "/Differences" in enc:
        known = next((v for k, v in KNOWN_GLYPHS.items() if k in base), {})
        mapping, code = {}, 0
        for item in enc.Differences:
            if isinstance(item, int):
                code = int(item)
                continue
            name = str(item).lstrip("/")
            uni = known.get(name) or toUnicode(name)
            if uni:
                mapping[code] = uni
            code += 1
        if mapping and (known or subtype == "/Type1"):
            font.ToUnicode = _cmap_stream(pdf, mapping, two_byte=False)
            stats["tounicode_differences"] += 1
            return
    if "ZapfDingbats" in base:
        font.ToUnicode = _cmap_stream(pdf, {ord(k): chr(v) for k, v in ZAPF_DINGBATS.items()}, False)
        stats["tounicode_zapf"] += 1
        return
    if subtype == "/TrueType" and symbolic and "/Encoding" not in font and CORE_TEXT_FONT.search(base):
        first, last = int(font.get("/FirstChar", 32)), int(font.get("/LastChar", 255))
        mapping = {}
        for code in range(max(first, 32), min(last, 255) + 1):
            ch = bytes([code]).decode("cp1252", errors="ignore")
            if ch:
                mapping[code] = ch
        font.ToUnicode = _cmap_stream(pdf, mapping, two_byte=False)
        stats["tounicode_cp1252"] += 1


def _iter_fonts(resources, seen: set):
    """Fonts in a resource dict, recursing into Form XObjects and Type3 glyph resources."""
    if not isinstance(resources, pikepdf.Dictionary):
        return
    for font in resources.get("/Font", {}).values():
        if isinstance(font, pikepdf.Dictionary) and font.objgen not in seen:
            seen.add(font.objgen)
            yield font
            yield from _iter_fonts(font.get("/Resources"), seen)
    for xobj in resources.get("/XObject", {}).values():
        if isinstance(xobj, pikepdf.Stream) and xobj.get("/Subtype") == "/Form" and xobj.objgen not in seen:
            seen.add(xobj.objgen)
            yield from _iter_fonts(xobj.get("/Resources"), seen)


# ---------------------------------------------------------------------------
# Embedding fonts the producer left out (veraPDF 7.21.4.1). The metric-compatible
# macOS system fonts stand in for the Windows/Adobe originals; the PDF's own
# /Widths are kept so text positions don't move.
# ---------------------------------------------------------------------------
SYS = "/System/Library/Fonts"
FAMILIES = [(re.compile(r"Arial|Helvetica", re.I), "Supplemental/Arial", "LiberationSans", "DejaVuSans"),
            (re.compile(r"Times", re.I), "Supplemental/Times New Roman", "LiberationSerif", "DejaVuSerif"),
            (re.compile(r"Courier", re.I), "Supplemental/Courier New", "LiberationMono", "DejaVuSansMono")]

LINUX_FONT_DIRS = (
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/liberation2"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/liberation"),
    Path("/usr/share/fonts/dejavu"),
)

SERIF_HINT = re.compile(r"serif|roman|garamond|georgia|minion|caslon|baskerville|palatino|bookman|cambria", re.I)


def _find_linux_font(liberation: str, dejavu: str, style: str) -> str | None:
    style_map = {
        "": ("Regular", ""),
        "Bold": ("Bold", "-Bold"),
        "Italic": ("Italic", "-Oblique"),
        "Bold Italic": ("BoldItalic", "-BoldOblique"),
    }
    lib_s, dej_s = style_map.get(style, ("Regular", ""))
    candidates = [f"{liberation}-{lib_s}.ttf", f"{dejavu}{dej_s}.ttf"]
    for d in LINUX_FONT_DIRS:
        if not d.is_dir():
            continue
        for fname in candidates:
            p = d / fname
            if p.exists():
                return str(p)
    return None


def _system_font_for(base: str, text_font: bool = False) -> tuple[str, bool] | None:
    """(path, symbolic) of the system font to embed for a BaseFont name. Checks macOS
    system fonts first, then metric-compatible Linux Liberation/DejaVu fonts. For a
    non-symbolic text font with no metric-compatible match (e.g. Proxima Nova), fall back
    to Arial/LiberationSans, or Times/LiberationSerif for serif faces: the PDF's own
    /Widths keep every character where it was."""
    name = base.lstrip("/").split("+")[-1]
    if name.startswith("ZapfDingbats"):
        p = f"{SYS}/ZapfDingbats.ttf"
        return (p, True) if Path(p).exists() else None
    if name.startswith("Symbol"):
        p = f"{SYS}/Symbol.ttf"
        return (p, True) if Path(p).exists() else None
    style = " ".join(s for s, rx in (("Bold", r"bold|black|heavy|semibold|demi"), ("Italic", r"italic|oblique"))
                     if re.search(rx, name, re.I))
    suffix = f" {style}" if style else ""
    for pattern, family, lib_name, dej_name in FAMILIES:
        if pattern.search(name):
            mac = f"{SYS}/{family}{suffix}.ttf"
            if Path(mac).exists():
                return mac, False
            lin = _find_linux_font(lib_name, dej_name, style)
            return (lin, False) if lin else None
    if text_font:
        serif = bool(SERIF_HINT.search(name) and "sans" not in name.lower())
        family = "Supplemental/Times New Roman" if serif else "Supplemental/Arial"
        mac = f"{SYS}/{family}{suffix}.ttf"
        if Path(mac).exists():
            return mac, False
        lin = _find_linux_font("LiberationSerif" if serif else "LiberationSans",
                               "DejaVuSerif" if serif else "DejaVuSans", style)
        return (lin, False) if lin else None
    return None


def _font_program(path: str, symbolic: bool, extra_unicodes: set[int]) -> TTFont:
    from fontTools import subset

    tt = TTFont(path)
    if symbolic:
        # PDF/UA 7.21.6: a symbolic TrueType program must carry exactly one cmap encoding.
        tt["cmap"].tables = [t for t in tt["cmap"].tables if (t.platformID, t.platEncID) == (1, 0)]
        return tt
    opts = subset.Options()
    opts.name_IDs, opts.name_languages, opts.notdef_outline = ["*"], ["*"], True
    opts.legacy_cmap = True  # keep the (1,0) table alongside (3,1)
    opts.layout_features = []
    sub = subset.Subsetter(opts)
    cp1252 = {ord(bytes([c]).decode("cp1252", "ignore") or " ") for c in range(32, 256)}
    sub.populate(unicodes=sorted(cp1252 | extra_unicodes))
    sub.subset(tt)
    return tt


def _differences_unicodes(font) -> set[int]:
    """Codepoints of glyph names an /Encoding /Differences array pulls in (they must exist in the font)."""
    enc = font.get("/Encoding")
    if not isinstance(enc, pikepdf.Dictionary):
        return set()
    names = [str(x).lstrip("/") for x in enc.get("/Differences", []) if not isinstance(x, int)]
    return {ord(u) for n in names if len(u := toUnicode(n)) == 1}


def _embed(pdf: pikepdf.Pdf, font: pikepdf.Dictionary, stats: Counter) -> None:
    if font.get("/Subtype") not in ("/TrueType", "/Type1"):
        return
    fd = font.get("/FontDescriptor")
    if fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
        return
    text_font = (fd is None or not int(fd.get("/Flags", 0)) & 4) and "/Widths" in font \
        and font.get("/Encoding") in ("/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding")
    choice = _system_font_for(str(font.get("/BaseFont", "")), bool(text_font))
    if choice is None:
        stats["unembedded_no_substitute"] += 1
        return
    path, symbolic = choice
    substitute = not symbolic and not any(p.search(str(font.get("/BaseFont", ""))) for p, *_ in FAMILIES)
    tt = _font_program(path, symbolic, _differences_unicodes(font))
    if tt["OS/2"].fsType == 2:  # restricted-license embedding
        stats["unembedded_restricted"] += 1
        return
    buf = io.BytesIO()
    tt.save(buf)
    upm = tt["head"].unitsPerEm
    scale = 1000 / upm
    ps_name = tt["name"].getDebugName(6) or Path(path).stem.replace(" ", "")
    if not symbolic:  # a subset: the PDF spec wants a six-letter tag (ISO 32000 9.6.4)
        import hashlib
        digest = hashlib.sha256(buf.getvalue()).digest()
        ps_name = "".join(chr(65 + b % 26) for b in digest[:6]) + "+" + ps_name

    if "/Widths" not in font:  # standard-14 fonts may omit widths
        if symbolic:
            cmap = {code: g for code, g in tt["cmap"].tables[0].cmap.items()}
        else:
            best = tt.getBestCmap()
            cmap = {c: best.get(ord(bytes([c]).decode("cp1252", "ignore") or "\0")) for c in range(32, 256)}
        codes = sorted(c for c, g in cmap.items() if g and 32 <= c <= 255)
        first, last = codes[0], codes[-1]
        hmtx = tt["hmtx"].metrics
        font.FirstChar, font.LastChar = first, last
        font.Widths = pikepdf.Array([round(hmtx[cmap[c]][0] * scale) if cmap.get(c) else 0
                                     for c in range(first, last + 1)])

    head, hhea, os2 = tt["head"], tt["hhea"], tt["OS/2"]
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name("/" + ps_name),
        Flags=(4 if symbolic else 32) | (64 if tt["post"].italicAngle else 0),
        FontBBox=pikepdf.Array([round(v * scale) for v in (head.xMin, head.yMin, head.xMax, head.yMax)]),
        ItalicAngle=float(tt["post"].italicAngle),
        Ascent=round(hhea.ascent * scale),
        Descent=round(hhea.descent * scale),
        CapHeight=round(getattr(os2, "sCapHeight", hhea.ascent) * scale),
        StemV=80,
        # /Length1 (the program's decoded length) is required for FontFile2; without it Acrobat
        # says "Cannot extract the embedded font" (veraPDF doesn't check it)
        FontFile2=pdf.make_stream(buf.getvalue(), Length1=len(buf.getvalue())),
    )
    font.FontDescriptor = pdf.make_indirect(descriptor)
    font.Subtype = pikepdf.Name.TrueType
    font.BaseFont = pikepdf.Name("/" + ps_name)
    if symbolic:
        if "/Encoding" in font:
            del font["/Encoding"]
    elif not isinstance(font.get("/Encoding"), pikepdf.Dictionary) \
            and font.get("/Encoding") not in ("/WinAnsiEncoding", "/MacRomanEncoding"):
        font.Encoding = pikepdf.Name.WinAnsiEncoding
    stats["embedded_" + Path(path).stem.replace(" ", "_") + ("_substitute" if substitute else "")] += 1


def _keep_form_fonts(pdf: pikepdf.Pdf) -> int:
    """The AcroForm's default fonts (/DR) are what Acrobat draws typed and formatted field values
    with. Replaced by an embedded subset, fields show garbage ("ŁŁŁ" in the SPW and travel
    forms), and PDF/UA only asks for fonts that are drawn, so they stay as the author made them.
    One that page or appearance content also uses gets its own copy here, before that content's
    font is repaired. Returns how many were copied."""
    acro = pdf.Root.get("/AcroForm")
    fonts = (acro.get("/DR") or {}).get("/Font") if acro is not None else None
    if not isinstance(fonts, pikepdf.Dictionary):
        return 0

    def copy(obj):
        return pikepdf.Dictionary({k: v for k, v in obj.items()})

    copied = 0
    for key, font in list(fonts.items()):
        if not isinstance(font, pikepdf.Dictionary):
            continue
        new = copy(font)
        for part in ("/FontDescriptor", "/Encoding"):
            if isinstance(font.get(part), pikepdf.Dictionary):
                new[part] = pdf.make_indirect(copy(font[part]))
        fonts[key] = pdf.make_indirect(new)
        copied += 1
    return copied


def fix_fonts(pdf: pikepdf.Pdf) -> Counter:
    stats: Counter = Counter()
    seen: set = set()
    if (n := _keep_form_fonts(pdf)):
        stats["form_fonts_kept"] += n
    for page in pdf.pages:
        fonts = list(_iter_fonts(page.get("/Resources"), seen))
        for annot in page.get("/Annots", []):
            for ap in (annot.get("/AP") or {}).values():
                streams = ap.values() if isinstance(ap, pikepdf.Dictionary) and not isinstance(ap, pikepdf.Stream) else [ap]
                for s in streams:
                    if isinstance(s, pikepdf.Stream):
                        fonts += _iter_fonts(s.get("/Resources"), seen)
        for font in fonts:
            if _cid_symbol_target(font) is not None:
                continue  # handled below: re-encoded as a simple embedded Symbol font
            _embed(pdf, font, stats)
            _fix_font(pdf, font, stats)
    fix_cid_symbol_fonts(pdf, stats)
    if (n := fix_font_file_lengths(pdf)):
        stats["font_length1_added"] += n
    return stats


def fix_font_file_lengths(pdf: pikepdf.Pdf) -> int:
    """Add the required /Length1 to embedded TrueType programs (FontFile2) that lack it, ours
    or the producer's. Returns how many were fixed."""
    fixed = 0
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            ff = obj.get("/FontFile2")
            if ff is not None and "/Length1" not in ff:
                ff.Length1 = len(ff.read_bytes())
                fixed += 1
    return fixed


# ---------------------------------------------------------------------------
# Word's Symbol-font bullets as a non-embedded CID font (Type0 /Symbol, CIDFontType0, Identity-H):
# the CIDs are glyph indexes in Adobe Symbol's order (the codes 32–126, then 160–254, that the
# font has), so the bullet is CID 118. No viewer can be sure which glyph that is, and nothing maps
# it to Unicode (PDF/UA 7.21.4.1, 7.21.7; Acrobat shows "î" or nothing). It becomes a simple
# symbolic TrueType font (macOS Symbol, embedded) with the PDF's own widths, and the strings drawn
# with it are re-encoded from 2-byte CIDs to 1-byte Symbol codes. Positions don't change.
# ---------------------------------------------------------------------------
def _symbol_cid_order() -> list[int]:
    try:
        sym = TTFont("/System/Library/Fonts/Symbol.ttf")
        by_code = next(t.cmap for t in sym["cmap"].tables if t.platformID == 1)
    except Exception:
        return []
    return [c for c in range(32, 127) if c in by_code] + [c for c in range(160, 255) if c in by_code]


def _cid_symbol_target(font) -> list[int] | None:
    """CID → Symbol code for a non-embedded Type0 Symbol font whose widths confirm the order."""
    if font.get("/Subtype") != "/Type0" or font.get("/Encoding") != "/Identity-H":
        return None
    if not str(font.get("/BaseFont", "")).lstrip("/").split("+")[-1].startswith("Symbol"):
        return None
    desc = font.DescendantFonts[0]
    fd = desc.get("/FontDescriptor")
    if fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
        return None
    order = _symbol_cid_order()
    widths = _cid_widths(desc)
    if not order or not widths:
        return None
    sym = TTFont("/System/Library/Fonts/Symbol.ttf")
    by_code = next(t.cmap for t in sym["cmap"].tables if t.platformID == 1)
    scale = 1000 / sym["head"].unitsPerEm
    known = [(cid, w) for cid, w in widths.items() if cid < len(order)]
    agree = sum(1 for cid, w in known if abs(sym["hmtx"].metrics[by_code[order[cid]]][0] * scale - w) <= 2)
    return order if known and agree >= 0.9 * len(known) else None


def _reencode(holder, names: set[str], order: list[int]) -> int:
    """Rewrite strings drawn with the given font resource names from 2-byte CIDs to 1-byte codes."""
    def one(s: pikepdf.String) -> pikepdf.String:
        b = bytes(s)
        return pikepdf.String(bytes(order[(b[i] << 8) | b[i + 1]] if ((b[i] << 8) | b[i + 1]) < len(order) else 32
                                    for i in range(0, len(b) - 1, 2)))
    ops, current, stack, changed = [], None, [], 0
    for ins in pikepdf.parse_content_stream(holder):
        op = str(ins.operator)
        if op == "q":
            stack.append(current)
        elif op == "Q" and stack:
            current = stack.pop()
        elif op == "Tf":
            current = str(ins.operands[0])
        elif current in names and op in ("Tj", "'", '"', "TJ"):
            operands = list(ins.operands)
            if op == "TJ":
                operands[0] = pikepdf.Array([one(x) if isinstance(x, pikepdf.String) else x for x in operands[0]])
            else:
                operands[-1] = one(operands[-1])
            ins = pikepdf.ContentStreamInstruction(operands, ins.operator)
            changed += 1
        ops.append(ins)
    if changed:
        target = holder.Contents if isinstance(holder, pikepdf.Page) else holder
        if isinstance(holder, pikepdf.Page):
            holder.contents_coalesce()
            target = holder.Contents
        target.write(pikepdf.unparse_content_stream(ops))
    return changed


def fix_cid_symbol_fonts(pdf: pikepdf.Pdf, stats: Counter) -> None:
    replaced: dict[tuple, pikepdf.Dictionary] = {}
    orders: dict[tuple, list[int]] = {}

    def visit(holder, resources, seen):
        fonts = resources.get("/Font", {}) if isinstance(resources, pikepdf.Dictionary) else {}
        names = set()
        for name, font in list(fonts.items()):
            key = font.objgen
            if key not in orders:
                order = _cid_symbol_target(font)
                if order is None:
                    continue
                orders[key] = order
                widths = _cid_widths(font.DescendantFonts[0])
                by_code = {order[cid]: w for cid, w in widths.items() if cid < len(order)}
                first, last = min(by_code), max(by_code)
                new = pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name.Symbol,
                    FirstChar=first, LastChar=last,
                    Widths=pikepdf.Array([round(by_code.get(c, 0)) for c in range(first, last + 1)])))
                _embed(pdf, new, stats)
                new.ToUnicode = _cmap_stream(pdf, {c: SYMBOL_ENCODING[c] for c in by_code if c in SYMBOL_ENCODING},
                                             two_byte=False)
                replaced[key] = new
            fonts[name] = replaced[key]
            names.add(str(name))
        if names:
            stats["cid_symbol_strings_reencoded"] += _reencode(holder, names, orders[next(iter(orders))])
        for xobj in (resources.get("/XObject", {}) if isinstance(resources, pikepdf.Dictionary) else {}).values():
            if xobj.get("/Subtype") == "/Form" and xobj.objgen not in seen:
                seen.add(xobj.objgen)
                visit(xobj, xobj.get("/Resources"), seen)

    seen: set = set()
    for page in pdf.pages:
        visit(page, page.get("/Resources"), seen)
    if replaced:
        stats["cid_symbol_fonts_embedded"] += len(replaced)
