"""Builds a folder of made-up edge cases (secured, password, empty, web page saved as .pdf, rotated
form, crop box offset, blank page, digitally signed, XFA, markups, PDF/A, Spanish, inline image,
odd names, PDF + Word with one name, a stale companion, a subfolder …) to run through the pipeline:

    .venv/bin/python -I scripts/make_edge_cases.py /tmp/edge-cases
    .venv/bin/python a11y_pipeline.py --source /tmp/edge-cases

Expected: 4 files skipped with reasons, the signed files held (signatures), the rest pass."""
import shutil
import sys
import zlib
from pathlib import Path

import pikepdf
import pymupdf

OUT = Path(sys.argv[1])
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
ROOT = Path(__file__).resolve().parents[1]

BODY = ("Facilities Work Request Procedure. Submit requests through the online portal at least five "
        "business days before the work is needed. Emergency requests: call 555-010-9675. Section 3.2 "
        "covers after-hours work; the rate is $85.00 per hour, minimum 2 hours.")


def text_doc(path, pages=1, size=(612, 792), title_text="Work Request Procedure", rotate=0, body=BODY):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 72), f"{title_text}" + (f" (page {i + 1})" if pages > 1 else ""), fontsize=18,
                         fontname="helv")
        page.insert_textbox(pymupdf.Rect(72, 100, size[0] - 72, 400), body, fontsize=11, fontname="helv")
        if rotate:
            page.set_rotation(rotate)
    doc.save(path)
    return path


# E1/E2 password protection
tmp = text_doc(OUT / "_plain.pdf")
with pikepdf.open(tmp) as pdf:
    pdf.save(OUT / "secured-no-editing.pdf", encryption=pikepdf.Encryption(
        owner="owner-secret", user="", allow=pikepdf.Permissions(modify_other=False, modify_annotation=False,
                                                                 modify_form=False, extract=False,
                                                                 accessibility=False, modify_assembly=False)))
    pdf.save(OUT / "open-password.pdf", encryption=pikepdf.Encryption(owner="owner-secret", user="letmein"))
tmp.unlink()

# E3/E4 not PDFs
(OUT / "download-error.pdf").write_text("<html><body><h1>403 Forbidden</h1>Box: sign in to view.</body></html>")
(OUT / "zero-bytes.pdf").write_bytes(b"")

# E5 rotated page with a form: labels must come from the right text
doc = pymupdf.open()
page = doc.new_page(width=612, height=792)
for i, label in enumerate(["Contractor Name:", "License Number:", "Phone:"]):
    y = 120 + 30 * i
    page.insert_text((72, y), label, fontsize=11, fontname="helv")
    w = pymupdf.Widget()
    w.field_type, w.field_name = pymupdf.PDF_WIDGET_TYPE_TEXT, f"Field{i + 1}"
    w.rect = pymupdf.Rect(200, y - 12, 450, y + 4)
    page.add_widget(w)
page.insert_text((72, 300), "Unrelated sentence far below the fields.", fontsize=11, fontname="helv")
page.set_rotation(90)
doc.save(OUT / "rotated-form.pdf")

# E6 crop box smaller than the media box, with a non-zero origin
text_doc(OUT / "_crop.pdf")
with pikepdf.open(OUT / "_crop.pdf") as pdf:
    pg = pdf.pages[0]
    pg.obj.MediaBox = pikepdf.Array([-50, -50, 662, 842])
    pg.obj.CropBox = pikepdf.Array([36, 36, 576, 756])
    pdf.save(OUT / "cropbox-offset.pdf")
(OUT / "_crop.pdf").unlink()

# E7 a blank page with no content stream between two text pages
text_doc(OUT / "_blank.pdf", pages=2)
with pikepdf.open(OUT / "_blank.pdf") as pdf:
    blank = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=pikepdf.Array([0, 0, 612, 792]))
    pdf.pages.insert(1, pikepdf.Page(pdf.make_indirect(blank)))
    pdf.save(OUT / "blank-page-inside.pdf")
(OUT / "_blank.pdf").unlink()

# E8 digitally signed (a signature value as Acrobat writes it; validity isn't the point)
text_doc(OUT / "_sig.pdf", title_text="Sealed Specification Section 01 10 00")
with pikepdf.open(OUT / "_sig.pdf") as pdf:
    sigv = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite"),
                                                SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                                                ByteRange=pikepdf.Array([0, 100, 200, 300]),
                                                Contents=pikepdf.String(b"\x00" * 64), Name=pikepdf.String("J. Engineer, P.E.")))
    field = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
                                                 FT=pikepdf.Name.Sig, T=pikepdf.String("Signature1"), V=sigv,
                                                 Rect=pikepdf.Array([400, 50, 560, 90]), F=4, P=pdf.pages[0].obj))
    pdf.pages[0].obj.Annots = pikepdf.Array([field])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)
    pdf.save(OUT / "digitally-signed-spec.pdf")
(OUT / "_sig.pdf").unlink()

# E9 XFA form (static XFA alongside AcroForm fields)
src = OUT / "rotated-form.pdf"
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 100), "Name:", fontsize=11, fontname="helv")
w = pymupdf.Widget()
w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_TEXT, "Name", pymupdf.Rect(120, 88, 400, 104)
page.add_widget(w)
doc.save(OUT / "_xfa.pdf")
with pikepdf.open(OUT / "_xfa.pdf") as pdf:
    pdf.Root.AcroForm.XFA = pdf.make_stream(b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'><template/></xdp:xdp>")
    pdf.save(OUT / "xfa-form.pdf")
(OUT / "_xfa.pdf").unlink()

# E10 the source already carries an attachment
text_doc(OUT / "_att.pdf")
with pikepdf.open(OUT / "_att.pdf") as pdf:
    pdf.attachments["original-notes.txt"] = pikepdf.AttachedFileSpec(pdf, b"notes", description="Author notes",
                                                                     filename="original-notes.txt")
    pdf.save(OUT / "has-own-attachment.pdf")
(OUT / "_att.pdf").unlink()

# E11 markups (Bluebeam/Acrobat review comments)
doc = pymupdf.open(text_doc(OUT / "_markup.pdf", title_text="Reviewed Submittal"))
page = doc[0]
page.add_text_annot((500, 80), "Revise per RFI 12")
page.add_freetext_annot(pymupdf.Rect(300, 420, 560, 460), "APPROVED AS NOTED", fontsize=12)
quads = page.search_for("five")
if quads:
    page.add_highlight_annot(quads[0])
page.add_ink_annot([[(100, 500), (150, 520), (200, 500)]])
page.add_rect_annot(pymupdf.Rect(70, 95, 300, 140))
page.add_stamp_annot(pymupdf.Rect(400, 600, 550, 650), stamp=0)
doc.save(OUT / "markup-comments.pdf")
(OUT / "_markup.pdf").unlink()

# E12 claims PDF/A
text_doc(OUT / "_pdfa.pdf")
with pikepdf.open(OUT / "_pdfa.pdf") as pdf:
    with pdf.open_metadata() as xmp:
        xmp.register_xml_namespace("http://www.aiim.org/pdfa/ns/id/", "pdfaid")
        xmp["pdfaid:part"] = "2"
        xmp["pdfaid:conformance"] = "B"
    pdf.save(OUT / "pdfa-archival.pdf")
(OUT / "_pdfa.pdf").unlink()

# E13 Spanish-language notice
text_doc(OUT / "aviso-de-seguridad.pdf", title_text="Aviso de seguridad",
         body="Use casco y chaleco reflectante en toda la obra. Las areas de trabajo estan cerradas al "
              "publico. Para emergencias llame al 555-010-9675. El horario de trabajo es de 7:00 a 17:00.")

# E14 AppleDouble metadata file (from a Mac zip / exFAT drive)
(OUT / "._secured-no-editing.pdf").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 200)

# E15 fonts inherited from the page tree (not on the page itself), not embedded
text_doc(OUT / "_inh.pdf")
with pikepdf.open(OUT / "_inh.pdf") as pdf:
    page = pdf.pages[0]
    pdf.Root.Pages.Resources = page.obj.Resources
    del page.obj["/Resources"]
    pdf.save(OUT / "inherited-resources.pdf")
(OUT / "_inh.pdf").unlink()

# E16 an inline image (BI … ID … EI) next to text
text_doc(OUT / "_inline.pdf", title_text="Site Photo Log")
with pikepdf.open(OUT / "_inline.pdf") as pdf:
    import random
    random.seed(4)
    w = h = 80
    raw = bytes(v for y in range(h) for x in range(w)
                for v in ((x * 3) % 256, (y * 3) % 256, random.randrange(120, 200)))
    inline = (b"q 200 0 0 200 72 380 cm BI /W 80 /H 80 /CS /RGB /BPC 8 /F /Fl ID " + zlib.compress(raw)
              + b" EI Q\n")
    page = pdf.pages[0]
    page.contents_add(pdf.make_stream(inline))
    pdf.save(OUT / "inline-image.pdf")
(OUT / "_inline.pdf").unlink()

# E17 awkward file name
text_doc(OUT / "Café plan #1 & \"final\" (rev 2).pdf", title_text="Cafe Renovation Plan")

# E18 one of our own outputs fed back in (a folder re-downloaded from Box)
sample_output = ROOT / "output/facilities-documents-2026-10-29/FY25HospPremRate.pdf"
if sample_output.exists():
    shutil.copy(sample_output, OUT / "FY25HospPremRate.pdf")
else:
    text_doc(OUT / "already-remediated-sample.pdf", title_text="Already Remediated Sample")

# E19 same name, two formats; E20 a companion re-downloaded beside its original
text_doc(OUT / "Bid Notice.pdf", title_text="Bid Notice")
from docx import Document
d = Document()
d.add_heading("Bid Notice", 1)
d.add_paragraph("Sealed bids are due at 2:00 p.m. on November 19, 2026.")
d.save(OUT / "Bid Notice.docx")
text_doc(OUT / "Bid Notice - accessible description.pdf", title_text="Accessible description: Bid Notice")

# E21 a PDF in a subfolder (Box folders often have "Addenda/")
(OUT / "Addenda").mkdir()
text_doc(OUT / "Addenda" / "Addendum 1.pdf", title_text="Addendum 1")

# E22 very long file name
text_doc(OUT / ("Long name " + "x" * 200 + ".pdf"), title_text="Long Name Document")

# E23 scan with the scanner's own invisible OCR layer
doc = pymupdf.open()
page = doc.new_page()
src = pymupdf.open(text_doc(OUT / "_scan.pdf"))
pix = src[0].get_pixmap(dpi=150)
page.insert_image(page.rect, pixmap=pix)
page.insert_textbox(pymupdf.Rect(72, 100, 540, 400), BODY, fontsize=11, fontname="helv", render_mode=3)
page.insert_text((72, 72), "Work Request Procedure", fontsize=18, fontname="helv", render_mode=3)
doc.save(OUT / "scanner-ocr-layer.pdf")
src.close()
(OUT / "_scan.pdf").unlink()

# E24 a PDF portfolio (its documents are attachments behind a cover page)
with pikepdf.open(OUT / "has-own-attachment.pdf") as pdf:
    pdf.Root.Collection = pikepdf.Dictionary(Type=pikepdf.Name.Collection, View=pikepdf.Name.D)
    pdf.save(OUT / "portfolio.pdf")

# E25 a landscape page stored rotated: white header on a dark band, light grey text, a picture
doc = pymupdf.open()
page = doc.new_page()
page.draw_rect(pymupdf.Rect(60, 90, 400, 130), color=None, fill=(0.2, 0.2, 0.2))
page.insert_text((72, 115), "White header text on dark grey", fontsize=14, fontname="helv", color=(1, 1, 1))
page.insert_text((72, 200), "Light grey body text", fontsize=11, fontname="helv", color=(0.75, 0.75, 0.75))
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
pix.set_rect(pix.irect, (220, 0, 0))
page.insert_image(pymupdf.Rect(300, 300, 400, 400), pixmap=pix)
page.set_rotation(90)
doc.save(OUT / "rotated-landscape-table.pdf")

for p in sorted(OUT.rglob("*")):
    print(f"{p.stat().st_size:>9}  {p.relative_to(OUT)}")
