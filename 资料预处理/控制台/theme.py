# -*- coding: utf-8 -*-
"""控制台主题令牌与 ttk.Style 初始化（#51：浅色 · macOS 清透，2026-09-17 用户拍板）。

唯一色板权威：界面所有颜色经本模块令牌，页面里禁止再写色值字面量；
历史遗留的英文色名（green/orange/...）一律经 `color()` 翻译成令牌。
观感层改动——布局/按钮去留/文案/交互零改动；ttk.Style 全局生效（clam 打底），
App 启动时 `apply(root)` 一次，之后创建的弹窗自动继承同一套样式。"""

import tkinter as tk
from tkinter import ttk

CANVAS = "#f5f5f7"        # 窗口底色
CARD = "#ffffff"          # 卡片 / 表格 / 输入框
SURFACE2 = "#fafafc"      # 表头 / 进度条槽
HAIRLINE = "#e0e0e0"      # 分隔线 / 边框
INK = "#1d1d1f"           # 主文字
INK2 = "#333333"          # 次文字
INK3 = "#7a7a7a"          # 弱提示文字
ACCENT = "#0066cc"        # 强调蓝（主按钮 / 进度条 / 选中）
ACCENT_ACTIVE = "#0a5cb0"
SEL = "#e5f0fb"           # 选中行底色
OK = "#248a3d"            # 语义·正常
WARN = "#b25000"          # 语义·警告
ERR = "#d70015"           # 语义·错误

FONT = ("Microsoft YaHei UI", 10)        # 全局基础字体（#51 布局 B：分组面板）
FONT_SMALL = ("Microsoft YaHei UI", 9)   # 表头等次级文字

_NAMED = {
    "black": INK, "gray": INK3, "grey": INK3,
    "orange": WARN, "green": OK, "red": ERR,
}


def color(name):
    """把历史遗留英文色名翻译成语义令牌（未知值原样返回，不拦正常色值）。"""
    return _NAMED.get(str(name).lower(), name)


def apply(root):
    """全局套用 macOS 清透主题；root 为 Tk 主窗，弹窗后建自动继承。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=FONT, background=CANVAS, foreground=INK,
                    bordercolor=HAIRLINE, lightcolor=CARD, darkcolor=HAIRLINE,
                    troughcolor=CANVAS, fieldbackground=CARD)
    style.configure("TFrame", background=CANVAS)
    # #51 布局 B（分组面板）：白色面板容器 + 面板内无边框按钮
    style.configure("Card.TFrame", background=CARD)
    style.configure("Strip.TButton", padding=(10, 5),
                    background=CARD, foreground=INK2, bordercolor=CARD)
    style.map("Strip.TButton",
              background=[("disabled", CARD), ("active", SURFACE2)],
              foreground=[("disabled", INK3)])
    style.configure("StripDanger.TButton", padding=(10, 5),
                    background=CARD, foreground=ERR, bordercolor=CARD)
    style.map("StripDanger.TButton",
              background=[("disabled", CARD), ("active", SURFACE2)],
              foreground=[("disabled", INK3)])
    style.configure("TLabel", background=CANVAS, foreground=INK)
    style.configure("TButton", padding=(12, 5),
                    background=CARD, foreground=INK, bordercolor=HAIRLINE)
    style.map("TButton",
              background=[("disabled", SURFACE2), ("active", SURFACE2)],
              foreground=[("disabled", INK3)])
    style.configure("Primary.TButton", font=(FONT[0], FONT[1], "bold"),
                    background=ACCENT, foreground="#ffffff", bordercolor=ACCENT)
    style.map("Primary.TButton",
              background=[("disabled", "#7fb0e0"), ("active", ACCENT_ACTIVE),
                          ("pressed", ACCENT_ACTIVE)],
              foreground=[("disabled", "#f2f7fd")])
    style.configure("Danger.TButton", foreground=ERR)
    style.map("Danger.TButton", foreground=[("disabled", INK3)])
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=INK, rowheight=26, bordercolor=HAIRLINE)
    style.configure("Treeview.Heading", font=(FONT_SMALL[0], FONT_SMALL[1], "bold"),
                    background=SURFACE2, foreground=INK3, padding=(6, 4))
    style.map("Treeview",
              background=[("selected", SEL)],
              foreground=[("selected", ACCENT)])
    # #51 布局 B：LabelFrame 白卡片化（与工具条带/状态条同一面板语言，三轮返工对稿）
    style.configure("TLabelframe", background=CARD, bordercolor=HAIRLINE)
    style.configure("TLabelframe.Label", background=CARD, foreground=INK3)
    style.configure("Horizontal.TProgressbar",
                    background=ACCENT, troughcolor=SURFACE2, bordercolor=HAIRLINE)
    style.configure("TEntry", fieldbackground=CARD, foreground=INK,
                    bordercolor=HAIRLINE)
    style.configure("TCombobox", fieldbackground=CARD, foreground=INK)
    style.configure("TSeparator", background=HAIRLINE)
    style.configure("Vertical.TScrollbar", background=SURFACE2,
                    troughcolor=CANVAS, bordercolor=HAIRLINE, arrowcolor=INK3)

    root.configure(bg=CANVAS)
    root.option_add("*Font", "{Microsoft YaHei UI} 10")
    root.option_add("*Background", CANVAS)
    root.option_add("*Foreground", INK)
    root.option_add("*Text*Background", CARD)
    root.option_add("*Entry*Background", CARD)
    root.option_add("*Listbox*Background", CARD)
