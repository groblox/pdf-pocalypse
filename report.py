"""Per-document remediation report (and a batch summary), written as accessible HTML and
printed to a tagged PDF with headless Chrome.

The report says what was done to the document, how it validates, what Haiku's content
review found, and what a diff of the original against the remediated file shows: metadata,
text (words added or lost, unreadable characters), and page-by-page visual change."""
from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pikepdf
import pymupdf
from PIL import Image

import ai_client
import contrast_fix
import drawing_pages
import font_fixes
import image_stage
import progress
import struct_fixes


def _find_chrome() -> str:
    for env_key in ("A11Y_CHROME", "CHROME_PATH"):
        if val := os.environ.get(env_key):
            return val
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    for cmd in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        if found := shutil.which(cmd):
            return found
    return candidates[0]


CHROME = _find_chrome()
DIFF_DPI = 40
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]{2,}")

WCAG_NAMES = {"1.1.1": "Non-text content (alt text)", "1.3.1": "Info and relationships (tags, tables)",
              "1.3.2": "Meaningful sequence (reading order)", "1.4.3": "Contrast", "2.4.2": "Page titled",
              "2.4.3": "Focus order", "3.1.1": "Language of page", "3.1.2": "Language of parts",
              "4.1.2": "Name, role, value (forms)"}
# Chrome tags inline emphasis with HTML names PDF/UA doesn't know; map them to standard types.
ROLE_MAP = {"Strong": "Span", "Em": "Span", "B": "Span", "I": "Span", "Mark": "Span", "Sub": "Span",
            "Sup": "Span", "Small": "Span", "Title": "H1", "Aside": "Div", "FENote": "Note"}

FONT_LABELS = {
    "tounicode": "Rebuilt the Unicode map of {n} font(s), so their text reads as words instead of symbols",
    "tounicode_filled": "Completed the partial Unicode map of {n} font(s)",
    "embedded": "Embedded {n} font(s) the original left out",
    "substituted": "Embedded a similar system font for {n} font(s) the original left out and that have no "
                   "exact match; character positions are unchanged but letter shapes may differ slightly "
                   "(see Appearance)",
    "cidset_removed": "Removed {n} stale font subset table(s)",
    "encoding": "Declared a standard text encoding for {n} font(s)",
    "cidtogidmap_added": "Made {n} font glyph map(s) explicit",
    "pua": "Mapped symbol characters (bullets, check marks) in {n} font(s) to standard Unicode",
    "partial_tounicode_unresolved": "{n} font(s) still have characters with no Unicode meaning (needs review)",
}
STRUCT_LABELS = {
    "forms_split": "Restructured {n} form field(s) so assistive technology finds each field",
    "forms_named": "Gave {n} form field(s) a readable name",
    "fields_labelled": "Labelled {n} form field(s) from the text next to them on their page (shown on hover and read by screen readers)",
    "tooltips_matched_to_label": "Set {n} form field tooltip(s) to the label printed next to the field (read by AI from the page), so screen readers and speech input use the words on the page",
    "fields_placed_by_label": "Moved {n} form field(s) to follow their printed label in reading order (the tagger had put them after the page's text)",
    "label_paragraphs_split": "Split {n} run-together label line(s) into their own paragraphs, so each field follows its label",
    "radio_buttons_named": "Gave {n} radio button(s) their own tooltip, the question and the option printed beside the button, so each choice is announced",
    "field_guesses_dropped": "Removed {n} form field label(s) the tagger guessed from unrelated text; no label was found next to them (needs review)",
    "lists_numbered": "Declared the numbering style of {n} list(s)",
    "nested_art_retagged": "Corrected {n} nested article section(s)",
    "annotations_synced": "Matched {n} annotation description(s) to their tags",
    "bookmarks": "Added {n} bookmarks built from the document's headings",
    "structure_destinations": "Pointed {n} link/bookmark target(s) at tagged content",
    "xobject_tags_demoted": "Marked {n} repeated or hidden graphic(s) as background so their tags don't conflict",
    "text_strings_cleaned": "Replaced private-use symbols in {n} bookmark/alt/metadata text(s) with standard characters",
    "placeholder_figures_demoted": "Marked {n} decorative image(s) as background so screen readers skip them",
    "popups_removed": "Removed {n} comment popup window(s), which only repeat their comment's text (viewers open a default window instead)",
    "markups_described": "Described {n} review markup(s) (highlights, drawn marks, stamps) that had no text for screen readers",
    "signatures_removed": "Removed {n} digital signature(s), which no longer matched the changed file (the manifest allows this; the signed original is the record copy)",
}


# ---------------------------------------------------------------------------
# Diff checker
# ---------------------------------------------------------------------------
def _unreadable(text: str) -> int:
    return sum(1 for ch in text if ch == "�" or (ord(ch) < 32 and ch not in "\n\r\t")
               or 0xE000 <= ord(ch) <= 0xF8FF)


def _doc_facts(path: Path) -> dict:
    with pikepdf.open(path) as pdf:
        root = pdf.Root.get("/StructTreeRoot")
        elements = figures = with_alt = 0
        if root is not None:
            stack, seen = [root], set()
            while stack:
                node = stack.pop()
                k = node.get("/K")
                for kid in (list(k) if isinstance(k, pikepdf.Array) else [k]):
                    if isinstance(kid, pikepdf.Dictionary) and "/S" in kid and kid.objgen not in seen:
                        seen.add(kid.objgen)
                        elements += 1
                        if kid.S == "/Figure":
                            figures += 1
                            with_alt += "/Alt" in kid
                        stack.append(kid)
        no_unicode, not_embedded, seen_fonts = 0, 0, set()
        for page in pdf.pages:
            for f in font_fixes._iter_fonts(page.get("/Resources"), seen_fonts):
                if f.get("/Subtype") == "/Type3":
                    continue
                desc = f.DescendantFonts[0] if f.get("/Subtype") == "/Type0" else f
                fd = desc.get("/FontDescriptor")
                if fd is None or not any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    not_embedded += 1
                if "/ToUnicode" not in f and f.get("/Subtype") == "/Type0":
                    no_unicode += 1
        outlines = pdf.Root.get("/Outlines")
        bookmarks = int(outlines.get("/Count", 0)) if outlines is not None else 0
        attached = len(pdf.attachments)
        facts = {
            "Title": str(pdf.docinfo.get("/Title", "")) or "—",
            "Language": str(pdf.Root.get("/Lang", "")) or "—",
            "Tagged for accessibility": "yes" if root is not None else "no",
            "Structure elements": f"{elements:,}",
            "Figures with alt text": f"{with_alt:,} of {figures:,}" if figures else "—",
            "Bookmarks": f"{abs(bookmarks):,}" if bookmarks else "none",
            "Attached files": str(attached) if attached else "none",
            "Fonts not embedded": str(not_embedded),
            "Composite fonts without a Unicode map": str(no_unicode),
            "Pages": str(len(pdf.pages)),
            "PDF version": pdf.pdf_version,
            "File size": f"{path.stat().st_size / 1e6:.1f} MB",
        }
    return facts


def _thumb(img: Image.Image, width: int) -> str:
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def diff(original: Path, remediated: Path, max_visual: int = 6, max_pages: int = 400) -> dict:
    """Original vs remediated. Documents longer than max_pages are sampled evenly."""
    a, b = pymupdf.open(original), pymupdf.open(remediated)
    pages = []
    n = min(a.page_count, b.page_count)
    step = max(1, -(-n // max_pages))
    for i in range(0, n, step):
        ta, tb = a[i].get_text(), b[i].get_text()
        wa, wb = set(w.lower() for w in WORD.findall(ta)), set(w.lower() for w in WORD.findall(tb))
        union = wa | wb
        pa, pb = a[i].get_pixmap(dpi=DIFF_DPI), b[i].get_pixmap(dpi=DIFF_DPI)
        changed = 0.0
        if (pa.w, pa.h) == (pb.w, pb.h):
            xa = np.frombuffer(pa.samples, np.uint8).reshape(pa.h, pa.w, pa.n)[..., :3].astype(int)
            xb = np.frombuffer(pb.samples, np.uint8).reshape(pb.h, pb.w, pb.n)[..., :3].astype(int)
            changed = float((np.abs(xa - xb).max(axis=2) > 40).mean())
        else:
            changed = 1.0
        pages.append({"page": i + 1, "similarity": len(wa & wb) / len(union) if union else 1.0,
                      "added": len(wb - wa), "removed": len(wa - wb),
                      "unreadable_before": _unreadable(ta), "unreadable_after": _unreadable(tb),
                      "chars_before": len(ta), "chars_after": len(tb), "visual_change": changed,
                      "snippet_before": " ".join(ta.split())[:180], "snippet_after": " ".join(tb.split())[:180]})

    words_a = Counter(w.lower() for p in a for w in WORD.findall(p.get_text()))
    words_b = Counter(w.lower() for p in b for w in WORD.findall(p.get_text()))
    visuals = []
    for p in sorted(pages, key=lambda p: -p["visual_change"])[:max_visual]:
        if p["visual_change"] < 0.001:
            break
        i = p["page"] - 1
        pa, pb = a[i].get_pixmap(dpi=60), b[i].get_pixmap(dpi=60)
        ia = Image.frombytes("RGB" if pa.n == 3 else "RGBA", (pa.w, pa.h), pa.samples)
        ib = Image.frombytes("RGB" if pb.n == 3 else "RGBA", (pb.w, pb.h), pb.samples)
        overlay = None
        if ia.size == ib.size:
            xa, xb = np.asarray(ia.convert("RGB")).astype(int), np.asarray(ib.convert("RGB")).astype(int)
            mask = np.abs(xa - xb).max(axis=2) > 40
            grey = np.asarray(ib.convert("L").convert("RGB")).copy()
            grey = (grey * 0.35 + 165).astype(np.uint8)
            grey[mask] = (200, 0, 0)
            overlay = _thumb(Image.fromarray(grey), 260)
        visuals.append({"page": p["page"], "change": p["visual_change"],
                        "before": _thumb(ia, 260), "after": _thumb(ib, 260), "overlay": overlay})
    result = {
        "facts_before": _doc_facts(original), "facts_after": _doc_facts(remediated),
        "pages": pages, "visuals": visuals,
        "words_only_before": [w for w, _ in (words_a - words_b).most_common(40)],
        "words_only_after": [w for w, _ in (words_b - words_a).most_common(40)],
        "distinct_before": len(words_a), "distinct_after": len(words_b),
        "kept": len(set(words_a) & set(words_b)) / len(words_a) if words_a else 1.0,
        "unreadable_before": sum(p["unreadable_before"] for p in pages),
        "unreadable_after": sum(p["unreadable_after"] for p in pages),
        "chars_before": sum(p["chars_before"] for p in pages),
        "chars_after": sum(p["chars_after"] for p in pages),
        "pages_visually_changed": sum(p["visual_change"] >= 0.001 for p in pages),
        "sampled_every": step,
    }
    a.close()
    b.close()
    return result


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
CSS = """
.notice { border: 1.5pt solid #1e4d2b; border-radius: 4pt; padding: 6pt 10pt; margin: 10pt 0; font-size: 10pt; }
.notice strong { font-size: inherit; }
@page { size: letter; margin: 0.6in 0.6in 0.7in; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 10pt; color: #1a1a1a; line-height: 1.4; }
h1 { font-size: 18pt; margin: 0 0 4pt; color: #1e4d2b; }
h2 { font-size: 13pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid #1e4d2b; color: #1e4d2b; break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; break-after: avoid; }
p.meta { color: #555; margin: 0 0 10pt; }
.outcome { border: 1.5pt solid #1e4d2b; border-radius: 4pt; padding: 8pt 10pt; margin: 8pt 0; }
.outcome.fail { border-color: #a12a2a; } .outcome.review { border-color: #8a6100; }
.outcome strong { font-size: 12pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 8pt; }
caption { text-align: left; font-weight: 600; padding-bottom: 3pt; }
th, td { border: 0.5pt solid #bbb; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #eef3ef; }
tr { break-inside: avoid; }
td.num { text-align: right; white-space: nowrap; }
.ok { color: #1e6b33; font-weight: 600; } .bad { color: #a12a2a; font-weight: 600; } .warn { color: #8a6100; font-weight: 600; }
ul { margin: 2pt 0 6pt 16pt; padding: 0; } li { margin: 1pt 0; }
.thumbs { display: flex; gap: 8pt; break-inside: avoid; margin: 4pt 0 10pt; }
.thumbs div { flex: 1; font-size: 8.5pt; color: #444; }
.thumbs img { width: 100%; border: 0.5pt solid #999; }
img.small { max-width: 110px; max-height: 90px; border: 0.5pt solid #ccc; }
.snip { font-family: Menlo, monospace; font-size: 8pt; color: #333; word-break: break-word; }
.note { color: #555; font-size: 9pt; }
"""


def _e(x) -> str:
    return html.escape(str(x))


def _cls(ok) -> str:
    return "ok" if ok else "bad"


def _labels(stats: dict, table: dict) -> list[str]:
    out = []
    for key, n in stats.items():
        if not n:
            continue
        label = next((v for k, v in table.items() if key == k or key.startswith(k)), None)
        if label:
            out.append(label.format(n=n))
    return out


def _font_lines(stats: dict) -> list[str]:
    grouped: Counter = Counter()
    for key, n in stats.items():
        if key.startswith("tounicode_filled"):
            grouped["tounicode_filled"] += n
        elif key.startswith("tounicode"):
            grouped["tounicode"] += n
        elif key.startswith("embedded") and key.endswith("_substitute"):
            grouped["substituted"] += n
        elif key.startswith("embedded"):
            grouped["embedded"] += n
        elif "pua" in key:
            grouped["pua"] += n
        elif "encoding" in key:
            grouped["encoding"] += n
        else:
            grouped[key] += n
    return _labels(grouped, FONT_LABELS)


def _image_rows(classes: dict, overrides: dict, pdf: Path, limit: int = 250) -> list[str]:
    rows = []
    with pymupdf.open(pdf) as doc:
        # minor units (slivers, blank tiles, fragments of a drawing) are part of a larger picture
        for key, c in [kc for kc in classes.items() if kc[1].get("type") != "minor"][:limit]:
            try:
                if c.get("type") == "composite":
                    clip = pymupdf.Rect(c["rect"]) * doc[c["page"]].rotation_matrix  # renders are rotated
                    pix = doc[c["page"]].get_pixmap(clip=clip, dpi=max(18, int(min(60, 300 * 72 / max(clip.width, clip.height)))))
                    img = Image.frombytes("RGB" if pix.n == 3 else "RGBA", (pix.w, pix.h), pix.samples)
                else:
                    xref = next((i["xref"] for i in doc[c["page"]].get_image_info(xrefs=True)
                                 if i.get("xref") and image_stage.pdf_a11y_hash(doc, i["xref"])
                                 and image_stage.pdf_a11y_hash(doc, i["xref"])[0] == key), None)
                    img = Image.open(io.BytesIO(image_stage.pdf_a11y_hash(doc, xref)[1])) if xref else None
                thumb = f'<img class="small" src="{_thumb(img, 220)}" alt="Thumbnail: {_e(c.get("alt") or c.get("kind"))}">' \
                    if img else "—"
            except Exception:
                thumb = "—"
            kind = c.get("kind", "?") + (" (whole sheet)" if c.get("sheet") else
                                         " (tiled)" if c.get("type") == "composite" else "")
            if c.get("kind") == "error":
                handling = '<span class="bad">description failed — needs alt text</span>'
            elif c.get("kind") in image_stage.TEXT_KINDS and all(
                    overrides.get(h) == "DECORATIVE" for h in ([key] if c.get("type") != "composite" else [t[0] for t in c["tiles"]])):
                handling = "Text extracted with OCR; image marked as background"
            elif c.get("kind") in ("logo", "decorative") and overrides.get(key, "") == "DECORATIVE":
                handling = "Marked as background (repeated/decorative)"
            elif c.get("drawing"):
                handling = "One Figure for the whole page" + (
                    "; marked complex" if c["drawing"].get("complex") and not c.get("reviewed") else "")
            else:
                handling = "Alt text"
            pages = sorted({p + 1 for p, _ in c.get("placements", [])})
            where = ", ".join(map(str, pages[:6])) + ("…" if len(pages) > 6 else "")
            # the alt text the Figure carries: a reviewed correction, else a drawing page's own
            alt = drawing_pages.alt_for(c) if c.get("drawing") else c.get("alt", "")
            rows.append(f"<tr><td>{thumb}</td><td>{_e(where)}</td><td>{_e(kind)}</td><td>{handling}</td>"
                        f"<td>{_e(alt)}</td></tr>")
    return rows


CHECK_NAMES = {"verapdf_ua1": "veraPDF PDF/UA-1", "verapdf_wtpdf": "veraPDF WTPDF 1.0 Accessibility",
               "pdf_syntax": "PDF syntax", "mcid_references": "Tags point at real content (PAC: MCID entries)",
               "scripts_kept": "Form scripts (formats, calculations) unchanged",
               "tagged": "Tagged PDF", "language": "Document language",
               "display_doc_title": "Title shown in title bar", "title": "Title", "tab_order": "Tab order follows structure",
               "bookmarks": "Bookmarks present for headings (PAC)",
               "heading_hierarchy": "Heading levels do not skip (WCAG 1.3.1 / PDF/UA 7.4.2)",
               "not_encrypted": "Not encrypted", "attachments": "Attached files embedded", "page_count": "Page count",
               "signatures": "Digital signatures", "xfa": "Ordinary PDF form (not dynamic XFA)",
               "contrast": "Text contrast (4.5:1, 3:1 for large text)",
               "text_retained": "Text kept from the input", "renders": "Renders", "file_size": "File size",
               "appearance": "Looks the same as the original", "numbers_retained": "Numbers kept from the original",
               "alt_text_sane": "Alt text sanity check"}


def build_html(name: str, meta: dict, stats: dict, compat: dict | None, intent: dict | None,
               d: dict, classes: dict, overrides: dict, image_pdf: Path, a11y_findings: dict) -> str:
    title = meta["title"]
    passed = bool(compat and compat.get("passed"))
    needs_review = bool(intent and intent.get("verdict") != "pass")
    outcome_cls = "fail" if not passed else ("review" if needs_review else "")
    base = (compat or {}).get("baseline", {})
    checks = (compat or {}).get("checks", {})
    failing = [CHECK_NAMES.get(k, k) for k, c in checks.items() if not c["ok"]]
    validators = checks.get("verapdf_ua1", {}).get("ok") and checks.get("verapdf_wtpdf", {}).get("ok")
    if passed:
        outcome = "Passes PDF/UA-1 and WTPDF 1.0 validation and every check before publishing"
    elif not compat:
        outcome = "Not checked yet in its current form, so not published: run the pipeline again to check it"
    else:
        outcome = (("Passes PDF/UA-1 and WTPDF 1.0 validation, but held" if validators else "Held")
                   + f" in “needs review”, not published. Failed: {', '.join(failing)}")
    if passed and needs_review:
        outcome += "; content review flagged items for a person to check"
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Accessibility report: {_e(title)}</title><style>{CSS}</style></head><body>
<h1>Accessibility report</h1>
<p class="meta">{_e(title)}<br>File: {_e(name)} · {date.today():%B %-d, %Y} · Standards: PDF/UA-1 (ISO 14289-1), WTPDF 1.0, WCAG 2.2</p>
<div class="outcome {outcome_cls}" role="note"><strong>{_e(outcome)}.</strong><br>
veraPDF PDF/UA-1 failed checks: {_count(base.get('ua1_failed_checks_before'))} in the original → {_count(base.get('ua1_failed_checks_after'))} after remediation.
Text kept from the original: {d['kept']:.1%} of distinct words. Unreadable characters: {d['unreadable_before']:,} → {d['unreadable_after']:,}.
Pages that look different: {d['pages_visually_changed']} of {len(d['pages'])}.</div>"""]

    # What was done
    done = []
    comp = stats.get("companion")  # drawing-set mode
    if comp:
        done.append(f"Treated as a large drawing set: instead of OCR, tagged each of its {comp['sheets']} sheets "
                    f"as one figure whose alt text names the sheet, describes it and says that its full detail "
                    f"does not translate to text ({comp['described']} described"
                    + (f", {comp['illegible']} with an illegible title block" if comp.get("illegible") else "")
                    + (f", <span class='bad'>{comp['failed']} could not be described</span>" if comp.get("failed") else "")
                    + "); page 1's alt text also summarises the set. Added a bookmark per sheet, title, language "
                    "and the PDF/UA identifier. The drawings themselves were not altered.")
    if stats.get("secured_original"):
        done.append("The original was secured with a password restricting editing or copying. This copy has no "
                    "restrictions: they can stop screen readers from reading it. Re-secure it if needed, keeping "
                    "content accessibility allowed")
    if stats.get("xfa") == "static_removed":
        done.append("Removed the form's XFA (LiveCycle) layer, so every viewer shows the tagged form fields")
    if stats.get("inline_images_converted"):
        done.append(f"Turned {stats['inline_images_converted']} inline image(s) into ordinary images so they could be "
                    "described and tagged")
    if stats.get("pdfa"):
        done.append(_e(stats["pdfa"]))
    if stats.get("ocr"):
        done.append(f"Ran OCR on the scanned pages ({stats['ocr']}) to add a text layer")
    done += _font_lines(stats.get("font_fixes", {}))
    img = stats.get("image_stage", {})
    ai_label = stats.get("ai_model") or ai_client.describe_role("primary")
    eval_label = (intent or {}).get("_label") or ai_client.describe_role("eval")
    if img:
        n_alt = sum(v for k, v in img.items() if "→" not in k and k not in ("errors", "unmatched"))
        n_text = sum(v for k, v in img.items() if k.endswith("→text"))
        n_bg = sum(v for k, v in img.items() if k.endswith("→artifact"))
        if n_alt:
            done.append(f"Described {n_alt} image(s) with alt text written from the image and its page context ({_e(ai_label)})")
        if n_text:
            done.append(f"Turned {n_text} image(s) of tables or text into real text with OCR "
                        f"({stats.get('image_ocr_pages', 0)} page(s))")
        if n_bg:
            done.append(f"Marked {n_bg} repeated logo/decorative image(s) as background")
        if img.get("errors"):
            done.append(f'<span class="bad">{img["errors"]} image description(s) failed and need alt text</span>')
    if not comp:
        done.append("Tagged the document structure (headings, paragraphs, lists, tables, figures) and set "
                    "reading order, tab order, title, language and the PDF/UA identifier (pdf-a11y 2.10.0)")
    done += _labels(stats.get("structure_fixes", {}), STRUCT_LABELS)
    con = stats.get("contrast") or {}
    if con.get("contrast_fixed"):
        done.append(f"Raised the contrast of {con['contrast_fixed']} text span(s) by darkening "
                    f"{con.get('colours_changed', 0)} colour(s), aiming for {contrast_fix.NORMAL + contrast_fix.MARGIN:.2f}:1 "
                    f"({contrast_fix.LARGE + contrast_fix.MARGIN:.2f}:1 for large text): WCAG asks 4.5:1, and the margin "
                    "covers checkers that measure colours a little differently")
    if con.get("contrast_failing"):
        done.append(f'<span class="bad">{con["contrast_failing"]} text span(s) still below 4.5:1: '
                    + _e("; ".join(con.get("contrast_examples", []))) + "</span>")
    drawn = stats.get("structure_fixes", {}).get("drawing_pages")
    if drawn:
        done.append(f"Tagged each drawing (page{'s' if len(drawn['pages']) > 1 else ''} "
                    f"{', '.join(map(str, drawn['pages']))}) as one Figure whose alt text gives its content, "
                    f"condensed ({_e(ai_label)}, checked against the drawings)"
                    + (f"; the alt text of the complex one{'s' if len(drawn['complex']) > 1 else ''} (page"
                       f"{'s' if len(drawn['complex']) > 1 else ''} {', '.join(map(str, drawn['complex']))}) says "
                       "that its full detail does not translate to text" if drawn["complex"] else ""))
    attached = stats.get("attachments")
    if attached and attached.get("files"):
        files = "; ".join(f"<strong>{_e(f['name'])}</strong> ({_e(f['description'])})"
                          + (" <span class='bad'>— not tagged, so a screen reader can't read it</span>"
                             if f.get("tagged") is False else "") for f in attached["files"])
        done.append(f"Attached {len(attached['files'])} file(s) to the PDF, listed in the viewer's Attachments panel"
                    + (" (each attached PDF also has a bookmark that opens it)" if attached.get("bookmarks") else "")
                    + f": {files}")
    if attached and attached.get("not_found"):
        done.append(f"<span class='bad'>Listed in the manifest but not found, so not attached: "
                    f"{_e(', '.join(attached['not_found']))}</span>")
    parts.append("<h2>What was done</h2><ul>" + "".join(f"<li>{x}</li>" for x in done) + "</ul>")
    if a11y_findings:
        rows = "".join(f"<tr><td>{_e(k)} {_e(WCAG_NAMES.get(k, ''))}</td><td class='num'>{v}</td></tr>"
                       for k, v in sorted(a11y_findings.items()))
        parts.append("<table><caption>pdf-a11y fixes by WCAG / PDF/UA criterion</caption>"
                     "<thead><tr><th scope='col'>Criterion</th><th scope='col'>Fixes</th></tr></thead>"
                     f"<tbody>{rows}</tbody></table>")

    # Validation
    parts.append("<h2>Validation</h2>")
    if compat:
        rows = []
        names = CHECK_NAMES
        for key, c in compat["checks"].items():
            detail = c.get("detail") or "—"  # Chrome drops empty cells from the tags, unbalancing rows
            if c.get("failed_rules"):
                detail = "; ".join(f"{r['rule']}: {r['count']} ({r['description'][:90]})" for r in c["failed_rules"])
            rows.append(f"<tr><td>{_e(names.get(key, key))}</td><td class='{_cls(c['ok'])}'>{'Pass' if c['ok'] else 'Fail'}</td>"
                        f"<td>{_e(detail)}</td></tr>")
        parts.append("<table><caption>Automated checks on the remediated file</caption><thead><tr><th scope='col'>Check</th>"
                     "<th scope='col'>Result</th><th scope='col'>Detail</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    # Diff
    parts.append("<h2>Original vs. remediated</h2>")
    rows = "".join(f"<tr><th scope='row'>{_e(k)}</th><td>{_e(d['facts_before'].get(k, '—'))}</td><td>{_e(v)}</td></tr>"
                   for k, v in d["facts_after"].items())
    parts.append("<table><caption>Document properties</caption><thead><tr><th scope='col'>Property</th>"
                 "<th scope='col'>Original</th><th scope='col'>Remediated</th></tr></thead><tbody>" + rows + "</tbody></table>")
    parts.append(f"<h3>Text</h3><p>{d['distinct_before']:,} distinct words in the original, {d['distinct_after']:,} after; "
                 f"{d['kept']:.1%} of the original's words are still present. Unreadable characters (symbols with no "
                 f"text meaning) went from {d['unreadable_before']:,} to {d['unreadable_after']:,}.</p>")
    if d["words_only_after"]:
        parts.append("<p><strong>Words that are only readable after remediation</strong> (from OCR or repaired fonts), "
                     f"most frequent first:</p><p class='snip'>{_e(', '.join(d['words_only_after'][:40]))}</p>")
    if d["words_only_before"]:
        parts.append("<p><strong>Words found only in the original</strong> — usually garbled text that now reads "
                     f"correctly; check any real word here:</p><p class='snip'>{_e(', '.join(d['words_only_before'][:40]))}</p>")
    changed = sorted((p for p in d["pages"] if p["similarity"] < 0.9 or p["unreadable_before"] != p["unreadable_after"]),
                     key=lambda p: p["similarity"])[:10]
    if changed:
        rows = "".join(f"<tr><td class='num'>{p['page']}</td><td class='num'>{p['similarity']:.0%}</td>"
                       f"<td class='num'>{p['unreadable_before']} → {p['unreadable_after']}</td>"
                       f"<td class='snip'>{_e(p['snippet_before'])}</td><td class='snip'>{_e(p['snippet_after'])}</td></tr>"
                       for p in changed)
        parts.append("<table><caption>Pages whose text changed most</caption><thead><tr><th scope='col'>Page</th>"
                     "<th scope='col'>Word overlap</th><th scope='col'>Unreadable chars</th><th scope='col'>Original text starts</th>"
                     "<th scope='col'>Remediated text starts</th></tr></thead><tbody>" + rows + "</tbody></table>")
    sampled = f" (every {d['sampled_every']} pages checked)" if d.get("sampled_every", 1) > 1 else ""
    parts.append(f"<h3>Appearance</h3><p>{d['pages_visually_changed']} of {len(d['pages'])} pages{sampled} differ visibly "
                 f"(more than 0.1% of pixels at {DIFF_DPI} dpi). Remediation is meant to leave the look unchanged; "
                 "small differences usually come from embedding a missing font.</p>")
    for v in d["visuals"]:
        overlay = (f"<div><img src='{v['overlay']}' alt='Page {v['page']} with changed pixels highlighted in red'>"
                   "Changes in red</div>") if v["overlay"] else ""
        parts.append(f"<h3>Page {v['page']} — {v['change']:.2%} of pixels changed</h3><div class='thumbs'>"
                     f"<div><img src='{v['before']}' alt='Page {v['page']} of the original'>Original</div>"
                     f"<div><img src='{v['after']}' alt='Page {v['page']} after remediation'>Remediated</div>{overlay}</div>")

    # Images
    if classes:
        rows = _image_rows(classes, overrides, image_pdf)
        more = f"<p class='note'>Showing the first {len(rows)} of {len(classes)}.</p>" if len(classes) > len(rows) else ""
        parts.append("<h2>Images</h2><table><caption>How each image was handled</caption><thead><tr>"
                     "<th scope='col'>Image</th><th scope='col'>Page(s)</th><th scope='col'>Kind</th>"
                     "<th scope='col'>Handling</th><th scope='col'>Alt text</th></tr></thead><tbody>"
                     + "".join(rows) + "</tbody></table>" + more)

    # Content review
    if intent:
        parts.append(f"<h2>Content review ({_e(eval_label)})</h2>")
        parts.append(f"<p>Verdict: <strong>{_e(intent.get('verdict', '—'))}</strong> · "
                     f"Matches how the site presents it: {_e(intent.get('intent_match', '—'))} · "
                     f"Alt text: {_e(intent.get('alt_text_quality', '—'))}</p>")
        if intent.get("document_purpose"):
            parts.append(f"<p><strong>What the document is:</strong> {_e(intent['document_purpose'])}</p>")
        if intent.get("purpose_on_site"):
            parts.append(f"<p><strong>How the site presents it:</strong> {_e(intent['purpose_on_site'])}</p>")
        for src, head in (("remediation", "Accessibility follow-up"), ("source_document", "For the document's author"),
                          ("site_listing", "For the web page")):
            items = [i["detail"] for i in intent.get("issues", []) if isinstance(i, dict) and i.get("source") == src]
            if items:
                parts.append(f"<h3>{head}</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in items) + "</ul>")

    models_note = f"AI: {ai_label}" + (f", Evals: {eval_label}" if eval_label != ai_label else "")
    parts.append(f"""<h2>Still needs a person</h2><ul>
<li>Read it with a screen reader (VoiceOver or NVDA) and confirm the reading order makes sense, especially multi-column pages and tables.</li>
<li>Check the alt text above against the images; put any correction in the document's manifest entry (alt_text:) and rerun.</li>
<li>For data tables, confirm header rows and columns are marked; OCR'd tables may need their structure checked.</li>
<li>Optional: run PAC 2024 or Acrobat's Accessibility Check for a second opinion.</li></ul>
<p class="note">Generated by the accessible document remediation pipeline (pdf-a11y 2.10.0, veraPDF 1.30.1, {_e(models_note)}).</p>
</body></html>""")
    return "\n".join(parts)


def _count(n) -> str:
    return "not measured" if n is None else f"{n:,}"


def to_pdf(html_path: Path, pdf_path: Path, title: str) -> None:
    """Print with headless Chrome (tagged PDF), then add what PDF/UA also wants: XMP title +
    PDF/UA identifier, and the title shown in the window."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # A throwaway profile per print: sharing the user's open Chrome profile can hang headless
    # Chrome until the timeout. One retry covers the occasional stall; a print takes ~2 s, so
    # a minute is plenty (300 s once left a run silent for five minutes).
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory(prefix="chrome-print-") as profile:
            try:
                subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                                "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile}",
                                "--generate-pdf-document-outline", f"--print-to-pdf={pdf_path}",
                                html_path.resolve().as_uri()], check=True, capture_output=True, timeout=60)
                break
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    raise
                progress.warn("Chrome stalled printing the report for a minute; retrying")
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata() as xmp:
            xmp["dc:title"] = title
            xmp.register_xml_namespace("http://www.aiim.org/pdfua/ns/id/", "pdfuaid")
            xmp["pdfuaid:part"] = "1"
        pdf.docinfo["/Title"] = title
        pdf.Root.ViewerPreferences = pikepdf.Dictionary(DisplayDocTitle=True)
        root = pdf.Root.get("/StructTreeRoot")
        if root is not None:
            role_map = root.get("/RoleMap") or pikepdf.Dictionary()
            for k, v in ROLE_MAP.items():
                if f"/{k}" not in role_map:
                    role_map[f"/{k}"] = pikepdf.Name("/" + v)
            root.RoleMap = role_map
            # Chrome's NonStruct is transparent, so text in one under Document counts as Document
            # content (WTPDF). Make it a real Div under grouping elements, a Span inside text ones.
            for parent, el in list(struct_fixes._walk(root)):
                if el.get("/S") == "/NonStruct":
                    grouping = str(parent.get("/S", "/Document")) in ("/Document", "/Div", "/Sect", "/Art", "/Part", "/BlockQuote")
                    el.S = pikepdf.Name.Div if grouping else pikepdf.Name.Span
            struct_fixes.artifact_stray_content(pdf)
            struct_fixes.regroup_chrome_structure(pdf)
            # Chrome's link annotations carry no description (PDF/UA 7.18.5, 7.18.1)
            for page in pdf.pages:
                for annot in page.get("/Annots", []):
                    if annot.get("/Subtype") == "/Link" and not str(annot.get("/Contents", "")).strip():
                        uri = str((annot.get("/A") or {}).get("/URI", ""))
                        annot.Contents = pikepdf.String(f"Link to {uri}" if uri else "Link")
            struct_fixes.sync_annotation_alt(pdf)
            struct_fixes.apply_wtpdf(pdf, pdf_path)
        pdf.save(pdf_path)


def write_report(name: str, meta: dict, original: Path, remediated: Path, html_path: Path, pdf_path: Path,
                 stats: dict, compat: dict | None, intent: dict | None, classes: dict, overrides: dict,
                 image_pdf: Path, a11y_report: Path | None) -> dict:
    # a drawing set's sheets are heavy to render (a 207-sheet CAD set took 3 minutes at every
    # sheet): 20 evenly spaced ones, as the publish check's appearance comparison does
    d = diff(original, remediated, max_pages=20) if stats.get("companion") else diff(original, remediated)
    findings: Counter = Counter()
    if a11y_report and a11y_report.exists():
        for f in json.loads(a11y_report.read_text()).get("findings", []):
            if f.get("severity") == "fixed" and f.get("wcag") not in ("validation",):
                findings[f["wcag"]] += 1
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_html(name, meta, stats, compat, intent, d, classes, overrides, image_pdf, dict(findings)))
    to_pdf(html_path, pdf_path, f"Accessibility report: {meta['title']}")
    return {k: v for k, v in d.items() if k not in ("pages", "visuals")}


def write_docx_report(name: str, meta: dict, result: dict, compat: dict, html_path: Path, pdf_path: Path) -> None:
    """Report for a Word document: what was fixed, what the author has to change, the checks."""
    fixed = [f for f in result["findings"] if f["severity"] == "fixed"]
    flags = [f for f in result["findings"] if f["severity"] == "flag"]
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    held = not compat["passed"]
    outcome = ("Held for review: a check failed, so this file was not published." if held else
               "Remediated and published. The items for the author below need a person to fix in Word.")

    def items(rows):
        return "<ul>" + "".join(f"<li><strong>{_e(r['rule'])}:</strong> {_e(r['detail'])}</li>" for r in rows) + "</ul>"

    names = {"docx_problems": "No unresolved problems", "text_unchanged": "Text unchanged",
             "alt_text_sane": "Alt text sanity check"}
    checks = "".join(f"<tr><td>{_e(names.get(k, k))}</td><td class='{_cls(c['ok'])}'>{'Pass' if c['ok'] else 'Fail'}</td>"
                     f"<td>{_e(c.get('detail') or '—')}</td></tr>" for k, c in compat["checks"].items())
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Accessibility report: {_e(meta['title'])}</title><style>{CSS}</style></head><body>
<h1>Accessibility report</h1><p class="meta">{_e(meta['title'])}<br>File: {_e(name)} · {date.today():%B %-d, %Y}
· Checked against Microsoft's guidance for accessible Word documents</p>
<div class="outcome{' fail' if held else (' review' if flags else '')}" role="note"><strong>{_e(outcome)}</strong></div>
<h2>Fixed automatically</h2>{items(fixed) if fixed else '<p>Nothing needed fixing automatically.</p>'}
<h2>For the author</h2>{items(flags) if flags else '<p>No items.</p>'}
<p class="note">These need the author's judgement (which text is a heading, how a table should be split, what a
link should say). Fix them in Word, then run Review › Check Accessibility before posting.</p>
{'<h2>Problems</h2>' + items(errors) if errors else ''}
<h2>Checks</h2><table><caption>Checks before publishing</caption><thead><tr><th scope="col">Check</th>
<th scope="col">Result</th><th scope="col">Detail</th></tr></thead><tbody>{checks}</tbody></table>
<p class="note">Alt text was written by {_e(ai_client.describe_role('primary'))} from each picture and the text around it. Generated by the
accessible document remediation pipeline.</p></body></html>""")
    to_pdf(html_path, pdf_path, f"Accessibility report: {meta['title']}")
