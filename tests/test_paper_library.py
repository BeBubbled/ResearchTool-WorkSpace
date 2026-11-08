from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_library import PaperLibrary

PDF_KEY = "a" * 64
MD_KEY = "b" * 64


class PaperLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.files = {
            (PDF_KEY, "source", "paper.pdf"): root / "paper.pdf",
            (PDF_KEY, "ocr", "paper.html"): root / "paper.html",
            (MD_KEY, "source", "notes.md"): root / "notes.md",
        }
        for path in self.files.values():
            path.write_text("x", encoding="utf-8")
        self.entries = {
            PDF_KEY: {"id": PDF_KEY, "title": "Diffusion Paper", "sourceName": "paper.pdf", "sourceType": "pdf",
                      "renderKind": "html", "useOcr": True, "storageRoot": "ocr", "documentFilename": "paper.html",
                      "sourceFilename": "paper.pdf", "updatedAt": 2},
            MD_KEY: {"id": MD_KEY, "title": "Notes", "sourceName": "notes.md", "sourceType": "markdown",
                     "renderKind": "markdown", "storageRoot": "source", "sourceFilename": "notes.md", "updatedAt": 1},
        }
        manifests = {PDF_KEY: {"document": {"sourceFilename": "paper.pdf"}}, MD_KEY: {"document": {"sourceFilename": "notes.md"}}}
        self.library = PaperLibrary(
            list_entries=lambda: list(self.entries.values()),
            get_entry=self.entries.get,
            cached_file=lambda key, storage, name: self.files.get((key, storage, name)),
            raw_manifest=lambda key: manifests.get(key, {}),
            usage=lambda: {"gaps": {PDF_KEY: [{"id": "p1", "name": "扩散模型综述"}]}},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sources_for_other_modules(self) -> None:
        self.assertEqual(self.library.analysis_source(PDF_KEY)["path"], str(self.files[(PDF_KEY, "ocr", "paper.html")]))
        self.assertEqual(self.library.analysis_source(MD_KEY)["path"], str(self.files[(MD_KEY, "source", "notes.md")]))
        self.assertEqual(self.library.pdf_source(PDF_KEY), self.files[(PDF_KEY, "source", "paper.pdf")])
        self.assertIsNone(self.library.pdf_source(MD_KEY))
        for bad in ("../etc", "A" * 64, "c" * 64):
            with self.subTest(key=bad):
                self.assertIsNone(self.library.analysis_source(bad))
                self.assertIsNone(self.library.pdf_source(bad))

    def test_workspace_gathers_one_paper_across_modules(self) -> None:
        library = PaperLibrary(
            list_entries=lambda: list(self.entries.values()), get_entry=self.entries.get,
            cached_file=lambda key, storage, name: self.files.get((key, storage, name)),
            raw_manifest=lambda key: {"document": {"sourceFilename": "paper.pdf"}} if key == PDF_KEY else {},
            usage=lambda: {"gaps": {PDF_KEY: [{"id": "p1", "name": "扩散模型综述"}]}},
            notes=lambda key: [{"id": "old", "text": "a", "createdAt": 1}, {"id": "new", "kind": "answer", "text": "b", "createdAt": 5}, "junk"],
            analyses=lambda key: [{"id": "x1", "status": "completed", "reportReady": True, "reportUrl": "/r/x1", "secret": "no"},
                                  {"id": "x2", "status": "failed", "reportUrl": "/r/x2"}],
            studio_jobs=lambda: [{"id": "s1", "paperId": PDF_KEY, "label": "海报", "status": "succeeded"},
                                 {"id": "s2", "paperId": MD_KEY, "label": "博客"}],
        )
        workspace = library.workspace(PDF_KEY)
        self.assertEqual([note["id"] for note in workspace["notes"]], ["new", "old"])
        self.assertEqual(workspace["notes"][1]["kind"], "selection")
        self.assertEqual([(a["id"], a["reportUrl"]) for a in workspace["analyses"]], [("x1", "/r/x1"), ("x2", None)])
        self.assertNotIn("secret", workspace["analyses"][0])
        self.assertEqual([job["id"] for job in workspace["studio"]], ["s1"])
        self.assertEqual(workspace["gaps"], [{"id": "p1", "name": "扩散模型综述"}])
        self.assertEqual(workspace["files"], [{"kind": "pdf", "label": "原始 PDF", "url": f"/api/library/{PDF_KEY}/pdf"}])
        self.assertIsNone(library.workspace("c" * 64))

    def test_workspace_survives_a_failing_module(self) -> None:
        def broken(*_):
            raise RuntimeError("database locked")
        library = PaperLibrary(
            list_entries=lambda: list(self.entries.values()), get_entry=self.entries.get,
            cached_file=lambda key, storage, name: self.files.get((key, storage, name)),
            notes=broken, analyses=broken, studio_jobs=broken,
        )
        workspace = library.workspace(MD_KEY)
        self.assertEqual((workspace["notes"], workspace["analyses"], workspace["studio"], workspace["files"]), ([], [], [], []))
        self.assertEqual(workspace["document"]["links"]["workspace"], f"/library/{MD_KEY}")

    def test_listing_has_usage_links_and_filters(self) -> None:
        documents = self.library.documents()
        self.assertEqual([d["id"] for d in documents], [PDF_KEY, MD_KEY])
        paper = documents[0]
        self.assertTrue(paper["hasPdf"])
        self.assertEqual(paper["usedBy"], {"gaps": [{"id": "p1", "name": "扩散模型综述"}]})
        self.assertEqual(paper["links"]["reader"], f"/reader?library={PDF_KEY}")
        self.assertEqual(paper["links"]["studio"], f"/researchstudio?paperId={PDF_KEY}")
        self.assertNotIn("studio", documents[1]["links"])
        self.assertEqual(documents[1]["usedBy"], {})
        self.assertNotIn("storageRoot", paper)
        self.assertEqual([d["id"] for d in self.library.documents(pdf_only=True)], [PDF_KEY])
        self.assertEqual([d["id"] for d in self.library.documents(query="notes")], [MD_KEY])
        self.assertEqual(self.library.document(MD_KEY)["title"], "Notes")
        self.assertIsNone(self.library.document("c" * 64))

    def test_broken_usage_source_does_not_hide_the_library(self) -> None:
        def broken():
            raise RuntimeError("database locked")
        self.library.usage = broken
        self.assertEqual(len(self.library.documents()), 2)
        self.assertEqual(self.library.document(PDF_KEY)["usedBy"], {})


if __name__ == "__main__":
    unittest.main()
