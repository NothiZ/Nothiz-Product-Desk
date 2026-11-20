import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class DesktopUiTests(unittest.TestCase):
    def test_taobao_and_tmall_share_one_login_button(self):
        buttons = [widget for widget, _ in self.app.editable_widgets if isinstance(widget, self.module.ctk.CTkButton)]
        labels = [widget.cget("text") for widget in buttons]
        self.assertIn("阿里巴巴", labels)
        self.assertIn("淘宝 / 天猫", labels)
        self.assertNotIn("淘宝", labels)
        self.assertNotIn("天猫", labels)
        next(widget for widget in buttons if widget.cget("text") == "淘宝 / 天猫").invoke()
        self.assertEqual(self.launch.call_args.kwargs["kind"], "login")
        self.assertEqual(self.launch.call_args.kwargs["site"], "taobao")

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("desktop_app"), "Desktop window has not been implemented")
        self.module = importlib.import_module("desktop_app")
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
        self.addCleanup(self.destroy_app, self.app)
        self.app.input_var.set(str(Path(self.temp.name) / "products.xlsx"))
        self.app.output_var.set(str(Path(self.temp.name) / "output.xlsx"))
        self.app.key_var.set("test-key")

    def destroy_app(self, app):
        # Child CTk widgets still own their Tcl commands after timer cancellation.
        for callback in app.tk.call("after", "info"):
            app.tk.call("after", "cancel", callback)
        app.destroy()

    def test_normal_entrypoint_opens_the_window(self):
        with patch("sys.argv", ["desktop_app.py"]), patch.object(self.module, "DesktopApp", return_value=self.app) as window, patch.object(self.app, "mainloop") as mainloop:
            self.assertEqual(self.module.main(), 0)
        window.assert_called_once_with()
        mainloop.assert_called_once_with()

    def test_search_opens_first_and_only_alibaba_is_available(self):
        self.assertEqual(self.app.active_tab, "选品搜索")
        self.assertNotIn("image_mode", self.app.collect_search_settings())
        self.assertIsInstance(self.app.search_platform, self.module.ctk.CTkComboBox)
        menu = self.app.search_platform._dropdown_menu
        self.assertEqual([(menu.entrycget(index, "label").strip(), menu.entrycget(index, "state")) for index in range(3)],
                         [("阿里巴巴国际站", "normal"), ("淘宝 / 天猫（暂未支持）", "disabled"),
                          ("亚马逊（暂未支持）", "disabled")])

    def test_tab_intro_changes_with_the_active_page(self):
        self.assertEqual(self.app.page_intro.cget("text"),
                         "按分类名和关键词搜索候选商品，在浏览器里勾选后导入，再进入图片采集。首次使用请先登录阿里巴巴。")
        for name, description in (
            ("图片采集", "按选品结果表下载商品图片并检查清晰度，清晰图不足的商品会被淘汰，不再生成详情。"),
            ("详情生成", "读取商品链接表，生成中英文标题与详情；从图片采集进入时，只处理已完成的商品。"),
        ):
            self.app._select_tab(name)
            self.assertEqual(self.app.page_intro.cget("text"), description)

    def test_search_image_button_opens_the_current_task_without_starting_it(self):
        wall = Path(self.temp.name).resolve() / "jobs" / "photos" / "candidates.html"
        wall.parent.mkdir(parents=True)
        workbook = wall.with_name("选品结果.xlsx")
        workbook.touch()
        self.app.wall_path = wall
        with patch.object(self.app, "_select_image_workbook") as choose:
            self.app.go_image_button.invoke()
        choose.assert_called_once_with(workbook)
        self.assertEqual(self.app.active_tab, "图片采集")
        self.launch.assert_not_called()

    def test_input_status_follows_file_changes_without_overwriting_running_status(self):
        source = Path(self.temp.name) / "selected.xlsx"
        source.touch()
        with patch.object(self.module.filedialog, "askopenfilename", return_value=str(source)):
            self.app.choose_input()
        self.assertIn("已选择 selected.xlsx", self.app.status_label.cget("text"))
        self.app.input_var.set("")
        self.assertIn("请选择 Excel 文件", self.app.status_label.cget("text"))
        self.app.input_var.set(str(source.with_name("missing.xlsx")))
        self.assertIn("文件不存在", self.app.status_label.cget("text"))
        self.app.task = self.task
        self.app._status("正在处理商品…")
        self.app.input_var.set(str(source))
        self.assertIn("正在处理商品", self.app.status_label.cget("text"))

    def test_saved_input_file_sets_the_initial_status(self):
        source = Path(self.temp.name) / "saved.xlsx"
        source.touch()
        with patch.object(self.module, "load_settings", return_value={"input_file": str(source)}):
            restarted = self.module.DesktopApp()
        restarted.withdraw()
        self.addCleanup(self.destroy_app, restarted)
        self.assertIn("已选择 saved.xlsx", restarted.status_label.cget("text"))

    def test_zero_progress_blends_into_track_and_positive_progress_is_blue(self):
        self.assertEqual(self.app.progress.get(), 0)
        self.assertEqual(self.app.progress.cget("progress_color"), self.app.progress.cget("fg_color"))
        self.app._progress(1, 2)
        self.assertEqual(self.app.progress.get(), 0.5)
        self.assertEqual(self.app.progress.cget("progress_color"), self.module.ACCENT)
        self.app._progress(0, 1)
        self.assertEqual(self.app.progress.get(), 0)
        self.assertEqual(self.app.progress.cget("progress_color"), self.app.progress.cget("fg_color"))

    def test_start_tracks_background_work_and_stop_keeps_window_alive(self):
        with patch.object(self.module, "validate_generation", side_effect=lambda settings, key: settings):
            self.app.start_generation()
        self.assertEqual(self.launch.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(self.app.start_button.cget("state"), "disabled")
        self.task.read_updates.return_value = [{"type": "progress", "total": 5, "done": 2, "failed": 1}]
        self.app.poll_task()
        self.assertIn("成功 2", self.app.counts_label.cget("text"))
        self.assertAlmostEqual(self.app.progress.get(), 0.6)
        self.assertIn("正在处理商品", self.app.status_label.cget("text"))
        self.assertNotIn("读取任务状态失败", self.app.log_text.get("1.0", "end"))
        self.app.stop_task()
        self.task.request_stop.assert_called_once()
        self.app.poll_task()
        self.assertIn("正在停止", self.app.status_label.cget("text"))
        self.assertTrue(self.app.winfo_exists())

    def test_candidate_search_progress_and_wall_leave_details_usable(self):
        self.app.search_start_button.invoke()
        settings = self.launch.call_args.args[0]
        self.assertEqual(self.launch.call_args.kwargs, {"kind": "search"})
        self.assertEqual(settings["search"], "婴儿奶瓶消毒器")
        self.assertEqual(settings["category"], "")
        self.assertEqual(settings["prefix"], "")
        self.assertEqual(self.app.start_button.cget("state"), "disabled")
        self.task.read_updates.return_value = [
            {"type": "progress", "total": 5, "done": 2, "failed": 1, "message": "正在下载缩略图"},
        ]
        self.app.poll_task()
        self.assertIn("正在下载缩略图", self.app.search_counts_label.cget("text"))
        self.assertAlmostEqual(self.app.search_progress.get(), 0.6)
        self.assertEqual(self.app.progress.get(), 0)
        self.app.search_stop_button.invoke()
        self.task.request_stop.assert_called_once()
        wall = Path(self.temp.name).resolve() / "候选图片墙.html"
        wall.touch()
        self.task.poll.return_value = 0
        self.task.read_updates.return_value = []
        self.task.result.return_value = {"status": "stopped", "wall_file": str(wall)}
        self.app.poll_task()
        self.assertEqual(self.app.start_button.cget("state"), "normal")
        self.assertEqual(self.save.call_args.args[0]["last_candidate_wall"], str(wall))
        with patch.object(self.module.webbrowser, "open", return_value=True) as browser:
            self.app.open_wall_button.invoke()
        browser.assert_called_once_with(wall.as_uri())

    def test_first_run_chooses_parent_before_constructing_desktop_app(self):
        root = Path(self.temp.name).resolve()
        destination = root / "工作数据"
        destination.mkdir()
        expected = destination / "ProductDetailsDesktop"

        def construct_app():
            self.assertEqual(self.module.user_data_dir(), expected)
            return Mock()

        with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local"), "PRODUCT_DETAILS_DATA_DIR": ""}), \
                patch("sys.argv", ["desktop_app.py"]), patch.object(self.module.tk, "Tk"), \
                patch.object(self.module.filedialog, "askdirectory", return_value=str(destination)) as choose, \
                patch.object(self.module, "DesktopApp", side_effect=construct_app) as window:
            self.assertEqual(self.module.main(), 0)
        choose.assert_called_once()
        window.assert_called_once_with()

    def test_canceling_first_run_does_not_create_a_data_folder(self):
        local = Path(self.temp.name).resolve() / "fresh-local"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local), "PRODUCT_DETAILS_DATA_DIR": ""}), \
                patch("sys.argv", ["desktop_app.py"]), patch.object(self.module.tk, "Tk"), \
                patch.object(self.module.filedialog, "askdirectory", return_value=""), \
                patch.object(self.module, "DesktopApp") as window:
            self.assertEqual(self.module.main(), 0)
        window.assert_not_called()
        self.assertFalse(local.exists())

    def test_candidate_import_uses_the_wall_manifest_and_local_workbook(self):
        root = Path(self.temp.name).resolve()
        job = root / "jobs" / "test-search"
        job.mkdir(parents=True)
        wall = job / "候选图片墙.html"
        wall.touch()
        manifest = job / "candidate_manifest.json"
        manifest.touch()
        selection = root / "选中结果.csv"
        selection.touch()
        self.app.wall_path = wall
        import_selection = Mock(return_value=["SP01"])
        with patch.object(self.module.filedialog, "askopenfilename", return_value=str(selection)), \
                patch.dict("sys.modules", {"candidate_wall": SimpleNamespace(import_selection=import_selection)}):
            self.app.import_candidates()
        import_selection.assert_called_once_with(selection, manifest, root / "选品结果.xlsx",
                                                 task_workbook_path=job / "选品结果.xlsx")
        self.assertIn("SP01", self.app.search_import_label.cget("text"))

    def test_image_tab_opens_after_import_and_only_hands_off_finished_rows(self):
        from openpyxl import Workbook, load_workbook

        root = Path(self.temp.name).resolve()
        job = root / "jobs" / "photos"
        job.mkdir(parents=True)
        wall = job / "candidates.html"
        wall.touch()
        wall.with_name("candidate_manifest.json").touch()
        selection = job / "selected.csv"
        selection.touch()
        output = job / "选品结果.xlsx"
        workbook = Workbook()
        workbook.active.append(["商品编号", "分类名", "链接", "图片模式", "采集状态"])
        workbook.active.append(["SP01", "洗发水", "https://www.alibaba.com/product-detail/test_1.html", "常规产品图"])
        workbook.save(output)
        workbook.close()
        self.app.wall_path = wall
        with patch.object(self.module.filedialog, "askopenfilename", return_value=str(selection)), \
                patch("candidate_wall.import_selection", return_value=["SP01"]):
            self.app.import_candidates()
        self.assertEqual(self.app.active_tab, "图片采集")
        self.assertEqual(self.app.image_vars["input_file"].get(), str(output))
        self.assertEqual(self.app.image_vars["output_dir"].get(), str(job / "图片"))
        self.assertIn("已读取 1 个商品", self.app.image_input_summary.cget("text"))
        self.assertEqual(self.app.image_details_button.cget("state"), "disabled")
        self.app._change_image_mode("平铺图")
        self.assertEqual(self.app.image_vars["min_clear"].get(), "2")
        self.app.image_vars["min_clear"].set("3")
        self.app._change_image_mode("常规产品图")
        self.assertEqual(self.app.image_vars["min_clear"].get(), "3")
        self.app.start_images()
        self.assertEqual(self.launch.call_args.kwargs["kind"], "images")
        self.assertEqual(self.app.start_button.cget("state"), "disabled")
        self.assertEqual(self.app.search_start_button.cget("state"), "disabled")
        workbook = load_workbook(output)
        workbook.active["E2"] = "已完成"
        workbook.save(output)
        workbook.close()
        self.app._finish_task({"status": "finished", "done": 1, "total": 1})
        self.assertEqual(self.app.image_details_button.cget("state"), "normal")
        self.app.image_details_button.invoke()
        self.assertEqual(self.app.active_tab, "详情生成")
        self.assertEqual(self.app.input_var.get(), str(output))

    def test_search_controls_match_the_details_styles(self):
        for search_widget, details_widget in (
            (self.app.search_start_button, self.app.start_button),
            (self.app.search_stop_button, self.app.stop_button),
        ):
            for option in ("fg_color", "hover_color", "text_color_disabled", "border_width", "corner_radius", "height"):
                with self.subTest(control=search_widget.cget("text"), option=option):
                    self.assertEqual(search_widget.cget(option), details_widget.cget(option))
        for option in ("fg_color", "border_width", "corner_radius"):
            self.assertEqual(self.app.search_log_text.cget(option), self.app.log_text.cget(option))

    def test_saved_wall_populates_ready_search_results(self):
        root = Path(self.temp.name).resolve()
        wall = root / "candidates.html"
        wall.touch()
        (root / "candidate_manifest.json").write_text(json.dumps({"items": [
            {"image": "candidates/123.jpg"}, {"image": ""},
        ]}), encoding="utf-8")
        with patch.object(self.module, "load_settings", return_value={"last_candidate_wall": str(wall)}):
            restarted = self.module.DesktopApp()
        restarted.withdraw()
        self.addCleanup(self.destroy_app, restarted)
        self.assertIn("候选 2 个", restarted.search_counts_label.cget("text"))
        self.assertIn("主图已下载 1/2", restarted.search_counts_label.cget("text"))
        self.assertEqual(restarted.open_wall_button.cget("state"), "normal")
        with patch.object(self.module.webbrowser, "open", return_value=True) as browser:
            restarted.open_wall_button.invoke()
        browser.assert_called_once_with(wall.as_uri())

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
        self.addCleanup(self.destroy_app, restarted)
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
        self.assertEqual(self.app.start_button.cget("state"), "normal")

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
        self.assertEqual(self.app.start_button.cget("state"), "normal")
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
        self.addCleanup(self.destroy_app, small)
        small._select_tab("详情生成")
        small.after(300, small.quit)
        small.mainloop()
        self.assertGreaterEqual(small.log_text.winfo_height(), 60)
        self.assertLess(small.start_button.winfo_rooty() + small.start_button.winfo_height(), small.winfo_rooty() + small.winfo_height())

    def test_search_button_surfaces_fit_inside_a_compact_window(self):
        with patch.object(self.module.DesktopApp, "winfo_screenheight", return_value=720), \
                patch.object(self.module.DesktopApp, "winfo_screenwidth", return_value=1280):
            small = self.module.DesktopApp()
        self.addCleanup(self.destroy_app, small)
        self.assertEqual(small.active_tab, "选品搜索")
        small.after(200, small.quit)
        small.mainloop()
        small.update()
        for button in (small.search_start_button, small.search_stop_button, small.open_wall_button,
                       small.import_candidates_button, small.go_image_button):
            with self.subTest(button=button.cget("text")):
                canvas = next(child for child in button.winfo_children() if isinstance(child, self.module.tk.Canvas))
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), small.winfo_rooty() + small.winfo_height())
                self.assertEqual((canvas.winfo_x(), canvas.winfo_y()), (0, 0))
                self.assertEqual((canvas.winfo_width(), canvas.winfo_height()), (button.winfo_width(), button.winfo_height()))
        start = small.search_start_button
        start.event_generate("<FocusIn>")
        self.assertGreater(start.cget("border_width"), 0)
        start.event_generate("<FocusOut>")
        self.assertEqual(start.cget("border_width"), 0)
        small.go_image_button.invoke()
        small.update()
        self.assertGreaterEqual(small.image_table.winfo_height(), 40)
        self.assertLessEqual(small.image_details_button.winfo_rooty() + small.image_details_button.winfo_height(),
                             small.winfo_rooty() + small.winfo_height())


if __name__ == "__main__":
    unittest.main()
