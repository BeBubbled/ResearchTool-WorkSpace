import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from researchtool import data_storage
from researchtool.gapr import project as gapr_project


class DataStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.default = self.root / "project" / ".runtime"
        self.default.mkdir(parents=True)
        self.config = self.root / "home" / "machine.json"
        self.store = data_storage.LocalStore(self.config, self.root / "project", "machine-a")
        self.drive = self.root / "drive"
        self.drive.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_default_when_unset(self):
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.active, self.default)
        status = data_storage.status(state, self.store)
        self.assertTrue(status["usingDefault"])
        self.assertFalse(status["restartRequired"])

    def test_path_is_per_machine(self):
        """同一个配置文件被同步到另一台电脑时，那台电脑读不到这台的路径。"""
        self.store.write(dataDir=str(self.drive))
        other = data_storage.LocalStore(self.config, self.root / "project", "machine-b")
        self.assertEqual(other.read()["dataDir"], "")
        other.write(dataDir="X:/Google Drive/ResearchTool")
        self.assertEqual(self.store.read()["dataDir"], str(self.drive))

    def test_first_use_creates_library_and_records_id(self):
        self.store.write(dataDir=str(self.drive))
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.active, self.drive)
        library = data_storage.read_library(self.drive)
        self.assertEqual(self.store.read()["libraryId"], library["id"])

    def test_library_mismatch_falls_back(self):
        library = data_storage.create_library(self.drive, "machine-a")
        self.store.write(dataDir=str(self.drive), libraryId="someone-else")
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.active, self.default)
        self.assertIn("不是同一个", state.warning)
        (self.drive / data_storage.LIBRARY_MARKER).unlink()
        self.store.write(libraryId=library["id"])
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.active, self.default)
        self.assertIn("没找到数据库标识", state.warning)

    def test_missing_dir_falls_back_with_warning(self):
        self.store.write(dataDir=str(self.root / "unmounted"))
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.active, self.default)
        self.assertIn("不可用", state.warning)
        self.assertFalse((self.root / "unmounted").exists())

    def test_pending_move_moves_subdirs_once_and_skips_conflicts(self):
        (self.default / "jobs" / "a").mkdir(parents=True)
        (self.default / "jobs" / "a" / "out.md").write_text("x", encoding="utf-8")
        (self.default / "reader-cache").mkdir()
        (self.drive / "reader-cache").mkdir()
        (self.default / "codex").mkdir()
        (self.default / "researchstudio" / ".venv").mkdir(parents=True)
        (self.default / "researchstudio" / "jobs" / "j1").mkdir(parents=True)
        self.store.write(dataDir=str(self.drive), moveFrom=str(self.default))
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.assertEqual(state.migration["moved"], ["jobs", "researchstudio/jobs"])
        self.assertEqual(state.migration["skipped"], ["reader-cache"])
        self.assertTrue((self.drive / "jobs" / "a" / "out.md").is_file())
        self.assertTrue((self.drive / "researchstudio" / "jobs" / "j1").is_dir())
        self.assertTrue((self.default / "codex").is_dir())  # 本机运行环境不迁移
        self.assertTrue((self.default / "researchstudio" / ".venv").is_dir())
        self.assertEqual(self.store.read()["moveFrom"], "")

    def test_restart_required_after_change(self):
        state = data_storage.resolve_data_dir(self.default, self.store)
        self.store.write(dataDir=str(self.drive))
        self.assertTrue(data_storage.status(state, self.store)["restartRequired"])

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
        self.assertEqual(data_storage.validate_data_dir(f'"{self.drive}"', self.default), self.drive)
        link = self.root / "G_drive"
        link.symlink_to(self.drive)
        self.assertEqual(data_storage.validate_data_dir(str(link), self.default), link)  # 不展开成真实路径
        link.unlink()
        self.assertEqual(list(self.drive.iterdir()), [])  # 写入探测文件已删除

    def test_move_env_keys_keeps_machine_local_keys(self):
        local = self.root / ".env"
        local.write_text("OPENALEX_API_KEY=mine\nCLAUDE_CODE_CLI=/usr/bin/claude\nGITHUB_TOKEN=local\n", encoding="utf-8")
        shared = data_storage.shared_env_path(self.drive)
        shared.parent.mkdir(parents=True)
        shared.write_text("GITHUB_TOKEN=shared\n", encoding="utf-8")
        data_storage.move_env_keys(local, shared, overwrite=False, backup=True)
        self.assertEqual(dotenv_values(shared), {"GITHUB_TOKEN": "shared", "OPENALEX_API_KEY": "mine"})
        self.assertEqual(dotenv_values(local), {"CLAUDE_CODE_CLI": "/usr/bin/claude"})
        self.assertIn("GITHUB_TOKEN=local", (self.root / ".env.before-sync").read_text(encoding="utf-8"))

    def test_load_shared_env_skips_machine_local_and_existing(self):
        shared = self.root / "settings.env"
        shared.write_text("RT_TEST_SHARED=1\nCLAUDE_CODE_CLI=/other/machine/claude\nRT_TEST_EXISTING=shared\n", encoding="utf-8")
        with patch.dict(os.environ, {"RT_TEST_EXISTING": "process"}, clear=False):
            os.environ.pop("CLAUDE_CODE_CLI", None)
            data_storage.load_shared_env(shared)
            self.assertEqual(os.environ["RT_TEST_SHARED"], "1")
            self.assertEqual(os.environ["RT_TEST_EXISTING"], "process")
            self.assertNotIn("CLAUDE_CODE_CLI", os.environ)

    def test_legacy_env_is_moved_to_local_store(self):
        env = self.root / ".env"
        env.write_text(f"{data_storage.LEGACY_DATA_DIR_ENV_KEY}={self.drive}\nOTHER=1\n", encoding="utf-8")
        from dotenv import unset_key
        data_storage.adopt_legacy_env(self.store, env, unset_key)
        self.assertEqual(self.store.read()["dataDir"], str(self.drive))
        self.assertEqual(dotenv_values(env), {"OTHER": "1"})

    def test_heartbeat_and_machines(self):
        data_storage.heartbeat(self.drive, self.store, 0)
        other = data_storage.LocalStore(self.config, self.root / "project", "machine-b")
        data_storage.heartbeat(self.drive, other, 0)
        machines = data_storage.list_machines(self.drive, "machine-a")
        self.assertEqual({item["machine"] for item in machines}, {"machine-a", "machine-b"})
        self.assertTrue(next(item for item in machines if item["machine"] == "machine-b")["active"])
        self.assertFalse(next(item for item in machines if item["machine"] == "machine-a")["active"])

    def test_scan_large(self):
        (self.drive / "jobs").mkdir()
        (self.drive / "jobs" / "a.pdf").write_bytes(b"x" * 2048)
        scan = data_storage.scan_large(self.drive, large_file=1024)
        self.assertEqual(scan["dirs"][0], {"name": "jobs", "bytes": 2048, "files": 1, "pdfs": 1})
        self.assertEqual(scan["largeFiles"], [{"path": "jobs/a.pdf", "bytes": 2048}])

    def test_gapr_cache_goes_to_local_root(self):
        project = gapr_project.Project(root=self.drive / "gap-survey" / "projects" / "p1")
        with patch.dict(os.environ, {"GAPR_LOCAL_ROOT": str(self.root / "local")}):
            self.assertEqual(project.cache_dir, (self.root / "local" / "p1" / "cache"))
            self.assertEqual(project.raw_dir, (self.root / "local" / "p1" / "raw"))
        with patch.dict(os.environ, {"GAPR_LOCAL_ROOT": ""}):
            self.assertEqual(project.cache_dir, project.root / "cache")


class StorageConfigApiTests(unittest.TestCase):
    def setUp(self):
        from researchtool import web_panel
        self.web_panel = web_panel
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.target = root / "drive"
        self.target.mkdir()
        self.store = data_storage.LocalStore(root / "machine.json", root / "project", "machine-a")
        self.client = web_panel.app.test_client()
        self.patches = [patch.object(web_panel, "STORAGE_STORE", self.store), patch.object(web_panel, "ENV_FILE", root / ".env")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_save_new_then_join_then_reset(self):
        response = self.client.put("/api/storage-config", json={"path": str(self.target), "moveExisting": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        storage = response.get_json()["storage"]
        self.assertFalse(storage["saved"]["joined"])
        self.assertEqual(storage["configured"], str(self.target))
        self.assertEqual(storage["pendingMove"], str(self.web_panel.DATA_DIR))
        library_id = data_storage.read_library(self.target)["id"]
        self.assertEqual(self.store.read()["libraryId"], library_id)

        other = data_storage.LocalStore(self.store.path, self.store.project_root, "machine-b")
        with patch.object(self.web_panel, "STORAGE_STORE", other):
            storage = self.client.put("/api/storage-config", json={"path": str(self.target)}).get_json()["storage"]
        self.assertTrue(storage["saved"]["joined"])
        self.assertEqual(other.read()["libraryId"], library_id)

        storage = self.client.delete("/api/storage-config").get_json()["storage"]
        self.assertEqual(storage["configured"], "")
        self.assertEqual(self.store.read()["libraryId"], "")
        self.assertEqual(other.read()["dataDir"], str(self.target))  # 不影响另一台电脑

    def test_rejects_missing_folder(self):
        response = self.client.put("/api/storage-config", json={"path": str(self.target / "missing")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.read()["dataDir"], "")

    def test_env_writes_route_to_shared_file(self):
        shared = self.target / "config" / "settings.env"
        project_env = Path(self.temp.name) / ".env"
        with patch.object(self.web_panel, "SHARED_ENV_FILE", shared), patch.object(self.web_panel, "PROJECT_ENV_FILE", project_env):
            project_env.write_text("OPENALEX_API_KEY=old\n", encoding="utf-8")
            self.web_panel.set_key(str(project_env), "OPENALEX_API_KEY", "new")
            self.web_panel.set_key(str(project_env), "CLAUDE_CODE_CLI", "/bin/claude")
        self.assertEqual(dotenv_values(shared), {"OPENALEX_API_KEY": "new"})
        self.assertEqual(dotenv_values(project_env), {"CLAUDE_CODE_CLI": "/bin/claude"})


if __name__ == "__main__":
    unittest.main()
