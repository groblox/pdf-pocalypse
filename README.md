# pdf-pocalypse — Accessible Document Remediation Pipeline (PDF & Word)

A local, fail-closed accessibility remediation and verification pipeline that converts PDFs (bid packages, specifications, forms, design standards, drawings, scans, org charts) and Word (`.docx`) documents toward **ADA Title II** web accessibility compliance (**WCAG 2.1 Level AA**, **PDF/UA-1 / ISO 14289-1**, **WTPDF 1.0**, and Microsoft's accessible Word guidelines).

**Jump to:**
[20-Second Explainer](#20-second-explainer) ·
[4-Minute Explainer (Directors & Leadership)](#4-minute-explainer-for-directors--leadership) ·
[2-Minute Explainer (IT & Engineering)](#2-minute-explainer-for-it--engineering) ·
[ADA Compliance Matrix](#ada-title-ii-compliance-what-this-tool-covers-and-what-still-needs-a-human) ·
[How It Works](#how-it-works-architecture--stages) ·
[Pluggable AI & Evals](#pluggable-ai--independent-eval-model-support) ·
[Setup](#setup--installation) ·
[Usage](#usage) ·
[Testing](#testing)

---

## 20-Second Explainer

Drop in a folder of PDFs or Word files; get back visually identical, screen-reader-ready documents under the same filenames, each with a before-and-after accessibility audit report. The pipeline runs OCR on scans, repairs broken fonts and contrast, writes context-aware alt text, fixes form labels and reading order, and validates every PDF against **PDF/UA-1** and **WTPDF 1.0** via **veraPDF** plus an optional second AI reviewer. **Nothing is published unless it passes every check**—failures are held in `needs review/`.

---

## 4-Minute Explainer (For Directors & Leadership)

### The problem we are solving
Public universities and government agencies publish thousands of PDFs and Word documents every year—bid packages, construction specifications, drawings, rate sheets, policies, and fillable forms. Most of these files look fine visually but are unusable for people who are blind, have low vision, or navigate by keyboard or voice: scanned pages have no readable text, fonts decode as gibberish, tables lose their headers, form fields aren't linked to their printed labels, and engineering drawings break into hundreds of meaningless image fragments.

Under the U.S. Department of Justice's **ADA Title II** final rule (28 CFR Part 35, Subpart H), public entities must make web content and posted documents meet **WCAG 2.1 Level AA** (with the compliance deadline set to **April 26, 2027** for large public entities under DOJ's April 2026 interim final rule—while the underlying ADA duty to provide effective communication applies today). Manually remediating a complex bid specification or form in Acrobat takes hours per document and creates a bottleneck on time-sensitive postings.

### What this tool does—in plain terms
Instead of fixing documents by hand one by one, staff drop a batch folder into the pipeline (via a double-clickable desktop launcher or command line). Within minutes per document, the tool:
1. **Preserves the original untouched** and builds an accessible copy under the exact same filename so re-posting is seamless.
2. **Makes every word readable** by running OCR on scanned documents and repairing broken font encodings so screen readers speak real words instead of garbled symbols.
3. **Adds real document structure**—headings, navigation bookmarks, reading order, table headers, clickable links, nested lists for organization charts, and fillable form fields announced with the exact labels printed beside them.
4. **Describes visuals responsibly**—AI writes concise, context-aware alt text for photos, charts, maps, and CAD drawings (with a second verification pass on complex visuals) and marks decorative borders/logos so screen readers skip them.
5. **Repairs low-contrast text** on text documents so light text and grey table headers meet WCAG contrast minimums without altering engineering drawings where color carries technical meaning.
6. **Handles Word files (`.docx`) natively**—fixing titles, language, alt text, repeating table headers, and contrast while generating a checklist of structural items only the document's author can fix.

### Why leadership can trust the output
Accessibility tools have a bad reputation when they rubber-stamp broken files or silently alter document content. This pipeline is built around **fail-closed governance and proof**:
* **Independent standards proof, before and after**: Every PDF is validated by **veraPDF**—the open-source reference validator built by the PDF Association—against both **PDF/UA-1** and **WTPDF 1.0**. Every report shows measured failures before and after (for example, large specification sets dropping from hundreds of thousands of failed checks to zero).
* **No silent edits to your documents**: Before any file is released, automated fidelity checks compare it page-by-page against the original to verify that **nothing changed visually** (≤1% pixel delta), **every number in the original text is still present** (dollar amounts, dates, dimensions, section numbers), and **every form calculation script is intact**.
* **Cross-model auditing (optional)**: Rather than relying on one AI to grade its own work, the pipeline lets you assign a **different, independent AI model** (e.g., OpenAI GPT-4.1, Google Gemini 2.5 Pro, or Claude Sonnet) to audit the primary model's alt text, form tooltips, and match against the live webpage where the document is posted.
* **Fail-closed publishing**: Any document that fails *any* validator, contrast check, digital-signature check, fidelity check, or alt-text sanity check is automatically quarantined in `output/<batch>/needs review/`—never mixed into publishable deliverables.
* **Built-in audit trail**: Every document receives its own tagged, accessible PDF report documenting what changed, visual diff thumbnails, validation results, and follow-up items for authors or web editors, rolled up into a batch `summary.pdf`.

### Cost, throughput, and human accountability
* **Local execution, low cost**: Everything runs locally on a workstation—no recurring per-page SaaS remediation fees. AI calls cost pennies per document via API (or run under an existing subscription or fully local Ollama model), and cached work is automatically reused on reruns.
* **Clear human boundary**: Automated validation proves technical conformance, not human judgment. Staff still own four targeted steps: resolving quarantined `needs review/` files, spot-checking complex drawing descriptions and screen-reader reading order, replacing severely degraded scans with clean originals, and forwarding author/website issues flagged in the summary report.

---

## 2-Minute Explainer (For IT & Engineering)

### Stack & architecture
The pipeline is a local **Python 3.12** CLI orchestrator ([`a11y_pipeline.py`](a11y_pipeline.py)) combining open-source PDF engines with a strict, schema-enforced multimodal AI layer:
* **Deterministic core**: `ocrmypdf` + Tesseract/Ghostscript (OCR), `pikepdf` (low-level PDF/XMP/content-stream/ParentTree surgery), `PyMuPDF` (fast rendering, coordinate transforms, image/text extraction), `fontTools` (ToUnicode CMap reconstruction, font subsetting/embedding), `pdf-a11y` / `opendataloader-pdf` (JVM-based semantic structure tagging), `python-docx` + `lxml` (OOXML remediation), `numpy` + `Pillow` (vectorized rendered-pixel contrast and appearance diffs), `veraPDF` (Java CLI standards validation), and headless Chrome/Chromium (printing tagged PDF/UA-1 audit reports).
* **Deterministic vs. AI boundary**: All structural PDF mutation, font repair, contrast math, MCID/ParentTree integrity, reading-order placement, and validation are **deterministic Python code**. Multimodal AI is strictly isolated behind JSON-Schema-validated calls for semantic tasks only: image/drawing descriptions, printed form-label extraction, org-chart hierarchy inference, alt-text sanity evaluation, and webpage-vs-document intent review.

### Pluggable AI & independent eval routing (`ai_client.py`)
AI access is abstracted in [`ai_client.py`](ai_client.py) (with [`haiku.py`](haiku.py) retained as a backward-compatible facade) with **two independent roles**:
* **`primary`** (generation/remediation): titles, image/drawing alt text, form labels, org charts, alt-text repair.
* **`eval`** (auditing): Stage 4 content/intent review and Stage 5 alt-text sanity checks.

Both roles independently support `claude-cli`, `anthropic`, `openai` (or any OpenAI-compatible `base_url`), `gemini`, `openrouter`, **`ollama`** (fully local/private VLMs—zero external data egress), or `none` (deterministic rules only). Precedence is **CLI flags > env vars (`.env`) > manifest (`project.ai`/`project.eval`) > `config/base.yaml` > defaults**.

### Reliability, caching & operations
* **Content-addressed incremental caching**: `plan_stamps()` hashes each stage's input file signature, Python module source code, config, manifest entry, and active eval model signature into `work/<batch>/stamps/`. Reruns skip unaffected stages; switching `--eval-model` reruns only `intent`/`compat` without re-tagging or regenerating image caches (`01_images/`, `01_forms/`, `companion/`).
* **Fail-closed gate (`stage_compat` + `fidelity.py`)**: Runs parallel `veraPDF` (`ua1` + `wt1a`), PDF syntax checks, PAC-equivalent checks (`mcid_references`, `bookmarks`, `heading_hierarchy`, rendered-pixel `contrast`, `scripts_kept`, `signatures`, `xfa`), 50-dpi luminance pixel diffs (`appearance`), exact regex number preservation (`numbers_retained`), ≥98% word retention, and Eval AI `alt_text_sane` (with up to 2 automatic red-outline repair loops via `alt_repair.py`).
* **Operational guardrails**: Single-batch file lock (`logs/.pipeline.lock`) prevents multiple OpenDataLoader JVMs from competing for RAM; preflight API checks fail fast before starting a batch; large drawing sets (>300 MB, >20 scanned pages, or OOMing OpenDataLoader) automatically fail over to lightweight drawing-set tagging (`companion.py`).
* **Portability & tests**: Works on macOS and Linux (portable discovery for `JAVA_HOME`, `verapdf`, `ocrmypdf`, Chrome/Chromium, and Liberation/DejaVu system fonts). Includes an offline `unittest` suite (`python -m unittest discover -s tests -v`) and a synthetic 25-case PDF/DOCX edge-case generator (`scripts/make_edge_cases.py`).

---

## ADA Title II Compliance: What This Tool Covers (and What Still Needs a Human)

Under the U.S. Department of Justice's **ADA Title II** final rule (28 CFR Part 35, Subpart H), public entities must ensure web content and posted documents (including PDFs and Word files) conform to **WCAG 2.1 Level AA** (compliance date April 26, 2027 for large public entities under DOJ's April 2026 interim final rule). For PDFs, WCAG 2.1 AA is met in practice through **PDF/UA-1 (ISO 14289-1)** and **WTPDF 1.0** plus visual/usability criteria verified by tools like PAC 2024.

### Automated by this pipeline
| Standard / Criterion | How the pipeline addresses it |
|---|---|
| **WCAG 1.1.1 / PDF/UA 7.3** (Non-text content) | Context-aware multimodal alt text for photos, charts, maps, logos, signatures, and CAD drawings; decorative/repeated ornaments demoted to `/Artifact`; text-heavy images converted to real text via targeted OCR; alt-text sanity eval + automatic page-snapshot repair. |
| **WCAG 1.3.1 & 1.3.2 / PDF/UA 7.1–7.5** (Structure & reading order) | OpenDataLoader semantic tagging (`pdf-a11y`), non-skipping heading hierarchy (`H1`–`H6`), table header cells (`TH`) with `Scope`, list numbering attributes, plain-text URLs/emails/phones promoted to real `/Link` annotations, org charts rebuilt as nested lists, form fields moved inline beside their labels or into table cells. |
| **WCAG 1.4.3** (Contrast minimum) | Rendered-pixel contrast measurement (`contrast_fix.py`, matching PAC 2024 rather than assuming white pages) with automatic vector and raster-background darkening to a 4.85:1 / 3.35:1 safety margin; unfixable failures hold the document. |
| **WCAG 2.4.2 & 2.4.5** (Titles & navigation) | Document title, XMP metadata, `DisplayDocTitle`, and PDF bookmarks (`/Outlines` with `/SD` structure destinations) generated whenever headings exist. |
| **WCAG 2.5.3 & 4.1.2** (Forms & Label in Name) | Multimodal AI reads each form page with numbered red outlines so every field's `/TU` tooltip carries the exact visible label printed next to it; radio groups and buttons get `"question: option"`; AcroForm JS scripts and formatted appearances are preserved. |
| **WCAG 3.1.1** (Language of page) | Automatic non-English detection (`es`, `fr`) or manifest override (`lang:`) written to PDF `/Lang` and Word defaults. |
| **PDF/UA 7.21** (Fonts & Unicode) | Rebuilds missing/partial `/ToUnicode` CMaps (Mac/ISOAdobe/SymbolMT glyph orders), embeds missing fonts with required `/Length1` for Acrobat, strips stale `/CIDSet`, remaps Word Private-Use-Area symbols to standard Unicode. |
| **Anti-regression & integrity** | Independent `veraPDF` validation (PDF/UA-1 + WTPDF 1.0 + pre-remediation baseline), PAC-style MCID/ParentTree integrity check, bookmarks-for-headings & heading-hierarchy checks, page-by-page pixel appearance diff (≤1% change), exact number retention, ≥98% word retention, JS action parity, and digital signature protection. |

### Honest limits — what still requires human review
Automated validation (including `veraPDF`) is **necessary, not sufficient** (see the PDF Association's Matterhorn Protocol):
- **Alt-text accuracy & nuance**: Spot-check AI descriptions (especially complex maps, charts, and engineering drawings) and override any entry in the manifest's `alt_text:`.
- **Reading order & table semantics**: Spot-check multi-column layouts, complex merged tables, and OCR'd tables with a screen reader (VoiceOver / NVDA) or PAC 2024.
- **Poor source scans**: Severely degraded scans cannot be fixed by OCR; the content review flags them so authors can supply born-digital files or clean 300-dpi rescans.
- **Language of parts (WCAG 3.1.2)**: Bilingual documents are tagged with their primary language only.
- **Color-only meaning (WCAG 1.4.1)** and **complex architectural geometry**: Drawings whose meaning lives in dimensioned geometry carry an explicit complex-drawing notice in alt text; Word reports flag author-only structural issues (heading styles, merged tables, vague link text, floating images).

---

## How It Works (Architecture & Stages)

```text
source PDF ──► 1. OCR + Fonts ──► 2. Image & Drawing Stage ──► 2b. Tagging (pdf-a11y)
               (ocrmypdf,             (Primary AI + page           + Structure & Form Fixes
                font_fixes.py,         context, tile/CAD            + Contrast Repair
                inline images,         clustering, targeted         + Optional Org Chart
                XFA, text links)       OCR & OCR-in-pic strip)      + Attachments)
                                                                          │
                                    3. Site Scrape / Crawl ───────────────┤
                                    (calendar, Box, web pages)            ▼
                                                                  4. Content Review [Eval AI]
                                                                  (intent, title, alt text vs.
                                                                   pictures, form tooltips)
                                                                          │
                                                                          ▼
                                                                  5. Compatibility Gate
                                                                  (alt sanity [Eval AI] + repair,
                                                                   veraPDF PDF/UA-1 & WTPDF 1.0,
                                                                   MCID/PAC checks, appearance &
                                                                   number fidelity vs. original)
                                                                          │
                                                                          ▼
                                                                  6. Publish or Hold + Report
                                                                  (output/<batch>/ or needs review/
                                                                   + tagged PDF/UA audit report)

source .docx ─► docx_fix.py (title, lang, alt text, table headers, contrast, author flags)
              ─► text-unchanged + alt sanity [Eval AI] ─► publish or hold + report
```

### Stage-by-stage breakdown

| Stage | Modules | What happens | Cached / Output |
|---|---|---|---|
| **0. Intake & Manifest** | `manifest_builder.py` | Scans a source folder (skipping empty files, HTML error pages saved as `.pdf`, password-locked PDFs, portfolios, macOS `._` files, and stale companion files), profiles each file (`text`, `scan`, `drawing`, `scan_drawing`, `docx`), detects non-English language, and drafts a title/subject via AI into `manifests/<batch>.yaml`. New files added later are appended without touching reviewed entries. | `manifests/<batch>.yaml` |
| **1. OCR & Font Prep** | `a11y_pipeline.py`, `font_fixes.py`, `struct_fixes.py` | Originals are copied to `work/<batch>/00_input/` (never modified in place). `scan*` PDFs run `ocrmypdf --skip-text --oversample 300`. Every PDF then gets: static XFA stripped, inline images promoted to XObjects, missing/partial `/ToUnicode` CMaps rebuilt, missing fonts embedded (with `/Length1` for Acrobat) while preserving AcroForm `/DR` fonts, stale `/CIDSet` removed, PUA Symbol/Wingdings remapped to Unicode, and plain-text emails/URLs/phones converted into real link annotations. | `work/<batch>/01_ocr/`, `01_fonts/` |
| **2. Image & Drawing Intelligence** | `image_stage.py`, `drawing_pages.py` | Deduplicates images by `pdf-a11y` hash, applies soft masks (`SMask`) so masked e-signatures render accurately, clusters touching image tiles (≥4 tiles) into single composite pictures, and detects CAD sheets with scattered line-art fragments. Each unit goes to the **Primary AI** with surrounding page text (`[THIS IMAGE]` position marked) for kind + alt text (≤150 chars; charts & full-page pictures get ≤400 chars plus a second verification pass; whole drawing pages get ≤600 chars + verification + `complex_note()` when geometry cannot translate to text). Images that are actually tables/text/forms trigger `ocrmypdf --redo-ocr` on their pages; gibberish OCR layers inside photos/maps/drawings are stripped. | `work/<batch>/01_images/<stem>.json` |
| **2b. Tagging, Structure, Forms & Contrast** | `pdf-a11y`, `struct_fixes.py`, `form_labels.py`, `contrast_fix.py`, `org_chart.py`, `attachments.py` | `pdf-a11y` (OpenDataLoader) tags the document using the image stage's `alt_overrides`. Then `struct_fixes.py` repairs what automated taggers miss: re-binds every `/Figure` to the alt text of the image it actually draws (fixing list-order mismatches), collapses drawing pages into one `/Figure`, demotes decorative/placeholder figures to `/Artifact`, fixes misused tags (long Captions → `P`, link-only headings → `P`, non-skipping headings, `TH` scope, list numbering, nested `Art`), syncs annotation `/Contents` ↔ `/Alt`, builds heading bookmarks with `/SD`, restores form JavaScript & prevents `/NeedAppearances` regressions, labels form fields from printed page labels (`form_labels.py`), and moves `/Form` elements into reading order (`place_form_fields`). `contrast_fix.py` fixes low-contrast text and raster backgrounds on non-drawing documents. Optional `org_chart: true` rebuilds org charts as nested lists (`org_chart.py`); `attachments:` embeds supplemental files with `/AF` and `GoToE` bookmarks. | `work/<batch>/02_a11y/*_a11y.pdf` |
| **2 (Alt). Drawing-Set Mode** | `companion.py` | Triggered automatically for PDFs >300 MB, `scan_drawing` sets >20 pages, `companion: true`, or when OpenDataLoader OOMs on heavy vector sets. Instead of noisy OCR, AI indexes each sheet (whole sheet + right-edge & bottom-right title-block close-ups) and lightly tags each page as one `/Figure` with its sheet description + `complex_note()`, a set summary on page 1, per-sheet bookmarks, and tagged sheet cross-reference links. | `output/<batch>/needs review/` (until checked) |
| **3. Site Context** | `a11y_pipeline.py`, `site_crawl.py` | Scrapes `--site-url` pages (Localist calendar JSON-LD, Box embedded file listings, page text), runs a polite robots.txt-respecting crawl (`--crawl`), or reads `project.site_text_file` for JS-only pages. | `work/<batch>/03_site/` |
| **4. Content / Intent Review** | `a11y_pipeline.py` (**Eval AI**) | Sends the site context, metadata, tagged reading-order text excerpt, up to 6 largest rendered pictures with their alt texts, and form field tooltips to the **Eval AI**. Evaluates whether the remediated PDF matches the website's intent, title accuracy, alt-text accuracy, and form usability, tagging issues as `remediation`, `source_document`, or `site_listing`. | `work/<batch>/04_intent/*.json` |
| **5. Compatibility & Fidelity Gate** | `a11y_pipeline.py`, `fidelity.py`, `alt_repair.py` (**Eval AI** + **Primary AI**) | 1) Runs rule-based + **Eval AI** alt-text sanity checks; rejected figures are re-rendered with red outlines + close-ups and rewritten (or demoted to decorative) by Primary AI for up to 2 repair rounds (`alt_repair.py`).<br>2) Runs `veraPDF` in parallel for **PDF/UA-1** and **WTPDF 1.0** (plus PDF/UA-1 baseline on the original).<br>3) Checks PDF syntax, PAC MCID/ParentTree integrity, bookmarks-for-headings, heading hierarchy, `/Lang`, `/Title`, `DisplayDocTitle`, `/Tabs /S`, encryption, digital signatures, contrast failures, dynamic XFA, JS script parity, attachments, page count, ≥98% word retention, renderability, file size (≤3×), pixel appearance diff (≤1% change), and exact number retention. | `work/<batch>/05_compat/*.json` |
| **6. Publish & Audit Reports** | `a11y_pipeline.py`, `report.py` | Files passing **every** check are published under their original filename in `output/<batch>/`; any failure holds the file in `output/<batch>/needs review/`. Headless Chrome prints an accessible, PDF/UA-1-tagged per-document report (before/after stats, visual diff thumbnails, alt-text table, content review, human checklist) plus `summary.pdf` / `summary.md`. | `output/<batch>/`, `reports/`, `summary.pdf` |
| **Word (`.docx`) Track** | `docx_fix.py` | Fixes document title, language, picture alt text (and Word 365 decorative flags), repeating table header rows, and text contrast; flags author-only issues (heading styles, merged/nested tables, vague links, typed lists, floating images, blank-paragraph spacing, color-only meaning); verifies exact text equality + Eval AI alt-text sanity; prints an accessible Word report. | `output/<batch>/*.docx`, `reports/` |

### Incremental fingerprinting
Every stage stores a SHA-256 fingerprint (`work/<batch>/stamps/`) covering its input file, the Python source code of the stage/modules, configuration, manifest entry, and active eval model. Reruns skip up-to-date stages automatically and only recompute downstream stages affected by a change. Expensive AI image/sheet/form caches (`01_images/`, `01_forms/`, `companion/`) are preserved independently unless deleted.

---

## Pluggable AI & Independent Eval Model Support

By default, the pipeline uses **Claude Haiku** (`claude-haiku-5-5`) via headless Claude Code (`claude -p` on your Claude subscription) or the Anthropic API (`ANTHROPIC_API_KEY`).

You can configure **any supported provider/model** for **primary remediation** (image/drawing/form/title descriptions) and — independently — choose a **different AI provider or stronger model for evaluations** (Stage 4 content/intent review and Stage 5 alt-text sanity checks). Using an independent evaluator avoids self-grading bias (e.g., generating alt text cheaply with Haiku or a local VLM while auditing quality with GPT-4.1, Gemini 2.5 Pro, or Claude Sonnet).

### Supported providers (`ai_client.py`)

| Provider value | Auth / Setup | Default model | Notes |
|---|---|---|---|
| `auto` (default) | `ANTHROPIC_API_KEY` → `claude` CLI → `OPENAI_API_KEY` → `GEMINI_API_KEY` | `claude-haiku-5-5` | Automatic fallback; zero config with `claude` CLI |
| `claude-cli` | Signed-in `claude` CLI (subscription) | `claude-haiku-5-5` | Runs `claude -p` with MCP/settings/skills disabled (~1K overhead) |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` | Direct Anthropic Messages API (Haiku, Sonnet, Opus) |
| `openai` | `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`) | `gpt-4.1-mini` | OpenAI (`gpt-4.1`, `gpt-4o`, `gpt-5`) or any OpenAI-compatible server |
| `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `gemini-2.5-flash` | Google Gemini REST API (`gemini-2.5-pro`, `gemini-2.5-flash`) |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4` | Access 100+ vision models via OpenRouter |
| `ollama` | Local Ollama (`http://localhost:11434/v1`) | `qwen2.5vl` | Local / air-gapped multimodal models; no API key required |
| `none` | — | — | Disables AI for that role (rules-only evals or `--no-vlm`) |

### Examples: running with a separate eval model

```bash
# Keep Haiku for image/form descriptions, use OpenAI GPT-4.1 for evals:
python a11y_pipeline.py --source ~/Downloads/batch \
    --eval-provider openai --eval-model gpt-4.1

# Keep Haiku for generation, use Gemini 2.5 Pro for evals:
python a11y_pipeline.py --source ~/Downloads/batch \
    --eval-provider gemini --eval-model gemini-2.5-pro

# Keep Haiku for generation, use Claude Sonnet (via Claude CLI subscription) for evals:
python a11y_pipeline.py --source ~/Downloads/batch \
    --eval-provider claude-cli --eval-model claude-sonnet-4-6

# Re-run ONLY the eval stages with a different evaluator (reuses existing remediated PDFs!):
python a11y_pipeline.py --source ~/Downloads/batch \
    --stages intent,compat,report --eval-provider openai --eval-model gpt-4.1

# Fully local / private run using Ollama:
python a11y_pipeline.py --source ~/Downloads/batch \
    --ai-provider ollama --ai-model qwen2.5vl \
    --eval-provider ollama --eval-model qwen2.5vl

# Disable AI evals (deterministic rules + veraPDF only):
python a11y_pipeline.py --source ~/Downloads/batch --eval-provider none
```

### Configuration precedence (highest → lowest)
1. **CLI flags**: `--ai-provider`, `--ai-model`, `--ai-base-url`, `--eval-provider`, `--eval-model`, `--eval-base-url`
2. **Environment / `.env`**: `A11Y_AI_PROVIDER`, `A11Y_AI_MODEL`, `A11Y_AI_BASE_URL`, `A11Y_EVAL_PROVIDER`, `A11Y_EVAL_MODEL`, `A11Y_EVAL_BASE_URL` (see [`.env.example`](.env.example))
3. **Batch manifest**: `project.ai` and `project.eval` in `manifests/<batch>.yaml`
4. **Base config**: `ai:` and `eval:` in [`config/base.yaml`](config/base.yaml)

Switching `--eval-provider` or `--eval-model` automatically updates the `intent` and `compat` cache fingerprints (`plan_stamps`), so only the evaluation stages re-run without re-tagging or re-describing images.

---

## Setup & Installation

### 1. System prerequisites
- **Python 3.12+**
- **Java 21 JRE** (for `veraPDF` and `opendataloader-pdf`) — either on `PATH` / `JAVA_HOME`, or unpacked at `./tools/java-home/`
- **veraPDF 1.30.1** — install locally with:
  ```bash
  ./scripts/install-verapdf.sh
  ```
  (Installs to `./tools/verapdf/verapdf`, or set `A11Y_VERAPDF` / put `verapdf` on `PATH`.)
- **Tesseract OCR** and **Ghostscript** (for `ocrmypdf`):
  - macOS: `brew install tesseract ghostscript qpdf`
  - Ubuntu/Debian: `sudo apt install tesseract-ocr ghostscript qpdf fonts-liberation`
- **Google Chrome or Chromium** (for printing accessible PDF reports; override path with `A11Y_CHROME`)
- **AI credentials**: either signed-in `claude` CLI, or copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `OPENROUTER_API_KEY`.

### 2. Python dependencies
Install into your preferred Python environment (virtual environment or system/conda/uv environment):
```bash
pip install --prefer-binary -r requirements.txt
```

---

## Usage

### Interactive macOS Launcher
Double-click **`Remediate PDFs.command`** in Finder and choose a folder of PDFs / `.docx` files:
- **New folder**: optionally paste website/calendar/Box URLs (or a section URL ending in `/` to crawl) and the pipeline drafts a manifest and runs.
- **Previously processed folder**: opens an interactive menu to reprocess new/changed files, attach companion files to PDFs, check document status, rebuild reports, or force-redo specific files.
- Keeps the Mac awake (`caffeinate`), prompts before runs >300 AI calls, strips noisy tool logs from the terminal while saving full logs to `logs/`, sends a macOS notification, and opens `output/<batch>/`.

### Command Line
```bash
# Process a folder of PDFs/DOCX files with a site crawl for content review
python a11y_pipeline.py --source ~/Downloads/my-docs --crawl https://example.edu/documents/

# Process a bid package posted on specific pages (repeatable --site-url)
python a11y_pipeline.py --source ~/Downloads/bid-pkg \
    --site-url https://example.edu/event/12345 \
    --site-url https://example.box.com/s/abcdef

# Process specific files only (comma-separated substring match; merges into batch summary)
python a11y_pipeline.py --source ~/Downloads/my-docs --only "Addendum 3,Roofing"

# Check status of every document in a batch without modifying anything
python a11y_pipeline.py --source ~/Downloads/my-docs --status

# Rebuild reports only (publishes/moves nothing if checks are stale)
python a11y_pipeline.py --source ~/Downloads/my-docs --stages report

# Run without AI (writes image review CSV; deterministic repairs + veraPDF only)
python a11y_pipeline.py --source ~/Downloads/my-docs --no-vlm

# Interactively attach accessible supplemental files (e.g. sheet index .xlsx) into PDFs
python attachments.py manifests/<batch>.yaml
```

### Manifest overrides & human corrections
See [`manifests/example.yaml`](manifests/example.yaml) for a complete annotated template. Key per-document options:
- `alt_text:` — map image/tile/sheet keys (from `work/<batch>/01_images/<stem>.json`) to reviewed alt text or `"decorative"`. Survives reruns and cache deletes. Inspect images with:
  ```bash
  python scripts/review_alts.py <batch> "<file>.pdf"   # generates contact sheet in work/review/
  python scripts/struct_tree.py <pdf>                 # prints the PDF tag tree
  ```
- `companion: true` — force drawing-set mode (one Figure + bookmark per sheet).
- `org_chart: true` — rebuild organization chart structure as nested lists in reporting order.
- `keep_colours: true` — opt out of automatic contrast repair.
- `publish_unsigned: true` — strip invalid digital signatures so a copy can be published (the signed original remains the record copy).
- `lang: es` — override document language (ISO 639-1).
- `attachments:` — list files to embed as PDF/UA `/AF` Supplement attachments (with `GoToE` bookmarks for attached PDFs).

---

## Unusual & Edge-Case Files

The pipeline handles real-world document hazards explicitly (exercisable via `scripts/make_edge_cases.py`):
- **Skipped safely (with reasons)**: 0-byte files, HTML sign-in/error pages saved as `.pdf`, open-password PDFs, corrupted files, PDF portfolios, macOS `._` AppleDouble files, and legacy `… - accessible description.pdf` files.
- **Owner-password ("secured") PDFs**: unlocked automatically (restrictions can block screen readers) and noted in the report.
- **Digitally signed PDFs**: held automatically (`signatures` check) because any edit invalidates cryptographic signatures; opt in with `publish_unsigned: true`.
- **XFA (LiveCycle) forms**: static XFA layers are removed so viewers expose the tagged AcroForm fields; dynamic XFA forms are held.
- **Review markups (Acrobat / Bluebeam)**: popups are removed from reading order (WTPDF) and unlabeled marks receive structured descriptions.
- **Inline images**: converted to ordinary Image XObjects before tagging so they receive alt text.
- **PDF/A claims**: preserved only if `veraPDF` confirms the remediated file still conforms; otherwise stripped (original remains the archival record).
- **Rotated pages & offset CropBoxes**: contrast sampling, image cropping, and form-field coordinate mapping account for rotation and transformation matrices.
- **Same-name `.pdf` + `.docx` twins**: tracked and reported separately (`<name> (Word) - accessibility report.pdf`).
- **Concurrency lock**: single-batch file lock (`logs/.pipeline.lock`) prevents multiple Java tagging JVMs from exhausting RAM.

---

## Output Layout

```text
output/<batch>/
├── <original filename>.pdf/.docx     # Remediated deliverables that passed every check
├── needs review/                     # Files held back because one or more checks failed
├── reports/
│   └── <name> - accessibility report.pdf   # Tagged PDF/UA-1 per-document audit report
├── summary.pdf                       # Tagged PDF/UA-1 batch summary + follow-up items
└── summary.md                        # Markdown batch summary

manifests/<batch>.yaml                # Editable batch manifest (titles, kinds, overrides)
attachments/<batch>/<document>/       # Stored copies of files embedded in PDFs
work/<batch>/                         # Intermediate files & caches (safe to delete)
logs/                                 # Run logs & per-document JSONL run history
```

---

## Testing

Run the fast offline unit test suite (covers multi-provider AI routing, independent eval configuration, WCAG contrast math, fidelity/eval callbacks, heading hierarchy repair, language detection, and edge-case filters):

```bash
python -m unittest discover -s tests -v
```

To generate a synthetic suite of 25+ tricky PDF/DOCX edge cases for end-to-end testing:

```bash
python scripts/make_edge_cases.py /tmp/a11y-edge-cases
python a11y_pipeline.py --source /tmp/a11y-edge-cases
```

---

## License

Released under the [MIT License](LICENSE).

## Credits & Acknowledgments

Built on open-source standards tools including [pdf-a11y](https://pypi.org/project/pdf-a11y/), [opendataloader-pdf](https://pypi.org/project/opendataloader-pdf/), [veraPDF](https://verapdf.org/), [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF), [pikepdf](https://github.com/pikepdf/pikepdf), [PyMuPDF](https://pymupdf.readthedocs.io/), and [fontTools](https://github.com/fonttools/fonttools). Page-context alt-text prompting, heading bookmarks, and before/after validation patterns were informed by ASU's `PDF_Accessibility` reference workflow.
