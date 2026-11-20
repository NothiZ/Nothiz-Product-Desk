"""Chinese desktop window for product search, image collection, and details generation."""
import argparse
import json
import multiprocessing
import os
import time
import webbrowser
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from desktop_runtime import (
    APP_TITLE, IMAGE_MODES, configure_user_data_dir, default_image_settings, default_search_settings, default_settings, launch_job, load_settings,
    needs_user_data_setup, resource_dir, save_settings, user_data_dir, validate_candidate_search, validate_generation,
    validate_image_settings,
)


BG, CARD, BORDER = "#F0F3F8", "#FFFFFF", "#E3E7EE"
TEXT, MUTED = "#1F2937", "#586579"
SECONDARY, SECONDARY_HOVER = "#E2E9F3", "#D4E0EF"
ACCENT, ACCENT_HOVER, ACCENT_OFF = "#2563EB", "#1D4ED8", "#A9BEEF"
OK, ERR = "#059669", "#DC2626"
FONT = "Microsoft YaHei UI"
PROGRESS_TRACK = "#E5E9F2"
INPUT_BORDER, TRACK, TRACK_HOVER, PANEL = "#D6DCE7", "#E4E9F2", "#D6DDEA", "#F6F8FB"


class _Controls:
    def __init__(self, compact):
        self.height = 30 if compact else 34

    @staticmethod
    def font(size, bold=False):
        return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")

    def text(self, parent, value, size=13, bold=False, color=TEXT, **options):
        return ctk.CTkLabel(parent, text=value, font=self.font(size, bold), text_color=color, **options)

    @staticmethod
    def card(parent):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=0, border_color=BORDER)

    def entry(self, parent, **options):
        return ctk.CTkEntry(parent, height=self.height, corner_radius=10, border_width=1, fg_color=CARD, border_color=INPUT_BORDER,
                            text_color=TEXT, font=self.font(13), **options)

    def combo(self, parent, **options):
        return ctk.CTkComboBox(parent, height=self.height, corner_radius=10, border_width=1, state="readonly", fg_color=CARD,
                               border_color=INPUT_BORDER, button_color=INPUT_BORDER, button_hover_color="#C5CDDB",
                               text_color=TEXT, dropdown_fg_color=CARD, dropdown_text_color=TEXT,
                               dropdown_hover_color="#EEF2F9", font=self.font(13), dropdown_font=self.font(13), **options)

    def check(self, parent, label, variable, command=None):
        return ctk.CTkCheckBox(parent, text=label, variable=variable, command=command, font=self.font(13),
                               text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                               border_color="#B8C0CE", corner_radius=5, checkbox_width=20, checkbox_height=20)

    def segmented(self, parent, values, **options):
        return ctk.CTkSegmentedButton(parent, values=values, height=self.height, corner_radius=10, border_width=4,
                                      font=self.font(13), fg_color=TRACK, selected_color=CARD, selected_hover_color=CARD,
                                      unselected_color=TRACK, unselected_hover_color=TRACK_HOVER,
                                      text_color=(TEXT, TEXT), **options)

    def button(self, parent, label, command, *, primary=False, width=None):
        if primary:
            return ctk.CTkButton(parent, text=label, command=command, height=self.height + 2, corner_radius=8,
                                 width=width or 110, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF",
                                 text_color_disabled="#FFFFFF", font=self.font(13, True))
        return ctk.CTkButton(parent, text=label, command=command, height=self.height, corner_radius=8, width=width or 88,
                             fg_color=SECONDARY, hover_color=SECONDARY_HOVER, text_color=TEXT,
                             text_color_disabled="#788699", border_width=0, border_color="#D5DAE3", font=self.font(13))

    @staticmethod
    def progress(parent):
        bar = ctk.CTkProgressBar(parent, height=8, corner_radius=4, fg_color=PROGRESS_TRACK,
                                 progress_color=PROGRESS_TRACK)
        bar.set(0)
        return bar

    @staticmethod
    def log(parent, height):
        box = ctk.CTkTextbox(parent, height=height, wrap="word", fg_color="#F5F7FB", text_color=TEXT,
                             border_width=0, corner_radius=8, font=ctk.CTkFont(family="Consolas", size=12),
                             state="disabled")
        for tag, color in (("err", ERR), ("ok", OK), ("dim", MUTED)):
            box.tag_config(tag, foreground=color)
        return box


def _focusable(widget, ring):
    """Let a CustomTkinter button take keyboard focus and draw the focus ring inside itself."""
    border_color = widget.cget("border_color")
    tk.Frame.configure(widget, takefocus=widget.register(lambda _path: int(widget.cget("state") == "normal")))
    tk.Frame.bind(widget, "<FocusIn>", lambda _event: widget.configure(border_width=2, border_color=ring))
    tk.Frame.bind(widget, "<FocusOut>", lambda _event: widget.configure(border_width=0, border_color=border_color))
    tk.Frame.bind(widget, "<Return>", lambda _event: widget.invoke())
    tk.Frame.bind(widget, "<space>", lambda _event: widget.invoke())


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
    SEARCH_IMPORT_HINT = "选择导出的 CSV，导入后会打开「图片采集」并填好输入文件。"

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
        self.search_task = False
        self.image_task = False
        self.task_kind = None
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
        search_settings = default_search_settings()
        saved_search = self.settings.get("candidate_search")
        if isinstance(saved_search, dict):
            search_settings.update({key: saved_search[key] for key in search_settings if key in saved_search})
        self.search_vars = {key: tk.StringVar(self, value=value) for key, value in search_settings.items()
                            if key not in ("show_browser", "image_mode")}
        self.search_browser_var = tk.BooleanVar(self, value=bool(search_settings["show_browser"]))
        self.image_vars = {key: (tk.BooleanVar if isinstance(value, bool) else tk.StringVar)(self, value=value)
                           for key, value in default_image_settings().items()}
        self.image_rows = []
        self.image_min_edited = False
        self.image_vars["min_clear"].trace_add("write", lambda *_: setattr(self, "image_min_edited", True))
        saved_wall = self.settings.get("last_candidate_wall")
        self.wall_path = Path(saved_wall) if isinstance(saved_wall, str) and saved_wall else None
        self._build_window(compact=height < 790)
        if self.wall_path and (task_workbook := self.wall_path.with_name("选品结果.xlsx")).is_file():
            self._select_image_workbook(task_workbook)
        self.input_var.trace_add("write", self._refresh_input_status)
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        if startup_error:
            self.append_log(startup_error)

    def _build_window(self, compact=False):
        controls = _Controls(compact)
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        header.pack(fill="x")
        logo_size = 20 if compact else 24
        title_padding = (22, 0)
        try:
            with Image.open(resource_dir() / "assets" / "logo.png") as image:
                self.logo = ImageTk.PhotoImage(image.resize((logo_size, logo_size), Image.Resampling.LANCZOS), master=header)
            tk.Label(header, image=self.logo, bg=CARD, borderwidth=0).pack(
                side="left", padx=(22, 10), pady=6 if compact else 11)
            title_padding = (0, 0)
        except OSError:
            pass
        controls.text(header, APP_TITLE, 15, True).pack(side="left", padx=title_padding)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        nav = ctk.CTkFrame(self, fg_color="transparent")
        inset = 16 if compact else 28
        nav.pack(fill="x", padx=inset, pady=(8, 0) if compact else (14, 0))
        track = ctk.CTkFrame(nav, fg_color=TRACK, corner_radius=10)
        track.pack(side="left")
        self.page_intro = controls.text(self, "", 13, color=MUTED, anchor="w", justify="left",
                                        height=20 if compact else 26)
        self.page_intro.pack(fill="x", padx=inset, pady=(4, 0) if compact else (10, 0))
        self.page_intro.bind("<Configure>", lambda event: self.page_intro.configure(wraplength=event.width))
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        tabs = {}
        descriptions = {
            "选品搜索": "按分类名和关键词搜索候选商品，在浏览器里勾选后导入，再进入图片采集。首次使用请先登录阿里巴巴。",
            "图片采集": "按选品结果表下载商品图片并检查清晰度，清晰图不足的商品会被淘汰，不再生成详情。",
            "详情生成": "读取商品链接表，生成中英文标题与详情；从图片采集进入时，只处理已完成的商品。",
        }

        def select_tab(name):
            self.active_tab = name
            self.page_intro.configure(text=descriptions[name])
            for title, (button, panel) in tabs.items():
                active = title == name
                button.configure(fg_color=CARD if active else TRACK, hover_color=CARD if active else TRACK_HOVER,
                                 font=controls.font(13, active))
                if active:
                    panel.grid(row=0, column=0, sticky="nsew")
                else:
                    panel.grid_remove()

        pages = (("选品搜索", self._build_search), ("图片采集", self._build_images), ("详情生成", self._build_details))
        for index, (name, build) in enumerate(pages):
            button = ctk.CTkButton(track, text=name, width=104, height=controls.height - 2, corner_radius=8,
                                   border_width=0, text_color=TEXT, command=lambda title=name: select_tab(title))
            button.pack(side="left", padx=(4, 4 if index == len(pages) - 1 else 0), pady=4)
            _focusable(button, ACCENT)
            panel = ctk.CTkFrame(content, fg_color="transparent")
            tabs[name] = (button, panel)
            build(panel, compact, controls)

        self._select_tab = select_tab
        select_tab("选品搜索")

    def _build_images(self, panel, compact, controls):
        controls = _Controls(compact=True)
        root = ctk.CTkFrame(panel, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=16 if compact else 28, pady=(2, 6) if compact else (6, 10))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(6, weight=1)
        gap = 5
        field_gap = 4 if compact else 5
        card_padding = 10 if compact else 12

        def section(row, title, *, grow=False, top=8):
            controls.text(root, title, 14, True, height=20 if compact else 28).grid(
                row=row, column=0, sticky="w", pady=(min(top, 6) if compact else top, 3))
            card = controls.card(root)
            card.grid(row=row + 1, column=0, sticky="nsew" if grow else "ew")
            card.grid_columnconfigure(0, minsize=84)
            card.grid_columnconfigure(1, weight=1)
            card.grid_rowconfigure(0, minsize=gap + 3)
            return card

        files = section(0, "选品结果表", top=2)
        files.grid_rowconfigure(0, minsize=card_padding)
        for row, label, key, action in ((1, "输入文件", "input_file", self.choose_image_workbook),
                                         (2, "保存到", "output_dir", self.choose_image_directory)):
            controls.text(files, label, color=MUTED).grid(row=row, column=0, sticky="w", padx=(18, 0))
            controls.entry(files, textvariable=self.image_vars[key], state="readonly").grid(
                row=row, column=1, sticky="ew", pady=field_gap)
            choose = controls.button(files, "选择", action, width=72)
            choose.grid(row=row, column=2, padx=(12, 18))
            self.editable_widgets.append((choose, "normal"))
        self.image_input_summary = controls.text(files, "请选择选品结果表。", 12, color=MUTED, anchor="w", justify="left")
        self.image_input_summary.grid(row=3, column=0, columnspan=3, sticky="ew", padx=18, pady=(2, card_padding))
        files.bind("<Configure>", lambda event: self.image_input_summary.configure(wraplength=max(200, event.width - 40)))

        settings = section(2, "采集设置")
        settings.grid_rowconfigure(0, minsize=card_padding)
        controls.text(settings, "图片模式", color=MUTED).grid(row=1, column=0, sticky="w", padx=(18, 0))
        modes = ctk.CTkFrame(settings, fg_color="transparent")
        modes.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 18), pady=field_gap)
        selector = controls.segmented(modes, list(IMAGE_MODES), variable=self.image_vars["mode"],
                                      command=self._change_image_mode)
        selector.pack(side="left")
        self.image_mode_hint = controls.text(modes, "", 12, color=MUTED)
        self.image_mode_hint.pack(side="left", padx=12)
        self.editable_widgets.append((selector, "normal"))
        self._change_image_mode(self.image_vars["mode"].get())

        controls.text(settings, "达标条件", color=MUTED).grid(row=2, column=0, sticky="w", padx=(18, 0))
        criteria = ctk.CTkFrame(settings, fg_color="transparent")
        criteria.grid(row=2, column=1, columnspan=2, sticky="w", pady=field_gap)
        controls.text(criteria, "至少").pack(side="left", padx=(0, 6))
        for key, label in (("min_clear", "张清晰图，最短边不小于"), ("min_side", "像素")):
            entry = controls.entry(criteria, width=68, textvariable=self.image_vars[key], justify="center")
            entry.pack(side="left")
            controls.text(criteria, label).pack(side="left", padx=6)
            self.editable_widgets.append((entry, "normal"))

        toggles = ctk.CTkFrame(settings, fg_color="transparent")
        toggles.grid(row=3, column=1, columnspan=2, sticky="w", pady=(field_gap, card_padding))
        for key, label in (("resume", "断点续跑（跳过已采集商品）"), ("show_browser", "显示浏览器（便于手动验证）"),
                           ("save_snapshot", "保存页面快照")):
            checkbox = controls.check(toggles, label, self.image_vars[key])
            checkbox.pack(side="left", padx=(0, 20))
            self.editable_widgets.append((checkbox, "normal"))

        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", pady=(4 if compact else 8, 0))
        controls.text(actions, "平台登录", color=MUTED).pack(side="left", padx=(2, 10))
        for site, title, width in (("alibaba", "阿里巴巴", 90), ("taobao", "淘宝 / 天猫", 112)):
            login = controls.button(actions, title, lambda site=site: self.start_login(site, image_panel=True), width=width)
            login.pack(side="left", padx=(0, 8))
            self.editable_widgets.append((login, "normal"))
        self.image_start_button = controls.button(actions, "开始采集", self.start_images, primary=True)
        self.image_start_button.pack(side="right")
        self.image_stop_button = controls.button(actions, "停止并保存", self.stop_task)
        self.image_stop_button.pack(side="right", padx=10)

        run = section(5, "运行状态", grow=True)
        if compact:
            run.grid_rowconfigure(0, minsize=2)
        run.grid_rowconfigure(4, weight=1)
        self.image_status_label = controls.text(run, "等待采集", bold=True, color=MUTED, anchor="w", justify="left")
        self.image_status_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18)
        self.image_counts_label = controls.text(run, "", 12, color=MUTED)
        self.image_counts_label.grid(row=1, column=2, sticky="e", padx=(0, 18))
        run.bind("<Configure>", lambda event: self.image_status_label.configure(wraplength=max(170, event.width - 330)))
        self.image_progress = controls.progress(run)
        self.image_progress.grid(row=2, column=0, columnspan=3, sticky="ew", padx=18, pady=(4, gap))
        self.image_continue_button = controls.button(run, "继续采集", self.continue_login, width=100)
        self.image_continue_button.grid(row=3, column=0, sticky="w", padx=18, pady=(0, gap))
        self.image_continue_button.grid_remove()
        table = ctk.CTkFrame(run, fg_color=PANEL, corner_radius=10)
        table.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=18, pady=(0, 4) if compact else (2, 14))
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.layout("Photos.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Photos.Treeview.Heading", [
            ("Treeheading.padding", {"sticky": "nswe", "children": [("Treeheading.text", {"sticky": "w"})]}),
        ])
        style.configure("Photos.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        font=(FONT, 10), rowheight=24 if compact else 28, borderwidth=0, relief="flat")
        style.configure("Photos.Treeview.Heading", foreground=MUTED, font=(FONT, 10), padding=(4, 4))
        style.map("Photos.Treeview", background=[("selected", SECONDARY)], foreground=[("selected", TEXT)])
        self.image_table = ttk.Treeview(table, columns=("code", "state", "clear", "note"),
                                       show="headings", height=4, style="Photos.Treeview")
        for key, title, width in (("code", "编号", 98), ("state", "状态", 112), ("clear", "清晰图", 80), ("note", "说明", 340)):
            self.image_table.heading(key, text=title, anchor="w")
            self.image_table.column(key, width=width, minwidth=width if key != "note" else 120, stretch=key == "note")
        for tag, color in (("ok", "#087F5B"), ("warning", "#8A5915"), ("error", "#B42318"), ("working", ACCENT), ("pending", MUTED)):
            self.image_table.tag_configure(tag, foreground=color)
        self.image_table.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=3 if compact else 6)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.image_table.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=3 if compact else 6)
        self.image_table.configure(yscrollcommand=scroll.set)

        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=7, column=0, sticky="ew", pady=(4 if compact else 8, 0))
        footer.grid_columnconfigure(0, weight=1)
        hint = controls.text(footer, "停止后会保存当前商品。详情生成只处理「已完成」的商品。",
                             12, color=MUTED, anchor="w", justify="left")
        hint.grid(row=0, column=0, sticky="w", padx=(2, 8))
        footer.bind("<Configure>", lambda event: hint.configure(wraplength=max(200, event.width - 280)))
        self.image_open_button = controls.button(footer, "打开图片文件夹", self.open_image_folder, width=126)
        self.image_open_button.grid(row=0, column=1, padx=(0, 8))
        self.image_details_button = controls.button(footer, "去生成详情", self.open_image_details, primary=True)
        self.image_details_button.grid(row=0, column=2)
        self.image_stop_button.configure(state="disabled")
        self._refresh_image_actions()

    def _build_details(self, panel, compact, controls):
        gap = 2 if compact else 6
        first_row_padding = (6 if compact else 14, gap)
        text, entry, combo = controls.text, controls.entry, controls.combo
        button, check = controls.button, controls.check
        root = ctk.CTkFrame(panel, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=16 if compact else 28, pady=(2, 8) if compact else (6, 16))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(8, weight=1)

        def section(row, title, *, padding=None):
            text(root, title, 14, True, height=24 if compact else 28).grid(
                row=row, column=0, sticky="w", pady=padding or ((6, 3) if compact else (14, 5)))

        def card(row):
            frame = controls.card(root)
            frame.grid(row=row, column=0, sticky="nsew" if row == 8 else "ew")
            return frame

        section(1, "Excel 文件", padding=(2, 3))
        files = card(2)
        files.grid_columnconfigure(1, weight=1)
        files.grid_columnconfigure(0, minsize=84)
        for row, label, variable, callback in [(0, "输入文件", self.input_var, self.choose_input), (1, "输出文件", self.output_var, self.choose_output)]:
            padding = first_row_padding if row == 0 else gap
            text(files, label, color=MUTED).grid(row=row, column=0, sticky="w", padx=(18, 0), pady=padding)
            field = entry(files, textvariable=variable)
            field.grid(row=row, column=1, sticky="ew", pady=padding)
            choose = button(files, "选择", callback)
            choose.grid(row=row, column=2, padx=(12, 18), pady=padding)
            self.editable_widgets.extend([(field, "normal"), (choose, "normal")])

        section(3, "生成设置")
        settings = card(4)
        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)
        settings.grid_columnconfigure(0, minsize=84)
        text(settings, "AI 服务", color=MUTED).grid(row=0, column=0, sticky="w", padx=(18, 0), pady=first_row_padding)
        provider = combo(settings, values=list(self.PROVIDERS), variable=self.provider_var, command=lambda _v: self.change_provider())
        provider.grid(row=0, column=1, sticky="ew", pady=first_row_padding)
        text(settings, "模型", color=MUTED).grid(row=0, column=2, padx=(16, 8), pady=first_row_padding)
        model = entry(settings, textvariable=self.model_var)
        model.grid(row=0, column=3, sticky="ew", padx=(0, 18), pady=first_row_padding)
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
        logs = card(8)
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
        self.progress = controls.progress(logs)
        self.progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 0))
        self.continue_button = button(logs, "我已完成登录，继续", self.continue_login)
        self.continue_button.configure(width=160)
        self.continue_button.grid(row=2, column=0, sticky="w", padx=18, pady=(10, 0))
        self.continue_button.grid_remove()
        self.log_text = controls.log(logs, 110 if compact else 170)
        self.log_text.grid(row=3, column=0, sticky="nsew", padx=18, pady=(4, 6) if compact else (8, 18))

        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=9, column=0, sticky="ew", pady=(6 if compact else 14, 0))
        text(footer, "停止后会等待当前商品处理完成并保存。", color=MUTED).pack(side="left")
        button(footer, "打开输出文件", self.open_output).pack(side="right")
        self._refresh_input_status()
        self._counts(0, 0, 0)

    def _build_search(self, panel, compact, controls):
        root = ctk.CTkFrame(panel, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=16 if compact else 28, pady=(2, 8) if compact else (8, 14))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)
        gap = 1 if compact else 5
        text = controls.text

        def section(row, title):
            text(root, title, 14, True, height=22 if compact else 28).grid(
                row=row, column=0, sticky="w", pady=(2, 3) if row == 0 else ((6, 3) if compact else (10, 4)))
            card = controls.card(root)
            card.grid(row=row + 1, column=0, sticky="nsew" if row == 3 else "ew")
            return card

        def button(parent, label, command, width, *, primary=False):
            widget = controls.button(parent, label, command, width=width, primary=primary)
            _focusable(widget, "#FFFFFF" if primary else ACCENT)
            return widget

        settings = section(0, "搜索设置")
        settings.grid_columnconfigure(0, minsize=100)
        settings.grid_columnconfigure(1, weight=1, uniform="search-fields")
        settings.grid_columnconfigure(3, weight=1, uniform="search-fields")
        settings.grid_rowconfigure(0, minsize=gap + 4)

        def field(row, column, title, key, *, span=1):
            text(settings, title, color=MUTED).grid(row=row, column=column, sticky="w",
                                                    padx=(18, 0) if column == 0 else (16, 8), pady=gap)
            widget = controls.entry(settings, textvariable=self.search_vars[key])
            self.editable_widgets.append((widget, "normal"))
            widget.grid(row=row, column=column + 1, columnspan=span, sticky="ew",
                        padx=(0, 18) if column + span == 3 else 0, pady=gap)

        field(1, 0, "分类名", "category")
        field(1, 2, "编号前缀", "prefix")
        field(2, 0, "搜索关键词", "search", span=3)
        text(settings, "平台", color=MUTED).grid(row=3, column=0, sticky="w", padx=(18, 0))
        platforms = ["阿里巴巴国际站", "淘宝 / 天猫（暂未支持）", "亚马逊（暂未支持）"]
        self.search_platform = controls.combo(settings, values=platforms)
        self.search_platform.set(platforms[0])
        # CTkComboBox has no public option for disabling individual entries in its Tk menu.
        self.search_platform._dropdown_menu.entryconfigure(1, state="disabled")
        self.search_platform._dropdown_menu.entryconfigure(2, state="disabled")
        self.search_platform.grid(row=3, column=1, sticky="ew", pady=gap)
        self.editable_widgets.append((self.search_platform, "readonly"))
        limits = ctk.CTkFrame(settings, fg_color="transparent")
        limits.grid(row=3, column=2, columnspan=2, sticky="e", padx=(16, 18), pady=gap)
        for label, key in (("搜索页数", "pages"), ("候选上限", "limit")):
            text(limits, label, color=MUTED).pack(side="left", padx=(12, 8))
            value = controls.entry(limits, width=62, textvariable=self.search_vars[key], justify="center")
            value.pack(side="left")
            self.editable_widgets.append((value, "normal"))
        field(4, 0, "标题必含词", "required")
        field(4, 2, "标题排除词", "excluded")
        show_browser = controls.check(settings, "显示浏览器（便于手动验证）", self.search_browser_var)
        show_browser.grid(row=5, column=0, columnspan=2, sticky="w", padx=(18, 0), pady=(gap, gap + 8))
        self.editable_widgets.append((show_browser, "normal"))
        text(settings, "多个关键词用逗号分隔 · 分类名可选 · 编号前缀留空时自动生成", 12, color=MUTED).grid(
            row=5, column=2, columnspan=2, sticky="e", padx=(0, 18), pady=(gap, gap + 8))

        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", pady=(8 if compact else 12, 0))
        text(actions, "平台登录", color=MUTED).pack(side="left", padx=(2, 10))
        login = button(actions, "阿里巴巴登录", lambda: self.start_login("alibaba", search_panel=True), 116)
        login.pack(side="left")
        self.editable_widgets.append((login, "normal"))
        self.search_start_button = button(actions, "开始搜索", self.start_search, 100, primary=True)
        self.search_start_button.pack(side="right")
        self.search_stop_button = button(actions, "停止并保存", self.stop_task, 100)
        self.search_stop_button.configure(state="disabled")
        self.search_stop_button.pack(side="right", padx=10)

        run = section(3, "运行状态")
        run.grid_columnconfigure(0, weight=1)
        run.grid_rowconfigure(3, weight=1)
        top = ctk.CTkFrame(run, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(8 if compact else 12, 0))
        top.grid_columnconfigure(0, weight=1)
        self.search_status_label = text(top, "等待搜索", 13, True, MUTED, anchor="w", justify="left", wraplength=560)
        self.search_status_label.grid(row=0, column=0, sticky="w")
        top.bind("<Configure>", lambda event: self.search_status_label.configure(wraplength=max(200, event.width - 260)))
        self.search_counts_label = text(top, "", color=MUTED)
        self.search_counts_label.grid(row=0, column=1, sticky="e")
        self.search_progress = controls.progress(run)
        self.search_progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 0))
        self.search_continue_button = button(run, "我已完成登录，继续", self.continue_login, 156)
        self.search_continue_button.grid(row=2, column=0, sticky="w", padx=18, pady=(10, 0))
        self.search_continue_button.grid_remove()
        self.search_log_text = controls.log(run, 60 if compact else 110)
        self.search_log_text.grid(row=3, column=0, sticky="nsew", padx=18, pady=(4, 7) if compact else (8, 14))

        results = section(5, "选品结果")
        results.grid_columnconfigure(1, weight=1)
        result_padding = 8 if compact else 14
        self.open_wall_button = button(results, "打开候选图片墙", self.open_candidate_wall, 152)
        self.open_wall_button.grid(row=0, column=0, sticky="w", padx=(18, 12), pady=(result_padding, gap))
        text(results, "在浏览器里勾选需要的商品，勾完点「导出选中」。",
             12, color=MUTED, anchor="w", justify="left", wraplength=520).grid(
                 row=0, column=1, sticky="w", pady=(result_padding, gap))
        self.import_candidates_button = button(results, "导入选中结果", self.import_candidates, 152, primary=True)
        self.import_candidates_button.grid(row=1, column=0, sticky="w", padx=(18, 12), pady=(gap, result_padding))
        self.search_import_label = text(results, self.SEARCH_IMPORT_HINT, 12, color=MUTED, anchor="w",
                                        justify="left", wraplength=520)
        self.search_import_label.grid(row=1, column=1, sticky="ew", padx=(0, 18), pady=(gap, result_padding))

        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=7, column=0, sticky="ew", pady=(4 if compact else 10, 0))
        text(footer, "停止后会等待当前页面处理完成并保存。", 12, color=MUTED).pack(side="left", padx=(2, 8))
        self.go_image_button = button(footer, "去采集图片", self.go_to_image_collection, 138)
        self.go_image_button.pack(side="right")
        self.editable_widgets.append((self.go_image_button, "normal"))
        self._refresh_candidate_actions()
        self._restore_wall_status()

    def _restore_wall_status(self):
        if not self.wall_path or not self.wall_path.is_file():
            return
        self.search_status_label.configure(text="候选已就绪", text_color=OK)
        try:
            manifest = self.wall_path.with_name("candidate_manifest.json")
            items = json.loads(manifest.read_text(encoding="utf-8"))["items"]
            downloaded = sum(bool(item.get("image")) for item in items)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.search_counts_label.configure(text="可打开上次生成的候选图片墙")
            return
        self.search_counts_label.configure(text=f"候选 {len(items)} 个  ·  主图已下载 {downloaded}/{len(items)}")
        if items:
            self.search_progress.configure(progress_color=ACCENT)
            self.search_progress.set(1)

    def _status(self, text, tone="run"):
        label = self.image_status_label if self.image_task else self.search_status_label if self.search_task else self.status_label
        label.configure(text=text if self.search_task or self.image_task else f"● {text}",
                        text_color={"idle": MUTED, "run": ACCENT, "ok": OK, "err": ERR}[tone])

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

    def _counts(self, total, done, failed, message="", rejected=0):
        if self.image_task:
            self._image_counts(total, done, rejected, failed)
        elif self.search_task:
            text = f"已处理 {done + failed}/{total}  ·  成功 {done}  ·  失败 {failed}" if total else ""
            self.search_counts_label.configure(text=message or text)
        else:
            self.counts_label.configure(text=f"待处理 {total}  ·  成功 {done}  ·  失败 {failed}")

    def _image_counts(self, total, done, rejected, failed):
        pending = max(0, total - done - rejected - failed)
        self.image_counts_label.configure(text=f"待处理 {pending}  ·  已采集 {done}  ·  淘汰 {rejected}  ·  失败 {failed}")

    def _progress(self, value, maximum):
        bar = self.image_progress if self.image_task else self.search_progress if self.search_task else self.progress
        self._set_progress(bar, value, maximum)

    @staticmethod
    def _set_progress(bar, value, maximum):
        fraction = min(1, max(0, value / max(maximum, 1)))
        bar.set(fraction)
        bar.configure(progress_color=ACCENT if fraction > 0 else bar.cget("fg_color"))

    def choose_input(self):
        selected = filedialog.askopenfilename(parent=self, title="选择商品链接 Excel", filetypes=[("Excel 工作簿", "*.xlsx")])
        if selected:
            self.input_var.set(selected)
            if not self.output_var.get().strip():
                source = Path(selected)
                self.output_var.set(str(source.with_name(source.stem + "_output.xlsx")))

    def _change_image_mode(self, mode):
        if not self.image_min_edited:
            self.image_vars["min_clear"].set(default_image_settings(mode)["min_clear"])
            self.image_min_edited = False
        self.image_mode_hint.configure(text="取最多 8 张候选图，命名为 候选_01 起" if mode == IMAGE_MODES[1]
                                       else "取前 5 张主图，命名为 主图_01 – 主图_05")

    def choose_image_workbook(self):
        selected = filedialog.askopenfilename(parent=self, title="选择当前任务的选品结果表",
                                              filetypes=[("Excel 工作簿", "*.xlsx")])
        if selected:
            self._select_image_workbook(Path(selected))

    def choose_image_directory(self):
        selected = filedialog.askdirectory(parent=self, title="选择图片保存目录", mustexist=True)
        if selected:
            self.image_vars["output_dir"].set(selected)
            self._refresh_image_actions()

    def _select_image_workbook(self, path):
        self.image_vars["input_file"].set(str(path))
        self.image_vars["output_dir"].set(str(path.parent / "图片"))
        self._refresh_image_results()
        modes = {record["mode"] for record in self.image_rows if record["mode"]}
        if len(modes) == 1 and (mode := next(iter(modes))) in IMAGE_MODES:
            self.image_vars["mode"].set(mode)
            self._change_image_mode(mode)

    def _refresh_image_results(self):
        from image_collection import read_selection

        self.image_rows = []
        self.image_table.delete(*self.image_table.get_children())
        try:
            self.image_rows = read_selection(Path(self.image_vars["input_file"].get()))
        except Exception as error:
            self.image_input_summary.configure(text=f"无法读取选品结果表：{error}", text_color=ERR)
        else:
            if self.image_rows:
                categories = list(dict.fromkeys(row["category"] or "未分类" for row in self.image_rows))
                codes = [row["code"] for row in self.image_rows]
                classification = "、".join(categories[:3]) if len(categories) <= 3 else f"{len(categories)} 个分类"
                number = codes[0] if len(codes) == 1 else f"{codes[0]} – {codes[-1]}"
                summary = (f"已读取 {len(self.image_rows)} 个商品  ·  分类 {classification}  ·  编号 {number}  ·  "
                           f"图片保存到 分类\\编号")
                self.image_input_summary.configure(text=summary, text_color=MUTED)
            else:
                self.image_input_summary.configure(text="选品结果表没有可采集的商品。", text_color=MUTED)
            for row in self.image_rows:
                self._set_image_row(row)
        states = [row["status"] for row in self.image_rows]
        done, rejected = states.count("已完成"), states.count("已淘汰")
        failed = sum(state not in ("", "待处理", "采集中", "已完成", "已淘汰") for state in states)
        self._image_counts(len(states), done, rejected, failed)
        self._set_progress(self.image_progress, done + rejected + failed, len(states))
        if not self.image_task:
            self.image_status_label.configure(text="已读取采集结果" if done + rejected + failed else "等待采集", text_color=MUTED)
        self._refresh_image_actions()

    def _set_image_row(self, row):
        status = row.get("status") or "待处理"
        tag = {"已完成": "ok", "已淘汰": "warning", "采集中": "working", "待处理": "pending"}.get(status, "error")
        key = str(row["row"])
        values = (row.get("code", ""), f"● {status}", row.get("clear") or "–", row.get("note", ""))
        if self.image_table.exists(key):
            self.image_table.item(key, values=values, tags=(tag,))
        else:
            self.image_table.insert("", "end", iid=key, values=values, tags=(tag,))
        if status == "采集中" and self.image_task:
            self.image_table.see(key)
            self._status(f"采集中：{row.get('code', '')}（{self.image_table.index(key) + 1} / {len(self.image_rows)}）")

    def _refresh_image_actions(self):
        busy = bool(self.task)
        self.image_start_button.configure(state="normal" if self.image_rows and not busy else "disabled",
                                          fg_color=ACCENT if self.image_rows and not busy else ACCENT_OFF)
        ready = any(row["status"] == "已完成" for row in self.image_rows) and all(
            row["status"] not in ("", "待处理", "采集中") for row in self.image_rows)
        self.image_details_button.configure(state="normal" if ready and not busy else "disabled",
                                            fg_color=ACCENT if ready and not busy else ACCENT_OFF)
        output = self.image_vars["output_dir"].get()
        self.image_open_button.configure(state="normal" if output and Path(output).is_dir() else "disabled")

    def open_image_folder(self):
        path = Path(self.image_vars["output_dir"].get())
        try:
            if os.name == "nt":
                os.startfile(path)
            elif not webbrowser.open(path.resolve().as_uri()):
                raise OSError("无法打开图片文件夹。")
        except OSError as error:
            messagebox.showerror("无法打开图片文件夹", str(error), parent=self)

    def open_image_details(self):
        if self.task or self.image_details_button.cget("state") != "normal":
            return
        path = Path(self.image_vars["input_file"].get())
        self.input_var.set(str(path))
        self.output_var.set(str(path.with_name(path.stem + "_详情.xlsx")))
        self._select_tab("详情生成")

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
            self.append_log("DeepSeek 余额查询失败：请先填写 API Key", search=False)
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
            self.append_log(message, search=False)

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
                "api_keys": dict(self.keys) if self.remember_var.get() else {},
                "candidate_search": self.collect_search_settings(), "last_candidate_wall": str(self.wall_path or "")}

    def collect_search_settings(self):
        return {**{key: variable.get() for key, variable in self.search_vars.items()},
                "show_browser": self.search_browser_var.get()}

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

    def start_search(self):
        if self.task:
            return
        try:
            settings = validate_candidate_search(self.collect_search_settings())
        except ValueError as exc:
            messagebox.showerror("请检查搜索设置", str(exc), parent=self)
            return
        self._start_task(settings, search_panel=True, kind="search")

    def start_images(self):
        if self.task:
            return
        try:
            settings = validate_image_settings({key: variable.get() for key, variable in self.image_vars.items()})
        except ValueError as error:
            messagebox.showerror("请检查采集设置", str(error), parent=self)
            return
        self._start_task(settings, image_panel=True, kind="images")

    def start_login(self, site, *, search_panel=False, image_panel=False):
        if not self.task:
            self._start_task(self.collect_settings(), search_panel=search_panel, image_panel=image_panel, kind="login", site=site)

    def _start_task(self, settings, *, search_panel=False, image_panel=False, **options):
        self._save_settings()
        try:
            self.task = launch_job(settings, **options)
        except Exception as exc:
            self.append_log(f"任务启动失败：{exc}", search=search_panel)
            messagebox.showerror("任务启动失败", str(exc), parent=self)
            return
        self.search_task = search_panel
        self.image_task = image_panel
        self.task_kind = options.get("kind")
        self.finished = None
        self.pending_prompt = None
        self._progress(0, 1)
        self._counts(len(self.image_rows) if self.task_kind == "images" else 0, 0, 0)
        self._status("正在启动…")
        self.append_log("任务已启动。")
        self._set_busy(True)
        self._schedule_poll()

    def _set_busy(self, busy):
        for widget, idle_state in self.editable_widgets:
            widget.configure(state="disabled" if busy else idle_state)
        self.start_button.configure(state="disabled" if busy else "normal", fg_color=ACCENT_OFF if busy else ACCENT)
        self.stop_button.configure(state="normal" if busy and not self.search_task and not self.image_task else "disabled")
        self.search_start_button.configure(state="disabled" if busy else "normal", fg_color=ACCENT_OFF if busy else ACCENT)
        self.search_stop_button.configure(state="normal" if busy and self.search_task else "disabled")
        self.image_stop_button.configure(state="normal" if busy and self.image_task else "disabled")
        self._refresh_candidate_actions()
        self._refresh_image_actions()

    def _active_stop_button(self):
        return self.image_stop_button if self.image_task else self.search_stop_button if self.search_task else self.stop_button

    def _active_continue_button(self):
        return (self.image_continue_button if self.image_task else
                self.search_continue_button if self.search_task else self.continue_button)

    def append_log(self, message, *, search=None):
        if search is None:
            search = self.search_task
        widget = self.search_log_text if search else self.log_text
        prefix = f"[{time.strftime('%H:%M:%S')}] " if search else ""
        widget.configure(state="normal")
        widget.insert("end", f"{prefix}{message}\n", log_tag(message))
        if int(widget.index("end-1c").split(".")[0]) > 5000:
            widget.delete("1.0", "1001.0")
        widget.see("end")
        widget.configure(state="disabled")

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
        elif kind == "image_row" and self.image_task:
            self._set_image_row(update)
        elif kind in {"progress", "finished"}:
            total, done, failed = (max(0, int(update.get(key, 0))) for key in ("total", "done", "failed"))
            rejected = max(0, int(update.get("rejected", 0)))
            self._progress(min(done + failed + rejected, total), total)
            self._counts(total, done, failed, update.get("message", ""), rejected=rejected)
            if kind == "finished":
                self.finished = update
            elif not self.closing and not self.image_task:
                stop = self._active_stop_button()
                if stop.cget("state") != "disabled":
                    self._status("正在搜索…" if self.search_task else "正在处理商品…")
        elif kind == "prompt":
            prompt = str(update["message"])
            self.append_log(prompt)
            if "y/n" in prompt.lower():
                answer = messagebox.askyesno("登录确认", prompt, parent=self)
                self.task.respond(update["id"], "y" if answer else "n")
            else:
                self.pending_prompt = update["id"]
                self._status(prompt)
                self._active_continue_button().grid()

    def continue_login(self):
        if self.task and self.pending_prompt is not None:
            try:
                self.task.respond(self.pending_prompt, "")
            except Exception as exc:
                messagebox.showerror("无法继续登录", str(exc), parent=self)
                return
            self.pending_prompt = None
            self._active_continue_button().grid_remove()
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
        self._active_stop_button().configure(state="disabled")
        self.pending_prompt = None
        self._active_continue_button().grid_remove()
        message = ("正在停止，请稍候。" if self.search_task else
                   "正在停止：等待当前商品处理完成并保存，请勿强制关闭。")
        self._status(message)
        self.append_log(message)

    def _finish_task(self, result):
        self.task = None
        self.pending_prompt = None
        self._active_continue_button().grid_remove()
        if self.task_kind == "search" and result.get("wall_file"):
            wall_path = Path(result["wall_file"])
            if wall_path.is_file():
                self.wall_path = wall_path
                self.search_import_label.configure(text=self.SEARCH_IMPORT_HINT)
                self.append_log(f"候选任务目录：{wall_path.parent}", search=True)
        if self.task_kind == "images":
            self._refresh_image_results()
        self._set_busy(False)
        if result.get("output_file"):
            self.output_var.set(result["output_file"])
        status = result.get("status", "error")
        labels = {"finished": "任务已完成", "stopped": "已停止并保存", "error": "任务失败"}
        if self.task_kind == "search":
            labels.update(finished="搜索完成", stopped="搜索已停止", error="搜索失败")
        elif self.task_kind == "images":
            labels.update(finished="图片采集完成", stopped="已停止并保存", error="图片采集失败")
        label = labels.get(status, "任务结束")
        message = result.get("message") or label
        self._status(label, {"finished": "ok", "stopped": "ok", "error": "err"}.get(status, "idle"))
        self.append_log(message)
        if status == "error":
            messagebox.showerror("任务失败", message, parent=self)
        self._save_settings()
        self.search_task = False
        self.image_task = False
        self.task_kind = None
        if self.closing:
            self.destroy()

    def _refresh_candidate_actions(self):
        has_wall = bool(self.wall_path and self.wall_path.is_file())
        can_open = has_wall and not self.task
        has_manifest = has_wall and self.wall_path.with_name("candidate_manifest.json").is_file()
        can_import = has_manifest and not self.task
        self.open_wall_button.configure(state="normal" if can_open else "disabled")
        self.import_candidates_button.configure(state="normal" if can_import else "disabled",
                                                fg_color=ACCENT if can_import else ACCENT_OFF)

    def open_candidate_wall(self):
        if not self.wall_path or not self.wall_path.is_file():
            messagebox.showinfo("候选图片墙尚未生成", "请先完成搜索。", parent=self)
            return
        try:
            if not webbrowser.open(self.wall_path.resolve().as_uri()):
                raise OSError("无法启动默认浏览器。")
        except OSError as exc:
            messagebox.showerror("无法打开候选图片墙", str(exc), parent=self)

    def go_to_image_collection(self):
        if self.task:
            return
        if self.wall_path:
            workbook = self.wall_path.with_name("选品结果.xlsx")
            if workbook.is_file() and workbook != Path(self.image_vars["input_file"].get()):
                self._select_image_workbook(workbook)
        self._select_tab("图片采集")

    def import_candidates(self):
        if self.task or not self.wall_path:
            return
        manifest = self.wall_path.with_name("candidate_manifest.json")
        if not manifest.is_file():
            messagebox.showerror("无法导入选中结果", "找不到候选图片墙对应的记录，请重新搜索。", parent=self)
            return
        selection = filedialog.askopenfilename(parent=self, title="选择候选图片墙导出的 CSV",
                                               filetypes=[("CSV 文件", "*.csv")])
        if not selection:
            return
        try:
            from candidate_wall import import_selection

            workbook = user_data_dir() / "选品结果.xlsx"
            task_workbook = manifest.with_name(workbook.name)
            codes = import_selection(Path(selection), manifest, workbook, task_workbook_path=task_workbook)
        except Exception as exc:
            self.append_log(f"导入失败：{exc}", search=True)
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        if codes:
            number = codes[0] if len(codes) == 1 else f"{codes[0]} – {codes[-1]}"
            message = f"已导入 {len(codes)} 个商品，编号 {number}"
            self.search_import_label.configure(text=f"{message}，已写入任务表和总表")
            self.append_log(f"{message}。总表：{workbook}", search=True)
        else:
            self.search_import_label.configure(text="没有新增商品。")
            self.append_log("导入检查完成，没有新增商品。", search=True)
        if task_workbook.is_file():
            self.append_log(f"本任务选品表：{task_workbook}", search=True)
            self._select_image_workbook(task_workbook)
            self._select_tab("图片采集")

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


def _choose_first_run_data_dir():
    parent = tk.Tk()
    parent.withdraw()
    try:
        while True:
            selected = filedialog.askdirectory(parent=parent, title="选择 ProductDetailsDesktop 的存放位置",
                                               initialdir=str(Path.home()), mustexist=True)
            if not selected:
                return False
            try:
                configure_user_data_dir(selected)
            except (OSError, ValueError) as error:
                messagebox.showerror("无法使用这个目录", str(error), parent=parent)
            else:
                return True
    finally:
        parent.destroy()


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
    try:
        if needs_user_data_setup() and not _choose_first_run_data_dir():
            return 0
        user_data_dir()
    except (OSError, ValueError) as error:
        messagebox.showerror("无法打开程序数据目录", str(error))
        return 1
    DesktopApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
