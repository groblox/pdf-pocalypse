# `pdf-pocalypse` — Deep Code, Pipeline & ADA / WCAG Compliance Audit

_Last updated: 2026-11-20 · Scope: Full line-by-line review of all 18 Python modules, launcher script, configuration, and validation gates against **DOJ ADA Title II** (28 CFR Part 35), **WCAG 2.1 / 2.2 Level A & AA**, **PDF/UA-1 (ISO 14289-1)**, **WTPDF 1.0 (PDF 2.0 / ISO 32005)**, the **Matterhorn Protocol 1.1**, **PAC 2024**, and **Microsoft's Accessible Word Checklist**._

---

## 1. Executive Summary

`pdf-pocalypse` is a substantive, standards-driven remediation and verification pipeline. Unlike superficial metadata wrappers, it performs real content-stream, font-program, structure-tree, form-widget, and rendered-pixel contrast repairs, then gates publication behind **veraPDF** (PDF/UA-1 + WTPDF 1.0), PAC-equivalent structural checks, visual/numerical fidelity checks, and multimodal AI evaluations.

At the same time, **no automated PDF/Word pipeline can guarantee 100% WCAG 2.1 AA / PDF/UA-1 compliance without targeted human review**, and a deep line-by-line audit of the codebase identified:
1. **Concrete code bugs** (all fixed and covered by unit tests in commits `509683c` and `4ca42d6` — see [§3](#3-code-bugs-found--fixed-during-audit)).
2. **15 ADA / WCAG / PDF/UA alignment gaps & best-practice divergences** where current heuristics or architectural trade-offs can fall short of strict WCAG 2.1/2.2 AA or assistive-technology best practices (documented in [§4](#4-ada--wcag--pdfua-alignment-gaps--best-practice-findings)).
3. **5 pipeline / caching quirks** that operators and developers should know when running partial stage combinations or Word documents (documented in [§5](#5-pipeline--architectural-quirks)).

---

## 2. WCAG 2.1 / 2.2 Level A & AA Criterion-by-Criterion Matrix

| WCAG Criterion | Level | PDF/UA-1 / WTPDF Mapping | Pipeline Status | Where Implemented | Notes & Remaining Gaps |
|---|---|---|---|---|---|
| **1.1.1 Non-text Content** | A | PDF/UA 7.3, WTPDF 8.2.5.26 | **Automated + Checked (with caps)** | `image_stage.py`, `drawing_pages.py`, `companion.py`, `alt_repair.py`, `fidelity.py`, `docx_fix.py` | Page-context alt text, tile/CAD clustering, verification pass, red-outline repair, decorative demotion. **Gaps:** hard character limits (150/400/600), 3+-page logos demoted on page 1, pure-vector diagrams without raster tiles, `.docx` SmartArt/shapes only flagged ([§4.1](#41-non-text-content--alt-text-wcag-111--pdfua-73)). |
| **1.3.1 Info and Relationships** | A | PDF/UA 7.1, 7.2, 7.4–7.7 | **Automated (PDF) / Partial (`.docx`)** | `pdf-a11y`, `struct_fixes.py`, `org_chart.py`, `docx_fix.py` | Semantic tags, `TH` `/Scope`, non-skipping headings, list numbering, org-chart nested lists, form field placement. **Gaps:** MasterFormat numbered spec headings, tables where tagger missed `<TH>`, multi-level complex table `/Headers` IDs, `.docx` headings/merged tables only flagged ([§4.2](#42-structure-headings-tables--lists-wcag-131--pdfua-7477)). |
| **1.3.2 Meaningful Sequence** | A | PDF/UA 7.2 | **Automated + Partial Human Check** | `pdf-a11y`, `struct_fixes.place_form_fields`, `org_chart.py`, `stage_intent` | Structure tree defines reading order; form fields moved after labels or into cells; `tagged_text()` sent to Stage 4 eval. **Gaps:** ~2% of form fields in untagged grids remain at page end; whole-page `<Figure>` flattening removes individual callout tags from reading order ([§4.1](#41-non-text-content--alt-text-wcag-111--pdfua-73), [§4.4](#44-forms-labels-tab-order--required-fields-wcag-135-243-253-332-412)). |
| **1.3.3 Sensory Characteristics** | A | Matterhorn 01 | **Partial (AI Review)** | `image_stage.py`, `drawing_pages.py`, `stage_intent` | Prompts forbid describing only shapes/colors or visual position without labels; source-document sensory references depend on Stage 4 AI review. |
| **1.3.4 Orientation** | AA | — | **Pass** | `alt_repair.py`, `contrast_fix.py`, `image_stage.py`, `form_labels.py` | PDFs/DOCX do not lock display orientation; rotated pages are derotated via `rotation_matrix` / `transformation_matrix`. |
| **1.3.5 Identify Input Purpose** | AA | PDF 2.0 / WTPDF attributes | **Not Covered** | — | AcroForm fields do not receive programmatic autocomplete/input-purpose attributes ([§4.4](#44-forms-labels-tab-order--required-fields-wcag-135-243-253-332-412)). |
| **1.4.1 Use of Color** | A | Matterhorn 01-007 | **Flagged (`.docx`) / Manual (PDF)** | `docx_fix.py`, `contrast_fix.py` | Drawings keep original colors (`keep_colours`); `.docx` flags recolored text to verify color isn't the sole indicator; PDFs do not detect color-only meaning automatically. |
| **1.4.3 Contrast (Minimum)** | AA | PAC 2024 contrast rule | **Automated + Gate (Text PDFs)** | `contrast_fix.py`, `docx_fix.py`, `stage_compat` | Rendered-pixel contrast measured and darkened to 4.85:1 / 3.35:1 (vector + raster under text); unfixable spans hold the PDF. **Gaps:** vector-outlined text, drawings (skipped by design), `.docx` theme/style-inherited colors and headers/footers ([§4.3](#43-color--contrast-wcag-141-143-1411)). |
| **1.4.4 Resize Text** | AA | Tagged PDF reflow | **Pass (Tagged PDF / `.docx`)** | `pdf-a11y`, `font_fixes.py`, `image_stage.py` | Real tagged text + embedded fonts + targeted OCR on text/table images allow zoom and reflow (except drawing pages intentionally flattened to `<Figure>`). |
| **1.4.5 Images of Text** | AA | Matterhorn 13 | **Automated (PDF) / Flagged (`.docx`)** | `image_stage.pages_needing_ocr`, `docx_fix.py` | PDF images classified as `text`/`table`/`form` trigger `ocrmypdf --redo-ocr` and become `/Artifact` once words are extracted; `.docx` flags pictures containing text for the author. |
| **1.4.10 Reflow** | AA | WTPDF 1.0 / Tagged PDF | **Partial** | `pdf-a11y`, `struct_fixes.py` | Valid structure tags support PDF reflow in assistive viewers; complex fixed-layout CAD sheets and multi-column forms remain fixed-layout. |
| **1.4.11 Non-text Contrast** | AA | — | **Not Covered** | — | 3:1 contrast for form-field borders, focus rings, and chart series bars/lines is not measured ([§4.3](#43-color--contrast-wcag-141-143-1411)). |
| **2.4.1 Bypass Blocks** | A | PDF/UA 7.4, 7.1 (Pagination) | **Automated** | `struct_fixes.add_bookmarks`, `pdf-a11y` | Running headers/footers marked `/Artifact /Pagination`; bookmarks (`/Outlines`) and heading structure allow jumping directly to sections. |
| **2.4.2 Page Titled** | A | PDF/UA 7.1-8, 7.1-9 | **Automated + Gate** | `manifest_builder.py`, `a11y_pipeline.py`, `docx_fix.py`, `stage_compat` | Sets XMP `dc:title`, `/Title`, `/DisplayDocTitle true`, and Word `core_properties.title`; verified in `stage_compat` and Stage 4 review. |
| **2.4.3 Focus Order** | A | PDF/UA 7.18.3 (`/Tabs /S`) | **Automated + Partial Gap** | `a11y_pipeline.py`, `struct_fixes.place_form_fields` | `/Tabs /S` enforced on every page (`tab_order` check) and `<Form>` tags moved in structure tree, **but** physical `/Annots` array order is not re-sorted ([§4.4](#44-forms-labels-tab-order--required-fields-wcag-135-243-253-332-412)). |
| **2.4.4 Link Purpose (In Context)** | A | PDF/UA 7.18.5 | **Partial** | `struct_fixes.add_text_links`, `fix_link_contents`, `companion._link_text`, `docx_fix.py` | Plain-text URLs/emails/phones linked with descriptive `/Contents`; drawing cross-references get `"Go to sheet …"`. **Gaps:** authored PDF links with vague anchor text (`"Click here"`) are not rewritten ([§4.5](#45-links-navigation--language-wcag-244-245-246-311-312)). |
| **2.4.5 Multiple Ways** | AA | PDF/UA 7.1, WTPDF 8.8 | **Automated + Gate** | `struct_fixes.add_bookmarks`, `add_structure_destinations`, `stage_compat` | Bookmarks with `/SD` structure destinations built whenever headings exist (`bookmarks` check); attached PDFs get `GoToE` bookmarks. |
| **2.4.6 Headings and Labels** | AA | PDF/UA 7.4.2, 7.18.1 | **Automated + Gate** | `struct_fixes.fix_misused_tags`, `fix_heading_levels`, `form_labels.py`, `stage_compat` | OCR junk/contact headings demoted to `<P>`; split headings joined; levels never skip (`heading_hierarchy` check); form tooltips matched to printed labels. |
| **2.5.3 Label in Name** | A | WCAG 2.5.3 / PDF `/TU` | **Automated** | `form_labels.py`, `struct_fixes.label_form_fields` | Vision model reads numbered red-outlined form fields so `/TU` (and `<Form> /Alt`) contains the exact printed label as whole words (`carries()`). |
| **3.1.1 Language of Page** | A | PDF/UA 7.2-1 | **Automated + Gate** | `manifest_builder.guess_language`, `a11y_pipeline.py`, `docx_fix.py`, `stage_compat` | Auto-detects `es`/`fr` or uses manifest `lang:`; sets PDF `/Lang` and Word `w:lang`; verified by `language` check. **Gap:** auto-detection only covers `en`/`es`/`fr` ([§4.5](#45-links-navigation--language-wcag-244-245-246-311-312)). |
| **3.1.2 Language of Parts** | AA | PDF/UA 7.2 | **Not Covered** | — | Foreign-language passages inside a document are not tagged with element-level `/Lang` ([§4.5](#45-links-navigation--language-wcag-244-245-246-311-312)). |
| **3.3.2 Labels or Instructions** | A | PDF/UA 7.18.1 | **Partial Gap** | `form_labels.py`, `struct_fixes.py` | Visible labels are copied into `/TU`, **but** trailing `*` required-field asterisks are stripped without setting the Required flag ([§4.4](#44-forms-labels-tab-order--required-fields-wcag-135-243-253-332-412)). |
| **4.1.2 Name, Role, Value** | A | PDF/UA 7.1, 7.18 | **Automated + Gate** | `pdf-a11y`, `struct_fixes.py`, `stage_compat` (`veraPDF` `ua1` + `wt1a`) | Full `veraPDF` PDF/UA-1 + WTPDF 1.0 validation, MCID/ParentTree integrity check (`mcid_references`), XFA handling, and annotation `/Contents` ↔ `/Alt` sync. |

---

## 3. Code Bugs Found & Fixed During Audit

All concrete bugs discovered during the two audit passes have been fixed in the codebase and covered by offline unit tests (`python -m unittest discover -s tests -v` — **22/22 passing**):

1. **Shadowed `CONTAINERS` constant broke form-field placement inside list items** ([struct_fixes.py](../struct_fixes.py#L320))
   * *Cause:* Line 320 defined `CONTAINERS = {"/LBody"}` for `place_form_fields()`, but line 966 redefined `CONTAINERS = {"/Document", "/Table", "/THead", "/TBody", "/TFoot", "/TR", "/L"}` for `artifact_stray_content()`. At runtime `place_form_fields()` used the second set, ignoring form labels inside `/LBody` list items.
   * *Fix (`4ca42d6`):* Renamed to `LIST_CONTAINERS` and `STRAY_CONTAINERS`.
2. **`drop_dangling_mcrs()` ignored multi-node `/ParentTree /Kids` B-trees** ([struct_fixes.py](../struct_fixes.py#L1203-L1220))
   * *Cause:* Only inspected `tree.get("/Nums")`, which is `None` on multi-page PDFs whose `ParentTree` uses `/Kids` leaf nodes, leaving unowned `/StructParents` keys that PAC flags.
   * *Fix (`4ca42d6`):* Walks both top-level `/Nums` and `/Kids` leaf nodes and updates `/Limits`.
3. **`drawing_pages._set_parent_tree()` wrote into `Kids[0]` regardless of `/Limits`** ([drawing_pages.py](../drawing_pages.py#L348-L359))
   * *Cause:* Used `next(k for k in tree.Kids if "/Nums" in k)`, inserting keys into the first kid even when `key` belonged in a higher `/Limits [lo, hi]` range.
   * *Fix (`4ca42d6`):* Routes `key` to the kid whose `/Limits` spans `key`.
4. **Heading-hierarchy check walked in reverse order if using `_walk()`** ([a11y_pipeline.py](../a11y_pipeline.py#L1142))
   * *Cause:* `struct_fixes._walk()` uses a LIFO stack without reversing children, yielding elements in reverse sibling order.
   * *Fix (`509683c`):* Uses `struct_fixes._in_order(root_tree)` (forward DFS reading order) for the `heading_hierarchy` check in `stage_compat`.
5. **Unimplemented `project.site_text_file` option** ([a11y_pipeline.py](../a11y_pipeline.py#L651-L659))
   * *Cause:* `stage_scrape()` printed a warning telling users to set `project.site_text_file` for JS-rendered pages, but never read that key.
   * *Fix (`509683c`):* Implemented `project.site_text_file` in `stage_scrape()`.
6. **Hardcoded paths, tool discovery, single-word truncation & missing direct dependency pins**
   * *Fix (`509683c`, `4ca42d6`):* Removed hardcoded `/Users/...` path in `scripts/review_alts.py`, guarded missing sample file in `scripts/make_edge_cases.py`, made `JAVA_HOME`/`verapdf`/`ocrmypdf`/Chrome/Linux fonts portable, fixed `rfind(" ") == -1` truncation in `fit_alt`/`trim`/`_short`, fixed `FAMILIES` tuple unpacking in `font_fixes._embed()`, and pinned `fonttools`, `lxml`, `numpy` in `requirements.txt`.

---

## 4. ADA / WCAG / PDF/UA Alignment Gaps & Best-Practice Findings

The following items represent genuine compliance or assistive-technology usability gaps in the current design that should be understood by reviewers or prioritized for future enhancements.

### 4.1 Non-Text Content & Alt Text (WCAG 1.1.1 / PDF/UA 7.3)

1. **Hard character caps on complex visuals (`150` / `400` / `600` chars) vs. WCAG 1.1.1 equivalent information**
   * **Where:** [ai_client.py](../ai_client.py#L74) (`ALT_MAX = 150`), [image_stage.py](../image_stage.py#L34) (`LONG_ALT_MAX = 400`), [drawing_pages.py](../drawing_pages.py#L31) (`ALT_MAX = 600`), [companion.py](../companion.py#L144) (`300` chars/sheet).
   * **Why it matters:** WCAG 1.1.1 requires non-text content to have a text alternative that serves the **equivalent purpose**. While `≤150` characters is ideal for simple photos/icons, a complex multi-series chart, dense campus map, or engineering drawing often contains more actionable data than fits in 400–600 characters. Appending `complex_note()` (*"This is a complex drawing; its full detail does not translate to text"*) satisfies PDF/UA syntactic validation (`veraPDF`) and provides honest notice, but under strict ADA Title II / WCAG 1.1.1 (Technique G92), public entities must still provide an accessible alternative or accommodation when a bidder/user needs the full data.
   * **Recommendation:** Keep concise `/Alt` for screen-reader ergonomics (since screen readers read `/Alt` as one un-navigable chunk), and use the existing `attachments:` feature ([attachments.py](../attachments.py)) to embed an accessible spreadsheet/schedule alongside complex charts or drawing sets when full data equivalence is required.

2. **Logos appearing on 3+ pages are demoted to `/Artifact` on ALL pages, including Page 1**
   * **Where:** [image_stage.py](../image_stage.py#L454) (`if kind == "decorative" or (kind == "logo" and len(pages_seen) > 2): return "DECORATIVE"`).
   * **Why it matters:** Because `pdf-a11y`'s `alt_overrides` maps an image's 8-char SHA-256 hash to `"DECORATIVE"` document-wide, a letterhead logo repeated across a 4-page memo becomes an `/Artifact` on Page 1 as well as pages 2–4. By contrast, [docx_fix.py](../docx_fix.py#L176) (`seen_hash[h] > 1`) keeps alt text on the **first** occurrence of a logo and marks only subsequent repeats decorative.
   * **Recommendation:** In `image_stage.rebind_figure_alts()`, keep the logo's alt text on its first `/Figure` occurrence (page 1) and demote only subsequent occurrences of that image hash to `/Artifact`.

3. **Pure-vector diagrams/charts with zero raster Image XObjects are invisible to `image_stage.py`**
   * **Where:** [image_stage.py](../image_stage.py#L228-L270) (`collect_units()`).
   * **Why it matters:** `collect_units()` only inspects `page.get_drawings()` when a page already contains `≥ 3` grey line-art raster image fragments (`DRAWING_MIN_FRAGMENTS = 3`). A vector chart, floor plan, or diagram drawn purely with PDF path operators (`m`, `l`, `c`, `S`, `f`) and `0` raster images is not seen by `image_stage.py`. If OpenDataLoader tags it as a `/Figure`, `pdf-a11y` falls back to context-free `_vlm_anthropic`; if OpenDataLoader doesn't tag it as a `/Figure`, `wrap_untagged_as_artifact: true` silently marks the vector paths as `/Artifact`.
   * **Recommendation:** Detect pages with high vector path counts (`len(page.get_drawings()) >= 50`) even when `len(major) == 0` raster fragments exist, or inspect any `<Figure>` created by OpenDataLoader that lacks an `alt_overrides` match using page-context snapshots.

4. **Whole-page `<Figure>` flattening on drawing pages trades granular text navigation for coherent alt text**
   * **Where:** [drawing_pages.py](../drawing_pages.py#L189-L274) (`figure_page()`), [companion.py](../companion.py#L230-L302) (`tag_original()`).
   * **Why it matters:** Replacing a drawing page's scattered `<P>` callout tags with a single `<Figure>` prevents a screen reader from babbling `"4• RISER #6 GND BUS"` in random drawing order. However, for vector drawings (in `drawing_pages.py` or vector `companion` sets), any callout text inside the drawing is no longer individually navigable as `<P>` tags in the structure tree (and in `companion.py`, `_drop_invisible_text()` strips hidden scanner OCR layers).

### 4.2 Structure, Headings, Tables & Lists (WCAG 1.3.1 / PDF/UA 7.4–7.7)

5. **Documents with zero headings or untagged MasterFormat headings pass `stage_compat`**
   * **Where:** [a11y_pipeline.py](../a11y_pipeline.py#L1147-L1160) (`bookmarks` and `heading_hierarchy` checks only run `if heading_levels:`), [struct_fixes.py](../struct_fixes.py#L1546-L1728).
   * **Why it matters:**
     * If a 15-page text PDF has **zero `<H1>`–`<H6>` tags** (because the tagger missed them all), `heading_levels` is empty, so `veraPDF`, `bookmarks`, and `heading_hierarchy` all report **Pass** even though a long text document without headings or bookmarks fails WCAG 1.3.1 / 2.4.5 / 2.4.6.
     * In construction specification books, CSI MasterFormat section titles (`"SECTION 14 20 00 - VERTICAL CONVEYING SYSTEMS"`) are frequently tagged by OpenDataLoader as `<P>` or `<Caption>`, resulting in thin bookmark trees.
     * `fix_heading_levels()` ([struct_fixes.py](../struct_fixes.py#L1756)) mechanically lowers skipped levels (`H1 → H3` becomes `H1 → H2`), which fixes PDF/UA 7.4.2 syntax, but can flatten intended subsection depth if an intermediate heading was missed by the tagger.
   * **Recommendation:** Add a rule in `fix_misused_tags()` promoting standalone MasterFormat/numbered section lines to headings, and flag multi-page (`≥ 5` pages) `kind: text` PDFs that end up with `0` headings in `stage_compat`.

6. **Tables missing `<TH>` cells or with complex multi-row headers**
   * **Where:** [struct_fixes.py](../struct_fixes.py#L1646-L1669), [docx_fix.py](../docx_fix.py#L221-L242).
   * **Why it matters:**
     * `fix_misused_tags()` adds `/Scope` to existing `<TH>` cells, but if OpenDataLoader tagged a table's header row as `<Caption>` (observed on the Travel Reimbursement form) or tagged all cells as `<TD>`, the table has **zero `<TH>` cells** and no check in `stage_compat` catches a `<Table>` with no `<TH>`.
     * Complex tables with spanning headers (`/ColSpan > 1` / `/RowSpan > 1` across multiple header rows) get heuristic `/Scope` rather than explicit `/Headers` ID arrays (Matterhorn 15-003).
     * In `.docx` ([docx_fix.py](../docx_fix.py#L232)), every `≥2×2` table gets `<w:tblHeader/>` on row 0, which could mis-mark a 2×2 layout table (such as a signature block) as a data table with a repeating header row.
   * **Recommendation:** In `fix_misused_tags()` (or `stage_compat`), detect `<Table>` elements that have `0` `<TH>` cells (promote row 0 `<TD>`s to `<TH Scope=/Column>` when row 0 is non-numeric text, or fail a `table_headers` check in `stage_compat`).

### 4.3 Color & Contrast (WCAG 1.4.1, 1.4.3, 1.4.11)

7. **PDF contrast repair misses outlined text, text-in-images, gradients, and WCAG 1.4.11 non-text contrast**
   * **Where:** [contrast_fix.py](../contrast_fix.py#L50-L84).
   * **Why it matters:**
     * `failing_pairs()` measures spans returned by `page.get_text("dict")`. Text converted to vector curves or embedded inside an un-OCR'd raster image has no visible font span and is not checked.
     * Background color is estimated from the single modal (most common) pixel in the span's bounding box (`counts.most_common(1)[0][0]`), which can misjudge text placed across a gradient or two-tone background.
     * WCAG 2.1 AA **1.4.11 Non-Text Contrast (3:1)** (form input borders, chart bars/lines, icons) is not checked.

8. **Word (`.docx`) contrast repair only checks explicit run hex colors on body/table paragraphs**
   * **Where:** [docx_fix.py](../docx_fix.py#L243-L245), lines 267–270, 293–322.
   * **Why it matters:** `_fix_runs()` only inspects `<w:rPr><w:color w:val="RRGGBB"/>` against cell or paragraph `<w:shd>`. It does not resolve colors inherited from Word paragraph/character styles (`w:pStyle`/`w:rStyle`), theme colors without an explicit hex `w:val`, run-level `<w:shd>`/`<w:highlight>`, or text inside headers, footers, and text boxes (`w:txbxContent`).

### 4.4 Forms: Labels, Tab Order & Required Fields (WCAG 1.3.5, 2.4.3, 2.5.3, 3.3.2, 4.1.2)

9. **Required-field asterisks (`*`) are stripped from tooltips without setting the PDF Required flag**
   * **Where:** [form_labels.py](../form_labels.py#L130) (`re.sub(r"^[\s:_$*]+|[\s:_$*]+$", "", t)`).
   * **Why it matters:** When a printed form labels a mandatory field `"Contractor Name *"` or `"* Bid Date:"`, `_tidy()` strips the `*` so the tooltip `/TU` becomes `"Contractor Name"`. Because neither `form_labels.py` nor `struct_fixes.label_form_fields()` sets the widget's `/Ff` Required bit (bit 2, value `2`) or appends `" (required)"`, screen-reader users lose the required-field indicator (WCAG 3.3.2 / 4.1.2).
   * **Recommendation:** Detect `*` or `required` in the raw printed label before stripping punctuation, and either set `/Ff |= 2` on the field dictionary or preserve `" (required)"` in `/TU`.

10. **Physical widget annotation order (`page.Annots`) is not re-sorted to match structure reading order**
    * **Where:** [struct_fixes.py](../struct_fixes.py#L338-L530) (`place_form_fields()`).
    * **Why it matters:** `place_form_fields()` moves `<Form>` elements into reading order inside `/StructTreeRoot`, and `/Tabs /S` instructs compliant viewers to follow structure order. However, some PDF viewers still tab through form fields in `page.Annots` array order, and ~2% of fields in visual grids not tagged as `<Table>` remain at the end of the page's structure tree.
    * **Recommendation:** Re-sort `page.obj.Annots` to match the final document order of `<Form>` and `<Link>` `/OBJR` references in `/StructTreeRoot`.

### 4.5 Links, Navigation & Language (WCAG 2.4.4, 2.4.5, 2.4.6, 3.1.1, 3.1.2)

11. **Authored PDF links with vague anchor text (`"Click here"`, bare URLs) are not flagged or descriptive**
    * **Where:** [struct_fixes.py](../struct_fixes.py#L1336-L1474).
    * **Why it matters:** `add_text_links()` adds `/Contents` (`"Link to …"`, `"Email …"`, `"Call …"`) to plain-text URLs/emails/phones it discovers, and `docx_fix.py` flags `"click here"` or bare-URL links in Word files. However, pre-existing `/Link` annotations in a PDF whose visible link text is `"click here"` or a raw URL and whose `/Contents` is empty are not rewritten or flagged (WCAG 2.4.4).

12. **Language auto-detection only covers English/Spanish/French, strips BCP-47 region subtags in `pdf-a11y`, and lacks WCAG 3.1.2 (Language of Parts)**
    * **Where:** [manifest_builder.py](../manifest_builder.py#L138-L156), [a11y_pipeline.py](../a11y_pipeline.py#L397).
    * **Why it matters:**
      * `guess_language()` only checks `en`, `es`, and `fr` stopwords; other languages default to `en` unless set in the manifest.
      * `a11y_pipeline.py` line 397 passes `meta["lang"].split("-")[0]` (`"en"` or `"es"`) to `pdf-a11y` (whereas `companion.py` and `docx_fix.py` write full BCP-47 codes like `"en-US"` / `"es-US"`).
      * Bilingual documents receive a single document-level `/Lang`; mixed-language passages are not tagged with element-level `/Lang` attributes (WCAG 3.1.2 Level AA).

13. **Generated report PDFs rely solely on Chrome's outline generator**
    * **Where:** [report.py](../report.py#L553-L603) (`to_pdf()`).
    * **Why it matters:** `to_pdf()` passes `--generate-pdf-document-outline` to headless Chrome and runs `add_structure_destinations(pdf)` via `apply_wtpdf()`, but does not call `struct_fixes.add_bookmarks(pdf)` as a fallback if a Chromium build omits `/Outlines`.

### 4.6 Word (`.docx`) Governance & Publishing Gate

14. **Word (`.docx`) files with structural author flags still auto-publish to `output/<batch>/`**
    * **Where:** [a11y_pipeline.py](../a11y_pipeline.py#L1335-L1358), [docx_fix.py](../docx_fix.py#L133-L290).
    * **Why it matters:** In `docx_fix.py`, missing heading styles, skipped heading levels, merged/split table cells, nested tables, floating images, text-in-pictures, SmartArt/charts without alt text, `"click here"` links, typed fake lists, and empty-paragraph spacing are recorded with `severity="flag"`. Because `process_docx()` only fails `docx_problems` on `severity == "error"`, a `.docx` with **no headings, merged tables, and untagged SmartArt** passes `compat["passed"] == True` and is published directly into `output/<batch>/` (with the issues listed in its report under *"For the author"*), rather than being held in `needs review/`.
    * **Recommendation:** Consider an option (e.g. `strict_docx: true` or holding `.docx` files when high-severity structural flags like missing headings, merged tables, or untagged shapes/SmartArt are present) so un-remediated author issues aren't inadvertently posted before the author fixes them.

15. **Word (`.docx`) files do not run Stage 4 (`stage_intent`) website content review**
    * **Where:** [a11y_pipeline.py](../a11y_pipeline.py#L1313-L1358).
    * **Why it matters:** PDFs are checked against the scraped website/calendar/Box context in `stage_intent` (verifying title accuracy, edition/date match, and alt-text quality in context), whereas `.docx` files skip `stage_intent` and only run `fidelity.alt_sanity_texts()`.

---

## 5. Pipeline & Architectural Quirks

1. **In-place mutation of `02_a11y` PDF by `alt_repair.py` during Stage 5 (`stage_compat`)**:
   * `alt_repair.repair()` modifies `work/<batch>/02_a11y/<stem>_a11y.pdf` (or `_org.pdf` / `_attached.pdf`) in place. In a standard full run, `stage_compat` executes *before* `stage_intent`, so `stage_intent` reviews the repaired alt text. However, `plan_stamps()` hashes `intent` from `final` (`remediate`/`attach`), not `compat` — so running `--stages remediate,intent` and later `--stages compat,intent` won't invalidate `intent` after an alt repair unless `--force` is used.
2. **Word (`.docx`) files are not tracked in `STAMPS`**:
   * `plan_stamps()` skips `.docx` files (`not n.lower().endswith(".docx")`). Every run that includes `remediate` re-runs `docx_fix.remediate()` and the `.docx` `alt_sanity_texts` eval call (image descriptions themselves remain cached in `work/<batch>/docx/<stem>.images.json`).
3. **macOS `/System/Library/Fonts/Symbol.ttf` dependency for Symbol PUA & CID Symbol repair**:
   * While `Arial`, `Times New Roman`, and `Courier New` fall back to Linux `Liberation` / `DejaVu` fonts, `_windows_symbol_mapping()`, `_symbol_code_to_unicode()`, and `fix_cid_symbol_fonts()` in [font_fixes.py](../font_fixes.py) inspect `/System/Library/Fonts/Symbol.ttf`. On Linux without `Symbol.ttf`, those helpers degrade safely to `{}` / `None`, leaving Symbol PUA bullets unmapped.
4. **Simple-font MCID text extraction (`_page_mcid_text`) assumes `cp1252` when `/ToUnicode` is absent**:
   * `_page_mcid_text()` decodes 1-byte character codes without a `/ToUnicode` map as `cp1252`, ignoring `/MacRomanEncoding` or custom `/Differences`. `stage_intent` mitigates this by requiring `≥ 97%` word agreement with PyMuPDF's page text before using `tagged_text()`.
5. **`fidelity.appearance()` 1.0% pixel threshold at 50 dpi**:
   * Catches page-level visual regressions (missing images, broken streams, shifted paragraphs) while ignoring `<0.3%` font/contrast shifts, but will not catch a 1–2 character glyph substitution on its own (numerical changes are caught separately by `fidelity.numbers()`).

---

## 6. Prioritized Recommendations for Stricter ADA / WCAG Alignment

If you want to tighten `pdf-pocalypse` further toward zero-gap WCAG 2.1/2.2 AA compliance, here is the highest-impact order of work:

1. **Preserve required-field indicators on forms (`form_labels.py` + `struct_fixes.py`)**: Before stripping `*` in `form_labels._tidy()`, detect trailing/leading `*` and either append `" (required)"` to `/TU` or set `/Ff |= 2` on the AcroForm field.
2. **First-page logo preservation (`image_stage.py`)**: Keep alt text on the first occurrence of a multi-page logo and demote only subsequent occurrences (`page > first_page`) to `/Artifact`.
3. **Table header verification (`struct_fixes.py` + `stage_compat`)**: Check that every `<Table>` has at least one `<TH>` (or promote row 0 to `<TH>` when appropriate) and report a `table_headers` check in `stage_compat`.
4. **MasterFormat heading promotion & zero-heading warning (`struct_fixes.py` + `stage_compat`)**: Promote standalone numbered specification section titles (`SECTION \d\d \d\d \d\d`) to headings, and warn/hold multi-page text PDFs that have `0` headings.
5. **Re-sort `page.Annots` widget order (`struct_fixes.place_form_fields`)**: Match `page.obj.Annots` widget order to the structure tree's reading order.
6. **Optional strict `.docx` hold gate (`a11y_pipeline.py`)**: Hold `.docx` files in `needs review/` when critical author flags (no headings, untagged shapes/SmartArt, merged tables) are present.
