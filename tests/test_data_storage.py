import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchtool import data_storage


class DataStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.default = self.root / ".runtime"
        self.default.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_default_when_unset(self):
        state = data_storage.resolve_data_dir(self.default, {})
        self.assertEqual(state.active, self.default)
        status = data_storage.status(state, {})
        self.assertTrue(status["usingDefault"])
        self.assertFalse(status["restartRequired"])

    def test_configured_dir_is_used(self):
        target = self.root / "drive"
        target.mkdir()
        env = {data_storage.DATA_DIR_ENV_KEY: str(target)}
        state = data_storage.resolve_data_dir(self.default, env)
        self.assertEqual(state.active, target.resolve())
        self.assertFalse(data_storage.status(state, env)["restartRequired"])

    def test_missing_dir_falls_back_with_warning(self):
        env = {data_storage.DATA_DIR_ENV_KEY: str(self.root / "unmounted")}
        state = data_storage.resolve_data_dir(self.default, env)
        self.assertEqual(state.active, self.default)
        self.assertIn("不可用", state.warning)
        self.assertFalse((self.root / "unmounted").exists())

    def test_pending_move_moves_subdirs_and_skips_conflicts(self):
        target = self.root / "drive"
        target.mkdir()
        (self.default / "jobs" / "a").mkdir(parents=True)
        (self.default / "jobs" / "a" / "out.md").write_text("x", encoding="utf-8")
        (self.default / "reader-cache").mkdir()
        (target / "reader-cache").mkdir()
        (self.default / "codex").mkdir()
        env = {data_storage.DATA_DIR_ENV_KEY: str(target), data_storage.PENDING_MOVE_ENV_KEY: str(self.default)}
        state = data_storage.resolve_data_dir(self.default, env)
        self.assertEqual(state.migration["moved"], ["jobs"])
        self.assertEqual(state.migration["skipped"], ["reader-cache"])
        self.assertTrue((target / "jobs" / "a" / "out.md").is_file())
        self.assertFalse((self.default / "jobs").exists())
        self.assertTrue((self.default / "codex").is_dir())  # 本机运行时不迁移

    def test_restart_required_after_change(self):
        state = data_storage.resolve_data_dir(self.default, {})
        target = self.root / "drive"
        target.mkdir()
        self.assertTrue(data_storage.status(state, {data_storage.DATA_DIR_ENV_KEY: str(target)})["restartRequired"])

    def test_validate_rejects_bad_paths(self):
        with self.assertRaises(ValueError):
            data_storage.validate_data_dir("", self.default)
        with self.assertRaises(ValueError):
            data_storage.validate_data_dir("relative/dir", self.default)
        with self.assertRaises(ValueError):
            data_storage.validate_data_dir(str(self.root / "nope"), self.default)
        (self.default / "jobs").mkdir()
        with self.assertRaises(ValueError):
            data_storage.validate_data_dir(str(self.default / "jobs"), self.default)
        target = self.root / "ok"
        target.mkdir()
        self.assertEqual(data_storage.validate_data_dir(f'"{target}"', self.default), target.resolve())
        self.assertEqual(list(target.iterdir()), [])  # 写入探测文件已删除


class StorageConfigApiTests(unittest.TestCase):
    def setUp(self):
        from researchtool import web_panel
        self.web_panel = web_panel
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.env_file = root / ".env"
        self.target = root / "drive"
        self.target.mkdir()
        self.client = web_panel.app.test_client()
        self.patches = [
            patch.object(web_panel, "ENV_FILE", self.env_file),
            patch.dict("os.environ", {}, clear=False),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_save_and_reset(self):
        response = self.client.put("/api/storage-config", json={"path": str(self.target), "moveExisting": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        storage = response.get_json()["storage"]
        self.assertEqual(storage["configured"], str(self.target.resolve()))
        self.assertTrue(storage["restartRequired"])
        self.assertEqual(storage["pendingMove"], str(self.web_panel.DATA_DIR))
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(data_storage.DATA_DIR_ENV_KEY, text)

        response = self.client.delete("/api/storage-config")
        storage = response.get_json()["storage"]
        self.assertEqual(storage["configured"], "")
        self.assertEqual(storage["pendingMove"], "")
        self.assertNotIn(data_storage.DATA_DIR_ENV_KEY, self.env_file.read_text(encoding="utf-8"))

    def test_rejects_missing_folder(self):
        response = self.client.put("/api/storage-config", json={"path": str(self.target / "missing")})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.env_file.exists() and data_storage.DATA_DIR_ENV_KEY in self.env_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
