"""Grounding checks for the full-text reading notes (claims and terms)."""
import unittest

from researchtool.paper_revision_context import validate_note

SOURCES = [
    {"id": "p0001", "text": "RUO reaches the tiny-object AP$_\\text{tiny}$ of threshold-based ROI coding with\n\\nImages{} images."},
    {"id": "p0002", "text": "Region-of-interest (ROI) coding is a common answer to this decision."},
]
MACROS = {"nImages": "1,610"}


def claim(quote, source="p0001", role="result"):
    return {"source_id": source, "role": role, "statement": "陈述。", "quote": quote}


class ValidateNoteTests(unittest.TestCase):
    def test_unlocatable_terms_are_dropped_not_fatal(self):
        note = {"claims": [claim("threshold-based ROI coding")],
                "terms": [{"source_id": "p0001", "en": "tiny-object AP", "zh": "微小目标 AP"},
                          {"source_id": "p0001", "en": "an invented term", "zh": "虚构"},
                          {"source_id": "p0001", "en": "region-of-interest (ROI) coding", "zh": "感兴趣区域编码"},
                          "not a dict"]}
        result = validate_note(note, SOURCES, MACROS)
        self.assertEqual([t["en"] for t in result["terms"]], ["tiny-object AP", "region-of-interest (ROI) coding"])
        self.assertEqual(result["terms"][1]["source_id"], "p0002")  # found in another source of the batch
        self.assertEqual(result["dropped"], 2)

    def test_quotes_match_rendered_text_and_whitespace(self):
        note = {"claims": [claim("AP_tiny of threshold-based ROI coding with 1,610 images"),
                           claim("ROI coding   with\nthe"), claim("common answer", source="p0001")],
                "terms": [{"source_id": "p0001", "en": "AP_tiny", "zh": "AP_tiny"}]}
        result = validate_note(note, SOURCES, MACROS)
        self.assertEqual([c["source_id"] for c in result["claims"]], ["p0001", "p0002"])
        self.assertEqual(result["terms"][0]["en"], "AP_tiny")

    def test_all_claims_fabricated_still_aborts(self):
        with self.assertRaisesRegex(ValueError, "编造"):
            validate_note({"claims": [claim("never written anywhere")], "terms": []}, SOURCES, MACROS)
        with self.assertRaises(ValueError):
            validate_note({"claims": "oops", "terms": []}, SOURCES, MACROS)
        self.assertEqual(validate_note({"claims": [], "terms": []}, SOURCES, MACROS)["claims"], [])


if __name__ == "__main__":
    unittest.main()
