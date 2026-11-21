#!/usr/bin/env python3
"""Builds the short, high-level stakeholder overview PDF ("what this does and why you can trust it").
Rerun any time to refresh:

    .venv/bin/python docs/build_overview.py
"""
from __future__ import annotations

import base64
import html
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import report  # noqa: E402

OUT = ROOT / "output" / "Accessible PDF Pipeline - Overview.pdf"
HTML = ROOT / "work" / "overview.html"
e = html.escape


def svg_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def pipeline_svg() -> str:
    steps = [("1", "Intake", ["Sort files, draft", "titles"]),
             ("2", "Text + fonts", ["Make every word", "readable"]),
             ("3", "Images", ["Describe pictures", "and drawings"]),
             ("4", "Tagging", ["Headings, tables,", "reading order"]),
             ("5", "Content review", ["AI checks it against", "the web page"]),
             ("6", "veraPDF", ["Standards validation,", "before and after"]),
             ("7", "Checks", ["Same as original?", "If not, hold it"]),
             ("8", "Report", ["What was done,", "per document"])]
    w, h, gx, gy, per_row = 150, 84, 28, 26, 4
    width = per_row * w + (per_row - 1) * gx + 8
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{2 * h + gy + 8}" '
             'font-family="Helvetica, Arial, sans-serif">']
    pos = []
    for i, (n, title, lines) in enumerate(steps):
        row, col = divmod(i, per_row)
        x, y = 4 + col * (w + gx), 4 + row * (h + gy)
        pos.append((x, y))
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#eef3ef" stroke="#1e4d2b" stroke-width="1.6"/>'
                     f'<text x="{x + w / 2}" y="{y + 27}" text-anchor="middle" font-size="16" font-weight="700" fill="#1e4d2b">{n}. {e(title)}</text>'
                     + "".join(f'<text x="{x + w / 2}" y="{y + 50 + 17 * k}" text-anchor="middle" font-size="12.5" fill="#333">{e(t)}</text>'
                               for k, t in enumerate(lines)))
    for i in range(len(steps) - 1):
        (x1, y1), (x2, y2) = pos[i], pos[i + 1]
        if y1 == y2:
            parts.append(f'<path d="M{x1 + w + 3} {y1 + h / 2} H{x2 - 4} m-7 -5 l7 5 l-7 5" stroke="#1e4d2b" stroke-width="1.8" fill="none"/>')
        else:  # wrap to the next row
            parts.append(f'<path d="M{x1 + w / 2} {y1 + h + 2} V{y1 + h + gy / 2} H{x2 + w / 2} V{y2 - 4} m-5 -7 l5 7 l5 -7" '
                         'stroke="#1e4d2b" stroke-width="1.8" fill="none"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def build() -> str:
    extra_css = """
.lede { font-size: 11.5pt; }
.callout { background: #f4f7f5; border-left: 3pt solid #1e4d2b; padding: 6pt 10pt; margin: 8pt 0; }
img.wide { width: 100%; border: 0.5pt solid #999; }
ol.steps > li { margin-bottom: 5pt; }
dt { font-weight: 600; margin-top: 4pt; } dd { margin: 0 0 2pt 12pt; }
"""
    today = f"{date.today():%B %-d, %Y}"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Accessible PDF Remediation Pipeline: Overview</title><style>{report.CSS}{extra_css}</style></head><body>

<h1>Accessible PDF Remediation Pipeline</h1>
<p class="meta">a11y-forge · Overview for reviewers and stakeholders · {today}</p>

<p class="lede">This pipeline takes the documents a public organization publishes (bid packages, design standards, guides and
forms, as PDFs or Word files) and produces versions that people using screen readers and other assistive technology
can use. It then proves the result: each file is checked by <strong>veraPDF</strong>, the independent open-source
validator for the PDF accessibility standard, compared with its original, and documented in its own report. Anything
that fails a check is held back for a person instead of being published.</p>

<h2>Why this matters</h2>
<p>Bid packages, specifications, standards and forms are how contractors, designers and vendors do business with
public facilities teams. A bidder who is blind, has low vision, or uses voice control or a keyboard instead of a mouse depends on
software that reads the PDF's hidden structure aloud or navigates by it. When that structure is missing, as it is in
most PDFs exported from design and office software and in every scanned document, the document is a picture of
words: headings, tables, form fields and drawings can't be found, and the bidder can't compete on equal terms.</p>
<p>It is also required. A 2024 U.S. Department of Justice rule under Title II of the ADA makes public entities'
web content, including the PDFs and documents they post, meet <strong>WCAG 2.1 Level AA</strong>. An April 2026 rule
moved the compliance date for large public entities to <strong>April 26, 2027</strong>; the standard
did not change, and the ADA's existing duty to communicate effectively with people with disabilities applies now.
For PDFs, WCAG is met in practice through <strong>PDF/UA</strong>, the ISO standard for accessible PDF.</p>
<p>Public agencies post many such documents, often under bid deadlines, and fixing them by hand takes hours each. The
pipeline does that work in minutes per document and proves the result.</p>

<h2>How results are proven: veraPDF</h2>
<p><strong>veraPDF</strong> is a free, open-source validator built and maintained by the PDF Association and the Open
Preservation Foundation, the industry bodies behind the PDF standards. It is the reference tool for checking a PDF
against the ISO standards for archiving (PDF/A) and accessibility (PDF/UA).</p>
<p>It reads the file's internal structure, not its appearance, and tests it against the standard's rules, each "shall"
in PDF/UA written as a machine-readable check. Among them:</p>
<ul>
<li>every piece of content is either tagged (heading, paragraph, list, table, figure, form field) or marked as
decoration that screen readers skip;</li>
<li>every image and drawing has alternative text, and tables have properly marked header cells;</li>
<li>fonts are embedded and every character maps to real text, so a screen reader reads words instead of garbage;</li>
<li>the document has a title and a language, and links, form fields and annotations are described.</li>
</ul>
<p>Every failure is reported with the rule broken and where. The pipeline runs veraPDF against two profiles, PDF/UA-1
and Well-Tagged PDF (WTPDF 1.0), on <strong>both the original and the remediated file</strong>, so every report shows
the improvement as a measured before and after. Across real-world batches, failed checks routinely drop from tens or
hundreds of thousands per batch to zero (for example, a 1,285-page specification set dropping from 244,453 to 0). A file
that does not pass is not published.</p>
<p>veraPDF tests what software can test. The accessibility standard's checklist (the Matterhorn Protocol) lists 136
ways a PDF can fail, and some, such as whether alt text is accurate or the reading order makes sense, need judgment.
Those are covered by the pipeline's own checks, the AI review and a short human review (below).</p>

<h2>What it does</h2>
<img class="wide" src="{svg_uri(pipeline_svg())}" alt="Pipeline in eight steps: 1 Intake, sorting files and drafting
titles; 2 Text and font repair so every word is readable; 3 Images and drawings described; 4 Tagging of headings,
tables and reading order; 5 Content review by AI against the web page; 6 Validation with the standard validator before
and after; 7 Checks against the original, holding the file if anything changed; 8 A report per document.">
<ol class="steps">
<li><strong>Prepares the document.</strong> Scanned pages get a text layer (OCR) and broken fonts are repaired, so a
screen reader reads real words instead of garbage.</li>
<li><strong>Describes images.</strong> An AI model (Claude Haiku) writes alt text for each meaningful picture using the
surrounding page for context; decorative items are marked so screen readers skip them.</li>
<li><strong>Adds structure.</strong> Open-source tools tag headings, lists, tables, reading order, title and language,
and follow-up repairs fix what those tools get wrong. In fillable forms, every field is announced with the label
printed next to it and is read right after that label, or in its table cell.</li>
<li><strong>Reviews the content.</strong> The AI compares the result with the original and with the web page that links
to it, and flags problems for whoever needs to act: the accessibility work, the document's author, or the page owner.</li>
<li><strong>Validates and checks.</strong> veraPDF validates the result against PDF/UA and WTPDF, and the pipeline
compares it with the original, before it is released.</li>
<li><strong>Reports.</strong> Every document gets a report of what was done and what still needs a person.</li>
</ol>
<p>Word documents are handled the same way and stay Word files. Items only the author can fix (such as heading styles
or vague link text) are listed in the report for them.</p>

<h2>Why you can trust the results</h2>
<ul>
<li><strong>Independent validation.</strong> veraPDF checks every output and its original (above), so the improvement
is measured rather than claimed. Results have also been cross-checked in PAC 2024 and Adobe Acrobat.</li>
<li><strong>Nothing is published unchecked.</strong> Each file must look the same as the original page by page, keep
all of its numbers and text, and have sensible alt text. A file that fails is held in a "needs review" folder.</li>
<li><strong>The AI is boxed in.</strong> The structural work is ordinary, predictable code. The AI writes descriptions
and reviews in fixed formats, its work is checked a second time, and any of it can be corrected by a person.</li>
<li><strong>Originals are never touched,</strong> and every step is documented in the report, which serves as the audit
trail.</li>
</ul>
<div class="callout"><strong>What validation does not prove.</strong> A validator can confirm that every image has alt
text; it cannot judge whether that text is accurate or the reading order sensible. That is why the checks and AI review
exist, and why a short human review remains part of the process.</div>

<h2>Drawings and large plan sets</h2>
<p>Construction drawings cannot be made fully accessible inside the drawing itself. Each drawing page is tagged as one
picture whose description (alt text) gives the drawing's key content in condensed form: its title, then the
quantities, sizes, products, routes or closures a reader needs. Where a drawing's detail lives in its geometry and
cannot be put into words, such as a floor plan or dense wiring, the description says so plainly: it is a complex
drawing whose full detail does not translate to text. There is no separate description file to post; everything
travels inside the original.</p>

<h2>The human role</h2>
<p>The pipeline does most of the work, but a person still:</p>
<ul>
<li>resolves any file held for review (usually a quick alt-text correction and a rerun);</li>
<li>skims each report's content review and image descriptions, and spot-checks a few documents per batch with a screen
reader;</li>
<li>spot-checks drawing descriptions against the drawings before posting;</li>
<li>forwards author and web-page issues to the people who own them.</li>
</ul>

<h2>Glossary</h2>
<dl>
<dt>WCAG 2.1 AA</dt><dd>Web Content Accessibility Guidelines, the standard the DOJ rule adopts.</dd>
<dt>PDF/UA</dt><dd>The ISO standard for accessible PDF (ISO 14289).</dd>
<dt>WTPDF</dt><dd>Well-Tagged PDF, the PDF Association's specification for PDFs whose structure supports
accessibility and reuse.</dd>
<dt>veraPDF</dt><dd>The open-source reference validator for the PDF/A and PDF/UA standards.</dd>
<dt>Matterhorn Protocol</dt><dd>The PDF Association's checklist of the ways a PDF can fail PDF/UA.</dd>
<dt>Tags</dt><dd>A hidden structure inside a PDF that says what each piece of content is and in what order to read it.</dd>
<dt>Alt text</dt><dd>A short text description of an image that screen readers read aloud.</dd>
<dt>OCR</dt><dd>Optical character recognition: turning an image of text into real text.</dd>
</dl>
<p class="note">Generated {today}; rerun <code>docs/build_overview.py</code> to refresh. Full technical detail is in the
project README.</p>
</body></html>"""


if __name__ == "__main__":
    HTML.parent.mkdir(parents=True, exist_ok=True)
    HTML.write_text(build())
    report.to_pdf(HTML, OUT, "Accessible PDF Remediation Pipeline: Overview")
    print(OUT)
