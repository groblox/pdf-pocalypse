"""Offline unit tests for core ADA / PDF-UA / WCAG compliance helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pikepdf
import pymupdf

import contrast_fix
import drawing_pages
import fidelity
import font_fixes
import form_labels
import manifest_builder
import struct_fixes


class ContrastTests(unittest.TestCase):
    def test_wcag_contrast_ratios(self):
        self.assertAlmostEqual(contrast_fix.ratio((0, 0, 0), (1, 1, 1)), 21.0, places=2)
        self.assertAlmostEqual(contrast_fix.ratio((1, 1, 1), (1, 1, 1)), 1.0, places=2)
        # White on medium grey #808080 is ~3.95:1 (fails WCAG AA normal text 4.5:1)
        grey = (128 / 255, 128 / 255, 128 / 255)
        self.assertLess(contrast_fix.ratio((1, 1, 1), grey), 4.5)

    def test_plan_darkens_to_pass_with_margin(self):
        grey = (128 / 255, 128 / 255, 128 / 255)
        pairs = [{"page": 0, "text": "Header", "fg": (1.0, 1.0, 1.0), "bg": grey,
                  "need": contrast_fix.NORMAL + contrast_fix.MARGIN, "bbox": [0, 0, 100, 20], "fails": True}]
        mapping = contrast_fix.plan(pairs)
        self.assertEqual(len(mapping), 1)
        old, new = next(iter(mapping.items()))
        self.assertGreaterEqual(contrast_fix.ratio((1.0, 1.0, 1.0), new), contrast_fix.NORMAL + contrast_fix.MARGIN - 0.02)


class FidelityAndEvalTests(unittest.TestCase):
    def test_rule_problem_catches_placeholders_and_unsure(self):
        self.assertEqual(fidelity._rule_problem(""), "no alt text")
        self.assertEqual(fidelity._rule_problem("image 12"), "placeholder")
        self.assertEqual(fidelity._rule_problem("Text is not legible in this scan."),
                         "describes what can't be seen instead of the image")
        self.assertIsNone(fidelity._rule_problem("Campus site plan showing the main hospital entrance on 13th Street."))

    def test_alt_sanity_texts_uses_custom_eval_callable(self):
        alts = ["University campus map showing construction staging along 13th Street.",
                "Floor plan of mechanical room showing two chillers and primary piping."]
        called = []

        def fake_eval(system, content, schema=None, effort="low"):
            called.append((system, content, schema, effort))
            return {"nonsense": [{"number": 2, "reason": "Contradicts document scope"}]}

        ok, detail, problems = fidelity.alt_sanity_texts(alts, "Bicycle Rack Detail", ask=fake_eval)
        self.assertEqual(len(called), 1)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["number"], 2)

    def test_numbers_and_appearance_checks_on_identical_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.pdf"
            doc = pymupdf.open()
            pg = doc.new_page()
            pg.insert_text((72, 72), "Section 3.2 rate is $85.00 per hour, minimum 2026 hours.", fontsize=12)
            doc.save(p)
            doc.close()

            ok_app, _ = fidelity.appearance(p, p)
            ok_num, _ = fidelity.numbers(p, p)
            self.assertTrue(ok_app)
            self.assertTrue(ok_num)


class StructureAndHelpersTests(unittest.TestCase):
    def test_fix_heading_levels_eliminates_skips(self):
        pdf = pikepdf.new()
        root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot))
        doc_el = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.Document, P=root))
        h2 = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.H2, P=doc_el))
        h4 = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.H4, P=doc_el))
        h6 = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.H6, P=doc_el))
        doc_el.K = pikepdf.Array([h2, h4, h6])
        root.K = pikepdf.Array([doc_el])

        fixed = struct_fixes.fix_heading_levels(root)
        self.assertEqual(fixed, 3)
        self.assertEqual([str(h.S) for h in (h2, h4, h6)], ["/H1", "/H2", "/H3"])

    def test_language_detection(self):
        es = "El horario de trabajo es de lunes a viernes para todos los empleados en la obra con equipo de seguridad."
        en = "The work schedule is Monday through Friday for all employees on the site with safety gear."
        self.assertEqual(manifest_builder.guess_language(es), "es")
        self.assertIsNone(manifest_builder.guess_language(en))

    def test_problem_detects_empty_and_html_masquerading_as_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty.pdf"
            empty.write_bytes(b"")
            self.assertIn("empty", manifest_builder.problem(empty))

            html_pdf = Path(td) / "error.pdf"
            html_pdf.write_text("<html><body>Sign in</body></html>")
            self.assertIn("web page", manifest_builder.problem(html_pdf))

    def test_trim_and_short_edge_cases(self):
        self.assertTrue(drawing_pages.trim("X" * 100, 40).endswith("…"))
        self.assertEqual(drawing_pages.trim("First sentence. Second sentence is very long indeed.", 25),
                         "First sentence.")
        short = form_labels._short("Very long question about contractor safety compliance history: Yes", 45)
        self.assertTrue(short.endswith(": Yes"))
        self.assertLessEqual(len(short), 46)

    def test_containers_constants_do_not_collide(self):
        self.assertEqual(struct_fixes.LIST_CONTAINERS, {"/LBody"})
        self.assertIn("/Table", struct_fixes.STRAY_CONTAINERS)
        self.assertNotIn("/LBody", struct_fixes.STRAY_CONTAINERS)

    def test_fix_fonts_embeds_unembedded_standard_font(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "unembedded.pdf"
            doc = pymupdf.open()
            pg = doc.new_page()
            pg.insert_text((72, 72), "Hello world in unembedded Helvetica", fontsize=12, fontname="helv")
            doc.save(p)
            doc.close()

            with pikepdf.open(p) as pdf:
                stats = font_fixes.fix_fonts(pdf)
                self.assertTrue(any(k.startswith("embedded_") for k in stats))

    def test_parent_tree_multi_kid_operations(self):
        pdf = pikepdf.new()
        page = pdf.add_blank_page()
        page.obj.StructParents = 1
        kid0 = pdf.make_indirect(pikepdf.Dictionary(Nums=pikepdf.Array([0, pikepdf.Dictionary(S=pikepdf.Name.P)]),
                                                    Limits=pikepdf.Array([0, 0])))
        kid1 = pdf.make_indirect(pikepdf.Dictionary(Nums=pikepdf.Array([1, pikepdf.Dictionary(S=pikepdf.Name.P),
                                                                        99, pikepdf.Dictionary(S=pikepdf.Name.P)]),
                                                    Limits=pikepdf.Array([1, 99])))
        tree = pdf.make_indirect(pikepdf.Dictionary(Kids=pikepdf.Array([kid0, kid1])))
        root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, ParentTree=tree, K=pikepdf.Array()))
        pdf.Root.StructTreeRoot = root

        # _set_parent_tree routes key=2 to kid1 (which spans [1, 99]), not kid0
        drawing_pages._set_parent_tree(tree, 2, pikepdf.Dictionary(S=pikepdf.Name.Figure))
        self.assertEqual([int(kid0.Nums[i]) for i in range(0, len(kid0.Nums), 2)], [0])
        self.assertIn(2, [int(kid1.Nums[i]) for i in range(0, len(kid1.Nums), 2)])

        # drop_dangling_mcrs cleans unowned ParentTree keys across Kids
        dropped = struct_fixes.drop_dangling_mcrs(pdf)
        self.assertGreaterEqual(dropped, 3)  # keys 0, 2, 99 unowned; only key 1 is owned by page
        self.assertEqual([int(kid1.Nums[i]) for i in range(0, len(kid1.Nums), 2)], [1])


if __name__ == "__main__":
    unittest.main()
