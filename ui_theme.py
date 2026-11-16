# -*- coding: utf-8 -*-
"""现代简洁主题：在创建 root 之后调用 apply_theme(root) 即可。"""
import sys
import tkinter as tk
from tkinter import ttk

BG = "#F4F6FA"        # 窗口背景
CARD = "#FFFFFF"      # 卡片背景
BORDER = "#E3E7EE"
TEXT = "#1F2937"
MUTED = "#6B7280"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
ACCENT_DISABLED = "#A9BEEF"
FONT = "Microsoft YaHei UI"


def enable_dpi_awareness() -> None:
    """在创建 Tk() 之前调用，文字更清晰。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def apply_theme(root: tk.Misc) -> ttk.Style:
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    # 下拉列表配色
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

    s = ttk.Style(root)
    s.theme_use("clam")

    s.configure(".", background=BG, foreground=TEXT, font=(FONT, 10),
                bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                focuscolor=BG)

    # 容器
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD, bordercolor=BORDER, relief="solid", borderwidth=1)
    s.configure("TLabelframe", background=CARD, bordercolor=BORDER, relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=(FONT, 10, "bold"))

    # 文字
    s.configure("TLabel", background=CARD, foreground=TEXT)
    s.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 20, "bold"))
    s.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
    s.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    s.configure("Section.TLabel", background=BG, foreground=TEXT, font=(FONT, 11, "bold"))
    # 运行状态：灰 / 蓝 / 绿 / 红
    for name, color in (("Idle", MUTED), ("Run", ACCENT), ("Ok", "#059669"), ("Err", "#DC2626")):
        s.configure(f"Status{name}.TLabel", foreground=color, font=(FONT, 10, "bold"))

    # 输入
    s.configure("TEntry", fieldbackground=CARD, padding=(8, 6), bordercolor=BORDER)
    s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
          darkcolor=[("focus", ACCENT)], fieldbackground=[("disabled", "#F1F3F7")],
          foreground=[("disabled", MUTED)])
    s.configure("TCombobox", fieldbackground=CARD, background=CARD, padding=(8, 6),
                arrowcolor=MUTED, bordercolor=BORDER)
    s.map("TCombobox", fieldbackground=[("disabled", "#F1F3F7"), ("readonly", CARD)],
          foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT)])
    s.configure("TCheckbutton", background=CARD, foreground=TEXT)
    s.map("TCheckbutton", background=[("active", CARD)], indicatorcolor=[("selected", ACCENT)],
          foreground=[("disabled", MUTED)])

    # 按钮：默认次要按钮 + Accent 主按钮
    s.configure("TButton", background=CARD, foreground=TEXT, padding=(16, 8),
                bordercolor="#D5DAE3", lightcolor="#D5DAE3", darkcolor="#D5DAE3", relief="flat")
    s.map("TButton", background=[("active", "#EEF2F9"), ("disabled", BG)],
          foreground=[("disabled", "#A0A7B4")])
    s.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                bordercolor=ACCENT, padding=(22, 8), font=(FONT, 10, "bold"))
    s.map("Accent.TButton",
          background=[("disabled", ACCENT_DISABLED), ("active", ACCENT_HOVER)],
          foreground=[("disabled", "#FFFFFF")], bordercolor=[("disabled", ACCENT_DISABLED)])

    # 进度条
    s.configure("Horizontal.TProgressbar", troughcolor="#E5E9F2", background=ACCENT,
                bordercolor="#E5E9F2", lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

    # 滚动条
    s.configure("Vertical.TScrollbar", background="#D5DAE3", troughcolor=CARD,
                bordercolor=CARD, arrowsize=12, relief="flat")
    return s


def style_log(text: tk.Text) -> None:
    """日志框：等宽、无边框、浅灰底。"""
    text.configure(bg="#FAFBFD", fg=TEXT, relief="flat", borderwidth=0,
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=BORDER, padx=10, pady=8,
                   font=("Consolas", 10), insertbackground=TEXT,
                   selectbackground="#DBE6FF")


def add_log_tags(text: tk.Text) -> None:
    """日志着色：错误红、成功绿、时间戳/INFO 灰。"""
    text.tag_configure("err", foreground="#DC2626")
    text.tag_configure("ok", foreground="#059669")
    text.tag_configure("dim", foreground=MUTED)


def log_tag(message: str) -> str:
    m = str(message)
    if any(k in m for k in ("失败", "错误", "Error", "Traceback", "Exception")):
        return "err"
    if any(k in m for k in ("✔", "已保存", "已完成", "已停止")):
        return "ok"
    if m.startswith("[20") or " INFO" in m:
        return "dim"
    return ""