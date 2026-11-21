"""Structure-tree repairs applied after pdf-a11y.

  - demote_placeholder_figures: pdf-a11y leaves decorative images as /Figure elements
    carrying the tagger's "image N" placeholder (it satisfies veraPDF's non-empty /Alt
    rule), so screen readers announce "image 1 … image 65". Drop those elements,
    re-mark their content /Artifact, and clear their ParentTree slots.
  - fix_form_elements: the tagger puts the field's underline/label content inside the
    /Form element next to the widget (PDF/UA 7.18.4-2 allows only the widget), and gives
    every field the alt text "Annotation". Move the content to a sibling /Span and name
    the field from its tooltip.
  - label_form_fields: pdf-a11y's "nearby text" tooltips search every page and mix up PDF and
    screen coordinates, so fields hover with random lines from elsewhere in the document.
    Replace each tooltip it invented with the field's real label from its own page.
  - set_list_numbering: WTPDF 8.2.5.25 wants /ListNumbering on lists with labels; read the
    labels' actual text to choose Decimal / LowerAlpha / Disc / ….
  - fix_nested_art: <Art> may not contain <Art> (ISO 32005); inner ones become <Sect>.
  - sync_annotation_alt: annotation /Contents and its element's /Alt must be byte-identical.
  - add_bookmarks: long documents get an outline built from their H1–H3 headings
    (WCAG 2.4.5; the approach ASU's PDF_Accessibility uses).
"""
from __future__ import annotations

import re

import pikepdf

from font_fixes import parse_tounicode

PLACEHOLDER_ALT = re.compile(r"^image \d+$", re.I)


def _kids(node) -> list:
    k = node.get("/K")
    if k is None:
        return []
    return list(k) if isinstance(k, pikepdf.Array) else [k]


def _walk(root):
    """Yield (parent, element) for every struct element under root."""
    stack, seen = [root], set()
    while stack:
        node = stack.pop()
        for kid in _kids(node):
            if isinstance(kid, pikepdf.Dictionary) and "/S" in kid and kid.objgen not in seen:
                seen.add(kid.objgen)
                yield node, kid
                stack.append(kid)


def _parent_tree_entry(tree, key: int):
    nums = tree.get("/Nums")
    if nums is not None:
        for i in range(0, len(nums), 2):
            if int(nums[i]) == key:
                return nums[i + 1]
    for kid in tree.get("/Kids", []):
        found = _parent_tree_entry(kid, key)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
def _figure_mcids(fig, page_index) -> list[tuple[int, int]] | None:
    """(page, MCID) pairs a /Figure owns, or None if it holds anything but plain marked content."""
    default_pg = fig.get("/Pg")
    out = []
    for k in _kids(fig):
        if isinstance(k, int):
            pg, mcid = default_pg, k
        elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR" and "/Stm" not in k:
            pg, mcid = k.get("/Pg", default_pg), int(k.MCID)
        else:
            return None
        if pg is None or pg.objgen not in page_index:
            return None
        out.append((page_index[pg.objgen], int(mcid)))
    return out or None


def _bdc_mcid(ins, props) -> int | None:
    if str(ins.operator) != "BDC" or ins.operands[0] != "/Figure":
        return None
    d = ins.operands[1]
    d = props.get(d) if isinstance(d, pikepdf.Name) else d
    return int(d.MCID) if isinstance(d, pikepdf.Dictionary) and "/MCID" in d else None


def demote_placeholder_figures(pdf: pikepdf.Pdf) -> int:
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    candidates = []
    for parent, el in _walk(root):
        if el.S == "/Figure" and PLACEHOLDER_ALT.match(str(el.get("/Alt", ""))):
            mcids = _figure_mcids(el, page_index)
            if mcids:
                candidates.append((parent, el, mcids))

    # Only demote figures whose marked content sits directly in the page stream.
    wanted: dict[int, set[int]] = {}
    for _, _, mcids in candidates:
        for pg, mcid in mcids:
            wanted.setdefault(pg, set()).add(mcid)
    page_ops, present = {}, {}
    for pg in wanted:
        page = pdf.pages[pg]
        props = page.get("/Resources", {}).get("/Properties", {})
        page_ops[pg] = pikepdf.parse_content_stream(page)
        present[pg] = {m for ins in page_ops[pg] if (m := _bdc_mcid(ins, props)) in wanted[pg]}
    demote = [c for c in candidates if all(m in present[pg] for pg, m in c[2])]
    if not demote:
        return 0
    drop = {(pg, m) for _, _, mcids in demote for pg, m in mcids}

    for pg, ops in page_ops.items():
        page = pdf.pages[pg]
        props = page.get("/Resources", {}).get("/Properties", {})
        new_ops = []
        for ins in ops:
            if (pg, _bdc_mcid(ins, props)) in drop:
                ins = pikepdf.ContentStreamInstruction([pikepdf.Name.Artifact], pikepdf.Operator("BMC"))
            new_ops.append(ins)
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
        entry = _parent_tree_entry(root.ParentTree, int(page.get("/StructParents", -1)))
        if isinstance(entry, pikepdf.Array):
            for p_, m in drop:
                if p_ == pg and m < len(entry):
                    entry[m] = pikepdf.Object.parse(b"null")

    for parent, fig, _ in demote:
        if isinstance(parent.K, pikepdf.Array):
            parent.K = pikepdf.Array([x for x in parent.K if not (isinstance(x, pikepdf.Dictionary)
                                                                  and x.objgen == fig.objgen)])
        else:
            del parent["/K"]
    return len(demote)


# ---------------------------------------------------------------------------
# What a field label plausibly looks like; anything else (e.g. the mis-encoded tooltip
# "reD»" on a DCM form check box) is not worth reading aloud.
LABEL_TEXT = re.compile(r"^[\w .,:;()/#&'’\-–?$%]+$")


def _field_name(widget) -> str | None:
    field = widget
    while "/T" not in field and "/TU" not in field and "/Parent" in field:
        field = field.Parent
    for key in ("/TU", "/T"):
        value = str(field.get(key, "")).strip()
        if value and LABEL_TEXT.match(value):
            return value
    return None


def fix_form_elements(pdf: pikepdf.Pdf) -> dict[str, int]:
    root = pdf.Root.get("/StructTreeRoot")
    stats = {"forms_split": 0, "forms_named": 0}
    if root is None:
        return stats
    for parent, form in list(_walk(root)):
        if form.S != "/Form" or isinstance(form.get("/A"), pikepdf.Dictionary) and "/Role" in form.A:
            continue
        kids = _kids(form)
        objrs = [k for k in kids if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR"]
        if len(objrs) != 1:
            continue
        widget = objrs[0].get("/Obj")
        name = _field_name(widget) if isinstance(widget, pikepdf.Dictionary) else None
        own = isinstance(widget, pikepdf.Dictionary) and "/T" not in widget and "/TU" in widget
        if name is not None and (own or str(form.get("/Alt", "")) in ("", "Annotation")
                                 or not LABEL_TEXT.match(str(form.get("/Alt", "")))):
            form.Alt = pikepdf.String(str(name))
            stats["forms_named"] += 1
        if isinstance(widget, pikepdf.Dictionary) and "/Alt" in form:
            widget.Contents = form.Alt  # WTPDF 8.9.4.2: annotation Contents must equal the enclosing Alt

        others = [k for k in kids if not (isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR")]
        if not others:
            continue
        pg = form.get("/Pg") or objrs[0].get("/Pg")
        span = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name.Span, P=parent, K=pikepdf.Array(others)))
        if pg is not None:
            span.Pg = pg
        form.K = pikepdf.Array([objrs[0]])

        # Repoint the moved MCIDs' ParentTree slots at the new span.
        for k in others:
            mcid, kpg = (k, pg) if isinstance(k, int) else (int(k.get("/MCID", -1)), k.get("/Pg", pg))
            if kpg is None or mcid < 0:
                continue
            entry = _parent_tree_entry(root.ParentTree, int(kpg.get("/StructParents", -1)))
            if isinstance(entry, pikepdf.Array) and mcid < len(entry):
                entry[mcid] = span

        siblings = _kids(parent)
        idx = next(i for i, s in enumerate(siblings)
                   if isinstance(s, pikepdf.Dictionary) and s.objgen == form.objgen)
        siblings.insert(idx + 1, span)
        parent.K = pikepdf.Array(siblings)
        stats["forms_split"] += 1
    return stats


CHECK_GLYPHS = "☐☑☒□■▪◻"  # box characters typed in front of check-box options


def _label_text(text: str) -> str:
    return re.sub(rf"^[\s{CHECK_GLYPHS}]+|[\s:*_=]+$", "", " ".join(text.split())).strip()


def _text_lines(page) -> list:
    """Each text line as (rect, text, spans); rects cover only the visible characters (a label's
    trailing spaces often run under the field next to it)."""
    import pymupdf
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        for line in b.get("lines", []):
            spans = []
            for s in line["spans"]:
                chars = [c for c in s["chars"] if c["c"].strip()]
                if chars:
                    r = pymupdf.Rect(chars[0]["bbox"]) | pymupdf.Rect(chars[-1]["bbox"])
                    r.y0, r.y1 = s["bbox"][1], s["bbox"][3]
                    spans.append((r, "".join(c["c"] for c in s["chars"])))
            if spans:
                rect = pymupdf.Rect(spans[0][0])
                for r, _ in spans:
                    rect |= r
                out.append((rect, "".join(t for _, t in spans), spans))
    return out


def _overlaps_x(a, b, frac: float = 0.5) -> bool:
    return min(a.x1, b.x1) - max(a.x0, b.x0) > frac * min(a.width, b.width)


def _page_field_labels(page) -> dict[str, str]:
    """Field name -> label read from the page: the text left of a field ("Bid Date: ____"); right
    of a check box, after the question it answers ("Completed on Schedule?: Yes"); a table
    cell's row label and column header ("TRIR, Year 2", or "Title, row 3"); else the prompt
    sentence right above a box."""
    lines, widgets = _text_lines(page), list(page.widgets())
    spans = [s for _, _, ss in lines for s in ss]
    rects = [w.rect for w in widgets]
    text_fields = [w.rect for w in widgets if w.field_type_string not in ("CheckBox", "RadioButton")]
    cells = [d["rect"] for d in page.get_drawings() if d.get("fill") is not None and 6 < d["rect"].height < 40]
    labels: dict[str, str] = {}

    def labels_a_field(b) -> bool:  # "Bonding Capacity ($): ____" is a field's label, not a question
        return any(o.x0 >= b.x1 - 2 and abs((o.y0 + o.y1 - b.y0 - b.y1) / 2) < max(5, o.height / 2)
                   for o in text_fields)

    def context(r) -> str | None:
        """What a check box answers: the nearest question ("Completed on Schedule?") or short
        heading ("Financial Statements") above it, starting left of it, past the other options
        (indented like this one) and the labels of text fields."""
        for _, b, t in sorted(((r.y0 - b.y1, b, t) for b, t, _ in lines
                               if 0 <= r.y0 - b.y1 < 200 and b.x0 < r.x0 - 4 and _label_text(t)),
                              key=lambda dbt: dbt[0]):  # a tie mustn't compare the Rects
            if labels_a_field(b):
                continue
            if t.rstrip().endswith((":", "?")) or (len(t.split()) <= 8 and not t.rstrip().endswith(".")):
                return re.sub(r"^section\s+\d+\s*[–-]\s*", "", _label_text(t), flags=re.I)
        return None

    for w in widgets:
        r = w.rect
        cy = (r.y0 + r.y1) / 2
        same = [(b, t) for b, t in spans if abs((b.y0 + b.y1) / 2 - cy) < max(5, r.height / 2) and _label_text(t)]
        if w.field_type_string in ("CheckBox", "RadioButton"):
            right = sorted((b.x0 - r.x1, t) for b, t in same if b.x0 >= r.x0 + r.width / 2 and b.x0 - r.x1 < 40)
            own = _label_text(right[0][1]) if right else None
            asked = context(r)
            if w.field_type_string == "RadioButton":  # one field for the whole group: named by its question
                label = asked or own
            else:
                label = f"{asked}: {own}" if asked and own else own
        else:
            left = sorted((r.x0 - b.x1, t) for b, t in same if b.x1 <= r.x0 + 2 and r.x0 - b.x1 < 150)
            label = _label_text(left[0][1]) if left else None
            # a table column: fields stacked under one another below a header in a filled cell
            top, row = r, 1
            while up := next((o for o in rects if _overlaps_x(o, top) and -5 <= top.y0 - o.y1 < 6), None):
                top, row = up, row + 1
            below = next((o for o in rects if _overlaps_x(o, r) and -5 <= o.y0 - r.y1 < 6), None)
            head = sorted((top.y0 - b.y1, t) for b, t in spans if -2 <= top.y0 - b.y1 < 8
                          and _overlaps_x(b, top, 0.8) and _label_text(t)
                          and any(c.contains(b) and c.width < 1.5 * top.width + 20 for c in cells))
            if head and (row > 1 or below):
                if not label:  # the row's label cell, however far left
                    row_label = sorted((r.x0 - b.x1, t) for b, t in same if b.x1 <= r.x0 + 2)
                    label = _label_text(row_label[0][1]) if row_label else None
                h = _label_text(head[0][1])
                label = f"{label}, {h}" if label else f"{h}, row {row}"
            if not label:  # a box under its prompt: the prompt's whole sentence
                above = sorted((r.y0 - b.y1, i) for i, (b, t, _) in enumerate(lines)
                               if -3 <= r.y0 - b.y1 < 20 and _overlaps_x(b, r, 0) and _label_text(t))
                if above:
                    cur = lines[above[0][1]]
                    parts = [cur[1]]
                    while prev := next((ln for ln in lines if ln is not cur and -5 <= cur[0].y0 - ln[0].y1 < 6
                                        and _overlaps_x(ln[0], cur[0], 0.3)
                                        and not ln[1].rstrip().endswith((":", "?", "."))
                                        and not any(c in ln[1] for c in CHECK_GLYPHS)), None):
                        cur = prev
                        parts.insert(0, cur[1])
                    label = _label_text(" ".join(parts))[:200]
        if label and w.field_name not in labels:
            labels[w.field_name] = label
    return labels


GROUPING = {"/Document", "/Part", "/Sect", "/Div", "/Art", "/NonStruct"}
TEXT_BLOCKS = {"/P", "/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
CONTAINERS = {"/LBody"}  # a label in one: its field goes inside, at the end
CELLS = {"/TD", "/TH"}  # table fields are placed by row and column (_table_cells), not by label text


def _in_order(root):
    """(parent, element) for every struct element, in document (reading) order."""
    out, seen = [], set()

    def visit(node):
        for kid in _kids(node):
            if isinstance(kid, pikepdf.Dictionary) and "/S" in kid and kid.objgen not in seen:
                seen.add(kid.objgen)
                out.append((node, kid))
                visit(kid)
    visit(root)
    return out


def place_form_fields(pdf: pikepdf.Pdf) -> dict[str, int]:
    """Put each form field's Form element right after its printed label in reading order. The
    tagger appends a page's fields after all its text, so a screen reader read "Project
    Manager: Email Address: Phone Number:" in one run-together paragraph and met the fields
    only after the rest of the page. The label is found by the field's tooltip (which carries
    the printed label: label_form_fields) among the page's marked-content pieces; a paragraph
    holding several labels is split after each one, and the field goes after its piece. A field
    whose label names a table's row and column ("Year 2022, Revenue", "Row 2, Title") goes into
    that cell. A link on the field (an e-mail address, a phone number) follows the field. Only
    fields that sit directly in a grouping element (Document, Sect, Div …) move; fields already
    in a table cell or list item stay. Run after label_form_fields and fix_form_elements."""
    from form_labels import norm
    root = pdf.Root.get("/StructTreeRoot")
    stats = {"fields_placed_by_label": 0, "label_paragraphs_split": 0, "fields_placed_in_cells": 0}
    if root is None:
        return stats
    tree = root.get("/ParentTree")
    pages = {pg.objgen: i for i, pg in enumerate(pdf.pages)}
    forms, links, blocks = [], [], []  # (parent, element) pairs
    for parent, el in _walk(root):
        s = str(el.get("/S"))
        if str(parent.get("/S")) not in GROUPING:
            continue
        objrs = [k for k in _kids(el) if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR"]
        if s == "/Form" and len(objrs) == 1:
            forms.append((parent, el, objrs[0].Obj))
        elif s == "/Link" and len(objrs) == 1:
            links.append((parent, el, objrs[0].Obj))
    for parent, el in _in_order(root):  # label pieces in reading order, outside tables
        if str(el.get("/S")) in TEXT_BLOCKS | CONTAINERS and str(parent.get("/S")) not in CELLS \
                and el.get("/Pg") is not None and _kids(el) and all(isinstance(k, int) for k in _kids(el)):
            blocks.append((parent, el))
    if not forms:
        return stats
    texts: dict = {}

    def mcid_text(pg) -> dict[int, str]:
        if pg.objgen not in texts:
            texts[pg.objgen] = _page_mcid_text(pg)
        return texts[pg.objgen]

    def page_of(annot):
        pg = annot.get("/P")
        return pg.objgen if pg is not None and pg.objgen in pages else None

    def top(annot) -> float:
        r = [float(v) for v in annot.get("/Rect", [0, 0, 0, 0])]
        return -max(r[1], r[3])

    # fields in a table's grid first (row and column both named: the surest match)
    cells = _table_cells(root, [(f, w) for _, f, w in forms], page_of, top, norm)
    in_cells = {f.objgen for f, _ in cells}
    # each other field's label piece: (block, mcid); an exact match beats a containing one, and of
    # equally good pieces the next one after the previous field's (fields go top to bottom)
    used: set = set()
    partial: set = set()  # pieces found as part of a wrapped label: no paragraph break before them
    placed: dict = {}  # (block objgen, mcid) -> [Form, …]
    by_page: dict = {}
    for parent, form, widget in forms:
        if form.objgen not in in_cells:
            by_page.setdefault(page_of(widget), []).append((parent, form, widget))
    for pg_key, items in by_page.items():
        if pg_key is None:
            continue
        cands = []  # (order, block, mcid, normalized text) in reading order
        for parent, b in blocks:
            if b.Pg.objgen == pg_key:
                for m in _kids(b):
                    cands.append((len(cands), b, int(m), norm(mcid_text(b.Pg).get(int(m), ""))))
        last = -1
        for parent, form, widget in sorted(items, key=lambda t: (top(t[2]), float(t[2].Rect[0]))):
            full = _field_name(widget) or ""
            label = norm(re.sub(r",? row \d+$| \(\d+ of \d+\)$", "", full))
            repeat = label != norm(full)  # "Previous Company Names, row 2" shares row 1's label
            if not label:
                continue
            free = [c for c in cands if (repeat or (c[1].objgen, c[2]) not in used) and c[3]]
            head = " ".join(label.replace("…", "").split()[:6])
            # the piece that says the label: exactly, containing it, starting like a long label (cut
            # at 150 characters), or the longest piece of it ("Is your company … project?" for a
            # tooltip that adds its "If yes, please explain")
            tiers = [[c for c in free if c[3] == label],
                     [c for c in free if f" {label} " in f" {c[3]} "],
                     [c for c in free if len(label.split()) >= 6 and c[3].startswith(head)],
                     [c for c in free if len(c[3].split()) >= 2 and f" {c[3]} " in f" {label} "]]
            n = next((i for i, t in enumerate(tiers) if t), None)
            if n is None:
                continue
            tier = tiers[n]
            if n == 3:  # pieces of the label: the longest says most of it
                tier = [c for c in tier if len(c[3]) == max(len(x[3]) for x in tier)]
            after = [c for c in tier if c[0] > last] or tier
            order, b, m, _ = min(after, key=lambda c: (c[0] <= last, len(c[3]), c[0]))
            used.add((b.objgen, m))
            if n >= 2:
                partial.add((b.objgen, m))
            last = order
            placed.setdefault((b.objgen, m), []).append(form)

    by_label = {f.objgen for fs in placed.values() for f in fs}
    if not placed and not cells:
        return stats
    moving = by_label | in_cells
    # a link on a moving field's widget follows that field
    follow: dict = {}
    for parent, link, annot in links:
        r = [float(v) for v in annot.get("/Rect", [0, 0, 0, 0])]
        for parent_f, form, widget in forms:
            if form.objgen not in moving or page_of(widget) != page_of(annot):
                continue
            w = [float(v) for v in widget.Rect]
            if min(r[2], w[2]) > max(r[0], w[0]) and min(r[3], w[3]) > max(r[1], w[1]):
                follow.setdefault(form.objgen, []).append(link)
                moving.add(link.objgen)
                break

    def detach(el):
        par = el.P
        par.K = pikepdf.Array([k for k in _kids(par) if not (isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen)])

    for _, el, _ in forms + links:
        if el.objgen in moving:
            detach(el)
    for form, cell in cells:  # into its table cell, after anything already there
        form.P = cell
        cell.K = pikepdf.Array(_kids(cell) + [form] + follow.get(form.objgen, []))
        for link in follow.get(form.objgen, []):
            link.P = cell
        stats["fields_placed_in_cells"] += 1
    for parent, b in blocks:
        kids = _kids(b)
        if not any((b.objgen, int(m)) in placed for m in kids):
            continue
        if str(b.S) in CONTAINERS:
            extra = []
            for m in kids:
                for f in placed.get((b.objgen, int(m)), []):
                    f.P = b
                    extra.append(f)
                    stats["fields_placed_by_label"] += 1
                    for link in follow.get(f.objgen, []):
                        link.P = b
                        extra.append(link)
            b.K = pikepdf.Array(list(kids) + extra)
            continue
        # each label is a paragraph of its own (with punctuation that trails it, "Project Duration" "="),
        # followed by its fields; text between labels ("Prior to Prebid Conference") stays a paragraph
        words = mcid_text(b.Pg)
        pieces, cur, fields = [], [], []
        for m in kids:
            if (b.objgen, int(m)) in placed:
                if cur and fields:
                    pieces.append((cur, fields))
                    cur = []
                elif cur and (b.objgen, int(m)) not in partial:
                    pieces.append((cur, []))
                    cur = []
                cur, fields = cur + [m], placed[(b.objgen, int(m))]
            elif fields and not norm(words.get(int(m), "")):
                cur.append(m)
            else:
                if fields:
                    pieces.append((cur, fields))
                    cur, fields = [], []
                cur.append(m)
        if cur:
            pieces.append((cur, fields))
        out = []
        for n, (mcids, fields) in enumerate(pieces):
            if n == 0:
                el = b
                b.K = pikepdf.Array(mcids) if len(mcids) > 1 else mcids[0]
            else:
                el = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=b.S, P=parent,
                                                          Pg=b.Pg, K=pikepdf.Array(mcids) if len(mcids) > 1 else mcids[0]))
                entry = _parent_tree_entry(tree, int(b.Pg.get("/StructParents", -1))) if tree is not None else None
                if isinstance(entry, pikepdf.Array):
                    for m in mcids:
                        if int(m) < len(entry):
                            entry[int(m)] = el
                stats["label_paragraphs_split"] += 1
            out.append(el)
            for f in fields:
                f.P = parent
                out.append(f)
                stats["fields_placed_by_label"] += 1
                for link in follow.get(f.objgen, []):
                    link.P = parent
                    out.append(link)
        siblings = _kids(parent)
        i = next(i for i, k in enumerate(siblings) if isinstance(k, pikepdf.Dictionary) and k.objgen == b.objgen)
        parent.K = pikepdf.Array(siblings[:i] + out + siblings[i + 1:])
    return stats


def _table_cells(root, forms: list, page_of, top, norm) -> list:
    """[(Form, the table cell its label names)]: the column whose header text is in the label, and
    the row whose first cell's text is in it (or "row k": the k-th body row). Fields are taken
    top to bottom and an empty cell is preferred, so two tables with the same headings (owner
    and design professional references) fill in order."""
    cache: dict = {}

    def text(el) -> str:
        return norm(_element_text(el, el.get("/Pg"), cache))

    def rows_of(table) -> list:
        rows = []
        for k in _kids(table):
            if isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) == "/TR":
                rows.append(k)
            elif isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) in ("/THead", "/TBody", "/TFoot"):
                rows += [r for r in _kids(k) if isinstance(r, pikepdf.Dictionary) and str(r.get("/S")) == "/TR"]
        return [[c for c in _kids(r) if isinstance(c, pikepdf.Dictionary) and str(c.get("/S")) in ("/TH", "/TD")]
                for r in rows]

    def page(el):
        stack = [el]
        while stack:
            n = stack.pop()
            if n.get("/Pg") is not None:
                return n.Pg.objgen
            stack += [k for k in _kids(n) if isinstance(k, pikepdf.Dictionary) and "/S" in k]
        return None

    tables = []  # (page, body rows, header texts, row-label texts), one per possible header row
    for _, t in _walk(root):
        if str(t.get("/S")) == "/Table":
            grid = rows_of(t)
            pg = page(t)
            for h in range(min(3, len(grid) - 1)):  # an instruction line can span the first row or two
                if grid[h]:
                    tables.append((pg, grid[h + 1:], [text(c) for c in grid[h]],
                                   [text(r[0]) if r else "" for r in grid[h + 1:]]))
    found, filled = [], set()
    for form, widget in sorted(forms, key=lambda fw: top(fw[1])):
        name = _field_name(widget) or ""
        label = f" {norm(name)} "
        k = re.search(r"\brow (\d+)\b", name, re.I)
        best = None
        for pg, grid, heads, row_labels in tables:
            if pg != page_of(widget):
                continue
            cols = [j for j, h in enumerate(heads) if h and f" {h} " in label]
            rows = [i for i, r in enumerate(row_labels) if r and f" {r} " in label and r not in heads]
            if not rows and k and 0 < int(k.group(1)) <= len(row_labels):
                rows = [int(k.group(1)) - 1]
            if not cols or not rows:
                continue
            j = max(cols, key=lambda j: len(heads[j]))
            i = max(rows, key=lambda i: len(row_labels[i]))
            if j >= len(grid[i]):
                continue
            cell = grid[i][j]
            empty = cell.objgen not in filled and not _kids(cell)
            if best is None or (empty and not best[1]):
                best = (cell, empty)
            if empty:
                break
        if best:
            found.append((form, best[0]))
            filled.add(best[0].objgen)
    return found


def label_form_fields(pdf: pikepdf.Pdf, original, visible: dict[str, str] | None = None,
                      buttons: dict[tuple[str, str], str] | None = None) -> dict[str, int]:
    """Make every field's tooltip (/TU) carry the label its page prints. `visible`: field name ->
    label read by Haiku (form_labels.py); an author's tooltip that already contains it is kept,
    any other tooltip becomes the label. Fields Haiku gave no label (or all fields, without
    `visible`) fall back to the text-position label for fields whose original had no tooltip,
    replacing what pdf-a11y guessed; a field with no findable label loses the guess and is named
    by its field name instead, and authors' tooltips are kept. `buttons`: (radio group name,
    button on-state) -> the button's own "question: option", set as that widget's /TU so each button is
    announced by its option, not just the group's question. Run before fix_form_elements, which
    names each Form element from the tooltip."""
    import pymupdf

    from form_labels import carries
    visible = visible or {}
    with pymupdf.open(original) as doc:
        authored = {w.field_name for page in doc for w in page.widgets() if (w.field_label or "").strip()}
        labels = {}
        for page in doc:
            if page.first_widget:
                for name, label in _page_field_labels(page).items():
                    labels.setdefault(name, label)
    stats = {"fields_labelled": 0, "field_guesses_dropped": 0, "tooltips_matched_to_label": 0,
             "radio_buttons_named": 0}
    acro = pdf.Root.get("/AcroForm")
    stack = [(f, "") for f in (acro.get("/Fields", []) if acro is not None else [])]
    while stack:
        field, parent = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        name = f"{parent}.{field.T}" if parent and "/T" in field else str(field.get("/T", parent))
        stack += [(k, name) for k in field.get("/Kids", []) if isinstance(k, pikepdf.Dictionary) and "/T" in k]
        if "/T" not in field:
            continue
        for kid in field.get("/Kids", []):  # a radio group's buttons, each by its on-state
            on = [str(k)[1:] for k in kid.get("/AP", {}).get("/N", {}).keys() if k != "/Off"] \
                if isinstance(kid, pikepdf.Dictionary) and "/T" not in kid else []
            if on and (name, on[0]) in (buttons or {}):
                kid.TU = pikepdf.String(buttons[(name, on[0])])
                stats["radio_buttons_named"] += 1
        if visible.get(name):
            if not carries(str(field.get("/TU", "")), visible[name]):
                field.TU = pikepdf.String(visible[name])
                stats["tooltips_matched_to_label"] += 1
            continue
        if name in authored:
            continue
        if name in labels:
            field.TU = pikepdf.String(labels[name])
            stats["fields_labelled"] += 1
        elif "/TU" in field:
            del field["/TU"]
            stats["field_guesses_dropped"] += 1
    return stats


# ---------------------------------------------------------------------------
def _page_mcid_text(holder) -> dict[int, str]:
    """Text of each MCID in a content stream holder (a page, or a form XObject such as an
    OCR text layer), decoded through the fonts' ToUnicode maps."""
    res = holder.get("/Resources", {})
    fonts, props = res.get("/Font", {}), res.get("/Properties", {})
    decoders: dict[str, tuple[bool, dict]] = {}
    out: dict[int, str] = {}
    stack: list[int | None] = []
    font = None
    for ins in pikepdf.parse_content_stream(holder):
        op = str(ins.operator)
        if op == "BMC":
            stack.append(None)
        elif op == "BDC":
            d = ins.operands[1]
            d = props.get(d) if isinstance(d, pikepdf.Name) else d
            stack.append(int(d.MCID) if isinstance(d, pikepdf.Dictionary) and "/MCID" in d else None)
        elif op == "EMC" and stack:
            stack.pop()
        elif op == "Tf":
            font = str(ins.operands[0])
        elif op in ("Tj", "TJ", "'", '"') and font:
            mcid = next((m for m in reversed(stack) if m is not None), None)
            if mcid is None:
                continue
            if font not in decoders:
                f = fonts.get(font)
                two = f is not None and f.get("/Subtype") == "/Type0"
                decoders[font] = (two, parse_tounicode(f.ToUnicode) if f is not None and "/ToUnicode" in f else {})
            two, cmap = decoders[font]
            operand = ins.operands[-1]
            strings = [x for x in operand if isinstance(x, pikepdf.String)] if op == "TJ" else [operand]
            for s in strings:
                raw = bytes(s)
                codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)] if two else list(raw)
                out[mcid] = out.get(mcid, "") + "".join(
                    cmap.get(c) or ("" if two else bytes([c]).decode("cp1252", "ignore")) for c in codes)
    return out


def _element_text(el, pg, cache: dict) -> str:
    """Text a structure element covers, following MCIDs on its page and MCRs into
    form XObjects (/Stm)."""
    pg = el.get("/Pg", pg)
    parts = []
    for k in _kids(el):
        holder, mcid = None, None
        if isinstance(k, int):
            holder, mcid = pg, k
        elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
            holder, mcid = k.get("/Stm", k.get("/Pg", pg)), int(k.MCID)
        elif isinstance(k, pikepdf.Dictionary) and "/S" in k:
            parts.append(_element_text(k, pg, cache))
            continue
        if holder is None:
            continue
        if holder.objgen not in cache:
            target = holder if isinstance(holder, pikepdf.Stream) else pikepdf.Page(holder)
            try:
                cache[holder.objgen] = _page_mcid_text(target)
            except Exception:
                cache[holder.objgen] = {}
        parts.append(cache[holder.objgen].get(mcid, ""))
    return "".join(parts)


ROMAN = re.compile(r"^(?=[ivxlc]+$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)


def _numbering(labels: list[str]) -> str:
    labels = [re.sub(r"^[\s(\[]+|[\s.):\]]+$", "", t) for t in labels if t.strip()]
    if not labels:
        return "/Unordered"
    if all(t.isdigit() for t in labels):
        return "/Decimal"
    if all(ROMAN.match(t) for t in labels) and any(len(t) > 1 for t in labels):
        return "/LowerRoman" if labels[0].islower() else "/UpperRoman"
    if all(len(t) == 1 and t.isalpha() for t in labels):
        return "/LowerAlpha" if labels[0].islower() else "/UpperAlpha"
    first = labels[0][0]
    if first in "◦o○":
        return "/Circle"
    if first in "▪■□":
        return "/Square"
    if first in "•·●-–—*":
        return "/Disc"
    return "/Ordered" if any(ch.isdigit() for t in labels for ch in t) else "/Unordered"


def set_list_numbering(pdf: pikepdf.Pdf) -> int:
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    cache: dict[tuple, dict[int, str]] = {}

    count = 0
    for _, lst in list(_walk(root)):
        if lst.S != "/L":
            continue
        labels = []
        for li in _kids(lst):
            if not (isinstance(li, pikepdf.Dictionary) and li.get("/S") == "/LI"):
                continue
            for lbl in _kids(li):
                if isinstance(lbl, pikepdf.Dictionary) and lbl.get("/S") == "/Lbl":
                    labels.append(_element_text(lbl, li.get("/Pg", lst.get("/Pg")), cache))
        if not labels:
            continue
        value = pikepdf.Name(_numbering(labels[:6]))
        attrs = lst.get("/A")
        if isinstance(attrs, pikepdf.Dictionary) and attrs.get("/O") == "/List":
            attrs.ListNumbering = value
        elif isinstance(attrs, pikepdf.Array):
            target = next((a for a in attrs if isinstance(a, pikepdf.Dictionary) and a.get("/O") == "/List"), None)
            if target is not None:
                target.ListNumbering = value
            else:
                attrs.append(pikepdf.Dictionary(O=pikepdf.Name.List, ListNumbering=value))
        else:
            new = pikepdf.Dictionary(O=pikepdf.Name.List, ListNumbering=value)
            lst.A = pikepdf.Array([attrs, new]) if isinstance(attrs, pikepdf.Dictionary) else new
        count += 1
    return count


def sync_annotation_alt(pdf: pikepdf.Pdf) -> int:
    """WTPDF 8.9.4.2 wants an annotation's /Contents to equal its structure element's /Alt.
    veraPDF 1.30 decodes /Contents but compares it with a UTF-16 /Alt undecoded, so even
    identical UTF-16 strings fail ("Gate" vs <FEFF0047…>). Rewrite both from one Python
    string: pikepdf stores ASCII as PDFDocEncoding, which compares cleanly."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    count = 0
    for _, el in list(_walk(root)):
        if "/Alt" not in el:
            continue
        for k in _kids(el):
            if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR":
                annot = k.get("/Obj")
                if isinstance(annot, pikepdf.Dictionary) and "/Contents" in annot:
                    before = (bytes(el.Alt), bytes(annot.Contents))
                    text = str(el.Alt)
                    el.Alt = pikepdf.String(text)
                    annot.Contents = pikepdf.String(text)
                    count += (bytes(el.Alt), bytes(annot.Contents)) != before
    return count


def fix_nested_art(pdf: pikepdf.Pdf) -> int:
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    count = 0
    for parent, el in list(_walk(root)):
        if el.S == "/Art" and parent.get("/S") == "/Art":
            el.S = pikepdf.Name.Sect
            count += 1
    return count


def add_bookmarks(pdf: pikepdf.Pdf, min_pages: int = 1, replace: bool = False) -> int:
    """Outline from H1–H3 headings for documents without one (PAC fails a document that has
    headings but no bookmarks, whatever its length). Each entry carries both a page
    destination and a structure destination (/SD), as WTPDF 8.8 requires."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None or len(pdf.pages) < min_pages:
        return 0
    outlines = pdf.Root.get("/Outlines")
    if outlines is not None and "/First" in outlines and not replace:
        return 0
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    cache: dict = {}
    headings = []
    stack = [(root, None)]
    while stack:  # document order: depth-first, children in order
        node, pg = stack.pop()
        pg = node.get("/Pg", pg) if isinstance(node, pikepdf.Dictionary) else pg
        kids = [k for k in _kids(node) if isinstance(k, pikepdf.Dictionary) and "/S" in k]
        for kid in reversed(kids):
            stack.append((kid, pg))
        level = {"/H1": 1, "/H2": 2, "/H3": 3}.get(str(node.get("/S", "")))
        if level:
            title = bookmark_title(str(node.get("/ActualText", "")) or _element_text(node, pg, cache))
            page = _first_page(node, pg, page_index)
            if title and page is not None and len(title) > 3:
                headings.append((level, title, page, node))
    if not headings:
        return 0
    with pdf.open_outline() as outline:
        outline.root.clear()  # with replace: the tagger's outline may point at retagged headings
        stack_lists = [(0, outline.root)]
        last = None
        for level, title, page, el in headings:
            if title == last:
                continue
            last = title
            action = pikepdf.Dictionary(S=pikepdf.Name.GoTo,
                                        D=pikepdf.Array([pdf.pages[page].obj, pikepdf.Name.Fit]),
                                        SD=pikepdf.Array([el]))
            item = pikepdf.OutlineItem(title, action=action)
            while stack_lists[-1][0] >= level:
                stack_lists.pop()
            stack_lists[-1][1].append(item)
            stack_lists.append((level, item.children))
    pdf.Root.PageMode = pikepdf.Name.UseOutlines
    return len(headings)


def _first_page(el, pg, page_index) -> int | None:
    pg = el.get("/Pg", pg)
    for k in _kids(el):
        if isinstance(k, int) and pg is not None:
            return page_index.get(pg.objgen)
        if isinstance(k, pikepdf.Dictionary):
            if k.get("/Type") == "/MCR":
                kpg = k.get("/Pg", pg)
                if kpg is not None:
                    return page_index.get(kpg.objgen)
            elif "/S" in k:
                found = _first_page(k, pg, page_index)
                if found is not None:
                    return found
    return page_index.get(pg.objgen) if pg is not None else None


def add_structure_destinations(pdf: pikepdf.Pdf) -> int:
    """WTPDF 8.8: every in-document destination needs a structure destination (/SD). pdf-a11y
    converts links and bookmarks but not the document's OpenAction; this covers all three,
    pointing /SD at the first structure element with content on the target page."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    top = next((k for k in _kids(root) if isinstance(k, pikepdf.Dictionary) and "/S" in k), None)
    first_on_page: dict[tuple, object] = {}

    def element_for(page) -> object:
        key = page.objgen
        if key not in first_on_page:
            entry = _parent_tree_entry(root.ParentTree, int(page.get("/StructParents", -1)))
            found = None
            if isinstance(entry, pikepdf.Array):
                found = next((e for e in entry if isinstance(e, pikepdf.Dictionary) and "/S" in e), None)
            first_on_page[key] = found if found is not None else top
        return first_on_page[key]

    def fix(holder, key: str) -> int:
        """holder[key] is an action (/A, /OpenAction) or a direct destination (/Dest)."""
        target = holder.get(key)
        if key == "/Dest":
            if not isinstance(target, pikepdf.Array) or not len(target):
                return 0
            holder.A = pikepdf.Dictionary(S=pikepdf.Name.GoTo, D=target)
            del holder["/Dest"]
            target = holder.A
        if not isinstance(target, pikepdf.Dictionary) or target.get("/S") != "/GoTo" or "/SD" in target:
            return 0
        dest = target.get("/D")
        if not isinstance(dest, pikepdf.Array) or not len(dest) or not isinstance(dest[0], pikepdf.Dictionary):
            return 0  # named destinations are left to pdf-a11y's WTPDF fixer
        el = element_for(dest[0])
        if el is None:
            return 0
        target.SD = pikepdf.Array([el] + list(dest[1:]))
        return 1

    count = 0
    if isinstance(pdf.Root.get("/OpenAction"), pikepdf.Dictionary):
        count += fix(pdf.Root, "/OpenAction")
    outlines = pdf.Root.get("/Outlines")
    stack = [outlines.get("/First")] if outlines is not None else []
    while stack:
        item = stack.pop()
        if not isinstance(item, pikepdf.Dictionary):
            continue
        count += fix(item, "/Dest") if "/Dest" in item else fix(item, "/A")
        stack += [item.get("/Next"), item.get("/First")]
    for page in pdf.pages:
        for annot in page.get("/Annots", []):
            if annot.get("/Subtype") == "/Link":
                count += fix(annot, "/Dest") if "/Dest" in annot else fix(annot, "/A")
    return count


def apply_wtpdf(pdf: pikepdf.Pdf, path, wrap_artifacts: bool = True) -> None:
    """pdf-a11y's WTPDF 1.0 fixer on its own (PDF 2.0 namespace on /Document, WTPDF declaration
    in XMP, structure destinations, PUA ActualText), for PDFs made outside pdf-a11y: Chrome-printed
    reports and companions, and companion-mode drawing sets. wrap_artifacts=False skips marking
    untagged content as artifacts, for files where nothing is untagged (a drawing set's sheets are
    each one Figure): it walks every operator in Python, 80% of the time on a 207-sheet CAD set."""
    from pathlib import Path

    from pdf_a11y.fixers.wtpdf import WTPDFFixer
    from pdf_a11y.report import RemediationReport

    from types import SimpleNamespace

    from pdf_a11y.fixers.artifact_wrap import ArtifactWrapFixer

    report = RemediationReport(source_pdf=str(path))
    # Chrome draws some backgrounds (e.g. rounded badges) outside any tag: mark them artifacts.
    if wrap_artifacts:
        ArtifactWrapFixer(SimpleNamespace(tagging={"wrap_untagged_as_artifact": True})).apply(pdf, Path(path), report)
    WTPDFFixer(None).apply(pdf, Path(path), report)
    add_structure_destinations(pdf)


CONTAINERS = {"/Document", "/Table", "/THead", "/TBody", "/TFoot", "/TR", "/L"}


def artifact_stray_content(pdf: pikepdf.Pdf) -> int:
    """Content items sitting directly in container elements (Chrome puts table borders and page
    backgrounds there; WTPDF forbids it) become artifacts: removed from the element, re-marked
    /Artifact in the page stream, ParentTree slot cleared."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    drop: set[tuple[int, int]] = set()
    for _, el in list(_walk(root)):
        if str(el.get("/S")) not in CONTAINERS:
            continue
        kept = []
        for k in _kids(el):
            pg = el.get("/Pg")
            if isinstance(k, int) and pg is not None and pg.objgen in page_index:
                drop.add((page_index[pg.objgen], int(k)))
            elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR" and "/Stm" not in k \
                    and k.get("/Pg", pg) is not None and k.get("/Pg", pg).objgen in page_index:
                drop.add((page_index[k.get("/Pg", pg).objgen], int(k.MCID)))
            else:
                kept.append(k)
        if len(kept) != len(_kids(el)):
            el.K = pikepdf.Array(kept)
    for pg in {p for p, _ in drop}:
        page = pdf.pages[pg]
        props = page.get("/Resources", {}).get("/Properties", {})
        new_ops = []
        for ins in pikepdf.parse_content_stream(page):
            if str(ins.operator) == "BDC":
                d = ins.operands[1]
                d = props.get(d) if isinstance(d, pikepdf.Name) else d
                if isinstance(d, pikepdf.Dictionary) and "/MCID" in d and (pg, int(d.MCID)) in drop:
                    ins = pikepdf.ContentStreamInstruction([pikepdf.Name.Artifact], pikepdf.Operator("BMC"))
            new_ops.append(ins)
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
        entry = _parent_tree_entry(root.ParentTree, int(page.get("/StructParents", -1)))
        if isinstance(entry, pikepdf.Array):
            for p_, m in drop:
                if p_ == pg and m < len(entry):
                    entry[m] = pikepdf.Object.parse(b"null")
    return len(drop)


GROUPING = {"/Document", "/Div", "/Sect", "/Art", "/Part", "/BlockQuote", "/NonStruct"}


def wrap_grouping_content(pdf: pikepdf.Pdf) -> int:
    """Text sitting directly in a grouping element (Chrome's <div> with loose text) goes into a
    <P>, since PDF 2.0 / WTPDF allow content items only in leaf-ish elements."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    count = 0

    def is_content(k):
        return isinstance(k, int) or (isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR")

    for _, el in list(_walk(root)):
        if str(el.get("/S")) not in GROUPING:
            continue
        kids = _kids(el)
        if not any(is_content(k) for k in kids):
            continue
        new_kids, run = [], []

        def flush():
            if not run:
                return
            p = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.P,
                                                     P=el, K=pikepdf.Array(list(run))))
            if "/Pg" in el:
                p.Pg = el.Pg
            for k in run:
                pg = el.get("/Pg") if isinstance(k, int) else k.get("/Pg", el.get("/Pg"))
                mcid = int(k) if isinstance(k, int) else int(k.MCID)
                if pg is not None:
                    entry = _parent_tree_entry(root.ParentTree, int(pg.get("/StructParents", -1)))
                    if isinstance(entry, pikepdf.Array) and mcid < len(entry):
                        entry[mcid] = p
            new_kids.append(p)
            run.clear()

        for k in kids:
            if is_content(k):
                run.append(k)
            else:
                flush()
                new_kids.append(k)
        flush()
        el.K = pikepdf.Array(new_kids)
        count += 1
    return count


# ---------------------------------------------------------------------------
def _form_xobjects(resources) -> dict:
    xobjs = resources.get("/XObject", {}) if isinstance(resources, pikepdf.Dictionary) else {}
    return {str(k): v for k, v in xobjs.items() if isinstance(v, pikepdf.Stream) and v.get("/Subtype") == "/Form"}


def _has_mcids(stream) -> bool:
    props = (stream.get("/Resources") or {}).get("/Properties", {})
    for ins in pikepdf.parse_content_stream(stream):
        if str(ins.operator) == "BDC":
            d = ins.operands[1]
            d = props.get(d) if isinstance(d, pikepdf.Name) else d
            if isinstance(d, pikepdf.Dictionary) and "/MCID" in d:
                return True
    return False


def demote_unreachable_xobject_tags(pdf: pikepdf.Pdf) -> int:
    """Tagged content inside Form XObjects that is drawn more than once (PDF/UA 7.20-2; pdf-a11y
    only handles reuse across pages) or only inside an /Artifact (7.1-2, e.g. Adobe Sign
    stamps) can't be tagged correctly: re-mark it /Artifact and drop its structure references."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    uses: dict[tuple, int] = {}
    in_artifact: set[tuple] = set()
    streams: dict[tuple, pikepdf.Stream] = {}
    visited: set[tuple] = set()

    def scan(holder, resources, artifact: bool, depth: int = 0):
        if depth > 12:
            return
        forms = _form_xobjects(resources)
        stack: list[bool] = []
        for ins in pikepdf.parse_content_stream(holder):
            op = str(ins.operator)
            if op in ("BMC", "BDC"):
                stack.append(ins.operands[0] == "/Artifact")
            elif op == "EMC" and stack:
                stack.pop()
            elif op == "Do" and str(ins.operands[0]) in forms:
                x = forms[str(ins.operands[0])]
                key = x.objgen
                streams[key] = x
                uses[key] = uses.get(key, 0) + 1
                inside = artifact or any(stack)
                if inside:
                    in_artifact.add(key)
                if (key, inside) not in visited:
                    visited.add((key, inside))
                    scan(x, x.get("/Resources") or resources, inside, depth + 1)

    for page in pdf.pages:
        scan(page, page.resources, False)
    has_mcids = {k: _has_mcids(x) for k, x in streams.items()}
    for k, x in streams.items():  # pdf-a11y strips MCIDs from shared XObjects but leaves this key,
        if not has_mcids[k] and "/StructParents" in x:  # which veraPDF reads as "still tagged"
            del x["/StructParents"]
    targets = {k for k, x in streams.items() if (uses[k] > 1 or k in in_artifact) and has_mcids[k]}
    if not targets:
        return 0
    for key in targets:
        x = streams[key]
        props = (x.get("/Resources") or {}).get("/Properties", {})
        new_ops = []
        for ins in pikepdf.parse_content_stream(x):
            if str(ins.operator) == "BDC":
                d = ins.operands[1]
                d = props.get(d) if isinstance(d, pikepdf.Name) else d
                if isinstance(d, pikepdf.Dictionary) and "/MCID" in d:
                    ins = pikepdf.ContentStreamInstruction([pikepdf.Name.Artifact], pikepdf.Operator("BMC"))
            new_ops.append(ins)
        x.write(pikepdf.unparse_content_stream(new_ops))
        if "/StructParents" in x:
            del x["/StructParents"]
    for _, el in list(_walk(root)):
        kids = _kids(el)
        kept = [k for k in kids if not (isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR"
                                        and "/Stm" in k and k.Stm.objgen in targets)]
        if len(kept) != len(kids):
            el.K = pikepdf.Array(kept)
    return len(targets)


def drop_dangling_mcrs(pdf: pikepdf.Pdf) -> int:
    """Remove structure references to marked content that no longer exists: MCRs into a Form
    XObject whose MCIDs were stripped (pdf-a11y does that to shared XObjects, leaving the
    references, e.g. Figure > Link > Adobe Sign stamp), and parent-tree entries no page,
    XObject or annotation owns. PAC reports the first as '"StructParents" key missing'. Also
    plain MCIDs a page's content doesn't have (the tagger leaves some, e.g. a Link pointing at
    an empty run in D50; PAC: 'Entry for given "MCID" missing'). Run before
    demote_placeholder_figures, which removes MCIDs on purpose and cleans up after itself."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    mcids: dict[tuple, set[int]] = {}

    def stream_mcids(x) -> set[int]:
        if x.objgen not in mcids:
            props = (x.get("/Resources") or {}).get("/Properties", {})
            found = set()
            for ins in pikepdf.parse_content_stream(x if isinstance(x, pikepdf.Stream) else pikepdf.Page(x)):
                if str(ins.operator) == "BDC":
                    d = ins.operands[1]
                    d = props.get(d) if isinstance(d, pikepdf.Name) else d
                    if isinstance(d, pikepdf.Dictionary) and "/MCID" in d:
                        found.add(int(d.MCID))
            mcids[x.objgen] = found
        return mcids[x.objgen]

    def missing(k, pg) -> bool:
        if isinstance(k, int):
            return pg is not None and int(k) not in stream_mcids(pg)
        if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
            holder = k.get("/Stm")
            if holder is not None:
                return int(k.MCID) not in stream_mcids(holder)
            holder = k.get("/Pg", pg)
            return holder is not None and int(k.MCID) not in stream_mcids(holder)
        return False

    dropped = 0
    for _, el in list(_walk(root)):
        kids = _kids(el)
        kept = [k for k in kids if not missing(k, el.get("/Pg"))]
        if len(kept) != len(kids):
            dropped += len(kids) - len(kept)
            el.K = pikepdf.Array(kept)
    # parent-tree keys still claimed by something
    owned = set()
    for page in pdf.pages:
        if "/StructParents" in page.obj:
            owned.add(int(page.obj.StructParents))
        for annot in page.get("/Annots", []):
            if "/StructParent" in annot:
                owned.add(int(annot.StructParent))
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and "/StructParents" in obj:
            owned.add(int(obj.StructParents))
    tree = root.get("/ParentTree")
    nums = tree.get("/Nums") if tree is not None else None
    if nums is not None:
        kept_nums = []
        for i in range(0, len(nums), 2):
            if int(nums[i]) in owned:
                kept_nums += [nums[i], nums[i + 1]]
            else:
                dropped += 1
        tree.Nums = pikepdf.Array(kept_nums)
    return dropped


PUA = re.compile(r"[-]")


def clean_text_strings(pdf: pikepdf.Pdf) -> int:
    """WTPDF 8.6: human-readable strings (bookmark titles, alt text, metadata, field names) may
    not use Private Use Area characters. Map Word's Symbol/Wingdings ones; drop the rest."""
    from font_fixes import SYMBOL_ENCODING, WINGDINGS

    def clean(text: str) -> str:
        def sub(m):
            c = ord(m.group())
            if 0xF020 <= c <= 0xF0FF:
                return SYMBOL_ENCODING.get(c - 0xF000) or WINGDINGS.get(c - 0xF000) or ""
            return ""
        return " ".join(PUA.sub(sub, text).split()) if PUA.search(text) else text

    count = 0

    def fix(d, key):
        nonlocal count
        v = d.get(key)
        if isinstance(v, pikepdf.String) and PUA.search(str(v)):
            d[key] = pikepdf.String(clean(str(v)))
            count += 1

    outlines = pdf.Root.get("/Outlines")
    stack = [outlines.get("/First")] if outlines is not None else []
    while stack:
        item = stack.pop()
        if isinstance(item, pikepdf.Dictionary):
            fix(item, "/Title")
            stack += [item.get("/Next"), item.get("/First")]
    root = pdf.Root.get("/StructTreeRoot")
    if root is not None:
        for _, el in _walk(root):
            for key in ("/Alt", "/ActualText", "/E", "/T"):
                fix(el, key)
    for key in list(pdf.docinfo.keys()):
        fix(pdf.docinfo, key)
    for page in pdf.pages:
        for annot in page.get("/Annots", []):
            for key in ("/Contents", "/TU", "/T"):
                fix(annot, key)
    return count


INLINE = {"/Span", "/Code", "/Strong", "/Em", "/Quote", "/Link", "/Sub", "/Sup", "/Reference", "/BibEntry",
          "/Note", "/Lbl"}


def _regroup(pdf, root, el, keep, new_type: str) -> bool:
    """Wrap each run of el's children that `keep` rejects into a new child of new_type."""
    kids = _kids(el)

    def is_content(k):
        return isinstance(k, int) or (isinstance(k, pikepdf.Dictionary) and k.get("/Type") in ("/MCR", "/OBJR"))

    if all(keep(k) for k in kids):
        return False
    new_kids, run = [], []

    def flush():
        if not run:
            return
        wrapper = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem,
                                                       S=pikepdf.Name("/" + new_type), P=el, K=pikepdf.Array(list(run))))
        if "/Pg" in el:
            wrapper.Pg = el.Pg
        for k in run:
            if is_content(k):
                if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR":
                    continue
                pg = el.get("/Pg") if isinstance(k, int) else k.get("/Pg", el.get("/Pg"))
                mcid = int(k) if isinstance(k, int) else int(k.MCID)
                if pg is not None:
                    entry = _parent_tree_entry(root.ParentTree, int(pg.get("/StructParents", -1)))
                    if isinstance(entry, pikepdf.Array) and mcid < len(entry):
                        entry[mcid] = wrapper
            elif isinstance(k, pikepdf.Dictionary):
                k.P = wrapper
        new_kids.append(wrapper)
        run.clear()

    for k in kids:
        if keep(k):
            flush()
            new_kids.append(k)
        else:
            run.append(k)
    flush()
    el.K = pikepdf.Array(new_kids)
    return True


def regroup_chrome_structure(pdf: pikepdf.Pdf) -> int:
    """Chrome tags <li>bold <code>x</code> text</li> as LI → Span/Code/content, and loose inline
    elements under Document/Div. PDF/UA wants LI → Lbl/LBody and WTPDF wants inline elements inside
    a block: wrap the strays in LBody / P."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    count = 0
    for _, el in list(_walk(root)):
        s = str(el.get("/S"))
        if s == "/LI":
            count += _regroup(pdf, root, el, lambda k: isinstance(k, pikepdf.Dictionary)
                              and str(k.get("/S")) in ("/Lbl", "/LBody"), "LBody")
        elif s in GROUPING:
            count += _regroup(pdf, root, el, lambda k: isinstance(k, pikepdf.Dictionary) and "/S" in k
                              and str(k.get("/S")) not in INLINE, "P")
    return count


# ---------------------------------------------------------------------------
TEXT_LINK = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
# North American phone numbers: 555-010-9675, (555) 010-9675, 555.010.9675, 1-800-…; not
# section numbers ("26 51 00") or dates, which don't have the 3-3-4 shape
PHONE = re.compile(r"(?<![\w.-])(?:\+?1[-. ])?(?:\(\d{3}\) ?|\d{3}[-. ])\d{3}[-.]\d{4}(?![\w-])")


def add_text_links(path) -> int:
    """Email addresses, URLs and phone numbers typed as plain text become real links (PAC: 'Link
    in text does not have a Link element'). Adds a URI annotation over each one, before tagging,
    so the tagger wraps the text in a Link element; places that already have a link are left
    alone, so it can run again after more OCR. Fax numbers stay text. Returns the number of
    links added."""
    import pymupdf
    from pathlib import Path
    path = Path(path)
    found = []
    with pymupdf.open(path) as doc:
        for page in doc:
            existing = [pymupdf.Rect(l["from"]) for l in page.get_links()]
            to_pdf = ~page.transformation_matrix
            text_layer = page.get_text()
            matches = [(m.group(0).rstrip(".,;:"), None) for m in TEXT_LINK.finditer(text_layer)]
            matches += [(m.group(0), "tel:+" + ("" if len(d := re.sub(r"\D", "", m.group(0))) == 11 else "1") + d)
                        for m in PHONE.finditer(text_layer)
                        if not re.search(r"fax\W{0,3}$", text_layer[max(0, m.start() - 8):m.start()], re.I)]
            for text, uri in matches:
                uri = uri or (f"mailto:{text}" if "@" in text and "://" not in text
                              else text if "://" in text else f"https://{text}")
                for r in page.search_for(text):
                    if any(r.intersects(e) for e in existing):
                        continue
                    existing.append(r)
                    q = r * to_pdf
                    found.append((page.number, [q.x0, q.y0, q.x1, q.y1], uri, text))
    if not found:
        return 0
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for n, rect, uri, text in found:
            page = pdf.pages[n]
            annot = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=pikepdf.Array(rect),
                Border=pikepdf.Array([0, 0, 0]), F=4, P=page.obj,
                Contents=pikepdf.String({"mailto": "Email ", "tel": "Call "}.get(uri.split(":")[0], "Link to ") + text),
                A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(uri))))
            if "/Annots" not in page.obj:
                page.obj.Annots = pikepdf.Array()
            page.obj.Annots.append(annot)
        pdf.save(path)
    return len(found)


LINK_PREFIXES = ("Link to ", "Call ", "Email ")  # the /Contents add_text_links gives its links


def fix_link_contents(pdf: pikepdf.Pdf) -> int:
    """Make each Link element hold the text its link shows. The tagger matches annotations to
    content by position, which fails in two ways on OCR'd scans: an annotation on top of the
    page image gets the image's content (Figure > Link > image, so the visible URL stays in a
    plain paragraph and the picture loses its Figure and alt text), and a word split at the
    annotation's edge leaves a character outside ("1" + Link "23-456-7890"). Only links with a
    known text (from add_text_links) are checked. Run before figures get their alt text.
    Returns the number of links repaired."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    tree, cache, fixed = root.get("/ParentTree"), {}, 0

    def norm(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    def is_leaf(k) -> bool:
        return isinstance(k, int) or (isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR")

    def leaf_text(k, pg) -> str:
        return _element_text(pikepdf.Dictionary(K=pikepdf.Array([k])), pg, cache)

    def reparent(k, pg, el) -> None:
        """Point the parent tree entry of marked content k at structure element el."""
        holder, mcid = (pg, int(k)) if isinstance(k, int) else (k.get("/Stm", k.get("/Pg", pg)), int(k.MCID))
        key = holder.get("/StructParents") if holder is not None else None
        entry = _parent_tree_entry(tree, int(key)) if key is not None and tree is not None else None
        if isinstance(entry, pikepdf.Array) and mcid < len(entry):
            entry[mcid] = el

    links = [(parent, el) for parent, el in _walk(root) if str(el.get("/S")) == "/Link"]
    for parent, link in links:
        objrs = [k for k in _kids(link) if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR"]
        contents = str(objrs[0].Obj.get("/Contents", "")) if objrs else ""
        prefix = next((p for p in LINK_PREFIXES if contents.startswith(p)), None)
        if not prefix:
            continue
        want, pg = norm(contents[len(prefix):]), link.get("/Pg", parent.get("/Pg"))
        have = norm(_element_text(link, pg, cache))
        if want in have:
            continue
        siblings = _kids(parent)
        i = next(n for n, k in enumerate(siblings) if isinstance(k, pikepdf.Dictionary) and k.objgen == link.objgen)
        if have and have in want:
            # a word split at the annotation's edge: take in the neighbouring pieces
            content = [k for k in _kids(link) if k not in objrs]
            while want not in have:
                before = siblings[i - 1] if i > 0 and is_leaf(siblings[i - 1]) else None
                after = siblings[i + 1] if i + 1 < len(siblings) and is_leaf(siblings[i + 1]) else None
                if before is not None and norm(leaf_text(before, pg)) + have in want:
                    have = norm(leaf_text(before, pg)) + have
                    content.insert(0, before)
                    siblings.pop(i - 1)
                    i -= 1
                    reparent(before, pg, link)
                elif after is not None and have + norm(leaf_text(after, pg)) in want:
                    have += norm(leaf_text(after, pg))
                    content.append(after)
                    siblings.pop(i + 1)
                    reparent(after, pg, link)
                else:
                    break
            link.K = pikepdf.Array(content + objrs)
            parent.K = pikepdf.Array(siblings)
            fixed += 1
            continue
        # the wrong content: move the link to the text it shows; what it held stays where it was
        target = next(((el, j) for _, el in _walk(root) if str(el.get("/S")) != "/Link"
                       for j, k in enumerate(_kids(el)) if is_leaf(k) and want in norm(leaf_text(k, el.get("/Pg", pg)))),
                      None)
        if target is None:
            continue
        host, j = target
        held = [k for k in _kids(link) if k not in objrs]
        for k in held:
            if is_leaf(k):
                reparent(k, pg, parent)
            elif isinstance(k, pikepdf.Dictionary):
                k.P = parent
        parent.K = pikepdf.Array(siblings[:i] + held + siblings[i + 1:])
        host_kids = _kids(host)
        leaf = host_kids[j]
        host_pg = host.get("/Pg", pg)
        link.K = pikepdf.Array([leaf] + objrs)
        link.P = host
        if host_pg is not None:
            link.Pg = host_pg
        host.K = pikepdf.Array(host_kids[:j] + [link] + host_kids[j + 1:])
        reparent(leaf, host_pg, link)
        fixed += 1
    return fixed


# ---------------------------------------------------------------------------
HEADINGS = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
CAPTION_MAX = 150  # characters; a caption is a short title, anything longer is body text


def _mcid_lines(page) -> dict[int, tuple[float, float, float]]:
    """MCID → (baseline y in page space, font size, x where it starts) of the first text it shows."""
    def mul(a, b):
        return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
                a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
                a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]
    props = page.get("/Resources", {}).get("/Properties", {})
    ctm, saved, stack, out = [1, 0, 0, 1, 0, 0], [], [], {}
    tm = tlm = [1, 0, 0, 1, 0, 0]
    leading = size = 0.0
    for ins in pikepdf.parse_content_stream(page):
        op, args = str(ins.operator), ins.operands
        if op == "q":
            saved.append(ctm)
        elif op == "Q" and saved:
            ctm = saved.pop()
        elif op == "cm":
            ctm = mul([float(v) for v in args], ctm)
        elif op == "BT":
            tm = tlm = [1, 0, 0, 1, 0, 0]
        elif op == "Tm":
            tm = tlm = [float(v) for v in args]
        elif op in ("Td", "TD"):
            tx, ty = float(args[0]), float(args[1])
            if op == "TD":
                leading = -ty
            tm = tlm = mul([1, 0, 0, 1, tx, ty], tlm)
        elif op == "TL":
            leading = float(args[0])
        elif op in ("T*", "'", '"'):
            tm = tlm = mul([1, 0, 0, 1, 0, -leading], tlm)
        elif op == "Tf":
            size = float(args[1])
        elif op == "BDC":
            d = args[1]
            d = props.get(d) if isinstance(d, pikepdf.Name) else d
            stack.append(int(d.MCID) if isinstance(d, pikepdf.Dictionary) and "/MCID" in d else None)
        elif op == "BMC":
            stack.append(None)
        elif op == "EMC" and stack:
            stack.pop()
        if op in ("Tj", "TJ", "'", '"'):
            mcid = next((m for m in reversed(stack) if m is not None), None)
            if mcid is not None and mcid not in out:
                m = mul(tm, ctm)
                out[mcid] = (m[5], abs(size * m[3]) or abs(size), m[4])
    return out


def _paragraphs(mcids: list[int], lines: dict) -> list[list[int]]:
    """Split a run of MCIDs where the text jumps more than a blank line down (or back up)."""
    groups, prev = [], None
    for m in mcids:
        y, size = lines.get(m, (None, 0))[:2]
        if prev is None or y is None or prev[0] is None or not (
                0 <= prev[0] - y <= 1.7 * max(size, prev[1], 1)):
            if not (prev and y is not None and prev[0] is not None and abs(prev[0] - y) < 0.5):
                groups.append([])
        groups[-1].append(m)
        if y is not None:
            prev = (y, size)
    return [g for g in groups if g]


def fix_misused_tags(pdf: pikepdf.Pdf) -> dict[str, int]:
    """Repairs the tagger's commonest wrong guesses:
    - a Caption holding body text (a memo's date, subject and first paragraph captioned onto
      its letterhead logo; a paragraph above a table) becomes P elements, split at paragraph
      gaps and placed before the table it sat in;
    - a heading whose only content is a link (an Adobe Sign stamp tagged H1) becomes a P, and so
      does one that isn't a heading at all: OCR noise from a photo ("\\ eee | 1 =) PARK"), contact
      details (a URL, email address or phone number), or a line that ends in a comma;
    - a title set on two or more lines that became consecutive headings ("CHARITY" H1 + "EVENT!"
      H2) becomes one heading at the higher level;
    - empty P/Span/Div elements are removed;
    - table header cells get /Scope (Column in a header row, Row in a row's first cell)."""
    stats = {"captions_to_paragraphs": 0, "link_headings_to_p": 0, "non_headings_to_p": 0,
             "split_headings_joined": 0, "empty_removed": 0, "th_scoped": 0, "heading_levels_fixed": 0}
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return stats
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    text_cache: dict = {}
    lines_cache: dict = {}
    tree = root.get("/ParentTree")

    def replace_in_parent_tree(page_obj, mapping: dict[int, pikepdf.Dictionary]):
        key = page_obj.get("/StructParents")
        entry = _parent_tree_entry(tree, int(key)) if key is not None and tree is not None else None
        if isinstance(entry, pikepdf.Array):
            for mcid, el in mapping.items():
                if mcid < len(entry):
                    entry[mcid] = el

    for parent, el in list(_walk(root)):
        s = str(el.get("/S"))
        kids = _kids(el)
        if s == "/Caption":
            text = " ".join(_element_text(el, el.get("/Pg"), text_cache).split())
            pg = el.get("/Pg")
            if len(text) < CAPTION_MAX or pg is None or pg.objgen not in page_index \
                    or not kids or not all(isinstance(k, int) for k in kids):
                continue
            if pg.objgen not in lines_cache:
                lines_cache[pg.objgen] = _mcid_lines(pdf.pages[page_index[pg.objgen]])
            in_table = str(parent.get("/S")) == "/Table"
            host = parent.P if in_table else parent
            new = [pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.P,
                                                        P=host, Pg=pg, K=pikepdf.Array(g)))
                   for g in _paragraphs([int(k) for k in kids], lines_cache[pg.objgen])]
            replace_in_parent_tree(pg, {m: p for p in new for m in p.K})
            parent.K = pikepdf.Array([k for k in _kids(parent)
                                      if not (isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen)])
            host_kids = _kids(host)
            anchor = parent if in_table else None
            pos = next((i for i, k in enumerate(host_kids) if isinstance(k, pikepdf.Dictionary)
                        and anchor is not None and k.objgen == anchor.objgen), None)
            if pos is None:  # caption sat beside a figure: the paragraphs follow it
                pos = len(host_kids)
            host.K = pikepdf.Array(host_kids[:pos] + new + host_kids[pos:])
            stats["captions_to_paragraphs"] += 1
        elif s == "/Figure" and any(isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) in ("/Link", "/Form")
                                    for k in kids):
            # Form fields and links inside a Figure are unreachable (a screen reader reads the
            # Figure's alt text instead of its content): they move out to follow it. What's left
            # (the picture's own marked content) stays; an "image N" Figure left with only
            # graphics is then demoted to an artifact by demote_placeholder_figures.
            moved = [k for k in kids if isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) in ("/Link", "/Form")]
            rest, owners = [], {}
            for k in kids:
                if k in moved:
                    continue
                sub = _kids(k) if isinstance(k, pikepdf.Dictionary) and "/S" in k else None
                if sub is not None and sub and all(isinstance(x, int) for x in sub) \
                        and k.get("/Pg", el.get("/Pg")) is not None:
                    rest += [int(x) for x in sub]  # a Span of the picture's own content: flatten
                    owners.update({int(x): k.get("/Pg", el.get("/Pg")) for x in sub})
                else:
                    rest.append(k)
            for k in moved:
                k.P = parent
            siblings = _kids(parent)
            i = next(i for i, k in enumerate(siblings) if isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen)
            if rest:
                el.K = pikepdf.Array(rest)
                if owners and "/Pg" not in el:
                    el.Pg = next(iter(owners.values()))
                for pg in {o.objgen: o for o in owners.values()}.values():
                    replace_in_parent_tree(pg, {m: el for m, o in owners.items() if o.objgen == pg.objgen})
                parent.K = pikepdf.Array(siblings[:i + 1] + moved + siblings[i + 1:])
            else:
                parent.K = pikepdf.Array(siblings[:i] + moved + siblings[i + 1:])
            stats["fields_moved_out_of_figures"] = stats.get("fields_moved_out_of_figures", 0) + len(moved)
        elif s in HEADINGS and kids and all(isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) == "/Link"
                                            for k in kids):
            el.S = pikepdf.Name.P
            stats["link_headings_to_p"] += 1
        elif s in HEADINGS and not_a_heading(_element_text(el, el.get("/Pg"), text_cache)):
            el.S = pikepdf.Name.P
            stats["non_headings_to_p"] += 1
        elif s in ("/P", "/Span", "/Div") and not kids and "/ID" not in el:
            parent.K = pikepdf.Array([k for k in _kids(parent)
                                      if not (isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen)])
            stats["empty_removed"] += 1
        elif s == "/Table":
            rows = [k for k in kids if isinstance(k, pikepdf.Dictionary) and str(k.get("/S")) == "/TR"]
            for sect in (k for k in kids if isinstance(k, pikepdf.Dictionary)
                         and str(k.get("/S")) in ("/THead", "/TBody", "/TFoot")):
                rows += [r for r in _kids(sect) if isinstance(r, pikepdf.Dictionary) and str(r.get("/S")) == "/TR"]
            for row in rows:
                cells = [c for c in _kids(row) if isinstance(c, pikepdf.Dictionary) and "/S" in c]
                header_row = cells and all(str(c.S) == "/TH" for c in cells)
                for c_i, cell in enumerate(cells):
                    if str(cell.S) != "/TH":
                        continue
                    attrs = cell.get("/A")
                    owned = [attrs] if isinstance(attrs, pikepdf.Dictionary) else \
                        [a for a in attrs if isinstance(a, pikepdf.Dictionary)] if isinstance(attrs, pikepdf.Array) else []
                    if any("/Scope" in a for a in owned):
                        continue
                    scope = pikepdf.Name.Column if header_row else pikepdf.Name.Row if c_i == 0 else pikepdf.Name.Both
                    table_attrs = next((a for a in owned if a.get("/O") == "/Table"), None)
                    if table_attrs is None:
                        table_attrs = pikepdf.Dictionary(O=pikepdf.Name.Table)
                        cell.A = pikepdf.Array(list(owned) + [table_attrs]) if owned else table_attrs
                    table_attrs.Scope = scope
                    stats["th_scoped"] += 1

    # Consecutive heading siblings on one page, the same size, left-aligned and one line apart
    # are one title broken over lines. Not joined: a heading ending in ':' or '.', a repeat of
    # the same text, or headings side by side in columns or table cells.
    def lines_of(holder):
        if holder.objgen not in lines_cache:
            lines_cache[holder.objgen] = _mcid_lines(
                holder if isinstance(holder, pikepdf.Stream) else pdf.pages[page_index[holder.objgen]])
        return lines_cache[holder.objgen]

    def marked_content(el) -> tuple | None:
        """(the page or form XObject holding the heading's text, its MCIDs there) when the heading
        holds only marked content from one such stream (OCR text sits in a form XObject)."""
        pg, holder, out = el.get("/Pg"), None, []
        if str(el.get("/S")) not in HEADINGS - {"/H"} or pg is None or pg.objgen not in page_index:
            return None
        for k in _kids(el):
            if isinstance(k, int):
                h, m = pg, int(k)
            elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
                h, m = k.get("/Stm", k.get("/Pg", pg)), int(k.MCID)
            else:
                return None
            if holder is not None and h.objgen != holder.objgen:
                return None
            holder = h
            out.append(m)
        return (holder, out) if out else None

    for parent in [root] + [el for _, el in _walk(root)]:
        kids, i, changed = _kids(parent), 0, False
        while i < len(kids) - 1:
            a, b = kids[i], kids[i + 1]
            ca = marked_content(a) if isinstance(a, pikepdf.Dictionary) else None
            cb = marked_content(b) if isinstance(b, pikepdf.Dictionary) and ca else None
            if ca and cb and ca[0].objgen == cb[0].objgen and a.Pg.objgen == b.Pg.objgen:
                (holder, ma), mb = ca, cb[1]
                lines = lines_of(holder)
                ya, sa, xa = lines.get(ma[-1], (None, 0, 0))
                yb, sb, xb = lines.get(mb[0], (None, 0, 0))
                ta = str(a.get("/ActualText", "")) or " ".join(_element_text(a, a.Pg, text_cache).split())
                tb = " ".join(_element_text(b, b.Pg, text_cache).split())
                if ya is not None and yb is not None and min(sa, sb) > 0 and max(sa, sb) / min(sa, sb) <= 1.25 \
                        and 0 < ya - yb <= 1.6 * max(sa, sb) and abs(lines.get(ma[0], (0, 0, xa))[2] - xb) <= sa \
                        and ta and tb and ta != tb and not ta.endswith((":", ".")) and len(ta) + len(tb) <= 120:
                    raw = _element_text(a, a.Pg, text_cache) + _element_text(b, b.Pg, text_cache)
                    if "/ActualText" in a or " ".join(raw.split()) != f"{ta} {tb}":
                        a.ActualText = pikepdf.String(f"{ta} {tb}")  # the line break was the only space
                    a.K = pikepdf.Array(list(_kids(a)) + list(_kids(b)))
                    a.S = min(a.S, b.S, key=str)  # /H1 before /H2
                    replace_in_parent_tree(holder, {m: a for m in mb})
                    kids = kids[:i + 1] + kids[i + 2:]
                    changed = True
                    stats["split_headings_joined"] += 1
                    continue
            i += 1
        if changed:
            parent.K = pikepdf.Array(kids)
    stats["heading_levels_fixed"] = fix_heading_levels(root)
    return stats


JUNK_CHARS = re.compile(r"[\\|=_~^*•·□►■◆£¥§¢{}<>�]")
NEUTRAL = {"-", "–", "—", "&", "/", ":"}


def not_a_heading(text: str) -> bool:
    """Text a tagger made a heading that a reader shouldn't meet as one: OCR noise (stray symbols
    from a photo or border), contact details, or a line cut off at a comma. Run-together words
    ("PART 1 –GENERALREQUIREMENTS", "237323AirHandlingUnits(AHU)") still count as words."""
    tokens = text.split()
    if not tokens:
        return False
    if TEXT_LINK.search(text) or PHONE.search(text) or text.rstrip().endswith(","):
        return True
    junk = sum(1 for t in tokens if JUNK_CHARS.search(t) or (t not in NEUTRAL and not any(c.isalnum() for c in t)))
    return junk / len(tokens) > 0.3 or not re.search(r"[^\W\d_]{2}", text)


def bookmark_title(text: str) -> str:
    """A heading's text as an outline entry: one line, without the stray marks OCR leaves at its
    ends ("| 123 ANYWHERE ST.," → "123 ANYWHERE ST.")."""
    text = " ".join(text.split())
    text = re.sub(r"^[^\w\"'“‘(\[]+", "", text)
    return re.sub(r"[\s|\\=_~^`*•·,;:]+$", "", text)[:120]


def fix_heading_levels(root) -> int:
    """PDF/UA 7.4.2: the first heading is H1 and levels never skip (H1 → H3 becomes H1 → H2).
    Levels are only lowered, so the outline keeps its shape."""
    order, seen = [], set()

    def walk(node):
        for k in _kids(node):
            if isinstance(k, pikepdf.Dictionary) and "/S" in k and k.objgen not in seen:
                seen.add(k.objgen)
                if re.fullmatch(r"/H[1-6]", str(k.S)):
                    order.append(k)
                walk(k)
    walk(root)
    fixed, prev = 0, 0
    for h in order:
        level = int(str(h.S)[2])
        allowed = prev + 1
        if level > allowed:
            h.S = pikepdf.Name(f"/H{allowed}")
            level = allowed
            fixed += 1
        prev = level
    return fixed


def mcid_problems(pdf: pikepdf.Pdf) -> list[str]:
    """What PAC reports as 'Entry for given MCID missing' and its reverse: structure elements
    pointing at marked content a page (or form XObject) doesn't have, and marked content with
    no parent-tree entry. veraPDF doesn't check either."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return []
    tree = root.get("/ParentTree")
    page_no = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}
    content: dict = {}  # holder objgen → MCIDs present in its content stream

    def mcids_in(holder) -> set:
        if holder.objgen not in content:
            props = (holder.get("/Resources") or {}).get("/Properties", {})
            found = set()
            try:
                target = holder if isinstance(holder, pikepdf.Stream) else pikepdf.Page(holder)
                for ins in pikepdf.parse_content_stream(target):
                    if str(ins.operator) == "BDC":
                        d = ins.operands[1]
                        d = props.get(d) if isinstance(d, pikepdf.Name) else d
                        if isinstance(d, pikepdf.Dictionary) and "/MCID" in d:
                            found.add(int(d.MCID))
            except Exception:
                pass
            content[holder.objgen] = found
        return content[holder.objgen]

    def where(holder) -> str:
        return f"page {page_no[holder.objgen]}" if holder.objgen in page_no else f"form {holder.objgen[0]}"

    problems, referenced = [], set()
    for _, el in _walk(root):
        pg = el.get("/Pg")
        for k in _kids(el):
            if isinstance(k, int):
                holder, mcid = pg, int(k)
            elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
                holder, mcid = k.get("/Stm", k.get("/Pg", pg)), int(k.MCID)
            else:
                continue
            if holder is None:
                problems.append(f"{el.get('/S')} has MCID {mcid} but no page")
                continue
            referenced.add((holder.objgen, mcid))
            if mcid not in mcids_in(holder):
                problems.append(f"{el.get('/S')} on {where(holder)} points at missing MCID {mcid}")
    holders = [p.obj for p in pdf.pages]
    for p in pdf.pages:
        holders += [x for x in p.get("/Resources", {}).get("/XObject", {}).values() if x.get("/Subtype") == "/Form"]
    for h in {h.objgen: h for h in holders}.values():
        missing = sorted(m for m in mcids_in(h) if (h.objgen, m) not in referenced)
        if missing:
            key = h.get("/StructParents")
            entry = _parent_tree_entry(tree, int(key)) if key is not None and tree is not None else None
            unowned = [m for m in missing if not isinstance(entry, pikepdf.Array) or m >= len(entry)
                       or not isinstance(entry[m], pikepdf.Dictionary)]
            if unowned:
                problems.append(f"{where(h)}: MCIDs {unowned[:5]} have no structure element")
    return problems


def _js_text(obj) -> str | None:
    js = obj.get("/JS") if isinstance(obj, pikepdf.Dictionary) else None
    if isinstance(js, pikepdf.Stream):
        return js.read_bytes().decode("latin-1")
    if isinstance(js, pikepdf.String):
        return bytes(js).decode("latin-1")
    return None


def scripts(pdf: pikepdf.Pdf) -> list[str]:
    """Every JavaScript action's text (field formats, calculations, validation, document JS)."""
    return sorted(t for obj in pdf.objects if (t := _js_text(obj)) is not None)


def restore_scripts(pdf: pikepdf.Pdf, original) -> int:
    """Put back JavaScript the tagger mangled. opendataloader drops every backslash from JS
    strings when it rewrites the file, so the travel form's AFNumber_Format(2, 0, 0, 0,
    "\\u0024 ", true) showed 'u0024 0.00' instead of '$ 0.00'. Each script whose text equals an
    original's with the backslashes removed gets the original back. Returns how many."""
    with pikepdf.open(original) as src:
        by_key = {t.replace("\\", ""): t for t in scripts(src) if "\\" in t}
    restored = 0
    for obj in pdf.objects:
        text = _js_text(obj)
        if text is None or "\\" in text or text not in by_key:
            continue
        obj.JS = pikepdf.String(by_key[text].encode("latin-1"))
        restored += 1
    return restored


# ---------------------------------------------------------------------------
def _signed_fields(pdf: pikepdf.Pdf) -> list:
    acro = pdf.Root.get("/AcroForm")
    stack = [(f, None) for f in (acro.get("/Fields", []) if acro is not None else [])]
    out = []
    while stack:
        field, inherited = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        ft = field.get("/FT", inherited)
        stack += [(k, ft) for k in field.get("/Kids", [])]
        if ft == "/Sig" and isinstance(field.get("/V"), pikepdf.Dictionary):
            out.append(field)
    return out


def signatures(pdf: pikepdf.Pdf) -> list[str]:
    """Who digitally signed the PDF, one entry per signed field (an engineer's seal on drawings
    or specifications, an e-signed contract). Any change to the file invalidates them."""
    return [str(f.V.get("/Name", "") or f.get("/T", "") or "a signer") for f in _signed_fields(pdf)]


def strip_signatures(pdf: pikepdf.Pdf) -> int:
    """Remove the signature values, which no longer match the changed file: viewers then show an
    unsigned field instead of "at least one signature is invalid". The visible appearance (a seal
    image) stays. Only when the manifest allows it (publish_unsigned); the signed original stays
    the record copy."""
    fields = _signed_fields(pdf)
    for field in fields:
        del field["/V"]
    if fields:
        if "/Perms" in pdf.Root:  # a certification signature's permissions
            del pdf.Root["/Perms"]
        acro = pdf.Root.AcroForm
        if "/SigFlags" in acro:
            del acro["/SigFlags"]
    return len(fields)


def remove_xfa(pdf: pikepdf.Pdf) -> str | None:
    """Adobe LiveCycle (XFA) forms. A static one also carries ordinary form fields, which are what
    the tagger tags, while Acrobat would show the untagged XFA layer instead; removing that layer
    makes every viewer use the fields (Acrobat's own advice). A dynamic one (/NeedsRendering) has
    no real pages to tag and is left alone. Returns "static_removed", "dynamic" or None."""
    acro = pdf.Root.get("/AcroForm")
    if acro is None or "/XFA" not in acro:
        return None
    if pdf.Root.get("/NeedsRendering"):
        return "dynamic"
    del acro["/XFA"]
    return "static_removed"


MARKUPS = {"/Highlight": "Highlighted text", "/Underline": "Underlined text", "/StrikeOut": "Struck-out text",
           "/Squiggly": "Marked text", "/Ink": "Hand-drawn markup", "/Square": "Rectangle markup",
           "/Circle": "Circle markup", "/Line": "Line markup", "/Polygon": "Shape markup",
           "/PolyLine": "Line markup", "/Stamp": "Stamp", "/FreeText": "Text box", "/Text": "Comment",
           "/Caret": "Insertion mark"}
PLACEHOLDER_CONTENTS = {"", "annotation"}  # what the tagger writes into a markup that has no comment


def fix_annotations(pdf: pikepdf.Pdf, path) -> dict[str, int]:
    """Review markups (Acrobat comments, Bluebeam markups on drawings and submittals):
    - popups are removed. A popup only stores where a comment's window opens; the text is the
      comment's own. WTPDF (8.9.2.4.9) forbids tagging popups and veraPDF's PDF/UA-1 test (7.18.1)
      wants every visible annotation tagged, so neither keeps both happy; viewers draw a default
      window for a comment without one. (An open popup would change the page; the appearance check
      would hold that file.)
    - a markup the tagger described as "Annotation" (and showed that as its comment) gets a
      description: what kind of mark it is, the text it marks, and who made it."""
    import pymupdf
    stats = {"popups_removed": 0, "markups_described": 0}
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return stats
    popups = set()
    for page in pdf.pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        for annot in annots:
            if annot.get("/Subtype") == "/Popup":
                popups.add(annot.objgen)
            elif "/Popup" in annot:
                del annot["/Popup"]
        if any(a.objgen in popups for a in annots):
            page.obj.Annots = pikepdf.Array([a for a in annots if a.objgen not in popups])
            stats["popups_removed"] += len(annots) - len(page.obj.Annots)
    if popups:
        for parent, el in list(_walk(root)):
            kids = _kids(el)
            kept = [k for k in kids if not (isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR"
                                            and isinstance(k.get("/Obj"), pikepdf.Dictionary)
                                            and k.Obj.objgen in popups)]
            if len(kept) == len(kids):
                continue
            if kept:
                el.K = pikepdf.Array(kept)
            else:  # the element held only the popup
                parent.K = pikepdf.Array([k for k in _kids(parent)
                                          if not (isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen)])
    # the element's /Alt and the annotation's /Contents must match (sync_annotation_alt copies Alt over)
    owner = {k.Obj.objgen: el for _, el in _walk(root) for k in _kids(el)
             if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR" and isinstance(k.get("/Obj"), pikepdf.Dictionary)}
    with pymupdf.open(path) as doc:
        for n, page in enumerate(pdf.pages):
            for annot in page.get("/Annots", []):
                word = MARKUPS.get(str(annot.get("/Subtype")))
                if word is None or str(annot.get("/Contents", "")).strip().lower() not in PLACEHOLDER_CONTENTS:
                    continue
                marked = ""
                if annot.get("/Subtype") in ("/Highlight", "/Underline", "/StrikeOut", "/Squiggly") and "/Rect" in annot:
                    rect = (pymupdf.Rect(*[float(v) for v in annot.Rect]) * doc[n].transformation_matrix).normalize()
                    marked = " ".join(w[4] for w in doc[n].get_text("words")  # whole words, by their centre
                                      if rect.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)))[:150]
                author = str(annot.get("/T", "")).strip()
                annot.Contents = pikepdf.String(word + (f": “{marked}”" if marked else "")
                                                + (f" (by {author})" if author else ""))
                if annot.objgen in owner:
                    owner[annot.objgen].Alt = annot.Contents
                stats["markups_described"] += 1
    return stats


DEVICE_SPACES = {"/DeviceRGB", "/DeviceGray", "/DeviceCMYK"}


def inline_images_to_xobjects(pdf: pikepdf.Pdf) -> int:
    """Inline images (BI … ID … EI inside the content stream; some scanners and older report
    writers use them) are invisible to the image stage and the tagger: one came out as untagged
    background, so a screen reader never learned it was there. Each becomes an ordinary image
    XObject drawn in its place: the same encoded data and dictionary, the same position (an
    inline image and an image XObject both fill the unit square of the current matrix).
    Returns how many were converted."""
    count = 0
    for page in pdf.pages:
        ops = pikepdf.parse_content_stream(page)
        if not any(isinstance(i, pikepdf.ContentStreamInlineImage) for i in ops):
            continue
        spaces = page.resources.get("/ColorSpace", {})
        new_ops = []
        for ins in ops:
            if not isinstance(ins, pikepdf.ContentStreamInlineImage):
                new_ops.append(ins)
                continue
            image = ins.iimage
            try:
                raw = image._data._inline_image_raw_bytes()
            except Exception:  # keep it inline rather than risk the picture
                new_ops.append(ins)
                continue
            attrs = dict(image.obj.items())
            cs = attrs.get("/ColorSpace")
            if isinstance(cs, pikepdf.Name) and str(cs) not in DEVICE_SPACES and cs in spaces:
                attrs["/ColorSpace"] = spaces[cs]  # a named space resolves through the page's resources
            xobj = pdf.make_stream(raw)
            for k, v in attrs.items():
                xobj[k] = v
            xobj.Type, xobj.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            name = page.add_resource(xobj, pikepdf.Name.XObject, prefix="Inl")
            new_ops.append(pikepdf.ContentStreamInstruction([pikepdf.Name(name)], pikepdf.Operator("Do")))
            count += 1
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
    return count
