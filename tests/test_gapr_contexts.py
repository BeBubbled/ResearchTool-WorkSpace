"""引用句抽取、OpenAlex 解析、API key 不落盘（离线）。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from researchtool.gapr import contexts as cx
from researchtool.gapr.fulltext import FulltextStore, check_in_pool, context_pool, html_to_text
from researchtool.gapr.evidence import Evidence
from researchtool.gapr.net import Http
from researchtool.gapr.paper import Paper
from researchtool.gapr.sources import openalex

CITED = Paper(title="A Time Series is Worth 64 Words: Long-term Forecasting with Transformers",
              source="openalex", source_id="W1", year=2023, authors=["Yuqi Nie", "Nam H. Nguyen"])

HTML = """<html><body><div class="ltx_page_main"><article class="ltx_document">
<section><h2>Introduction</h2>
<p class="ltx_p">Patching was popularized by PatchTST <cite>(<a href="#bib.bib3" class="ltx_ref">Nie et al., 2023</a>)</cite>.
However, PatchTST <cite>(<a href="#bib.bib3">Nie et al., 2023</a>)</cite> treats channels independently and overlooks
cross-variable dependency. Other work <cite>(<a href="#bib.bib5">Zhou et al., 2021</a>)</cite> uses sparse attention.</p>
<p class="ltx_p">As shown in Fig. 2, our model is better. We follow e.g. standard splits.</p>
</section>
<section class="ltx_bibliography"><h2>References</h2><ul>
<li id="bib.bib3" class="ltx_bibitem">Y. Nie, N. H. Nguyen. A time series is worth 64 words: Long-term forecasting with transformers. In ICLR, 2023.</li>
<li id="bib.bib5" class="ltx_bibitem">H. Zhou et al. Informer: Beyond efficient transformer. AAAI 2021.</li>
</ul></section></article></div></body></html>"""

PDF_TEXT = """1 Introduction
Recent work on patching (Nie et al., 2023) improves long-term forecas-
ting. However, Nie et al. (2022) do not model
cross-channel interactions, which limits accuracy.
Another line [12] uses graphs.

References
Yuqi Nie, Nam H Nguyen. 2023. A time series is worth 64 words.
"""


class TestHtmlExtraction(unittest.TestCase):
    def test_bib_link_sentences(self):
        found, note = cx.from_html(HTML, CITED, [])
        texts = [f["text"] for f in found]
        self.assertEqual(note, "")
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[1].startswith("However, PatchTST (Nie et al., 2023) treats channels independently"))
        self.assertTrue(all(f["method"] == "html-bib" for f in found))
        self.assertFalse(any("Zhou" in t for t in texts))

    def test_extracted_sentence_verifies_against_fulltext(self):
        root = Path(tempfile.mkdtemp())
        try:
            st = FulltextStore(root)
            st.add("arXiv:2310.06625", html_to_text(HTML), "u", "arxiv-html")
            found, _ = cx.from_html(HTML, CITED, [])
            for f in found:
                self.assertTrue(st.check_quote("arXiv:2310.06625", f["text"])[0], f["text"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_title_not_in_bibliography(self):
        other = Paper(title="Completely Different Paper Title About Graphs", source="x", source_id="y")
        found, note = cx.from_html(HTML, other, [])
        self.assertEqual(found, [])
        self.assertIn("没找到", note)

    def test_alias(self):
        other = Paper(title="Completely Different Paper Title About Graphs", source="x", source_id="y")
        found, _ = cx.from_html(HTML, other, ["PatchTST"])
        self.assertEqual({f["method"] for f in found}, {"alias"})


class TestPdfExtraction(unittest.TestCase):
    def test_author_year(self):
        found, note = cx.from_text(PDF_TEXT, CITED, [])
        texts = [f["text"] for f in found]
        self.assertEqual(note, "")
        self.assertEqual(len(texts), 2)
        self.assertIn("forecasting", texts[0])  # 断行连字符已合并
        self.assertTrue(texts[1].startswith("However, Nie et al. (2022) do not model cross-channel"))
        self.assertFalse(any("References" in t or "64 words" in t for t in texts))

    def test_numeric_style_reported(self):
        found, note = cx.from_text("Another line [12] uses graphs.\n", CITED, [])
        self.assertEqual(found, [])
        self.assertIn("数字编号", note)


class TestSentenceSplit(unittest.TestCase):
    def test_abbreviations_do_not_split(self):
        s = cx.split_sentences("As in Nie et al. (2023), we patch. See Fig. 2 for details. Done.")
        self.assertEqual(s, ["As in Nie et al. (2023), we patch.", "See Fig. 2 for details.", "Done."])


class TestOpenAlex(unittest.TestCase):
    def test_to_paper_ids(self):
        p = openalex.to_paper({"id": "https://openalex.org/W9", "title": "T", "publication_date": "2020-07-01",
                               "ids": {"doi": "https://doi.org/10.18653/v1/2020.acl-main.432"},
                               "authorships": [{"author": {"display_name": "Danish Pruthi"}}]})
        self.assertEqual(p.acl_id, "2020.acl-main.432")
        self.assertEqual(p.key, "ACL:2020.acl-main.432")
        self.assertEqual(p.first_surname, "Pruthi")

    def test_work_paths(self):
        self.assertEqual(openalex.work_path("arXiv:2211.14730v2"), "doi:10.48550/arXiv.2211.14730")
        self.assertEqual(openalex.work_path("ACL:2020.acl-main.432"), "doi:10.18653/v1/2020.acl-main.432")
        self.assertEqual(openalex.work_path("w123"), "W123")
        with self.assertRaises(ValueError):
            openalex.work_path("something")


class TestSecretsNotCached(unittest.TestCase):
    def test_api_key_not_in_cache(self):
        root = Path(tempfile.mkdtemp())
        try:
            http = Http(root)
            seen = {}

            def fake_download(url, headers):
                seen["url"] = url
                return json.dumps({"results": [], "meta": {"count": 0}}).encode()

            with mock.patch.object(http, "_download", side_effect=fake_download), \
                    mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "SECRET123"}):
                openalex.search(http, "probing")
            self.assertIn("api_key=SECRET123", seen["url"])  # 实际请求带 key
            for f in (root / "http").glob("*"):
                self.assertNotIn("SECRET123", f.read_text(encoding="utf-8"))  # 缓存里没有
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestPoolIncludesFulltextContexts(unittest.TestCase):
    def test_pool(self):
        root = Path(tempfile.mkdtemp())
        try:
            Evidence(root).save("citations", "arXiv:2211.14730.fulltext-contexts",
                                [{"paper": {}, "contexts": ["However, PatchTST treats channels independently "
                                                           "and overlooks cross-variable dependency."],
                                  "methods": ["html-bib"]}])
            self.assertTrue(check_in_pool(context_pool(root, "citations"),
                                          "PatchTST treats channels independently")[0])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
