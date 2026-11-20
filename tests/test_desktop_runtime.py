import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import desktop_runtime as runtime
except ModuleNotFoundError:
    runtime = None


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "Desktop runtime is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"PRODUCT_DETAILS_DATA_DIR": str(self.root / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.input = self.root / "input.xlsx"
        self.input.write_bytes(b"placeholder")
        self.settings = {**runtime.default_settings(), "input_file": str(self.input), "output_file": str(self.root / "out.xlsx")}

    def test_user_data_location_can_be_isolated(self):
        self.assertEqual(runtime.user_data_dir(), self.root / "data")
        self.assertTrue(runtime.user_data_dir().is_dir())

    def test_unremembering_removes_previously_saved_keys(self):
        self.settings.update(remember_key=True, api_keys={"deepseek": "test-secret"})
        runtime.save_settings(self.settings)
        self.assertEqual(runtime.load_settings()["api_keys"], {"deepseek": "test-secret"})
        self.settings["remember_key"] = False
        runtime.save_settings(self.settings)
        text = (runtime.user_data_dir() / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("test-secret", text)
        self.assertEqual(runtime.load_settings()["api_keys"], {})

    def test_validate_prevents_overwriting_input_workbook(self):
        self.settings["output_file"] = str(self.input)
        with self.assertRaisesRegex(ValueError, "输入|同"):
            runtime.validate_generation(self.settings, "test-secret")

    def test_generation_requires_an_api_key(self):
        with self.assertRaisesRegex(ValueError, "API|Key"):
            runtime.validate_generation(self.settings, "")

    def test_task_file_never_contains_keys_but_worker_environment_does(self):
        self.settings["api_keys"] = {"deepseek": "test-secret"}
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(runtime.subprocess, "Popen", return_value=child) as spawn:
            task = runtime.launch_job(self.settings, api_key="test-secret")
        task_text = (task.folder / "task.json").read_text(encoding="utf-8")
        self.assertNotIn("test-secret", task_text)
        self.assertNotIn("api_keys", task_text)
        self.assertEqual(spawn.call_args.kwargs["env"]["DEEPSEEK_API_KEY"], "test-secret")
        self.assertEqual(task.result(), {})

    def test_search_serializes_only_validated_search_settings(self):
        settings = {"search": " 婴儿奶瓶消毒器 ", "pages": "2", "limit": "60",
                    "prefix": "ab1", "api_keys": {"deepseek": "test-secret"}}
        with patch.object(runtime.subprocess, "Popen"):
            task = runtime.launch_job(settings, kind="search")
        payload = json.loads((task.folder / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["settings"], {"search": "婴儿奶瓶消毒器", "pages": 2, "limit": 60,
                                               "required": "", "excluded": "", "category": "",
                                               "prefix": "AB1", "image_mode": "常规产品图", "show_browser": True})
        self.assertEqual(task.folder.parent, runtime.user_data_dir() / "jobs")
        for invalid in ({"pages": 0}, {"search": " ,， "}, {"prefix": "中文"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                runtime.launch_job({**settings, **invalid}, kind="search")

    def test_selected_data_parent_is_restored_for_settings_and_worker_jobs(self):
        local = self.root / "local"
        destination = self.root / "工作数据"
        destination.mkdir()
        expected = destination / "ProductDetailsDesktop"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local), "PRODUCT_DETAILS_DATA_DIR": ""}), \
                patch.object(runtime, "resource_dir", return_value=self.root / "empty_resources"):
            self.assertIsNotNone(getattr(runtime, "configure_user_data_dir", None))
            self.assertTrue(runtime.needs_user_data_setup())
            runtime.configure_user_data_dir(destination)
            self.assertFalse(runtime.needs_user_data_setup())
            runtime.save_settings(self.settings)
            self.assertEqual(runtime.user_data_dir(), expected)
            self.assertTrue((expected / "settings.json").is_file())
            self.assertEqual(runtime.login_state_path("alibaba_state.json").parent, expected)
            with patch.object(runtime.subprocess, "Popen"):
                task = runtime.launch_job(self.settings["candidate_search"], kind="search")
                login = runtime.launch_job(self.settings, kind="login")
            self.assertEqual(task.folder.parent, expected / "jobs")
            self.assertEqual(login.folder.parent, expected / "jobs")
            self.assertFalse((local / "ProductDetailsDesktop").exists())

    def test_existing_default_data_is_reused_without_setup(self):
        local = self.root / "local"
        legacy = local / "ProductDetailsDesktop"
        legacy.mkdir(parents=True)
        (legacy / "settings.json").write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local), "PRODUCT_DETAILS_DATA_DIR": ""}):
            self.assertFalse(runtime.needs_user_data_setup())
            self.assertEqual(runtime.user_data_dir(), legacy)

    def test_log_reader_preserves_partial_lines_and_events(self):
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(runtime.subprocess, "Popen", return_value=child):
            task = runtime.launch_job(self.settings, api_key="test-secret")
        task.log_path.write_bytes("正在".encode("utf-8"))
        self.assertEqual(task.read_updates(), [])
        with task.log_path.open("ab") as log:
            log.write(("运行\n" + runtime.EVENT_PREFIX + json.dumps({"type":"progress","total":2,"done":1,"failed":0}) + "\n").encode("utf-8"))
        updates = task.read_updates()
        self.assertEqual(updates[0], {"type":"log","message":"正在运行"})
        self.assertEqual(updates[1]["done"], 1)
        self.assertEqual(task.read_updates(), [])

    def test_progress_events_are_independent_of_unfinished_thread_log_lines(self):
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(runtime.subprocess, "Popen", return_value=child):
            task = runtime.launch_job(self.settings, api_key="test-secret")
        task.log_path.write_text("an unfinished thread log",encoding="utf-8")
        (task.folder/"events.jsonl").write_text(json.dumps({"type":"progress","total":2,"done":1,"failed":0})+"\n",encoding="utf-8")
        self.assertEqual(task.read_updates(),[{"type":"progress","total":2,"done":1,"failed":0}])
        self.assertEqual(task.read_updates(), [])


if __name__ == "__main__":
    unittest.main()
