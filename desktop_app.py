"""Chinese desktop window; all browser and API work runs in a child process."""
import argparse
import multiprocessing
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from ui_theme import BG, CARD, add_log_tags, apply_theme, enable_dpi_awareness, log_tag, style_log

from desktop_runtime import (
    APP_TITLE, default_settings, launch_job, load_settings, save_settings,
    resource_dir, validate_generation,
)


class DesktopApp(tk.Tk):
    PROVIDERS = {"DeepSeek": "deepseek", "Gemini": "gemini"}
    TEMPLATES = {"模板 1 · Key: Value 列表": "template1", "模板 2 · Details + Specifications": "template2"}

    def __init__(self):
        # Give the app its own Windows taskbar group instead of Python's.
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ProductDetailsDesktop.App")
        super().__init__()
        self.title(APP_TITLE)
        self.iconbitmap(default=str(resource_dir() / "assets" / "logo.ico"))
        k = max(1.0, self.winfo_fpixels("1i") / 96)  # 高分屏缩放
        width = min(int(980 * k), self.winfo_screenwidth() - 80)
        height = min(int(820 * k), self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}")
        self.minsize(min(int(860 * k), width), min(int(720 * k), height))
        style = apply_theme(self)
        style.configure("CardBody.TFrame", background=CARD)
        if height < 720:
            style.configure("TButton", padding=(12, 4))
            style.configure("Accent.TButton", padding=(18, 4))
            style.configure("TEntry", padding=(8, 3))
            style.configure("TCombobox", padding=(8, 3))
        self.settings = default_settings()
        startup_error = ""
        try:
            self.settings.update(load_settings())
        except Exception as exc:
            startup_error = f"读取设置失败，已使用默认设置：{exc}"
        self.keys = dict(self.settings.get("api_keys", {}))
        self.models = {"deepseek": "deepseek-chat", "gemini": "gemini-2.5-flash"}
        saved_models = self.settings.get("models")
        if isinstance(saved_models, dict):
            self.models.update({provider: model.strip() for provider, model in saved_models.items()
                                if provider in self.models and isinstance(model, str) and model.strip()})
        self.current_provider = self.settings.get("provider", "deepseek")
        if self.current_provider not in self.PROVIDERS.values():
            self.current_provider = "deepseek"
        self.models[self.current_provider] = self.settings.get("model") or self.models[self.current_provider]
        self.task = None
        self.finished = None
        self.pending_prompt = None
        self.closing = False
        self.poll_after = None
        self.editable_widgets = []
        self.input_var = tk.StringVar(value=self.settings.get("input_file", ""))
        self.output_var = tk.StringVar(value=self.settings.get("output_file", ""))
        self.provider_var = tk.StringVar(value=next(label for label, value in self.PROVIDERS.items() if value == self.current_provider))
        self.model_var = tk.StringVar(value=self.models[self.current_provider])
        self.key_var = tk.StringVar(value=self.keys.get(self.current_provider, ""))
        self.remember_var = tk.BooleanVar(value=self.settings.get("remember_key", False))
        self.template_var = tk.StringVar(value=next((label for label, value in self.TEMPLATES.items() if value == self.settings.get("template")), list(self.TEMPLATES)[1]))
        self.resume_var = tk.BooleanVar(value=self.settings.get("resume", True))
        self.browser_var = tk.BooleanVar(value=self.settings.get("show_browser", True))
        self.status_var = tk.StringVar(value="就绪 · 选择 Excel 文件后开始生成")
        self.counts_var = tk.StringVar(value="待处理 0  ·  成功 0  ·  失败 0")
        self._build_window(compact=height < 720)
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        if startup_error:
            self.append_log(startup_error)

    def _build_window(self, compact=False):
        root = ttk.Frame(self, padding=(16, 10) if compact else (28, 20, 28, 16))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(8, weight=1)

        def section(row, title, pady=(14, 6)):
            ttk.Label(root, text=title, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(6, 3) if compact else pady)

        def card(row):
            frame = ttk.Frame(root, style="Card.TFrame", padding=(12, 6) if compact else (18, 12))
            frame.grid(row=row, column=0, sticky="nsew" if row == 8 else "ew")
            return frame

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="w")
        try:
            # Display the 512px source logo at 64px in the header.
            self.logo = tk.PhotoImage(file=str(resource_dir() / "assets" / "logo.png")).subsample(8)
            tk.Label(header, image=self.logo, bg=BG, bd=0).grid(row=0, column=0, rowspan=2, padx=(0, 14))
        except tk.TclError:
            pass
        ttk.Label(header, text="产品详情生成", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="导入商品链接，生成中英文标题与详情。首次使用请先登录商品平台。", style="Sub.TLabel").grid(row=1, column=1, sticky="w")

        section(1, "Excel 文件")
        files = card(2)
        files.columnconfigure(1, weight=1)
        files.columnconfigure(0, minsize=72)
        for row, label, variable, callback in [(0, "输入文件", self.input_var, self.choose_input), (1, "输出文件", self.output_var, self.choose_output)]:
            ttk.Label(files, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(files, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            button = ttk.Button(files, text="选择…", command=callback)
            button.grid(row=row, column=2, padx=(12, 0), pady=5)
            self.editable_widgets.extend([(entry, "normal"), (button, "normal")])

        section(3, "生成设置")
        settings = card(4)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        settings.columnconfigure(0, minsize=72)
        ttk.Label(settings, text="AI 服务", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        provider = ttk.Combobox(settings, textvariable=self.provider_var, values=list(self.PROVIDERS), state="readonly", width=15)
        provider.grid(row=0, column=1, sticky="ew", pady=5)
        provider.bind("<<ComboboxSelected>>", self.change_provider)
        ttk.Label(settings, text="模型", style="Muted.TLabel").grid(row=0, column=2, padx=(16, 8))
        model = ttk.Entry(settings, textvariable=self.model_var)
        model.grid(row=0, column=3, sticky="ew", pady=5)
        ttk.Label(settings, text="API Key", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        key = ttk.Entry(settings, textvariable=self.key_var, show="●")
        key.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        remember = ttk.Checkbutton(settings, text="在本机记住 API Key", variable=self.remember_var, command=self.remember_changed)
        remember.grid(row=1, column=3, sticky="w", padx=(16, 0), pady=5)
        ttk.Label(settings, text="详情模板", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        template = ttk.Combobox(settings, textvariable=self.template_var, values=list(self.TEMPLATES), state="readonly")
        template.grid(row=2, column=1, columnspan=3, sticky="ew", pady=5)
        flags = ttk.Frame(settings, style="CardBody.TFrame")
        flags.grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))
        resume = ttk.Checkbutton(flags, text="断点续跑（跳过已完成商品）", variable=self.resume_var)
        resume.pack(side="left", padx=(0, 24))
        browser = ttk.Checkbutton(flags, text="显示浏览器（便于手动验证）", variable=self.browser_var)
        browser.pack(side="left")
        self.editable_widgets.extend([(provider, "readonly"), (model, "normal"), (key, "normal"), (remember, "normal"), (template, "readonly"), (resume, "normal"), (browser, "normal")])

        # Preserve usable log space when the screen cannot fit the full-size spacing.
        if compact:
            for panel in (files, settings):
                for widget in panel.winfo_children():
                    widget.grid_configure(pady=2)

        actions = ttk.Frame(root)
        actions.grid(row=6, column=0, sticky="ew", pady=(8 if compact else 16, 0))
        ttk.Label(actions, text="平台登录", style="Sub.TLabel").pack(side="left", padx=(0, 10))
        for label, site in [("阿里巴巴", "alibaba"), ("淘宝", "taobao"), ("天猫", "tmall")]:
            button = ttk.Button(actions, text=label, command=lambda selected=site: self.start_login(selected))
            button.pack(side="left", padx=(0, 8))
            self.editable_widgets.append((button, "normal"))
        self.start_button = ttk.Button(actions, text="开始生成", style="Accent.TButton", command=self.start_generation)
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(actions, text="停止并保存", command=self.stop_task, state="disabled")
        self.stop_button.pack(side="right", padx=10)

        section(7, "运行状态", pady=(18, 6))
        logs = card(8)
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(3, weight=1)
        top = ttk.Frame(logs, style="CardBody.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(top, textvariable=self.status_var, style="StatusIdle.TLabel", wraplength=560)
        self.status_label.grid(row=0, column=0, sticky="w")
        top.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(200, e.width - 260)))
        self.counts_label = ttk.Label(top, textvariable=self.counts_var, style="Muted.TLabel")
        self.counts_label.grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(logs, maximum=1)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.continue_button = ttk.Button(logs, text="我已完成登录，继续", command=self.continue_login)
        self.continue_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.continue_button.grid_remove()
        self.log_text = ScrolledText(logs, height=7, wrap="word", state="disabled")
        style_log(self.log_text)
        add_log_tags(self.log_text)
        self.log_text.grid(row=3, column=0, sticky="nsew", pady=(12, 0))

        footer = ttk.Frame(root)
        footer.grid(row=9, column=0, sticky="ew", pady=(6 if compact else 14, 0))
        ttk.Label(footer, text="停止后会等待当前商品处理完成并保存。", style="Sub.TLabel").pack(side="left")
        ttk.Button(footer, text="打开输出文件", command=self.open_output).pack(side="right")
        self._status("就绪 · 选择 Excel 文件后开始生成", "idle")

    def _status(self, text, tone="run"):
        self.status_var.set(f"● {text}")
        self.status_label.configure(style={"idle": "StatusIdle", "run": "StatusRun", "ok": "StatusOk", "err": "StatusErr"}[tone] + ".TLabel")

    def _counts(self, total, done, failed):
        self.counts_var.set(f"待处理 {total}  ·  成功 {done}  ·  失败 {failed}")

    def choose_input(self):
        selected = filedialog.askopenfilename(parent=self, title="选择商品链接 Excel", filetypes=[("Excel 工作簿", "*.xlsx")])
        if selected:
            self.input_var.set(selected)
            if not self.output_var.get().strip():
                source = Path(selected)
                self.output_var.set(str(source.with_name(source.stem + "_output.xlsx")))

    def choose_output(self):
        selected = filedialog.asksaveasfilename(parent=self, title="选择输出位置", defaultextension=".xlsx", filetypes=[("Excel 工作簿", "*.xlsx")])
        if selected:
            self.output_var.set(selected)

    def change_provider(self, _event=None):
        self.keys[self.current_provider] = self.key_var.get()
        self.models[self.current_provider] = self.model_var.get()
        self.current_provider = self.PROVIDERS[self.provider_var.get()]
        self.key_var.set(self.keys.get(self.current_provider, ""))
        self.model_var.set(self.models[self.current_provider])

    def collect_settings(self):
        self.keys[self.current_provider] = self.key_var.get()
        self.models[self.current_provider] = self.model_var.get()
        return {**self.settings, "provider": self.current_provider, "model": self.model_var.get().strip(), "models": dict(self.models),
                "template": self.TEMPLATES[self.template_var.get()], "input_file": self.input_var.get().strip(),
                "output_file": self.output_var.get().strip(), "resume": self.resume_var.get(),
                "show_browser": self.browser_var.get(), "remember_key": self.remember_var.get(),
                "api_keys": dict(self.keys) if self.remember_var.get() else {}}

    def remember_changed(self):
        self._save_settings()

    def _save_settings(self):
        self.settings = self.collect_settings()
        try:
            save_settings(self.settings)
        except Exception as exc:
            self.append_log(f"设置未能保存：{exc}")

    def start_generation(self):
        if self.task:
            return
        try:
            settings = validate_generation(self.collect_settings(), self.key_var.get())
        except Exception as exc:
            messagebox.showerror("请检查生成设置", str(exc), parent=self)
            return
        self.input_var.set(settings["input_file"])
        self.output_var.set(settings["output_file"])
        self._start_task(settings, kind="generate", api_key=self.key_var.get())

    def start_login(self, site):
        if not self.task:
            self._start_task(self.collect_settings(), kind="login", site=site)

    def _start_task(self, settings, **options):
        self._save_settings()
        try:
            self.task = launch_job(settings, **options)
        except Exception as exc:
            self.append_log(f"任务启动失败：{exc}")
            messagebox.showerror("任务启动失败", str(exc), parent=self)
            return
        self.finished = None
        self.pending_prompt = None
        self.progress.configure(value=0, maximum=1)
        self._counts(0, 0, 0)
        self._status("正在启动…")
        self.append_log("任务已启动。")
        self._set_busy(True)
        self._schedule_poll()

    def _set_busy(self, busy):
        for widget, idle_state in self.editable_widgets:
            widget.configure(state="disabled" if busy else idle_state)
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n", log_tag(message))
        if int(self.log_text.index("end-1c").split(".")[0]) > 5000:
            self.log_text.delete("1.0", "1001.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _schedule_poll(self):
        if self.poll_after is not None:
            self.after_cancel(self.poll_after)
        self.poll_after = self.after(150, self.poll_task)

    def poll_task(self):
        if self.poll_after is not None:
            self.after_cancel(self.poll_after)
            self.poll_after = None
        if not self.task:
            return
        try:
            for update in self.task.read_updates():
                try:
                    self._handle_update(update)
                except (KeyError, TypeError, ValueError) as exc:
                    self.append_log(f"已忽略无效状态记录：{exc}")
            exit_code = self.task.poll()
        except Exception as exc:
            self.append_log(f"读取任务状态失败：{exc}")
            self._schedule_poll()
            return
        if exit_code is None:
            self._schedule_poll()
            return
        try:
            result = self.finished or self.task.result()
        except Exception as exc:
            self.append_log(f"读取任务结果失败：{exc}")
            result = {}
        if not result:
            result = {"status": "error", "message": f"任务进程已退出（代码 {exit_code}），未返回完成记录。\n日志：{self.task.log_path}"}
        self._finish_task(result)

    def _handle_update(self, update):
        if not isinstance(update, dict):
            raise ValueError("记录格式不正确")
        kind = update.get("type")
        if kind == "log":
            self.append_log(update.get("message", ""))
        elif kind in {"progress", "finished"}:
            total, done, failed = (max(0, int(update.get(key, 0))) for key in ("total", "done", "failed"))
            self.progress.configure(maximum=max(total, 1), value=min(done + failed, total))
            self._counts(total, done, failed)
            if kind == "finished":
                self.finished = update
            elif not self.closing and str(self.stop_button["state"]) != "disabled":
                self._status("正在处理商品…")
        elif kind == "prompt":
            prompt = str(update["message"])
            self.append_log(prompt)
            if "y/n" in prompt.lower():
                answer = messagebox.askyesno("登录确认", prompt, parent=self)
                self.task.respond(update["id"], "y" if answer else "n")
            else:
                self.pending_prompt = update["id"]
                self._status(prompt)
                self.continue_button.grid()

    def continue_login(self):
        if self.task and self.pending_prompt is not None:
            try:
                self.task.respond(self.pending_prompt, "")
            except Exception as exc:
                messagebox.showerror("无法继续登录", str(exc), parent=self)
                return
            self.pending_prompt = None
            self.continue_button.grid_remove()
            self._status("正在确认登录状态…")

    def stop_task(self):
        if not self.task:
            return
        try:
            self.task.request_stop()
        except Exception as exc:
            self.closing = False
            messagebox.showerror("停止请求未能发送", str(exc), parent=self)
            return
        self.stop_button.configure(state="disabled")
        self.pending_prompt = None
        self.continue_button.grid_remove()
        message = "正在停止：等待当前商品处理完成并保存，请勿强制关闭。"
        self._status(message)
        self.append_log(message)

    def _finish_task(self, result):
        self.task = None
        self.pending_prompt = None
        self.continue_button.grid_remove()
        self._set_busy(False)
        if result.get("output_file"):
            self.output_var.set(result["output_file"])
        status = result.get("status", "error")
        label = {"finished": "任务已完成", "stopped": "已停止并保存", "error": "任务失败"}.get(status, "任务结束")
        message = result.get("message") or label
        self._status(label, {"finished": "ok", "stopped": "ok", "error": "err"}.get(status, "idle"))
        self.append_log(message)
        if status == "error":
            messagebox.showerror("任务失败", message, parent=self)
        self._save_settings()
        if self.closing:
            self.destroy()

    def open_output(self):
        path = Path(self.output_var.get().strip())
        if not self.output_var.get().strip() or not path.is_file():
            messagebox.showinfo("输出文件尚未生成", "请等待保存完成后再打开输出文件。", parent=self)
            return
        try:
            os.startfile(str(path.resolve()))
        except OSError as exc:
            messagebox.showerror("无法打开输出文件", str(exc), parent=self)

    def close_window(self):
        if self.task:
            if self.closing:
                return
            if not messagebox.askyesno("停止后退出", "任务仍在运行。是否停止并保存，然后退出？\n选择“否”可继续运行。", parent=self):
                return
            self.closing = True
            self.stop_task()
            return
        self._save_settings()
        self.destroy()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.worker:
        from desktop_worker import run_worker
        return run_worker(args.worker)
    if args.self_test:
        if args.report is None:
            parser.error("--self-test requires --report")
        from desktop_worker import self_test
        return self_test(args.report)
    enable_dpi_awareness()
    DesktopApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
