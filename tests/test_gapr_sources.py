"""数据源解析和日期防火墙（离线，不访问网络）。"""

import unittest
from datetime import date

from researchtool.gapr.paper import Paper, after_cutoff, apply_cutoff, norm_arxiv_id
from researchtool.gapr.sources import arxiv, openalex, s2

ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2211.14730v2</id>
    <published>2022-11-27T05:15:42Z</published>
    <title>A Time Series is Worth 64 Words:
      Long-term Forecasting with Transformers</title>
    <summary>We propose an efficient design.</summary>
    <arxiv:journal_ref>ICLR 2023</arxiv:journal_ref>
  </entry>
</feed>"""


class TestArxiv(unittest.TestCase):
    def test_parse(self):
        papers, total = arxiv.parse_feed(ATOM)
        self.assertEqual(total, 1)
        p = papers[0]
        self.assertEqual(p.arxiv_id, "2211.14730")
        self.assertEqual(p.first_public, "2022-11-27")
        self.assertEqual(p.date_basis, "arXiv v1")
        self.assertEqual(p.title, "A Time Series is Worth 64 Words: Long-term Forecasting with Transformers")
        self.assertEqual(p.key, "arXiv:2211.14730")

    def test_query_building(self):
        self.assertEqual(arxiv.build_query('patch "time series"'), 'all:patch AND all:"time series"')
        self.assertEqual(arxiv.build_query("ti:PatchTST"), "ti:PatchTST")
        q = arxiv.build_query("probing", date_to=date(2022, 6, 30))
        self.assertTrue(q.endswith("submittedDate:[199101010000 TO 202206302359]"), q)

    def test_norm_id(self):
        for raw in ("arXiv:2211.14730v2", "http://arxiv.org/abs/2211.14730v1", "2211.14730",
                    "https://arxiv.org/pdf/2211.14730v3"):
            self.assertEqual(norm_arxiv_id(raw), "2211.14730")
        self.assertEqual(norm_arxiv_id("cs/0112017v1"), "cs/0112017")


class TestS2(unittest.TestCase):
    def test_to_paper(self):
        p = s2.to_paper({"paperId": "abc", "title": "T", "year": 2023, "publicationDate": "2023-05-01",
                         "externalIds": {"ArXiv": "2211.14730", "ACL": "2023.acl-long.1"}})
        self.assertEqual((p.arxiv_id, p.acl_id, p.date_basis), ("2211.14730", "2023.acl-long.1", "publicationDate"))
        q = s2.to_paper({"paperId": "x", "title": "T", "year": 2021})
        self.assertEqual((q.first_public, q.date_basis), ("2021", "year"))

    def test_critique_filter(self):
        ctx = ["However, X fails to capture Y.", "We follow X.", "Unlike X, we model Z."]
        self.assertEqual(s2.critique_contexts(ctx, ["however", "unlike"]), [ctx[0], ctx[2]])


class TestOpenAlex(unittest.TestCase):
    def test_abstract_and_arxiv(self):
        p = openalex.to_paper({"id": "https://openalex.org/W1", "title": "T", "publication_date": "2022-01-02",
                               "abstract_inverted_index": {"hello": [0], "world": [1]},
                               "ids": {"doi": "https://doi.org/10.48550/arXiv.2211.14730"}})
        self.assertEqual(p.abstract, "hello world")
        self.assertEqual(p.arxiv_id, "2211.14730")


class TestFirewall(unittest.TestCase):
    cutoff = date(2022, 6, 30)

    def P(self, fp):
        return Paper(title="t", source="s2", source_id=fp, first_public=fp)

    def test_after_cutoff(self):
        self.assertIs(after_cutoff(self.P("2022-07-01"), self.cutoff), True)
        self.assertIs(after_cutoff(self.P("2022-06-30"), self.cutoff), False)
        self.assertIs(after_cutoff(self.P("2023"), self.cutoff), True)
        self.assertIs(after_cutoff(self.P("2021"), self.cutoff), False)
        self.assertIsNone(after_cutoff(self.P("2022"), self.cutoff))
        self.assertIsNone(after_cutoff(self.P(""), self.cutoff))

    def test_apply(self):
        ps = [self.P("2022-07-01"), self.P("2022-01-01"), self.P("2022")]
        kept, dropped = apply_cutoff(ps, self.cutoff)
        self.assertEqual([p.first_public for p in dropped], ["2022-07-01"])
        self.assertEqual([p.first_public for p in kept], ["2022-01-01", "2022"])
        self.assertEqual(kept[1].flags, ["日期待核"])

    def test_no_cutoff_keeps_all(self):
        ps = [self.P("2030-01-01")]
        self.assertEqual(apply_cutoff(ps, None), (ps, []))


if __name__ == "__main__":
    unittest.main()
