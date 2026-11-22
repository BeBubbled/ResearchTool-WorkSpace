import shutil
import tempfile
import unittest
from pathlib import Path

from researchtool.gapr.evidence import Evidence, normalize, quote_in
from researchtool.gapr.fulltext import FulltextStore, check_in_pool, context_pool, html_to_text

HTML = """<html><head><title>x</title><script>var a=1;</script></head><body>
<div class="ltx_page_main"><article class="ltx_document">
<p class="ltx_p">However, existing Transformer-based models
 only capture cross-time dependency and <em>overlook</em> the cross-dimension
 dependency, where <math alttext="x_t"><mi>x</mi><mi>t</mi></math> is the input.</p>
<p>We propose ﬁve new modules.</p>
</article></div></body></html>"""


class TestNormalizeAndMatch(unittest.TestCase):
    def test_html_to_text(self):
        t = html_to_text(HTML)
        self.assertIn("However, existing Transformer-based models only capture", t)
        self.assertIn("x_t", t)
        self.assertNotIn("var a", t)

    def test_quote_tolerates_whitespace_punct_ligature(self):
        norm = normalize(html_to_text(HTML))
        self.assertTrue(quote_in(norm, "existing Transformer–based models only capture cross-time dependency")[0])
        self.assertTrue(quote_in(norm, "We propose five new modules")[0])

    def test_ellipsis_segments_in_order(self):
        norm = normalize(html_to_text(HTML))
        self.assertTrue(quote_in(norm, "existing Transformer-based models … overlook the cross-dimension dependency")[0])
        self.assertFalse(quote_in(norm, "overlook the cross-dimension dependency ... existing Transformer-based models")[0])

    def test_fabricated_and_short_quotes(self):
        norm = normalize(html_to_text(HTML))
        ok, why = quote_in(norm, "existing models fail on long inputs and lack locality")
        self.assertFalse(ok)
        ok, why = quote_in(norm, "However")
        self.assertFalse(ok)
        self.assertIn("太短", why)

    def test_pdf_line_break_hyphen(self):
        self.assertEqual(normalize("cross-\ndimension"), normalize("crossdimension"))


class TestProvenance(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_fulltext_store_and_tamper(self):
        st = FulltextStore(self.root)
        path = st.add("arXiv:2211.14730v2", html_to_text(HTML), "https://arxiv.org/html/2211.14730", "arxiv-html")
        self.assertEqual(st.check_quote("arXiv:2211.14730", "only capture cross-time dependency and overlook")[0], True)
        # 手改全文后不再被当作证据
        path.write_text(path.read_text(encoding="utf-8") + "\nThe model fails badly on long inputs everywhere.",
                        encoding="utf-8")
        st2 = FulltextStore(self.root)
        ok, why = st2.check_quote("arXiv:2211.14730", "The model fails badly on long inputs everywhere")
        self.assertIsNone(ok)
        self.assertEqual(Evidence(self.root).tampered(), [path.relative_to(self.root / "evidence").as_posix()])

    def test_unregistered_file_ignored(self):
        d = self.root / "evidence" / "fulltext"
        d.mkdir(parents=True)
        (d / "arXiv_9999.00001.txt").write_text("hand written text that claims anything at all", encoding="utf-8")
        self.assertIsNone(FulltextStore(self.root).check_quote("arXiv:9999.00001", "hand written text that claims")[0])

    def test_citation_context_pool(self):
        ev = Evidence(self.root)
        ev.save("citations", "arXiv:2205.13504.critique",
                [{"paper": {}, "contexts": ["However, DLinear ignores cross-variable dependency entirely."]}])
        pool = context_pool(self.root, "citations")
        self.assertTrue(check_in_pool(pool, "DLinear ignores cross-variable dependency")[0])
        self.assertFalse(check_in_pool(pool, "DLinear cannot handle missing values at all")[0])


if __name__ == "__main__":
    unittest.main()
