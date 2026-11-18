"""Chinese desktop window with a separate worker process for generation and login."""
import argparse
import multiprocessing
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

from desktop_runtime import (
    APP_TITLE, default_settings, launch_job, load_settings, save_settings,
    resource_dir, validate_generation,
)


BG, CARD, BORDER = "#F0F3F8", "#FFFFFF", "#E3E7EE"
TEXT, MUTED = "#1F2937", "#586579"
SECONDARY, SECONDARY_HOVER = "#E2E9F3", "#D4E0EF"
ACCENT, ACCENT_HOVER, ACCENT_OFF = "#2563EB", "#1D4ED8", "#A9BEEF"
OK, ERR = "#059669", "#DC2626"
FONT = "Microsoft YaHei UI"


def log_tag(message):
    m = str(message)
    if any(k in m for k in ("失败", "错误", "Error", "Traceback", "Exception")):
        return "err"
    if any(k in m for k in ("✔", "已保存", "已完成", "已停止")):
        return "ok"
    if m.startswith("[20") or " INFO" in m:
        return "dim"
    return ""


class DesktopApp(ctk.CTk):
    PROVIDERS = {"DeepSeek": "deepseek", "Gemini": "gemini"}
    TEMPLATES = {"模板 1 · Key: Value 列表": "template1", "模板 2 · Details + Specifications": "template2"}

    def __init__(self):
        # Give the app its own Windows taskbar group instead of Python's.
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ProductDetailsDesktop.App")
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=BG)
        self.title(APP_TITLE)
        icon = str(resource_dir() / "assets" / "logo.ico")

        def set_icon():
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                pass

        set_icon()
        self.after(250, set_icon)  # CustomTkinter 会在启动后覆盖图标，延后再设一次
        width = min(980, self.winfo_screenwidth() - 80)
        height = min(840, self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}")
        self.minsize(min(860, width), min(720, height))
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
        self._build_window(compact=height < 720)
        self.input_var.trace_add("write", self._refresh_input_status)
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        if startup_error:
            self.append_log(startup_error)

    def _build_window(self, compact=False):
        gap = 2 if compact else 6
        h = 30 if compact else 34  # 控件高度
        font = lambda size, bold=False: ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=16 if compact else 28, pady=(10, 10) if compact else (20, 16))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(8, weight=1)

        def text(parent, value, size=13, bold=False, color=TEXT, **kw):
            return ctk.CTkLabel(parent, text=value, font=font(size, bold), text_color=color, **kw)

        def section(row, title):
            text(root, title, 14, True).grid(
                row=row, column=0, sticky="w", pady=(6, 3) if compact else (14, 5))

        def card(row, border_width=0):
            frame = ctk.CTkFrame(root, fg_color=CARD, corner_radius=12, border_width=border_width, border_color=BORDER)
            frame.grid(row=row, column=0, sticky="nsew" if row == 8 else "ew")
            return frame

        def entry(parent, **kw):
            return ctk.CTkEntry(parent, height=h, corner_radius=8, fg_color=CARD, border_color=BORDER,
                                text_color=TEXT, font=font(13), **kw)

        def combo(parent, **kw):
            return ctk.CTkComboBox(parent, height=h, corner_radius=8, state="readonly", fg_color=CARD,
                                   border_color=BORDER, button_color=BORDER, button_hover_color="#D5DAE3",
                                   text_color=TEXT, dropdown_fg_color=CARD, dropdown_text_color=TEXT,
                                   dropdown_hover_color="#EEF2F9", font=font(13), dropdown_font=font(13), **kw)

        def check(parent, label, variable, command=None):
            return ctk.CTkCheckBox(parent, text=label, variable=variable, command=command, font=font(13),
                                   text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                   border_color="#B8C0CE", corner_radius=5, checkbox_width=20, checkbox_height=20)

        def button(parent, label, command, primary=False, border_width=0):
            if primary:
                return ctk.CTkButton(parent, text=label, command=command, height=h + 2, corner_radius=8, width=110,
                                     fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF",
                                     text_color_disabled="#FFFFFF", font=font(13, True))
            return ctk.CTkButton(parent, text=label, command=command, height=h, corner_radius=8, width=88,
                                 fg_color=SECONDARY, hover_color=SECONDARY_HOVER,
                                 text_color=TEXT, text_color_disabled="#788699",
                                 border_width=border_width, border_color="#D5DAE3", font=font(13))

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="w")
        try:
            size = 36 if compact else 48
            self.logo = ctk.CTkImage(Image.open(resource_dir() / "assets" / "logo.png"), size=(size, size))
            ctk.CTkLabel(header, image=self.logo, text="").grid(row=0, column=0, rowspan=2, padx=(0, 14))
        except Exception:
            pass
        text(header, "产品详情生成", 20 if compact else 24, True).grid(row=0, column=1, sticky="w")
        if not compact:  # 小屏省掉副标题，把空间留给日志
            text(header, "导入商品链接，生成中英文标题与详情。首次使用请先登录商品平台。", 13, color=MUTED).grid(row=1, column=1, sticky="w")

        section(1, "Excel 文件")
        files = card(2)
        files.grid_columnconfigure(1, weight=1)
        files.grid_columnconfigure(0, minsize=84)
        for row, label, variable, callback in [(0, "输入文件", self.input_var, self.choose_input), (1, "输出文件", self.output_var, self.choose_output)]:
            text(files, label, color=MUTED).grid(row=row, column=0, sticky="w", padx=(18, 0), pady=gap)
            field = entry(files, textvariable=variable)
            field.grid(row=row, column=1, sticky="ew", pady=gap)
            choose = button(files, "选择", callback)
            choose.grid(row=row, column=2, padx=(12, 18), pady=gap)
            self.editable_widgets.extend([(field, "normal"), (choose, "normal")])

        section(3, "生成设置")
        settings = card(4)
        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)
        settings.grid_columnconfigure(0, minsize=84)
        text(settings, "AI 服务", color=MUTED).grid(row=0, column=0, sticky="w", padx=(18, 0), pady=gap)
        provider = combo(settings, values=list(self.PROVIDERS), variable=self.provider_var, command=lambda _v: self.change_provider())
        provider.grid(row=0, column=1, sticky="ew", pady=gap)
        text(settings, "模型", color=MUTED).grid(row=0, column=2, padx=(16, 8))
        model = entry(settings, textvariable=self.model_var)
        model.grid(row=0, column=3, sticky="ew", padx=(0, 18), pady=gap)
        text(settings, "API Key", color=MUTED).grid(row=1, column=0, sticky="w", padx=(18, 0), pady=gap)
        key_row = ctk.CTkFrame(settings, fg_color="transparent")
        key_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=gap)
        key_row.grid_columnconfigure(0, weight=1)
        key = entry(key_row, textvariable=self.key_var, show="●")
        key.grid(row=0, column=0, sticky="ew")
        self.balance_button = button(key_row, "查询余额", self.query_balance)
        if self.current_provider == "deepseek":
            self.balance_button.grid(row=0, column=1, padx=(12, 0))
        remember = check(settings, "在本机记住 API Key", self.remember_var, self.remember_changed)
        remember.grid(row=1, column=3, sticky="w", padx=(16, 18), pady=gap)
        text(settings, "详情模板", color=MUTED).grid(row=2, column=0, sticky="w", padx=(18, 0), pady=gap)
        template = combo(settings, values=list(self.TEMPLATES), variable=self.template_var)
        template.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 18), pady=gap)
        flags = ctk.CTkFrame(settings, fg_color="transparent")
        flags.grid(row=3, column=1, columnspan=3, sticky="w", pady=(2, 8 if compact else 14))
        resume = check(flags, "断点续跑（跳过已完成商品）", self.resume_var)
        resume.pack(side="left", padx=(0, 28))
        browser = check(flags, "显示浏览器（便于手动验证）", self.browser_var)
        browser.pack(side="left")
        self.editable_widgets.extend([(provider, "readonly"), (model, "normal"), (key, "normal"), (remember, "normal"), (template, "readonly"), (resume, "normal"), (browser, "normal")])

        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.grid(row=6, column=0, sticky="ew", pady=(8 if compact else 14, 0))
        text(actions, "平台登录", color=MUTED).pack(side="left", padx=(0, 10))
        for label, site in [("阿里巴巴", "alibaba"), ("淘宝 / 天猫", "taobao")]:
            login = button(actions, label, lambda selected=site: self.start_login(selected))
            if site == "taobao":
                login.configure(width=112)
            login.pack(side="left", padx=(0, 8))
            self.editable_widgets.append((login, "normal"))
        self.start_button = button(actions, "开始生成", self.start_generation, primary=True)
        self.start_button.pack(side="right")
        self.stop_button = button(actions, "停止并保存", self.stop_task)
        self.stop_button.configure(state="disabled")
        self.stop_button.pack(side="right", padx=10)

        section(7, "运行状态")
        logs = card(8, border_width=0)
        logs.grid_columnconfigure(0, weight=1)
        logs.grid_rowconfigure(3, weight=1)
        top = ctk.CTkFrame(logs, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(8 if compact else 12, 0))
        top.grid_columnconfigure(0, weight=1)
        self.status_label = text(top, "", 13, True, MUTED, anchor="w", justify="left", wraplength=560)
        self.status_label.grid(row=0, column=0, sticky="w")
        top.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(200, e.width - 260)))
        self.counts_label = text(top, "", color=MUTED)
        self.counts_label.grid(row=0, column=1, sticky="e")
        self.progress = ctk.CTkProgressBar(logs, height=8, corner_radius=4, fg_color="#E5E9F2", progress_color=ACCENT)
        self._progress(0, 1)
        self.progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 0))
        self.continue_button = button(logs, "我已完成登录，继续", self.continue_login)
        self.continue_button.configure(width=160)
        self.continue_button.grid(row=2, column=0, sticky="w", padx=18, pady=(10, 0))
        self.continue_button.grid_remove()
        self.log_text = ctk.CTkTextbox(logs, height=110 if compact else 170, wrap="word", fg_color="#F5F7FB", text_color=TEXT,
                                       border_width=0, corner_radius=8,
                                       font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        for tag, color in (("err", "#DC2626"), ("ok", "#059669"), ("dim", MUTED)):
            self.log_text.tag_config(tag, foreground=color)
        self.log_text.grid(row=3, column=0, sticky="nsew", padx=18, pady=(8, 12 if compact else 18))

        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=9, column=0, sticky="ew", pady=(6 if compact else 14, 0))
        text(footer, "停止后会等待当前商品处理完成并保存。", color=MUTED).pack(side="left")
        button(footer, "打开输出文件", self.open_output).pack(side="right")
        self._refresh_input_status()
        self._counts(0, 0, 0)

    def _status(self, text, tone="run"):
        self.status_label.configure(text=f"● {text}", text_color={"idle": MUTED, "run": ACCENT, "ok": OK, "err": ERR}[tone])

    def _refresh_input_status(self, *_args):
        if self.task:
            return
        filename = self.input_var.get().strip()
        if not filename:
            self._status("请选择 Excel 文件", "idle")
        elif not Path(filename).is_file():
            self._status("输入文件不存在，请重新选择", "err")
        else:
            self._status(f"已选择 {Path(filename).name}，点击开始生成", "idle")

    def _counts(self, total, done, failed):
        self.counts_label.configure(text=f"待处理 {total}  ·  成功 {done}  ·  失败 {failed}")

    def _progress(self, value, maximum):
        fraction = min(1, max(0, value / max(maximum, 1)))
        self.progress.set(fraction)
        self.progress.configure(progress_color=ACCENT if fraction > 0 else self.progress.cget("fg_color"))

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
        if self.current_provider == "deepseek":
            self.balance_button.grid(row=0, column=1, padx=(12, 0))
        else:
            self.balance_button.grid_forget()

    def query_balance(self):
        if self.current_provider != "deepseek" or self.balance_button.cget("state") == "disabled":
            return
        key = self.key_var.get().strip()
        if not key:
            self.append_log("DeepSeek 余额查询失败：请先填写 API Key")
            return
        result = SimpleQueue()

        def fetch():
            try:
                from llm_client import get_deepseek_balance
                message = f"DeepSeek 余额：{get_deepseek_balance(key)}"
            except RuntimeError as exc:
                message = f"DeepSeek 余额查询失败：{exc}"
            except Exception:
                message = "DeepSeek 余额查询失败：查询服务暂时无法使用"
            result.put(message)

        def show_result():
            try:
                message = result.get_nowait()
            except Empty:
                self.after(100, show_result)
                return
            self.balance_button.configure(state="normal", text="查询余额")
            self.append_log(message)

        self.balance_button.configure(state="disabled", text="查询中…")
        Thread(target=fetch, daemon=True).start()
        self.after(100, show_result)

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
        self._progress(0, 1)
        self._counts(0, 0, 0)
        self._status("正在启动…")
        self.append_log("任务已启动。")
        self._set_busy(True)
        self._schedule_poll()

    def _set_busy(self, busy):
        for widget, idle_state in self.editable_widgets:
            widget.configure(state="disabled" if busy else idle_state)
        self.start_button.configure(state="disabled" if busy else "normal", fg_color=ACCENT_OFF if busy else ACCENT)
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
            self._progress(min(done + failed, total), total)
            self._counts(total, done, failed)
            if kind == "finished":
                self.finished = update
            elif not self.closing and self.stop_button.cget("state") != "disabled":
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
    DesktopApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
