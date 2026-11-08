from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engines
from engines import EngineCatalog


class EngineCatalogTest(unittest.TestCase):
    def test_normalizers_report_ready_and_errors(self) -> None:
        self.assertFalse(engines.normalize_claude({"available": True, "plan": False, "error": "API key login"})["ready"])
        self.assertEqual(engines.normalize_claude({"plan": False, "error": "API key login"})["error"], "API key login")
        codex = engines.normalize_codex({
            "chatgptAuthenticated": True, "defaultModel": "gpt-x", "defaultReasoningEffort": "high",
            "models": [{"id": "gpt-x", "displayName": "GPT X", "supportedEfforts": ["low", "high"]}, {"bad": 1}],
        })
        self.assertTrue(codex["ready"])
        self.assertEqual(codex["models"], [{"id": "gpt-x", "label": "GPT X", "efforts": ["low", "high"]}])
        self.assertEqual(engines.normalize_codex({"chatgptAuthenticated": True})["error"], "Codex 没有可用的默认模型。")
        self.assertFalse(engines.normalize_google({})["ready"])
        self.assertEqual(engines.normalize_codex({})["settingsTab"], "codex")

    def test_api_presets_never_expose_keys(self) -> None:
        presets = engines.normalize_api([{"id": "a", "apiKey": "sk-1", "model": "m"}, {"name": "no id"}])
        self.assertEqual(presets, [{"id": "a", "name": "a", "model": "m", "protocol": "auto", "concurrency": 1, "contextWindow": None}])

    def test_one_failing_engine_does_not_hide_the_others(self) -> None:
        def broken(refresh=False):
            raise RuntimeError("codex app-server crashed")
        catalog = EngineCatalog(codex_status=broken, google_status=lambda: {"configured": True})
        snapshot = catalog.snapshot()
        self.assertFalse(snapshot["codex"]["ready"])
        self.assertIn("crashed", snapshot["codex"]["error"])
        self.assertTrue(snapshot["google"]["ready"])
        self.assertFalse(snapshot["claude"]["ready"])
        self.assertEqual(snapshot["ready"], ["google"])

    def test_snapshot_is_cached_until_refresh_or_invalidate(self) -> None:
        calls = []
        catalog = EngineCatalog(google_status=lambda: calls.append(1) or {"configured": False}, ttl=60)
        catalog.snapshot()
        catalog.snapshot()
        self.assertEqual(len(calls), 1)
        catalog.snapshot(refresh=True)
        self.assertEqual(len(calls), 2)
        catalog.invalidate()
        catalog.snapshot()
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
