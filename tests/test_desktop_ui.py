import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


class DesktopUiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("desktop_app"), "Desktop window has not been implemented")
        self.module = importlib.import_module("desktop_app")
        self.module.enable_dpi_awareness()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"PRODUCT_DETAILS_DATA_DIR": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.task = Mock()
        self.task.poll.return_value = None
        self.task.read_updates.return_value = []
        self.task.result.return_value = {}
        self.task.log_path = Path(self.temp.name) / "worker.log"
        self.launch = patch.object(self.module, "launch_job", return_value=self.task).start()
        self.addCleanup(patch.stopall)
        self.save = patch.object(self.module, "save_settings").start()
        self.app = self.module.DesktopApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.addCleanup(self.app.destroy)
        self.app.input_var.set(str(Path(self.temp.name) / "products.xlsx"))
        self.app.output_var.set(str(Path(self.temp.name) / "output.xlsx"))
        self.app.key_var.set("test-key")

    def test_start_tracks_background_work_and_stop_keeps_window_alive(self):
        with patch.object(self.module, "validate_generation", side_effect=lambda settings, key: settings):
            self.app.start_generation()
        self.assertEqual(self.launch.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.task.read_updates.return_value = [{"type": "progress", "total": 5, "done": 2, "failed": 1}]
        self.app.poll_task()
        self.assertIn("2", self.app.counts_var.get())
        self.app.stop_task()
        self.task.request_stop.assert_called_once()
        self.assertTrue(self.app.winfo_exists())

    def test_provider_switch_preserves_keys_and_models_without_remembering(self):
        self.app.model_var.set("deepseek-custom")
        self.app.provider_var.set("Gemini")
        self.app.change_provider()
        self.assertEqual(self.app.key_var.get(), "")
        self.app.key_var.set("gemini-key")
        self.app.model_var.set("gemini-custom")
        self.app.provider_var.set("DeepSeek")
        self.app.change_provider()
        self.assertEqual(self.app.key_var.get(), "test-key")
        self.assertEqual(self.app.model_var.get(), "deepseek-custom")
        self.app._save_settings()
        self.assertEqual(self.save.call_args.args[0]["api_keys"], {})
        self.app.provider_var.set("Gemini")
        self.app.change_provider()
        self.assertEqual(self.app.key_var.get(), "gemini-key")
        self.assertEqual(self.app.model_var.get(), "gemini-custom")
        self.app._save_settings()
        saved = self.save.call_args.args[0]
        with patch.object(self.module, "load_settings", return_value=saved):
            restarted = self.module.DesktopApp()
        restarted.withdraw()
        restarted.update_idletasks()
        self.addCleanup(restarted.destroy)
        self.assertEqual(restarted.model_var.get(), "gemini-custom")
        restarted.provider_var.set("DeepSeek")
        restarted.change_provider()
        self.assertEqual(restarted.model_var.get(), "deepseek-custom")

    def test_login_prompts_and_worker_failure_are_actionable(self):
        self.app.start_login("taobao")
        self.task.read_updates.return_value = [None, {"type": "progress", "total": "broken"}, {"type": "prompt", "id": "login", "message": "请完成浏览器登录"}]
        self.app.poll_task()
        self.assertTrue(self.app.continue_button.winfo_manager())
        self.app.continue_login()
        self.task.respond.assert_called_with("login", "")
        self.task.read_updates.return_value = [{"type": "prompt", "id": "check", "message": "已登录？(y/n)"}]
        with patch.object(self.module.messagebox, "askyesno", return_value=True):
            self.app.poll_task()
        self.task.respond.assert_called_with("check", "y")
        self.task.read_updates.return_value = []
        self.task.poll.return_value = 1
        with patch.object(self.module.messagebox, "showerror") as error:
            self.app.poll_task()
        self.assertIsNone(self.app.task)
        self.assertIn("worker.log", error.call_args.args[1])
        self.assertEqual(str(self.app.start_button["state"]), "normal")

    def test_close_can_be_cancelled_then_waits_for_worker_to_save(self):
        self.app.start_login("alibaba")
        with patch.object(self.module.messagebox, "askyesno", return_value=False):
            self.app.close_window()
        self.task.request_stop.assert_not_called()
        with patch.object(self.module.messagebox, "askyesno", return_value=True):
            self.app.close_window()
        self.task.request_stop.assert_called_once()
        self.assertTrue(self.app.winfo_exists())
        self.task.poll.return_value = 0
        self.task.result.return_value = {"status": "stopped", "message": "已保存"}
        with patch.object(self.app, "destroy") as destroy:
            self.app.poll_task()
        destroy.assert_called_once()

    def test_startup_error_restores_ready_controls(self):
        self.launch.side_effect = OSError("无法启动子进程")
        with patch.object(self.module.messagebox, "showerror") as error:
            self.app.start_login("tmall")
        self.assertIsNone(self.app.task)
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.assertIn("无法启动", error.call_args.args[1])

    def test_failed_stop_allows_user_to_try_closing_again(self):
        self.app.start_login("alibaba")
        self.task.request_stop.side_effect = OSError("磁盘不可写")
        with patch.object(self.module.messagebox, "askyesno", return_value=True), patch.object(self.module.messagebox, "showerror"):
            self.app.close_window()
        self.assertFalse(self.app.closing)

    def test_small_screen_keeps_log_and_actions_visible(self):
        with patch.object(self.module.DesktopApp, "winfo_screenheight", return_value=720), patch.object(self.module.DesktopApp, "winfo_screenwidth", return_value=1280):
            small = self.module.DesktopApp()
        self.addCleanup(small.destroy)
        small.update()
        self.assertGreaterEqual(small.log_text.winfo_height(), 60)
        self.assertLess(small.start_button.winfo_rooty() + small.start_button.winfo_height(), small.winfo_rooty() + small.winfo_height())


if __name__ == "__main__":
    unittest.main()
