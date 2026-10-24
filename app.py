from __future__ import annotations

import base64
from collections import Counter
import ctypes
from datetime import datetime
import hashlib
import json
import math
import queue
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import Image, ImageDraw, ImageOps, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    BaseTk = tk.Tk


APP_NAME = "简易 PDF 阅读器"
APP_VERSION = "1.8.0"
FONT = "Microsoft YaHei UI"

COLORS = {
    "window": "#10141a",
    "toolbar": "#171c23",
    "toolbar_hover": "#252d38",
    "canvas": "#0b0e12",
    "text": "#edf2f7",
    "muted": "#8f9aa8",
    "accent": "#58a9e8",
    "accent_hover": "#75bdf2",
    "entry": "#0e1319",
    "border": "#2b3440",
    "shadow": "#05070a",
    "page": "#ffffff",
}

ANNOTATION_COLORS = {
    "yellow": ("#f2c84b", (242, 200, 75, 118)),
    "green": ("#72c894", (114, 200, 148, 112)),
    "blue": ("#68aee8", (104, 174, 232, 112)),
    "pink": ("#e58fb2", (229, 143, 178, 112)),
}


def enable_windows_integration() -> None:
    """Improve high-DPI rendering and taskbar grouping on Windows."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Xii.SimplePDFReader.1"
        )
    except Exception:
        pass


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def format_size(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def protect_secret(secret: str) -> str:
    if not secret or os.name != "nt":
        return ""
    raw = secret.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    input_blob = _DataBlob(len(raw), buffer)
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "ShiiPDFReader",
        None,
        None,
        None,
        1,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(encoded: str) -> str:
    if not encoded or os.name != "nt":
        return ""
    raw = base64.b64decode(encoded)
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    input_blob = _DataBlob(len(raw), buffer)
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        1,
        ctypes.byref(output_blob),
    ):
        return ""
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode(
            "utf-8", errors="replace"
        )
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


class ToolbarButton(tk.Button):
    def __init__(self, parent: tk.Misc, text: str, command, *, accent: bool = False):
        normal = COLORS["accent"] if accent else COLORS["toolbar"]
        active = COLORS["accent_hover"] if accent else COLORS["toolbar_hover"]
        foreground = "#0d1720" if accent else COLORS["text"]
        super().__init__(
            parent,
            text=text,
            command=command,
            font=(FONT, 10),
            foreground=foreground,
            background=normal,
            activeforeground=foreground,
            activebackground=active,
            disabledforeground="#68717d",
            relief="flat",
            borderwidth=0,
            padx=9,
            pady=6,
            cursor="hand2",
            takefocus=0,
            highlightthickness=0,
        )
        self._normal = normal
        self._active = active
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _event=None) -> None:
        if self["state"] != "disabled":
            self.configure(background=self._active)

    def _on_leave(self, _event=None) -> None:
        self.configure(background=self._normal)


class SimplePDFReader(BaseTk):
    def __init__(self) -> None:
        super().__init__()
        self.doc: pdfium.PdfDocument | None = None
        self.current_path: Path | None = None
        self.current_password: str | None = None
        self.current_page = 0
        self.rotation = 0
        self.fit_mode: str | None = "width"
        self.current_scale = 1.0
        self.photo: ImageTk.PhotoImage | None = None
        self.spread_photos: list[ImageTk.PhotoImage] = []
        self.view_mode = "single"
        self.continuous_pages: list[dict] = []
        self._page_sizes: dict[int, tuple[float, float]] = {}
        self.state = self._load_state()
        self.search_matches: list[dict] = []
        self.search_index = -1
        self._search_job: str | None = None
        self._search_generation = 0
        self._search_query_active = ""
        self._save_job: str | None = None
        self._thumb_job: str | None = None
        self._thumb_generation = 0
        self.thumb_photos: list[ImageTk.PhotoImage] = []
        self.thumb_buttons: list[tk.Button] = []
        self.outline_targets: dict[str, int] = {}
        self.link_boxes: list[dict] = []
        self._pan_start: tuple[int, int] | None = None
        self._pan_moved = False
        self.progress_fraction = 0.0
        self.sidebar_visible = bool(
            self.state.get("ui", {}).get("sidebar_visible", True)
        )
        self.night_mode = bool(self.state.get("ui", {}).get("night_mode", False))
        self.history: list[tuple[str, int]] = []
        self.history_index = -1
        self.bookmarks: list[dict] = []
        self.annotations: list[dict] = []
        self.selected_text = ""
        self._selection_pdf_rect: tuple[float, float, float, float] | None = None
        self.search_result_indices: list[int] = []
        self.summary_text = ""
        self._summary_job: str | None = None
        self._summary_generation = 0
        self.api_config = self._load_api_config()
        self._api_history: list[dict] = []
        self._ai_pages: list[str] = []
        self._ai_extract_job: str | None = None
        self._ai_extract_generation = 0
        self._ai_pending_action: dict | None = None
        self._api_queue: queue.Queue = queue.Queue()
        self._api_job: str | None = None
        self._api_generation = 0
        self._api_target = ""
        self._api_response_text = ""
        self._api_current_question = ""
        self._api_settings_window: tk.Toplevel | None = None
        self.selection_canvas_id: int | None = None
        self._selecting = False
        self._selection_start: tuple[float, float] | None = None
        self.page_rects: list[dict] = []
        self._thumb_loaded_count = 0
        self._thumb_batch_size = 24
        self.thumb_more_button: tk.Button | None = None
        self._render_job: str | None = None
        self._resize_job: str | None = None
        self._scroll_job: str | None = None
        self._continuous_rendering = False
        self._fullscreen = False
        self._last_canvas_size = (0, 0)

        self.title(APP_NAME)
        self.geometry("1180x820")
        self.minsize(1040, 640)
        self.configure(background=COLORS["window"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        icon = resource_path("pdf_reader.ico")
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

        self.page_var = tk.StringVar(value="0")
        self.total_var = tk.StringVar(value="0")
        self.zoom_var = tk.StringVar(value="100%")
        self.view_mode_var = tk.StringVar(value="single")
        self.night_mode_var = tk.BooleanVar(value=self.night_mode)
        self.status_var = tk.StringVar(value="打开一份 PDF 开始阅读")
        self.search_count_var = tk.StringVar(value="0/0")

        self._build_ui()
        self._bind_shortcuts()
        self.show_welcome()
        self.after(80, self._center_window)

    def _center_window(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _toolbar_separator(self) -> tk.Frame:
        return tk.Frame(
            self.toolbar,
            width=1,
            height=24,
            background=COLORS["border"],
        )

    def _build_ui(self) -> None:
        self.toolbar = tk.Frame(self, background=COLORS["toolbar"], height=56)
        self.toolbar.pack(side="top", fill="x")
        self.toolbar.pack_propagate(False)
        tk.Frame(
            self.toolbar, background=COLORS["border"], height=1
        ).pack(side="bottom", fill="x")

        self.open_button = ToolbarButton(
            self.toolbar, "打开 PDF", self.open_file_dialog, accent=True
        )
        self.open_button.pack(side="left", padx=(12, 8), pady=9)

        self.recent_button = tk.Menubutton(
            self.toolbar,
            text="导航 ▾",
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground=COLORS["text"],
            activebackground=COLORS["toolbar_hover"],
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            cursor="hand2",
        )
        self.recent_button.pack(side="left", padx=(0, 4), pady=9)
        self.recent_menu = tk.Menu(
            self.recent_button,
            tearoff=False,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground="#0d1720",
            activebackground=COLORS["accent_hover"],
        )
        self.recent_button.configure(menu=self.recent_menu)
        self._refresh_recent_menu()

        self.search_button = ToolbarButton(self.toolbar, "搜索", self.show_search)
        self.search_button.pack(side="left", padx=(0, 6), pady=9)

        self._toolbar_separator().pack(side="left", padx=2, pady=14)

        self.prev_button = ToolbarButton(self.toolbar, "‹", self.previous_page)
        self.prev_button.pack(side="left", padx=2, pady=9)

        page_box = tk.Frame(
            self.toolbar,
            background=COLORS["entry"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        page_box.pack(side="left", padx=2, pady=9)

        self.page_entry = tk.Entry(
            page_box,
            textvariable=self.page_var,
            width=4,
            justify="center",
            font=(FONT, 10),
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            background=COLORS["entry"],
            relief="flat",
            borderwidth=0,
        )
        self.page_entry.pack(side="left", ipady=5, padx=(7, 3))

        tk.Label(
            page_box,
            text="/",
            font=(FONT, 10),
            foreground=COLORS["muted"],
            background=COLORS["entry"],
        ).pack(side="left")

        tk.Label(
            page_box,
            textvariable=self.total_var,
            font=(FONT, 10),
            foreground=COLORS["muted"],
            background=COLORS["entry"],
        ).pack(side="left", padx=(3, 8))

        self.next_button = ToolbarButton(self.toolbar, "›", self.next_page)
        self.next_button.pack(side="left", padx=2, pady=9)

        self._toolbar_separator().pack(side="left", padx=8, pady=14)

        tool_group = tk.Frame(self.toolbar, background=COLORS["toolbar"])
        tool_group.pack(side="left", padx=(0, 0), pady=9)

        self.zoom_out_button = ToolbarButton(tool_group, "−", self.zoom_out)
        self.zoom_out_button.pack(side="left", padx=1)

        self.zoom_label = tk.Label(
            tool_group,
            textvariable=self.zoom_var,
            width=6,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
        )
        self.zoom_label.pack(side="left", padx=1)

        self.zoom_in_button = ToolbarButton(tool_group, "+", self.zoom_in)
        self.zoom_in_button.pack(side="left", padx=1)

        self.fit_width_button = ToolbarButton(
            tool_group, "适宽", self.fit_width
        )
        self.fit_width_button.pack(side="left", padx=(8, 1))

        self.fit_page_button = ToolbarButton(tool_group, "适页", self.fit_page)
        self.fit_page_button.pack(side="left", padx=1)

        self.rotate_button = ToolbarButton(tool_group, "旋转", self.rotate_page)
        self.rotate_button.pack(side="left", padx=1)

        self.view_mode_button = tk.Menubutton(
            tool_group,
            text="视图 ▾",
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground=COLORS["text"],
            activebackground=COLORS["toolbar_hover"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.view_mode_button.pack(side="left", padx=(8, 1))
        view_menu = tk.Menu(
            self.view_mode_button,
            tearoff=False,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground="#0d1720",
            activebackground=COLORS["accent_hover"],
            selectcolor=COLORS["accent"],
        )
        view_menu.add_radiobutton(
            label="单页",
            value="single",
            variable=self.view_mode_var,
            command=lambda: self.set_view_mode("single"),
        )
        view_menu.add_radiobutton(
            label="双页并排",
            value="spread",
            variable=self.view_mode_var,
            command=lambda: self.set_view_mode("spread"),
        )
        view_menu.add_radiobutton(
            label="多页连续",
            value="continuous",
            variable=self.view_mode_var,
            command=lambda: self.set_view_mode("continuous"),
        )
        view_menu.add_separator()
        view_menu.add_checkbutton(
            label="夜间护眼模式",
            variable=self.night_mode_var,
            command=self.toggle_night_mode,
        )
        self.view_mode_button.configure(menu=view_menu)

        self.fullscreen_button = ToolbarButton(
            self.toolbar, "全屏", self.toggle_fullscreen
        )
        self.fullscreen_button.pack(side="right", padx=(2, 12), pady=9)

        self.sidebar_button = ToolbarButton(
            self.toolbar, "☰", self.toggle_sidebar
        )
        self.sidebar_button.pack(side="right", padx=(2, 2), pady=9)

        self.search_frame = tk.Frame(
            self, background=COLORS["toolbar"], height=42
        )
        self.search_frame.pack_propagate(False)
        tk.Label(
            self.search_frame,
            text="查找",
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            padx=14,
        ).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            self.search_frame,
            textvariable=self.search_var,
            font=(FONT, 10),
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            background=COLORS["entry"],
            relief="flat",
            borderwidth=0,
            width=32,
        )
        self.search_entry.pack(side="left", ipady=5, padx=(0, 8), pady=8)
        self.search_entry.bind("<Return>", self._on_search_enter)
        self.search_entry.bind("<Shift-Return>", lambda _e: self.previous_match())
        self.search_prev_button = ToolbarButton(
            self.search_frame, "上一个", self.previous_match
        )
        self.search_prev_button.pack(side="left", padx=2, pady=5)
        self.search_next_button = ToolbarButton(
            self.search_frame, "下一个", self.next_match
        )
        self.search_next_button.pack(side="left", padx=2, pady=5)
        tk.Label(
            self.search_frame,
            textvariable=self.search_count_var,
            width=10,
            font=(FONT, 9),
            foreground=COLORS["muted"],
            background=COLORS["toolbar"],
        ).pack(side="left", padx=8)
        self.search_close_button = ToolbarButton(
            self.search_frame, "关闭", self.hide_search
        )
        self.search_close_button.pack(side="right", padx=(2, 12), pady=5)

        self.workspace = tk.Frame(self, background=COLORS["canvas"])
        self.workspace.pack(side="top", fill="both", expand=True)

        self.sidebar = tk.Frame(
            self.workspace,
            background="#171c23",
            width=260,
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.sidebar.pack_propagate(False)
        sidebar_header = tk.Frame(self.sidebar, background="#171c23", height=42)
        sidebar_header.pack(fill="x")
        sidebar_header.pack_propagate(False)
        tk.Frame(
            sidebar_header,
            width=3,
            height=16,
            background=COLORS["accent"],
        ).pack(side="left", padx=(12, 0))
        tk.Label(
            sidebar_header,
            text="导航",
            font=(FONT, 10, "bold"),
            foreground=COLORS["text"],
            background="#171c23",
            padx=8,
        ).pack(side="left")
        tk.Button(
            sidebar_header,
            text="×",
            command=self.hide_sidebar,
            font=(FONT, 12),
            foreground=COLORS["muted"],
            background="#171c23",
            activeforeground=COLORS["text"],
            activebackground=COLORS["toolbar_hover"],
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            padx=8,
        ).pack(side="right")

        sidebar_tabs_top = tk.Frame(self.sidebar, background="#171c23")
        sidebar_tabs_top.pack(fill="x", padx=8, pady=(2, 2))
        sidebar_tabs_bottom = tk.Frame(self.sidebar, background="#171c23")
        sidebar_tabs_bottom.pack(fill="x", padx=8, pady=(0, 8))
        self.pages_tab_button = ToolbarButton(
            sidebar_tabs_top, "页面", lambda: self._show_sidebar_tab("pages")
        )
        self.pages_tab_button.configure(padx=4)
        self.pages_tab_button.pack(side="left", fill="x", expand=True, padx=1)
        self.outline_tab_button = ToolbarButton(
            sidebar_tabs_top, "目录", lambda: self._show_sidebar_tab("outline")
        )
        self.outline_tab_button.configure(padx=4)
        self.outline_tab_button.pack(side="left", fill="x", expand=True, padx=1)
        self.bookmarks_tab_button = ToolbarButton(
            sidebar_tabs_top, "书签", lambda: self._show_sidebar_tab("bookmarks")
        )
        self.bookmarks_tab_button.configure(padx=4)
        self.bookmarks_tab_button.pack(side="left", fill="x", expand=True, padx=1)
        self.annotations_tab_button = ToolbarButton(
            sidebar_tabs_top, "批注", lambda: self._show_sidebar_tab("annotations")
        )
        self.annotations_tab_button.configure(padx=4)
        self.annotations_tab_button.pack(
            side="left", fill="x", expand=True, padx=1
        )
        self.search_tab_button = ToolbarButton(
            sidebar_tabs_bottom, "搜索", lambda: self._show_sidebar_tab("search")
        )
        self.search_tab_button.configure(padx=4)
        self.search_tab_button.pack(side="left", fill="x", expand=True, padx=1)
        self.summary_tab_button = ToolbarButton(
            sidebar_tabs_bottom, "摘要", lambda: self._show_sidebar_tab("summary")
        )
        self.summary_tab_button.configure(padx=4)
        self.summary_tab_button.pack(side="left", fill="x", expand=True, padx=1)
        self.qa_tab_button = ToolbarButton(
            sidebar_tabs_bottom, "问答", lambda: self._show_sidebar_tab("qa")
        )
        self.qa_tab_button.configure(padx=4)
        self.qa_tab_button.pack(side="left", fill="x", expand=True, padx=1)

        self.sidebar_pages = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_outline = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_bookmarks = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_annotations = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_search = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_summary = tk.Frame(self.sidebar, background="#15191e")
        self.sidebar_qa = tk.Frame(self.sidebar, background="#15191e")
        self.thumb_canvas = tk.Canvas(
            self.sidebar_pages,
            background="#15191e",
            highlightthickness=0,
            borderwidth=0,
        )
        self.thumb_scroll = tk.Scrollbar(
            self.sidebar_pages,
            orient="vertical",
            command=self._on_thumb_scrollbar,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.thumb_canvas.configure(yscrollcommand=self.thumb_scroll.set)
        self.thumb_scroll.pack(side="right", fill="y")
        self.thumb_canvas.pack(side="left", fill="both", expand=True)
        self.thumb_inner = tk.Frame(self.thumb_canvas, background="#15191e")
        self._thumb_window = self.thumb_canvas.create_window(
            (0, 0), window=self.thumb_inner, anchor="nw"
        )
        self.thumb_inner.bind(
            "<Configure>",
            lambda _e: self.thumb_canvas.configure(
                scrollregion=self.thumb_canvas.bbox("all")
            ),
        )
        self.thumb_canvas.bind(
            "<Configure>",
            lambda e: self.thumb_canvas.itemconfigure(
                self._thumb_window, width=e.width
            ),
        )
        self.thumb_canvas.bind("<MouseWheel>", self._on_thumb_mousewheel)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Outline.Treeview",
            background="#15191e",
            fieldbackground="#15191e",
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=28,
            font=(FONT, 9),
        )
        style.map(
            "Outline.Treeview",
            background=[("selected", COLORS["accent"])],
            foreground=[("selected", "#0d1720")],
        )
        self.outline_tree = ttk.Treeview(
            self.sidebar_outline,
            show="tree",
            style="Outline.Treeview",
            selectmode="browse",
        )
        self.outline_scroll = tk.Scrollbar(
            self.sidebar_outline,
            orient="vertical",
            command=self.outline_tree.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.outline_tree.configure(yscrollcommand=self.outline_scroll.set)
        self.outline_scroll.pack(side="right", fill="y")
        self.outline_tree.pack(side="left", fill="both", expand=True)
        self.outline_tree.bind("<Double-1>", self._open_selected_outline)
        self.outline_tree.bind("<Return>", self._open_selected_outline)

        bookmark_actions = tk.Frame(
            self.sidebar_bookmarks, background="#15191e"
        )
        bookmark_actions.pack(fill="x", padx=8, pady=6)
        self.bookmark_add_button = ToolbarButton(
            bookmark_actions, "+ 当前页", self.toggle_bookmark
        )
        self.bookmark_add_button.pack(side="left", padx=1)
        self.bookmark_remove_button = ToolbarButton(
            bookmark_actions, "删除", self.remove_selected_bookmark
        )
        self.bookmark_remove_button.pack(side="right", padx=1)
        self.bookmark_list = tk.Listbox(
            self.sidebar_bookmarks,
            background="#15191e",
            foreground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#0d1720",
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            exportselection=False,
        )
        self.bookmark_scroll = tk.Scrollbar(
            self.sidebar_bookmarks,
            orient="vertical",
            command=self.bookmark_list.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.bookmark_list.configure(yscrollcommand=self.bookmark_scroll.set)
        self.bookmark_scroll.pack(side="right", fill="y")
        self.bookmark_list.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.bookmark_list.bind("<Double-1>", self._open_selected_bookmark)
        self.bookmark_list.bind("<Return>", self._open_selected_bookmark)

        annotation_actions = tk.Frame(
            self.sidebar_annotations, background="#15191e"
        )
        annotation_actions.pack(fill="x", padx=6, pady=6)
        self.annotation_highlight_button = ToolbarButton(
            annotation_actions, "高亮", self.highlight_selection
        )
        self.annotation_highlight_button.configure(padx=5)
        self.annotation_highlight_button.pack(side="left", padx=1)
        self.annotation_note_button = ToolbarButton(
            annotation_actions, "便签", self.add_note_to_selection
        )
        self.annotation_note_button.configure(padx=5)
        self.annotation_note_button.pack(side="left", padx=1)
        self.annotation_delete_button = ToolbarButton(
            annotation_actions, "删除", self.remove_selected_annotation
        )
        self.annotation_delete_button.configure(padx=5)
        self.annotation_delete_button.pack(side="left", padx=1)
        self.annotation_export_button = tk.Menubutton(
            annotation_actions,
            text="导出 ▾",
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground=COLORS["text"],
            activebackground=COLORS["toolbar_hover"],
            relief="flat",
            borderwidth=0,
            padx=5,
            pady=6,
            cursor="hand2",
        )
        self.annotation_export_button.pack(side="right", padx=1)
        export_menu = tk.Menu(
            self.annotation_export_button,
            tearoff=False,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground="#0d1720",
            activebackground=COLORS["accent_hover"],
        )
        export_menu.add_command(
            label="阅读摘录（Markdown/TXT）…",
            command=self.export_annotations,
        )
        export_menu.add_command(
            label="带批注 PDF…",
            command=self.export_annotated_pdf,
        )
        self.annotation_export_button.configure(menu=export_menu)
        self.annotation_list = tk.Listbox(
            self.sidebar_annotations,
            background="#15191e",
            foreground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#0d1720",
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            exportselection=False,
        )
        self.annotation_scroll = tk.Scrollbar(
            self.sidebar_annotations,
            orient="vertical",
            command=self.annotation_list.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.annotation_list.configure(yscrollcommand=self.annotation_scroll.set)
        self.annotation_scroll.pack(side="right", fill="y")
        self.annotation_list.pack(
            side="left", fill="both", expand=True, padx=(8, 0)
        )
        self.annotation_list.bind(
            "<Double-1>", self._open_selected_annotation
        )
        self.annotation_list.bind("<Return>", self._open_selected_annotation)
        self.annotation_list.bind("<Button-3>", self._show_annotation_menu)

        tk.Label(
            self.sidebar_search,
            text="搜索结果",
            font=(FONT, 9, "bold"),
            foreground=COLORS["muted"],
            background="#15191e",
            anchor="w",
            padx=10,
            pady=6,
        ).pack(fill="x")
        self.search_result_list = tk.Listbox(
            self.sidebar_search,
            background="#15191e",
            foreground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#0d1720",
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            exportselection=False,
        )
        self.search_result_scroll = tk.Scrollbar(
            self.sidebar_search,
            orient="vertical",
            command=self.search_result_list.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.search_result_list.configure(
            yscrollcommand=self.search_result_scroll.set
        )
        self.search_result_scroll.pack(side="right", fill="y")
        self.search_result_list.pack(
            side="left", fill="both", expand=True, padx=(8, 0)
        )
        self.search_result_list.bind(
            "<Double-1>", self._open_selected_search_result
        )
        self.search_result_list.bind(
            "<Return>", self._open_selected_search_result
        )

        summary_actions = tk.Frame(
            self.sidebar_summary, background="#15191e"
        )
        summary_actions.pack(fill="x", padx=6, pady=6)
        self.summary_generate_button = ToolbarButton(
            summary_actions, "生成", self.generate_summary
        )
        self.summary_generate_button.configure(padx=5)
        self.summary_generate_button.pack(side="left", padx=1)
        self.summary_ai_button = ToolbarButton(
            summary_actions, "AI", self.generate_ai_summary
        )
        self.summary_ai_button.configure(padx=5)
        self.summary_ai_button.pack(side="left", padx=1)
        self.summary_copy_button = ToolbarButton(
            summary_actions, "复制", self.copy_summary
        )
        self.summary_copy_button.configure(padx=5)
        self.summary_copy_button.pack(side="left", padx=1)
        self.summary_export_button = ToolbarButton(
            summary_actions, "导出", self.export_summary
        )
        self.summary_export_button.configure(padx=5)
        self.summary_export_button.pack(side="right", padx=1)
        self.summary_view = tk.Text(
            self.sidebar_summary,
            wrap="word",
            background="#15191e",
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#0d1720",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.summary_scroll = tk.Scrollbar(
            self.sidebar_summary,
            orient="vertical",
            command=self.summary_view.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.summary_view.configure(yscrollcommand=self.summary_scroll.set)
        self.summary_scroll.pack(side="right", fill="y")
        self.summary_view.pack(side="left", fill="both", expand=True)
        self._set_summary_text("点击“生成”提取文档信息、关键词和核心句子。")

        qa_header = tk.Frame(self.sidebar_qa, background="#15191e")
        qa_header.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(
            qa_header,
            text="基于当前 PDF 提问",
            font=(FONT, 9, "bold"),
            foreground=COLORS["muted"],
            background="#15191e",
        ).pack(side="left", padx=4)
        self.qa_settings_button = ToolbarButton(
            qa_header, "API", self.show_api_settings
        )
        self.qa_settings_button.configure(padx=5)
        self.qa_settings_button.pack(side="right", padx=1)
        self.qa_clear_button = ToolbarButton(
            qa_header, "清空", self.clear_qa_history
        )
        self.qa_clear_button.configure(padx=5)
        self.qa_clear_button.pack(side="right", padx=1)
        self.qa_view = tk.Text(
            self.sidebar_qa,
            wrap="word",
            background="#15191e",
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#0d1720",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            padx=8,
            pady=6,
            state="disabled",
        )
        self.qa_scroll = tk.Scrollbar(
            self.sidebar_qa,
            orient="vertical",
            command=self.qa_view.yview,
            width=12,
            borderwidth=0,
            troughcolor="#15191e",
            background=COLORS["toolbar"],
        )
        self.qa_view.configure(yscrollcommand=self.qa_scroll.set)
        self.qa_scroll.pack(side="right", fill="y")
        self.qa_view.pack(side="top", fill="both", expand=True)
        qa_input = tk.Frame(self.sidebar_qa, background="#15191e")
        qa_input.pack(side="bottom", fill="x", padx=6, pady=6)
        self.qa_question_var = tk.StringVar()
        self.qa_entry = tk.Entry(
            qa_input,
            textvariable=self.qa_question_var,
            font=(FONT, 9),
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            background=COLORS["entry"],
            relief="flat",
            borderwidth=0,
        )
        self.qa_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.qa_entry.bind("<Return>", self.send_question)
        self.qa_send_button = ToolbarButton(
            qa_input, "发送", self.send_question, accent=True
        )
        self.qa_send_button.configure(padx=7)
        self.qa_send_button.pack(side="right", padx=(4, 0))
        self._set_qa_text(
            "先在“API”中填写 Base URL 和模型名称，"
            "然后可以针对当前 PDF 提问。\n"
        )

        self.body = tk.Frame(self.workspace, background=COLORS["canvas"])
        self.body.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(
            self.body,
            background=COLORS["canvas"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.vscroll = tk.Scrollbar(
            self.body,
            orient="vertical",
            command=self._on_vertical_scrollbar,
            width=14,
            borderwidth=0,
            troughcolor=COLORS["window"],
            background=COLORS["toolbar"],
            activebackground=COLORS["toolbar_hover"],
        )
        self.vscroll.pack(side="right", fill="y")
        self.hscroll = tk.Scrollbar(
            self,
            orient="horizontal",
            command=self.canvas.xview,
            width=14,
            borderwidth=0,
            troughcolor=COLORS["window"],
            background=COLORS["toolbar"],
            activebackground=COLORS["toolbar_hover"],
        )
        self.hscroll.pack(side="bottom", fill="x")
        self.canvas.configure(
            yscrollcommand=self.vscroll.set, xscrollcommand=self.hscroll.set
        )

        status = tk.Frame(self, background=COLORS["toolbar"], height=30)
        status.pack(side="bottom", fill="x")
        status.pack_propagate(False)
        self.status_label = tk.Label(
            status,
            textvariable=self.status_var,
            anchor="w",
            font=(FONT, 9),
            foreground=COLORS["muted"],
            background=COLORS["toolbar"],
            padx=12,
        )
        self.status_label.pack(side="left", fill="both", expand=True)
        tk.Label(
            status,
            text="Shift+拖选文字 · Ctrl+D 书签 · Ctrl+F 搜索 · Alt+← 返回",
            font=(FONT, 8),
            foreground="#77818d",
            background=COLORS["toolbar"],
            padx=12,
        ).pack(side="right")

        self.progress_canvas = tk.Canvas(
            self,
            background=COLORS["border"],
            height=5,
            highlightthickness=0,
            borderwidth=0,
        )
        self.progress_canvas.pack(side="bottom", fill="x")
        self.progress_fill = self.progress_canvas.create_rectangle(
            0, 0, 0, 5, fill=COLORS["accent"], outline=""
        )
        self.progress_canvas.bind("<Configure>", self._draw_progress)

        self._show_sidebar_tab("pages")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._start_pan)
        self.canvas.bind("<B1-Motion>", self._drag_pan)
        self.canvas.bind("<ButtonRelease-1>", self._end_pan)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Button-3>", self._show_selection_menu)
        self.page_entry.bind("<Return>", self._jump_to_entered_page)
        self.page_entry.bind("<FocusOut>", self._restore_page_entry)
        if DND_FILES is not None:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_file_drop)
            except tk.TclError:
                pass
        self._set_document_buttons(False)

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-o>", lambda _e: self.open_file_dialog())
        self.bind_all("<Control-plus>", lambda _e: self.zoom_in())
        self.bind_all("<Control-equal>", lambda _e: self.zoom_in())
        self.bind_all("<Control-minus>", lambda _e: self.zoom_out())
        self.bind_all("<Control-0>", lambda _e: self.fit_width())
        self.bind_all("<Control-m>", lambda _e: self.toggle_view_mode())
        self.bind_all("<Control-f>", lambda _e: self.show_search())
        self.bind_all("<Control-d>", lambda _e: self.toggle_bookmark())
        self.bind_all("<Control-c>", self._copy_selected_text)
        self.bind_all("<Alt-Left>", lambda _e: self._history_shortcut(-1))
        self.bind_all("<Alt-Right>", lambda _e: self._history_shortcut(1))
        self.bind_all("<F11>", lambda _e: self.toggle_fullscreen())
        self.bind_all("<Escape>", self._on_escape)
        self.bind_all("<Prior>", lambda _e: self.previous_page())
        self.bind_all("<Next>", lambda _e: self.next_page())
        self.bind_all("<Home>", lambda _e: self.go_to_page(0))
        self.bind_all("<End>", lambda _e: self.go_to_page(self.page_count - 1))
        self.bind_all("<Left>", self._on_left_key)
        self.bind_all("<Right>", self._on_right_key)
        self.bind_all("<Up>", lambda _e: self._scroll_vertical(-2))
        self.bind_all("<Down>", lambda _e: self._scroll_vertical(2))

    @property
    def page_count(self) -> int:
        return len(self.doc) if self.doc is not None else 0

    def _history_shortcut(self, direction: int) -> None:
        if isinstance(self.focus_get(), (tk.Entry, tk.Text)):
            return
        self.navigate_history(direction)

    def _set_document_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (
            self.prev_button,
            self.next_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.fit_width_button,
            self.fit_page_button,
            self.rotate_button,
            self.view_mode_button,
            self.search_button,
            self.sidebar_button,
        ):
            button.configure(state=state)
        self.page_entry.configure(state=state)

    def _show_sidebar_tab(self, tab: str) -> None:
        self.sidebar_pages.pack_forget()
        self.sidebar_outline.pack_forget()
        self.sidebar_bookmarks.pack_forget()
        self.sidebar_annotations.pack_forget()
        self.sidebar_search.pack_forget()
        self.sidebar_summary.pack_forget()
        self.sidebar_qa.pack_forget()
        for button in (
            self.pages_tab_button,
            self.outline_tab_button,
            self.bookmarks_tab_button,
            self.annotations_tab_button,
            self.search_tab_button,
            self.summary_tab_button,
            self.qa_tab_button,
        ):
            button.configure(
                background=COLORS["toolbar"],
                foreground=COLORS["muted"],
            )
        if tab == "outline":
            self.sidebar_outline.pack(fill="both", expand=True)
            self.outline_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
        elif tab == "bookmarks":
            self.sidebar_bookmarks.pack(fill="both", expand=True)
            self.bookmarks_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
            self._refresh_bookmarks()
        elif tab == "annotations":
            self.sidebar_annotations.pack(fill="both", expand=True)
            self.annotations_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
            self._refresh_annotation_list()
        elif tab == "search":
            self.sidebar_search.pack(fill="both", expand=True)
            self.search_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
            self._refresh_search_results()
        elif tab == "summary":
            self.sidebar_summary.pack(fill="both", expand=True)
            self.summary_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
        elif tab == "qa":
            self.sidebar_qa.pack(fill="both", expand=True)
            self.qa_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
        else:
            self.sidebar_pages.pack(fill="both", expand=True)
            self.pages_tab_button.configure(
                background=COLORS["toolbar_hover"],
                foreground=COLORS["accent"],
            )
            if (
                self.doc is not None
                and self.sidebar_visible
                and not self.thumb_buttons
            ):
                self._start_thumbnails()
            elif self.doc is not None:
                self._show_thumb_more()

    def toggle_sidebar(self) -> None:
        if self.sidebar_visible:
            self.hide_sidebar()
        else:
            self.show_sidebar()

    def show_sidebar(self) -> None:
        if self.doc is None:
            return
        if not self.sidebar.winfo_ismapped():
            self.sidebar.pack(side="left", fill="y", before=self.body)
        self.sidebar_visible = True
        self.state.setdefault("ui", {})["sidebar_visible"] = True
        self._save_state_now()
        if not self.thumb_buttons:
            self._start_thumbnails()

    def hide_sidebar(self) -> None:
        self.sidebar.pack_forget()
        self.sidebar_visible = False
        self.state.setdefault("ui", {})["sidebar_visible"] = False
        self._save_state_now()

    def _prepare_sidebar_for_document(self) -> None:
        self._clear_thumbnails()
        self._refresh_outline()
        if self.sidebar_visible:
            self.show_sidebar()
        else:
            self.hide_sidebar()

    def _clear_thumbnails(self) -> None:
        self._thumb_generation += 1
        if self._thumb_job is not None:
            try:
                self.after_cancel(self._thumb_job)
            except tk.TclError:
                pass
            self._thumb_job = None
        for child in self.thumb_inner.winfo_children():
            child.destroy()
        self.thumb_photos = []
        self.thumb_buttons = []
        self.thumb_more_button = None
        self._thumb_loaded_count = 0

    def _start_thumbnails(self) -> None:
        if self.doc is None:
            return
        self._clear_thumbnails()
        generation = self._thumb_generation
        end_index = min(self.page_count, self._thumb_batch_size)
        self._thumb_step(generation, 0, end_index)

    def _thumb_step(
        self, generation: int, page_index: int, end_index: int
    ) -> None:
        self._thumb_job = None
        if (
            generation != self._thumb_generation
            or self.doc is None
            or page_index >= min(end_index, self.page_count)
        ):
            self._show_thumb_more()
            return
        page = None
        bitmap = None
        try:
            page = self.doc[page_index]
            width, height = page.get_size()
            if self.rotation in (90, 270):
                width, height = height, width
            scale = 132 / max(width, 1)
            bitmap = page.render(
                scale=scale,
                rotation=self.rotation,
                draw_annots=True,
                optimize_mode="lcd",
                rev_byteorder=True,
            )
            photo = ImageTk.PhotoImage(bitmap.to_pil().convert("RGB").copy())
            button = tk.Button(
                self.thumb_inner,
                image=photo,
                text=f"第 {page_index + 1} 页",
                compound="top",
                font=(FONT, 8),
                foreground=COLORS["muted"],
                background="#15191e",
                activeforeground=COLORS["text"],
                activebackground="#29313c",
                relief="flat",
                borderwidth=0,
                padx=8,
                pady=5,
                cursor="hand2",
                command=lambda target=page_index: self.go_to_page(target),
            )
            button.pack(fill="x", padx=8, pady=3)
            self.thumb_photos.append(photo)
            self.thumb_buttons.append(button)
            self._thumb_loaded_count = max(
                self._thumb_loaded_count, page_index + 1
            )
            self._update_thumb_selection()
        except Exception:
            pass
        finally:
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception:
                    pass
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        if generation == self._thumb_generation:
            if page_index + 1 < end_index:
                self._thumb_job = self.after(
                    8,
                    self._thumb_step,
                    generation,
                    page_index + 1,
                    end_index,
                )
            else:
                self._show_thumb_more()

    def _show_thumb_more(self) -> None:
        if self.thumb_more_button is not None:
            try:
                self.thumb_more_button.destroy()
            except tk.TclError:
                pass
            self.thumb_more_button = None
        if (
            self.doc is None
            or self._thumb_loaded_count >= self.page_count
            or not self.sidebar_pages.winfo_ismapped()
        ):
            return
        self.thumb_more_button = tk.Button(
            self.thumb_inner,
            text=f"继续加载 · 还有 {self.page_count - self._thumb_loaded_count} 页",
            command=self._load_more_thumbnails,
            font=(FONT, 9),
            foreground=COLORS["muted"],
            background="#20262e",
            activeforeground=COLORS["text"],
            activebackground=COLORS["toolbar_hover"],
            relief="flat",
            borderwidth=0,
            pady=8,
            cursor="hand2",
        )
        self.thumb_more_button.pack(fill="x", padx=8, pady=8)

    def _load_more_thumbnails(self) -> None:
        if self.doc is None or self._thumb_loaded_count >= self.page_count:
            return
        if self.thumb_more_button is not None:
            self.thumb_more_button.destroy()
            self.thumb_more_button = None
        start = self._thumb_loaded_count
        end = min(self.page_count, start + self._thumb_batch_size)
        self._thumb_step(self._thumb_generation, start, end)

    def _on_thumb_scrollbar(self, *args) -> None:
        self.thumb_canvas.yview(*args)
        self._maybe_load_more_thumbnails()

    def _on_thumb_mousewheel(self, event) -> str:
        self.thumb_canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
        self._maybe_load_more_thumbnails()
        return "break"

    def _maybe_load_more_thumbnails(self) -> None:
        if (
            self.doc is None
            or self._thumb_loaded_count >= self.page_count
            or self._thumb_job is not None
        ):
            return
        try:
            if self.thumb_canvas.yview()[1] >= 0.94:
                self._load_more_thumbnails()
        except tk.TclError:
            pass

    def _update_thumb_selection(self) -> None:
        for index, button in enumerate(self.thumb_buttons):
            active = index == self.current_page
            button.configure(
                background="#2a3441" if active else "#15191e",
                foreground=COLORS["text"] if active else COLORS["muted"],
            )
        if (
            self.sidebar_pages.winfo_ismapped()
            and 0 <= self.current_page < len(self.thumb_buttons)
        ):
            button = self.thumb_buttons[self.current_page]
            try:
                self.thumb_canvas.yview_moveto(
                    max(0.0, (button.winfo_y() - 60) / max(
                        self.thumb_inner.winfo_height(), 1
                    ))
                )
            except tk.TclError:
                pass

    def _refresh_outline(self) -> None:
        for item in self.outline_tree.get_children():
            self.outline_tree.delete(item)
        self.outline_targets = {}
        if self.doc is None:
            return
        stack: dict[int, str] = {}
        count = 0
        try:
            for bookmark in self.doc.get_toc():
                if hasattr(bookmark, "title"):
                    title = bookmark.title
                else:
                    title = bookmark.get_title()
                if hasattr(bookmark, "page_index"):
                    page_index = bookmark.page_index
                else:
                    destination = bookmark.get_dest()
                    page_index = destination.get_index() if destination else None
                if page_index is None:
                    continue
                level = max(0, int(bookmark.level))
                parent = stack.get(level - 1, "")
                item = self.outline_tree.insert(
                    parent, "end", text=title, open=True
                )
                stack[level] = item
                for stale_level in [
                    key for key in stack if key > level
                ]:
                    stack.pop(stale_level, None)
                self.outline_targets[item] = page_index
                count += 1
        except Exception:
            pass
        if count == 0:
            self.outline_tree.insert(
                "", "end", text="此 PDF 没有书签目录", tags=("empty",)
            )

    def _open_selected_outline(self, _event=None) -> None:
        selection = self.outline_tree.selection()
        if not selection:
            return
        target = self.outline_targets.get(selection[0])
        if target is not None:
            self.go_to_page(target)

    def _refresh_bookmarks(self) -> None:
        self.bookmark_list.delete(0, "end")
        self.bookmark_pages: list[int] = []
        for bookmark in sorted(
            self.bookmarks, key=lambda item: int(item.get("page", 0))
        ):
            page_index = int(bookmark.get("page", 0))
            if not 0 <= page_index < self.page_count:
                continue
            label = bookmark.get("label") or f"第 {page_index + 1} 页"
            self.bookmark_list.insert("end", f"★  {label}")
            self.bookmark_pages.append(page_index)
        self._sync_bookmark_selection(self.current_page)

    def _open_selected_bookmark(self, _event=None) -> None:
        selection = self.bookmark_list.curselection()
        if not selection:
            return
        index = selection[0]
        if 0 <= index < len(self.bookmark_pages):
            self.go_to_page(self.bookmark_pages[index])

    def toggle_bookmark(self) -> None:
        if self.doc is None:
            return
        page = self.current_page
        existing = [
            index
            for index, bookmark in enumerate(self.bookmarks)
            if int(bookmark.get("page", -1)) == page
        ]
        if existing:
            for index in reversed(existing):
                self.bookmarks.pop(index)
            self.status_var.set(f"已移除第 {page + 1} 页书签")
        else:
            self.bookmarks.append(
                {"page": page, "label": f"第 {page + 1} 页"}
            )
            self.status_var.set(f"已添加第 {page + 1} 页书签")
        self.bookmarks.sort(key=lambda item: int(item.get("page", 0)))
        self._remember_current_state()
        self._refresh_bookmarks()

    def remove_selected_bookmark(self) -> None:
        selection = self.bookmark_list.curselection()
        if not selection:
            return
        index = selection[0]
        if not 0 <= index < len(self.bookmark_pages):
            return
        page = self.bookmark_pages[index]
        self.bookmarks = [
            bookmark
            for bookmark in self.bookmarks
            if int(bookmark.get("page", -1)) != page
        ]
        self._remember_current_state()
        self._refresh_bookmarks()

    def _sync_bookmark_selection(self, page_index: int) -> None:
        if not hasattr(self, "bookmark_pages"):
            return
        try:
            matches = [
                index
                for index, page in enumerate(self.bookmark_pages)
                if page == page_index
            ]
            if matches:
                self.bookmark_list.selection_clear(0, "end")
                self.bookmark_list.selection_set(matches[0])
                self.bookmark_list.see(matches[0])
        except tk.TclError:
            pass

    def _annotation_file(self) -> Path | None:
        if self.current_path is None:
            return None
        key = hashlib.sha256(
            str(self.current_path.resolve()).encode("utf-8", errors="replace")
        ).hexdigest()
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        return appdata / "ShiiPDFReader" / "annotations" / f"{key}.json"

    def _load_annotations(self) -> None:
        self.annotations = []
        path = self._annotation_file()
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            annotations = data.get("annotations", [])
            if isinstance(annotations, list):
                for annotation in annotations:
                    if not isinstance(annotation, dict):
                        continue
                    try:
                        page = int(annotation.get("page", -1))
                        rect = tuple(float(value) for value in annotation["rect"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if (
                        0 <= page < self.page_count
                        and len(rect) == 4
                    ):
                        annotation["page"] = page
                        annotation["rect"] = rect
                        self.annotations.append(annotation)
        except (OSError, ValueError):
            self.annotations = []

    def _save_annotations(self) -> None:
        path = self._annotation_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "document": str(self.current_path),
                        "annotations": self.annotations,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _refresh_annotation_list(self) -> None:
        self.annotation_list.delete(0, "end")
        self.annotation_list_items: list[dict] = []
        colors = {
            "yellow": "黄",
            "green": "绿",
            "blue": "蓝",
            "pink": "粉",
        }
        for annotation in sorted(
            self.annotations,
            key=lambda item: (
                int(item.get("page", 0)),
                float(item.get("created", 0)),
            ),
        ):
            page = int(annotation.get("page", 0))
            excerpt = re.sub(
                r"\s+",
                " ",
                str(annotation.get("text", "")).strip(),
            )
            if len(excerpt) > 34:
                excerpt = excerpt[:34] + "…"
            note = re.sub(
                r"\s+", " ", str(annotation.get("note", "")).strip()
            )
            if len(note) > 24:
                note = note[:24] + "…"
            color = colors.get(str(annotation.get("color", "yellow")), "黄")
            label = f"[{color}] 第 {page + 1} 页  {excerpt}"
            if note:
                label += f"  ·  {note}"
            self.annotation_list.insert("end", label)
            self.annotation_list_items.append(annotation)
        self._sync_annotation_selection(self.current_page)

    def _sync_annotation_selection(self, page_index: int) -> None:
        if not hasattr(self, "annotation_list_items"):
            return
        matches = [
            index
            for index, annotation in enumerate(self.annotation_list_items)
            if int(annotation.get("page", -1)) == page_index
        ]
        if matches:
            try:
                self.annotation_list.selection_clear(0, "end")
                self.annotation_list.selection_set(matches[0])
                self.annotation_list.see(matches[0])
            except tk.TclError:
                pass

    def _selected_annotation(self) -> dict | None:
        selection = self.annotation_list.curselection()
        if not selection:
            return None
        index = selection[0]
        if 0 <= index < len(getattr(self, "annotation_list_items", [])):
            return self.annotation_list_items[index]
        return None

    def _open_selected_annotation(self, _event=None) -> None:
        annotation = self._selected_annotation()
        if annotation is not None:
            self.go_to_page(int(annotation["page"]))

    def _create_annotation(self, color: str, note: str) -> None:
        if (
            self.current_path is None
            or not self.selected_text
            or self._selection_pdf_rect is None
        ):
            self.status_var.set("请先按住 Shift 拖动选择文字")
            return
        annotation = {
            "id": time.time_ns(),
            "page": self.current_page,
            "rect": list(self._selection_pdf_rect),
            "text": self.selected_text,
            "note": note.strip(),
            "color": color if color in ANNOTATION_COLORS else "yellow",
            "created": time.time(),
            "updated": time.time(),
        }
        self.annotations.append(annotation)
        self._save_annotations()
        self._refresh_annotation_list()
        self.render_page()
        self.status_var.set("批注已保存")

    def highlight_selection(self) -> None:
        self._create_annotation("yellow", "")

    def add_note_to_selection(self) -> None:
        if not self.selected_text or self._selection_pdf_rect is None:
            self.status_var.set("请先按住 Shift 拖动选择文字")
            return
        note = simpledialog.askstring(
            "添加便签",
            "写在这段文字旁边的笔记：",
            parent=self,
        )
        if note is None:
            return
        self._create_annotation("pink", note)

    def remove_selected_annotation(self) -> None:
        annotation = self._selected_annotation()
        if annotation is None:
            return
        annotation_id = annotation.get("id")
        self.annotations = [
            item for item in self.annotations if item.get("id") != annotation_id
        ]
        self._save_annotations()
        self._refresh_annotation_list()
        self.render_page()

    def edit_selected_annotation(self) -> None:
        annotation = self._selected_annotation()
        if annotation is None:
            return
        note = simpledialog.askstring(
            "编辑便签",
            "笔记内容：",
            initialvalue=str(annotation.get("note", "")),
            parent=self,
        )
        if note is None:
            return
        annotation["note"] = note.strip()
        annotation["updated"] = time.time()
        self._save_annotations()
        self._refresh_annotation_list()
        self.render_page()

    def _apply_annotation_highlights(
        self, image: Image.Image, page_index: int, scale: float
    ) -> Image.Image:
        page_annotations = [
            item for item in self.annotations if int(item.get("page", -1)) == page_index
        ]
        if not page_annotations:
            return image
        page_width, page_height = self._page_size(page_index)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for annotation in page_annotations:
            try:
                left, bottom, right, top = (
                    float(value) for value in annotation["rect"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            color_name = str(annotation.get("color", "yellow"))
            _outline, rgba = ANNOTATION_COLORS.get(
                color_name, ANNOTATION_COLORS["yellow"]
            )
            x1, y1, x2, y2 = self._pdf_box_to_canvas(
                left,
                bottom,
                right,
                top,
                page_width,
                page_height,
                scale,
                self.rotation,
                0,
                0,
            )
            x1 = max(0, min(image.width - 1, x1))
            y1 = max(0, min(image.height - 1, y1))
            x2 = max(0, min(image.width, x2))
            y2 = max(0, min(image.height, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            draw.rectangle((x1, y1, x2, y2), fill=rgba)
            if annotation.get("note"):
                radius = 6
                cx = min(image.width - radius, x2 + radius + 2)
                cy = max(radius, y1 + radius)
                draw.ellipse(
                    (cx - radius, cy - radius, cx + radius, cy + radius),
                    fill=rgba,
                    outline=(255, 255, 255, 220),
                )
        return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    def export_annotations(self) -> None:
        if self.current_path is None:
            return
        default_name = f"{self.current_path.stem}-阅读摘录.md"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出阅读摘录",
            initialdir=str(self.current_path.parent),
            initialfile=default_name,
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        target = Path(path)
        lines = [
            f"# {self.current_path.name} 阅读摘录",
            "",
            f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        if self.bookmarks:
            lines.extend(["## 书签", ""])
            for bookmark in sorted(
                self.bookmarks, key=lambda item: int(item.get("page", 0))
            ):
                lines.append(
                    f"- 第 {int(bookmark.get('page', 0)) + 1} 页"
                    f"：{bookmark.get('label', '')}"
                )
            lines.append("")
        if self.annotations:
            lines.extend(["## 高亮与笔记", ""])
            for annotation in sorted(
                self.annotations,
                key=lambda item: (
                    int(item.get("page", 0)),
                    float(item.get("created", 0)),
                ),
            ):
                page = int(annotation.get("page", 0)) + 1
                text = str(annotation.get("text", "")).strip()
                note = str(annotation.get("note", "")).strip()
                lines.extend([f"### 第 {page} 页", ""])
                if text:
                    lines.extend([f"> {text.replace(chr(10), chr(10) + '> ')}", ""])
                if note:
                    lines.extend([f"**笔记：** {note}", ""])
        if not self.bookmarks and not self.annotations:
            lines.append("还没有书签或批注。")
        try:
            target.write_text("\n".join(lines), encoding="utf-8")
            self.status_var.set(f"摘录已导出到 {target.name}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def export_annotated_pdf(self) -> Path | None:
        if self.current_path is None or self.doc is None:
            return None
        if not self.annotations and not self.bookmarks:
            messagebox.showinfo(
                "没有可导出的批注",
                "请先添加高亮、便签或书签。",
                parent=self,
            )
            return None
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出带批注 PDF",
            initialdir=str(self.current_path.parent),
            initialfile=f"{self.current_path.stem}-带批注.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
        )
        if not path:
            return None
        target = Path(path)
        if target.resolve() == self.current_path.resolve():
            messagebox.showerror(
                "不能覆盖原文件",
                "请选择不同的文件名，原 PDF 会保持不变。",
                parent=self,
            )
            return None
        temporary = target.with_name(target.stem + ".tmp.pdf")
        try:
            from pypdf import PdfReader, PdfWriter
            from pypdf.annotations import Highlight, Text
            from pypdf.constants import AnnotationFlag
            from pypdf.generic import (
                ArrayObject,
                FloatObject,
                NameObject,
                TextStringObject,
            )

            reader = PdfReader(str(self.current_path))
            was_encrypted = bool(reader.is_encrypted)
            if was_encrypted and reader.decrypt(self.current_password or "") == 0:
                raise ValueError("无法解密原 PDF，可能缺少打开密码。")
            writer = PdfWriter()
            writer.clone_document_from_reader(reader)
            color_hex = {
                "yellow": "f2c84b",
                "green": "72c894",
                "blue": "68aee8",
                "pink": "e58fb2",
            }
            for item in self.annotations:
                page_index = int(item.get("page", -1))
                if not 0 <= page_index < len(reader.pages):
                    continue
                left, bottom, right, top = (
                    float(value) for value in item["rect"]
                )
                x1, x2 = sorted((left, right))
                y1, y2 = sorted((bottom, top))
                if x2 - x1 < 1:
                    x2 = x1 + 1
                if y2 - y1 < 1:
                    y2 = y1 + 1
                page_box = reader.pages[page_index].mediabox
                x1 = max(0.0, min(float(page_box.right), x1))
                x2 = max(0.0, min(float(page_box.right), x2))
                y1 = max(0.0, min(float(page_box.top), y1))
                y2 = max(0.0, min(float(page_box.top), y2))
                quad = ArrayObject(
                    [
                        FloatObject(x1),
                        FloatObject(y2),
                        FloatObject(x2),
                        FloatObject(y2),
                        FloatObject(x1),
                        FloatObject(y1),
                        FloatObject(x2),
                        FloatObject(y1),
                    ]
                )
                color = color_hex.get(
                    str(item.get("color", "yellow")), color_hex["yellow"]
                )
                highlight = Highlight(
                    rect=(x1, y1, x2, y2),
                    quad_points=quad,
                    highlight_color=color,
                    printing=True,
                    title_bar=APP_NAME,
                )
                excerpt = str(item.get("text", "")).strip()
                note = str(item.get("note", "")).strip()
                if excerpt or note:
                    highlight[NameObject("/Contents")] = TextStringObject(
                        "\n\n".join(part for part in (excerpt, note) if part)
                    )
                writer.add_annotation(page_index, highlight)
                if note:
                    icon_left = min(float(page_box.right) - 20, x2 + 3)
                    icon_bottom = max(2.0, y2 - 18)
                    note_annotation = Text(
                        rect=(
                            icon_left,
                            icon_bottom,
                            icon_left + 18,
                            icon_bottom + 18,
                        ),
                        text=note,
                        open=False,
                        flags=int(AnnotationFlag.PRINT),
                        title_bar=APP_NAME,
                    )
                    writer.add_annotation(page_index, note_annotation)
            for bookmark in self.bookmarks:
                page_index = int(bookmark.get("page", -1))
                if 0 <= page_index < len(reader.pages):
                    writer.add_outline_item(
                        str(bookmark.get("label") or f"第 {page_index + 1} 页"),
                        page_index,
                    )
            if was_encrypted:
                writer.encrypt(
                    user_password=self.current_password or "",
                    algorithm="AES-256",
                )
            with temporary.open("wb") as stream:
                writer.write(stream)
            check = PdfReader(str(temporary))
            if check.is_encrypted:
                check.decrypt(self.current_password or "")
            if len(check.pages) != len(reader.pages):
                raise ValueError("导出后的页数与原 PDF 不一致。")
            os.replace(temporary, target)
            self.status_var.set(f"带批注 PDF 已导出到 {target.name}")
            messagebox.showinfo(
                "导出完成",
                f"带批注 PDF 已保存：\n{target}",
                parent=self,
            )
            return target
        except Exception as exc:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            messagebox.showerror("导出失败", str(exc), parent=self)
            return None

    def _show_annotation_menu(self, event) -> None:
        index = self.annotation_list.nearest(event.y)
        if index >= 0:
            self.annotation_list.selection_clear(0, "end")
            self.annotation_list.selection_set(index)
        menu = tk.Menu(
            self,
            tearoff=False,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground="#0d1720",
            activebackground=COLORS["accent_hover"],
        )
        menu.add_command(label="跳转到批注", command=self._open_selected_annotation)
        menu.add_command(label="编辑笔记", command=self.edit_selected_annotation)
        menu.add_command(label="删除批注", command=self.remove_selected_annotation)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_reading_progress(self, page_index: int) -> None:
        if self.page_count <= 0:
            self.progress_fraction = 0.0
        else:
            self.progress_fraction = min(
                1.0, (page_index + 1) / self.page_count
            )
        self._draw_progress()
        self._update_thumb_selection()
        self._sync_outline_selection(page_index)
        self._sync_bookmark_selection(page_index)

    def _draw_progress(self, _event=None) -> None:
        try:
            width = max(self.progress_canvas.winfo_width(), 1)
            height = max(self.progress_canvas.winfo_height(), 5)
            self.progress_canvas.coords(
                self.progress_fill, 0, 0, width * self.progress_fraction, height
            )
        except tk.TclError:
            pass

    def _sync_outline_selection(self, page_index: int) -> None:
        if not self.outline_targets:
            return
        candidate = None
        candidate_page = -1
        for item, target in self.outline_targets.items():
            if target <= page_index and target >= candidate_page:
                candidate = item
                candidate_page = target
        if candidate:
            try:
                self.outline_tree.selection_set(candidate)
                self.outline_tree.see(candidate)
            except tk.TclError:
                pass

    def _state_file(self) -> Path:
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        return appdata / "ShiiPDFReader" / "state.json"

    def _api_config_file(self) -> Path:
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        return appdata / "ShiiPDFReader" / "api-config.json"

    def _load_api_config(self) -> dict:
        default = {
            "base_url": "https://api.openai.com/v1",
            "model": "",
            "api_key": "",
            "max_tokens": 1200,
            "context_chars": 32000,
            "temperature": 0.2,
        }
        path = self._api_config_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(data, dict):
            return default
        default["base_url"] = str(data.get("base_url") or default["base_url"])
        default["model"] = str(data.get("model") or "")
        default["api_key"] = unprotect_secret(
            str(data.get("api_key_protected") or "")
        )
        try:
            default["max_tokens"] = max(
                64, min(8192, int(data.get("max_tokens", 1200)))
            )
        except (TypeError, ValueError):
            pass
        try:
            default["context_chars"] = max(
                4000, min(120000, int(data.get("context_chars", 32000)))
            )
        except (TypeError, ValueError):
            pass
        try:
            default["temperature"] = max(
                0.0, min(2.0, float(data.get("temperature", 0.2)))
            )
        except (TypeError, ValueError):
            pass
        return default

    def _save_api_config(self) -> None:
        path = self._api_config_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "base_url": self.api_config.get("base_url", ""),
                        "model": self.api_config.get("model", ""),
                        "api_key_protected": protect_secret(
                            str(self.api_config.get("api_key", ""))
                        ),
                        "max_tokens": int(
                            self.api_config.get("max_tokens", 1200)
                        ),
                        "context_chars": int(
                            self.api_config.get("context_chars", 32000)
                        ),
                        "temperature": float(
                            self.api_config.get("temperature", 0.2)
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _api_config_ready(self) -> bool:
        return bool(
            str(self.api_config.get("base_url", "")).strip()
            and str(self.api_config.get("model", "")).strip()
        )

    def show_api_settings(self) -> None:
        if (
            self._api_settings_window is not None
            and self._api_settings_window.winfo_exists()
        ):
            self._api_settings_window.lift()
            self._api_settings_window.focus_force()
            return
        window = tk.Toplevel(self)
        self._api_settings_window = window
        window.title("API 设置")
        window.configure(background=COLORS["window"])
        window.resizable(False, False)
        window.transient(self)
        window.grab_set()
        frame = tk.Frame(window, background=COLORS["window"], padx=18, pady=16)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="兼容 OpenAI Chat Completions 的服务",
            font=(FONT, 11, "bold"),
            foreground=COLORS["text"],
            background=COLORS["window"],
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fields = [
            ("Base URL", "base_url"),
            ("模型名称", "model"),
            ("API Key", "api_key"),
            ("最大输出 token", "max_tokens"),
            ("最大上下文字符", "context_chars"),
            ("Temperature", "temperature"),
        ]
        variables: dict[str, tk.StringVar] = {}
        for row, (label, key) in enumerate(fields, 1):
            tk.Label(
                frame,
                text=label,
                font=(FONT, 10),
                foreground=COLORS["muted"],
                background=COLORS["window"],
            ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            variable = tk.StringVar()
            if key != "api_key":
                variable.set(str(self.api_config.get(key, "")))
            variables[key] = variable
            entry = tk.Entry(
                frame,
                textvariable=variable,
                width=44,
                font=(FONT, 10),
                foreground=COLORS["text"],
                insertbackground=COLORS["text"],
                background=COLORS["entry"],
                relief="flat",
                borderwidth=0,
                show="*" if key == "api_key" else "",
            )
            entry.grid(row=row, column=1, sticky="ew", pady=4, ipady=4)
        hint = (
            "API Key 会使用 Windows 用户级加密保存。"
            "留空表示保留现有密钥；本地服务可以不需要密钥。"
        )
        tk.Label(
            frame,
            text=hint,
            wraplength=480,
            justify="left",
            font=(FONT, 9),
            foreground=COLORS["muted"],
            background=COLORS["window"],
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 8))
        clear_key = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame,
            text="清除已保存的 API Key",
            variable=clear_key,
            font=(FONT, 9),
            foreground=COLORS["muted"],
            background=COLORS["window"],
            activeforeground=COLORS["text"],
            activebackground=COLORS["window"],
            selectcolor=COLORS["entry"],
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 10))
        buttons = tk.Frame(frame, background=COLORS["window"])
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(6, 0))

        def save_settings() -> None:
            base_url = variables["base_url"].get().strip().rstrip("/")
            model = variables["model"].get().strip()
            if not base_url or not model:
                messagebox.showerror(
                    "设置不完整",
                    "Base URL 和模型名称不能为空。",
                    parent=window,
                )
                return
            try:
                max_tokens = max(
                    64, min(8192, int(variables["max_tokens"].get()))
                )
                context_chars = max(
                    4000,
                    min(120000, int(variables["context_chars"].get())),
                )
                temperature = max(
                    0.0, min(2.0, float(variables["temperature"].get()))
                )
            except ValueError:
                messagebox.showerror(
                    "参数错误",
                    "Token、上下文字符和 Temperature 必须是数字。",
                    parent=window,
                )
                return
            key_text = variables["api_key"].get().strip()
            self.api_config = {
                "base_url": base_url,
                "model": model,
                "api_key": (
                    ""
                    if clear_key.get()
                    else key_text or str(self.api_config.get("api_key", ""))
                ),
                "max_tokens": max_tokens,
                "context_chars": context_chars,
                "temperature": temperature,
            }
            self._save_api_config()
            window.grab_release()
            window.destroy()
            self._api_settings_window = None
            self.status_var.set("API 设置已保存")

        ToolbarButton(buttons, "取消", window.destroy).pack(
            side="right", padx=(8, 0)
        )
        ToolbarButton(buttons, "保存", save_settings, accent=True).pack(
            side="right"
        )
        window.protocol(
            "WM_DELETE_WINDOW",
            lambda: (
                window.grab_release(),
                window.destroy(),
                setattr(self, "_api_settings_window", None),
            ),
        )
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        window.geometry(f"+{x}+{y}")

    def _load_state(self) -> dict:
        default = {
            "recent": [],
            "documents": {},
            "ui": {"sidebar_visible": True, "night_mode": False},
        }
        path = self._state_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return default
            recent = data.get("recent", [])
            documents = data.get("documents", {})
            return {
                "recent": recent if isinstance(recent, list) else [],
                "documents": documents if isinstance(documents, dict) else {},
                "ui": data.get(
                    "ui", {"sidebar_visible": True, "night_mode": False}
                )
                if isinstance(data.get("ui", {}), dict)
                else {"sidebar_visible": True, "night_mode": False},
            }
        except (OSError, ValueError, TypeError):
            return default

    def _save_state_now(self) -> None:
        self._save_job = None
        try:
            path = self._state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _schedule_save_state(self) -> None:
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except tk.TclError:
                pass
        self._save_job = self.after(450, self._remember_current_state)

    def _remember_current_state(self, *, save: bool = True) -> None:
        if self.doc is None or self.current_path is None:
            return
        key = str(self.current_path.resolve())
        previous = self.state["documents"].get(key, {})
        if not isinstance(previous, dict):
            previous = {}
        previous.update({
            "page": self.current_page,
            "view_mode": self.view_mode,
            "fit_mode": self.fit_mode,
            "rotation": self.rotation,
            "scale": self.current_scale,
            "bookmarks": self.bookmarks,
        })
        self.state["documents"][key] = previous
        recent = [item for item in self.state.get("recent", []) if item != key]
        recent.insert(0, key)
        self.state["recent"] = recent[:12]
        if save:
            self._save_state_now()

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        self.recent_menu.add_command(
            label="后退    Alt+←",
            command=lambda: self.navigate_history(-1),
            state="normal" if self.history_index > 0 else "disabled",
        )
        self.recent_menu.add_command(
            label="前进    Alt+→",
            command=lambda: self.navigate_history(1),
            state=(
                "normal"
                if 0 <= self.history_index < len(self.history) - 1
                else "disabled"
            ),
        )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(
            label="API 设置…", command=self.show_api_settings
        )
        self.recent_menu.add_separator()
        recent = [
            item
            for item in self.state.get("recent", [])
            if isinstance(item, str) and Path(item).is_file()
        ]
        if not recent:
            self.recent_menu.add_command(label="暂无最近文件", state="disabled")
            return
        for path_text in recent:
            path = Path(path_text)
            saved = self.state.get("documents", {}).get(path_text, {})
            page = int(saved.get("page", 0)) + 1 if isinstance(saved, dict) else 1
            label = f"{path.name}  ·  第 {page} 页"
            self.recent_menu.add_command(
                label=label,
                command=lambda selected=path: self.open_pdf(selected),
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="清除最近记录", command=self._clear_recent)

    def _clear_recent(self) -> None:
        self.state["recent"] = []
        self._save_state_now()
        self._refresh_recent_menu()

    def _push_history(
        self, path: str | os.PathLike[str] | None = None, page_index: int | None = None
    ) -> None:
        if path is None:
            path = self.current_path
        if path is None:
            return
        if page_index is None:
            page_index = self.current_page
        entry = (str(Path(path).resolve()), int(page_index))
        if 0 <= self.history_index < len(self.history):
            if self.history[self.history_index] == entry:
                return
        if self.history_index < len(self.history) - 1:
            self.history = self.history[: self.history_index + 1]
        self.history.append(entry)
        if len(self.history) > 100:
            self.history = self.history[-100:]
        self.history_index = len(self.history) - 1
        self._refresh_recent_menu()

    def navigate_history(self, direction: int) -> None:
        target_index = self.history_index + direction
        if not 0 <= target_index < len(self.history):
            return
        self.history_index = target_index
        path_text, page_index = self.history[target_index]
        path = Path(path_text)
        if self.current_path is not None and path.resolve() == self.current_path.resolve():
            self.go_to_page(page_index, record_history=False)
        else:
            self.open_pdf(
                path, target_page=page_index, record_history=False
            )
        self._refresh_recent_menu()

    def _on_escape(self, _event=None) -> str:
        if self.search_frame.winfo_ismapped():
            self.hide_search()
        else:
            self.exit_fullscreen()
        return "break"

    def _on_file_drop(self, event):
        try:
            dropped = list(self.tk.splitlist(event.data))
        except tk.TclError:
            dropped = [event.data]
        pdfs = [item for item in dropped if Path(item).suffix.lower() == ".pdf"]
        if pdfs:
            self.open_pdf(pdfs[0])
        elif dropped:
            messagebox.showwarning(
                "不支持的文件",
                "请拖入 PDF 文件。",
                parent=self,
            )
        return getattr(event, "action", "copy")

    def show_welcome(self) -> None:
        self.canvas.delete("all")
        self.canvas.update_idletasks()
        width = max(self.canvas.winfo_width(), 700)
        height = max(self.canvas.winfo_height(), 420)
        cx, cy = width / 2, height / 2 - 20

        self.canvas.create_rectangle(
            cx - 90,
            cy - 122,
            cx + 90,
            cy + 94,
            fill="#242a32",
            outline="#4a5563",
            width=2,
        )
        self.canvas.create_polygon(
            cx + 38,
            cy - 122,
            cx + 90,
            cy - 70,
            cx + 38,
            cy - 70,
            fill="#323b47",
            outline="#4a5563",
        )
        self.canvas.create_text(
            cx,
            cy + 4,
            text="PDF",
            font=(FONT, 25, "bold"),
            fill=COLORS["accent"],
        )
        self.canvas.create_text(
            cx,
            cy + 142,
            text="打开一份 PDF 开始阅读",
            font=(FONT, 16, "bold"),
            fill=COLORS["text"],
        )
        self.canvas.create_text(
            cx,
            cy + 174,
            text="按 Ctrl+O，或点击左上角的「打开 PDF」",
            font=(FONT, 10),
            fill=COLORS["muted"],
        )
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.status_var.set("打开一份 PDF 开始阅读")

    def open_file_dialog(self) -> None:
        initial_dir = str(self.current_path.parent) if self.current_path else ""
        path = filedialog.askopenfilename(
            parent=self,
            title="打开 PDF",
            initialdir=initial_dir,
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if path:
            self.open_pdf(path)

    def open_pdf(
        self,
        path: str | os.PathLike[str],
        *,
        target_page: int | None = None,
        record_history: bool = True,
    ) -> bool:
        source = Path(path)
        if not source.is_file():
            messagebox.showerror("无法打开", f"文件不存在：\n{source}", parent=self)
            return False
        if source.suffix.lower() != ".pdf":
            messagebox.showwarning(
                "文件类型",
                "请选择 PDF 文件。",
                parent=self,
            )
            return False

        password: str | None = None
        for _attempt in range(4):
            document = None
            try:
                document = pdfium.PdfDocument(str(source), password=password)
            except Exception as exc:
                text = str(exc).lower()
                if "password" in text or "encrypted" in text:
                    password = simpledialog.askstring(
                        "需要密码",
                        "这份 PDF 已加密，请输入打开密码：",
                        show="*",
                        parent=self,
                    )
                    if password is None:
                        return False
                    continue
                messagebox.showerror(
                    "无法打开 PDF",
                    f"文件可能已损坏或格式不受支持。\n\n{exc}",
                    parent=self,
                )
                return False

            try:
                if len(document) == 0:
                    document.close()
                    messagebox.showerror("空文件", "这份 PDF 没有可显示的页面。", parent=self)
                    return False
            except Exception:
                try:
                    document.close()
                except Exception:
                    pass
                messagebox.showerror("空文件", "无法读取这份 PDF 的页面。", parent=self)
                return False

            if self.doc is not None:
                self._remember_current_state()
                try:
                    self.doc.close()
                except Exception:
                    pass
            self.doc = document
            self.current_path = source
            self.current_password = password
            saved = self.state.get("documents", {}).get(str(source.resolve()), {})
            if not isinstance(saved, dict):
                saved = {}
            try:
                saved_page = int(saved.get("page", 0))
            except (TypeError, ValueError):
                saved_page = 0
            try:
                saved_rotation = int(saved.get("rotation", 0))
            except (TypeError, ValueError):
                saved_rotation = 0
            try:
                saved_scale = float(saved.get("scale", 1.0))
            except (TypeError, ValueError):
                saved_scale = 1.0
            self.current_page = min(max(saved_page, 0), len(self.doc) - 1)
            if target_page is not None:
                self.current_page = min(
                    max(int(target_page), 0), len(self.doc) - 1
                )
            self.rotation = saved_rotation % 360
            self.fit_mode = saved.get("fit_mode", "width")
            if self.fit_mode not in {"width", "page", None}:
                self.fit_mode = "width"
            self.view_mode = saved.get("view_mode", "single")
            if self.view_mode not in {"single", "spread", "continuous"}:
                self.view_mode = "single"
            self.view_mode_var.set(self.view_mode)
            self.current_scale = min(max(saved_scale, 0.08), 8.0)
            saved_bookmarks = saved.get("bookmarks", [])
            self.bookmarks = []
            if isinstance(saved_bookmarks, list):
                for bookmark in saved_bookmarks:
                    if not isinstance(bookmark, dict):
                        continue
                    try:
                        page_index = int(bookmark.get("page", -1))
                    except (TypeError, ValueError):
                        continue
                    if 0 <= page_index < len(self.doc):
                        self.bookmarks.append(
                            {
                                "page": page_index,
                                "label": str(
                                    bookmark.get("label")
                                    or f"第 {page_index + 1} 页"
                                ),
                            }
                        )
            self._page_sizes.clear()
            self.continuous_pages = []
            self.spread_photos = []
            self.search_matches = []
            self.search_index = -1
            self.search_count_var.set("0/0")
            self._cancel_summary()
            self._cancel_api_request()
            self._cancel_ai_extraction()
            self._ai_pages = []
            self._ai_pending_action = None
            self._api_history = []
            self._api_current_question = ""
            self.summary_text = ""
            self._summary_pages = []
            self._set_summary_text(
                "点击“生成”提取文档信息、关键词和核心句子。"
            )
            self._set_qa_text(
                "先在“API”中填写 Base URL 和模型名称，"
                "然后可以针对当前 PDF 提问。\n"
            )
            self.page_var.set(str(self.current_page + 1))
            self.total_var.set(str(len(self.doc)))
            self.title(f"{source.name} — {APP_NAME}")
            self._set_document_buttons(True)
            self._prepare_sidebar_for_document()
            self._refresh_bookmarks()
            self._load_annotations()
            self._refresh_annotation_list()
            self._set_reading_progress(self.current_page)
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
            self._remember_current_state()
            self._refresh_recent_menu()
            if record_history:
                self._push_history(source, self.current_page)
            self.render_page()
            return True

        return False

    def _cancel_render(self) -> None:
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except tk.TclError:
                pass
            self._render_job = None

    def show_search(self) -> None:
        if self.doc is None:
            return
        if not self.search_frame.winfo_ismapped():
            self.search_frame.pack(side="top", fill="x", before=self.body)
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")

    def hide_search(self) -> None:
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
            self._search_job = None
        self._search_generation += 1
        self.search_frame.pack_forget()
        self.search_matches = []
        self.search_index = -1
        self.search_count_var.set("0/0")
        self._refresh_search_results()
        if self.doc is not None:
            self.render_page()
        self.canvas.focus_set()

    def _on_search_enter(self, _event=None) -> str:
        query = self.search_var.get().strip()
        if not query:
            return "break"
        if (
            query != self._search_query_active
            or not self.search_matches
            or self.search_index < 0
        ):
            self._start_search(query)
        else:
            self.next_match()
        return "break"

    def _start_search(self, query: str) -> None:
        if self.doc is None:
            return
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
            self._search_job = None
        self._search_generation += 1
        generation = self._search_generation
        self._search_query_active = query
        self.search_matches = []
        self.search_index = -1
        self.search_count_var.set("搜索中…")
        self._refresh_search_results()
        self._search_step(generation, 0, query)

    def _search_step(self, generation: int, start_page: int, query: str) -> None:
        self._search_job = None
        if generation != self._search_generation or self.doc is None:
            return

        pages_to_check = 5
        page_index = start_page
        try:
            while page_index < self.page_count and pages_to_check > 0:
                page = self.doc[page_index]
                textpage = None
                searcher = None
                try:
                    textpage = page.get_textpage()
                    searcher = textpage.search(query, match_case=False)
                    while len(self.search_matches) < 10_000:
                        found = searcher.get_next()
                        if found is None:
                            break
                        char_index, char_count = found
                        rect = self._union_charboxes(
                            textpage, char_index, char_count
                        )
                        if rect is not None:
                            snippet_start = max(0, char_index - 22)
                            snippet_length = min(
                                textpage.count_chars() - snippet_start,
                                char_count + 54,
                            )
                            try:
                                snippet = textpage.get_text_range(
                                    snippet_start, snippet_length
                                )
                                snippet = re.sub(r"\s+", " ", snippet).strip()
                            except Exception:
                                snippet = ""
                            self.search_matches.append(
                                {
                                    "page": page_index,
                                    "char_index": char_index,
                                    "char_count": char_count,
                                    "rect": rect,
                                    "snippet": snippet,
                                }
                            )
                    if len(self.search_matches) >= 10_000:
                        break
                except Exception:
                    pass
                finally:
                    if searcher is not None:
                        try:
                            searcher.close()
                        except Exception:
                            pass
                    if textpage is not None:
                        try:
                            textpage.close()
                        except Exception:
                            pass
                    try:
                        page.close()
                    except Exception:
                        pass
                page_index += 1
                pages_to_check -= 1
                self.search_count_var.set(
                    f"{len(self.search_matches)} 项 · 扫描中"
                )
        except Exception:
            pass

        if page_index < self.page_count and len(self.search_matches) < 10_000:
            self._search_job = self.after(
                1, self._search_step, generation, page_index, query
            )
        else:
            self._finish_search(generation)

    @staticmethod
    def _union_charboxes(
        textpage: pdfium.PdfTextPage, char_index: int, char_count: int
    ) -> tuple[float, float, float, float] | None:
        lefts: list[float] = []
        bottoms: list[float] = []
        rights: list[float] = []
        tops: list[float] = []
        for index in range(char_index, char_index + char_count):
            try:
                left, bottom, right, top = textpage.get_charbox(index)
            except Exception:
                continue
            if right <= left or top <= bottom:
                continue
            lefts.append(left)
            bottoms.append(bottom)
            rights.append(right)
            tops.append(top)
        if not lefts:
            return None
        return min(lefts), min(bottoms), max(rights), max(tops)

    def _finish_search(self, generation: int) -> None:
        if generation != self._search_generation:
            return
        if not self.search_matches:
            self.search_count_var.set("0/0")
            self._refresh_search_results()
            self.status_var.set(
                f"未找到“{self._search_query_active}”；扫描版 PDF 可能没有文字层"
            )
            if self.doc is not None:
                self.render_page()
            return
        self.search_index = 0
        self.search_count_var.set(f"1/{len(self.search_matches)}")
        self._refresh_search_results()
        if self.sidebar_visible:
            self._show_sidebar_tab("search")
        self._activate_search_match()

    def previous_match(self) -> None:
        if not self.search_matches:
            self._on_search_enter()
            return
        self.search_index = (self.search_index - 1) % len(self.search_matches)
        self.search_count_var.set(
            f"{self.search_index + 1}/{len(self.search_matches)}"
        )
        self._activate_search_match()

    def next_match(self) -> None:
        if not self.search_matches:
            self._on_search_enter()
            return
        self.search_index = (self.search_index + 1) % len(self.search_matches)
        self.search_count_var.set(
            f"{self.search_index + 1}/{len(self.search_matches)}"
        )
        self._activate_search_match()

    def _activate_search_match(self) -> None:
        if not self.search_matches or self.search_index < 0:
            return
        match = self.search_matches[self.search_index]
        self.current_page = int(match["page"])
        self.page_var.set(str(self.current_page + 1))
        self._sync_search_result_selection()
        self.render_page()

    def _refresh_search_results(self) -> None:
        self.search_result_list.delete(0, "end")
        self.search_result_indices = []
        for index, match in enumerate(self.search_matches[:2000]):
            page = int(match.get("page", 0)) + 1
            snippet = str(match.get("snippet", "")).strip()
            if len(snippet) > 48:
                snippet = snippet[:48] + "…"
            self.search_result_list.insert(
                "end", f"第 {page} 页  {snippet}"
            )
            self.search_result_indices.append(index)
        self._sync_search_result_selection()

    def _sync_search_result_selection(self) -> None:
        if not self.search_result_indices:
            return
        try:
            index = self.search_result_indices.index(self.search_index)
            self.search_result_list.selection_clear(0, "end")
            self.search_result_list.selection_set(index)
            self.search_result_list.see(index)
        except (ValueError, tk.TclError):
            pass

    def _open_selected_search_result(self, _event=None) -> None:
        selection = self.search_result_list.curselection()
        if not selection:
            return
        list_index = selection[0]
        if not 0 <= list_index < len(self.search_result_indices):
            return
        self.search_index = self.search_result_indices[list_index]
        self.search_count_var.set(
            f"{self.search_index + 1}/{len(self.search_matches)}"
        )
        self._activate_search_match()

    def _set_summary_text(self, text: str) -> None:
        self.summary_view.configure(state="normal")
        self.summary_view.delete("1.0", "end")
        self.summary_view.insert("1.0", text)
        self.summary_view.configure(state="disabled")

    def _cancel_summary(self) -> None:
        if self._summary_job is not None:
            try:
                self.after_cancel(self._summary_job)
            except tk.TclError:
                pass
            self._summary_job = None
        self._summary_generation += 1

    def generate_summary(self) -> None:
        if self.doc is None:
            return
        self._cancel_summary()
        generation = self._summary_generation
        self.summary_text = ""
        self._summary_pages: list[str] = []
        self._set_summary_text(
            f"正在读取文字层…\n\n0 / {self.page_count} 页"
        )
        if self.sidebar_visible:
            self._show_sidebar_tab("summary")
        self._summary_step(generation, 0)

    def _summary_step(self, generation: int, start_page: int) -> None:
        self._summary_job = None
        if generation != self._summary_generation or self.doc is None:
            return
        page_index = start_page
        pages_to_read = 6
        try:
            while page_index < self.page_count and pages_to_read > 0:
                page = self.doc[page_index]
                textpage = None
                try:
                    textpage = page.get_textpage()
                    text = textpage.get_text_bounded()
                    self._summary_pages.append(text or "")
                except Exception:
                    self._summary_pages.append("")
                finally:
                    if textpage is not None:
                        try:
                            textpage.close()
                        except Exception:
                            pass
                    try:
                        page.close()
                    except Exception:
                        pass
                page_index += 1
                pages_to_read -= 1
                self._set_summary_text(
                    f"正在读取文字层…\n\n{page_index} / {self.page_count} 页"
                )
        except Exception:
            pass
        if page_index < self.page_count:
            self._summary_job = self.after(
                1, self._summary_step, generation, page_index
            )
        else:
            self._build_summary(generation)

    def _build_summary(self, generation: int) -> None:
        if generation != self._summary_generation or self.doc is None:
            return
        self._summary_job = None
        metadata: dict[str, str] = {}
        try:
            for key, value in self.doc.get_metadata_dict().items():
                if value is None:
                    continue
                text = str(value).strip()
                if text and text.lower() not in {
                    "untitled",
                    "anonymous",
                    "unspecified",
                    "none",
                }:
                    metadata[str(key)] = text
        except Exception:
            metadata = {}

        pages = getattr(self, "_summary_pages", [])
        text_pages = sum(1 for text in pages if text.strip())
        full_text = "\n".join(pages)
        normalized = re.sub(r"\r\n?", "\n", full_text)
        normalized = re.sub(r"[ \t]+", " ", normalized).strip()
        if not normalized:
            result = [
                "文档信息",
                f"文件：{self.current_path.name if self.current_path else ''}",
                f"页数：{self.page_count}",
                "",
                "未检测到可提取的文字层。",
                "这可能是扫描版 PDF，需要 OCR 后才能生成本地摘要。",
            ]
            self.summary_text = "\n".join(result)
            self._set_summary_text(self.summary_text)
            return

        keyword_counts, keyword_labels = self._summary_keywords(normalized)
        keywords = [keyword_labels[key] for key, _count in keyword_counts[:10]]
        sentences = self._summary_sentences(normalized, keyword_counts)
        title = metadata.get("Title") or (
            self.current_path.stem if self.current_path else "PDF 文档"
        )
        author = metadata.get("Author")
        lines = [
            "文档信息",
            f"标题：{title}",
            f"文件：{self.current_path.name if self.current_path else ''}",
            f"页数：{self.page_count}",
            f"含文字页：{text_pages} / {self.page_count}",
            f"可提取字符：{len(normalized)}",
        ]
        if author:
            lines.append(f"作者：{author}")
        if metadata.get("CreationDate"):
            lines.append(f"创建时间：{metadata['CreationDate']}")
        lines.extend(["", "关键词"])
        lines.append("、".join(keywords) if keywords else "未能提取明显关键词")
        lines.extend(["", "核心内容"])
        if sentences:
            for index, sentence in enumerate(sentences, 1):
                lines.append(f"{index}. {sentence}")
        else:
            lines.append("未能从文字中提取核心句子。")
        lines.extend(
            [
                "",
                "说明：这是本地抽取式摘要，依据 PDF 中可提取的文字和词频生成，不会上传文件。",
            ]
        )
        self.summary_text = "\n".join(lines)
        self._set_summary_text(self.summary_text)

    def _summary_keywords(
        self, text: str
    ) -> tuple[list[tuple[str, int]], dict[str, str]]:
        english_stop = {
            "the",
            "and",
            "that",
            "this",
            "with",
            "from",
            "have",
            "will",
            "your",
            "you",
            "for",
            "are",
            "was",
            "were",
            "not",
            "but",
            "can",
            "has",
            "into",
            "its",
            "their",
            "they",
            "our",
        }
        chinese_stop = {
            "我们",
            "你们",
            "他们",
            "一个",
            "可以",
            "这个",
            "那个",
            "以及",
            "因为",
            "所以",
            "如果",
            "但是",
            "进行",
            "需要",
            "没有",
            "不是",
            "就是",
            "自己",
            "什么",
            "这些",
            "那些",
            "其中",
            "通过",
            "对于",
            "已经",
            "还是",
            "可能",
            "应该",
            "不要",
            "一种",
            "方面",
        }
        counter: Counter[str] = Counter()
        labels: dict[str, str] = {}
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
            lowered = word.lower()
            if lowered not in english_stop:
                counter[f"en:{lowered}"] += 1
                labels[f"en:{lowered}"] = word
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for index in range(len(sequence) - 1):
                token = sequence[index : index + 2]
                if token not in chinese_stop:
                    counter[f"zh:{token}"] += 1
                    labels[f"zh:{token}"] = token
        filtered = [
            (key, count)
            for key, count in counter.most_common(40)
            if count >= 2
        ]
        return filtered, labels

    def _summary_sentences(
        self, text: str, keyword_counts: list[tuple[str, int]]
    ) -> list[str]:
        sentence_candidates = re.split(
            r"(?<=[。！？!?；;.!])\s+|\n+|\r+", text
        )
        candidates: list[tuple[int, str, float]] = []
        seen: set[str] = set()
        weights = {key: max(1, count) for key, count in keyword_counts}
        for index, sentence in enumerate(sentence_candidates):
            cleaned = re.sub(r"\s+", " ", sentence).strip()
            if not 18 <= len(cleaned) <= 260:
                continue
            normalized = cleaned.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            tokens = [
                f"en:{word.lower()}"
                for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", cleaned)
            ]
            for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
                tokens.extend(
                    f"zh:{sequence[i:i + 2]}"
                    for i in range(len(sequence) - 1)
                )
            score = sum(weights.get(token, 0) for token in set(tokens))
            score = score / math.sqrt(max(len(cleaned), 20))
            if index < max(1, len(sentence_candidates) // 6):
                score *= 1.18
            candidates.append((index, cleaned, score))
        selected = sorted(candidates, key=lambda item: item[2], reverse=True)[:6]
        return [sentence for _index, sentence, _score in sorted(selected)]

    def copy_summary(self) -> None:
        if not self.summary_text:
            self.generate_summary()
            return
        self.clipboard_clear()
        self.clipboard_append(self.summary_text)
        self.status_var.set("摘要已复制到剪贴板")

    def export_summary(self) -> None:
        if not self.summary_text and self.doc is not None:
            self.generate_summary()
            return
        if self.current_path is None or not self.summary_text:
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出 PDF 摘要",
            initialdir=str(self.current_path.parent),
            initialfile=f"{self.current_path.stem}-摘要.md",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.summary_text, encoding="utf-8")
            self.status_var.set(f"摘要已导出到 {Path(path).name}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def _set_qa_text(self, text: str) -> None:
        self.qa_view.configure(state="normal")
        self.qa_view.delete("1.0", "end")
        self.qa_view.insert("1.0", text)
        self.qa_view.configure(state="disabled")

    def _append_to_text_widget(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _cancel_api_request(self) -> None:
        self._api_generation += 1
        if self._api_job is not None:
            try:
                self.after_cancel(self._api_job)
            except tk.TclError:
                pass
            self._api_job = None

    def clear_qa_history(self) -> None:
        self._cancel_api_request()
        self._api_history = []
        self._api_current_question = ""
        self.qa_send_button.configure(state="normal")
        self._set_qa_text(
            "先在“API”中填写 Base URL 和模型名称，"
            "然后可以针对当前 PDF 提问。\n"
        )

    def _require_api_config(self) -> bool:
        if self._api_config_ready():
            return True
        messagebox.showinfo(
            "需要 API 设置",
            "请先在“导航 → API 设置”中填写 Base URL 和模型名称。",
            parent=self,
        )
        self.show_api_settings()
        return False

    def generate_ai_summary(self) -> None:
        if self.doc is None or not self._require_api_config():
            return
        self._cancel_summary()
        self.summary_text = ""
        self._set_summary_text("正在准备 AI 摘要…")
        self._start_ai_action({"type": "summary"})

    def send_question(self, _event=None) -> str:
        if self.doc is None or not self._require_api_config():
            return "break"
        question = self.qa_question_var.get().strip()
        if not question:
            return "break"
        self.qa_question_var.set("")
        self._append_to_text_widget(
            self.qa_view, f"\n\n你：{question}\n\nAI："
        )
        self.qa_send_button.configure(state="disabled")
        self._start_ai_action({"type": "qa", "question": question})
        return "break"

    def _cancel_ai_extraction(self) -> None:
        if self._ai_extract_job is not None:
            try:
                self.after_cancel(self._ai_extract_job)
            except tk.TclError:
                pass
            self._ai_extract_job = None
        self._ai_extract_generation += 1

    def _start_ai_action(self, action: dict) -> None:
        self._ai_pending_action = action
        if self._ai_pages and len(self._ai_pages) == self.page_count:
            self._continue_ai_action(action)
            return
        self._cancel_ai_extraction()
        generation = self._ai_extract_generation
        self._ai_pages = []
        self._extract_ai_pages_step(generation, 0)

    def _extract_ai_pages_step(self, generation: int, start_page: int) -> None:
        self._ai_extract_job = None
        if generation != self._ai_extract_generation or self.doc is None:
            return
        page_index = start_page
        pages_to_read = 6
        try:
            while page_index < self.page_count and pages_to_read > 0:
                page = self.doc[page_index]
                textpage = None
                try:
                    textpage = page.get_textpage()
                    self._ai_pages.append(textpage.get_text_bounded() or "")
                except Exception:
                    self._ai_pages.append("")
                finally:
                    if textpage is not None:
                        try:
                            textpage.close()
                        except Exception:
                            pass
                    try:
                        page.close()
                    except Exception:
                        pass
                page_index += 1
                pages_to_read -= 1
                self.status_var.set(
                    f"正在读取 PDF 文字：{page_index}/{self.page_count}"
                )
        except Exception:
            pass
        if page_index < self.page_count:
            self._ai_extract_job = self.after(
                1, self._extract_ai_pages_step, generation, page_index
            )
        else:
            action = self._ai_pending_action
            self._ai_pending_action = None
            if action is not None:
                self._continue_ai_action(action)

    def _question_tokens(self, question: str) -> set[str]:
        tokens = {
            word.lower()
            for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", question)
        }
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", question):
            tokens.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
        return tokens

    def _build_document_context(
        self, question: str | None, max_chars: int
    ) -> str:
        pages = [
            (index + 1, re.sub(r"[ \t]+", " ", text).strip())
            for index, text in enumerate(self._ai_pages)
            if text.strip()
        ]
        if not pages:
            return ""
        tokens = self._question_tokens(question or "")
        scored = []
        for page_number, text in pages:
            lowered = text.casefold()
            score = sum(lowered.count(token) for token in tokens)
            if page_number == self.current_page + 1:
                score += 12
            scored.append((score, page_number, text))
        if tokens:
            scored.sort(key=lambda item: (-item[0], item[1]))
        else:
            count = len(scored)
            stride = max(1, count // 24)
            scored = [
                (0, page, text)
                for index, (_score, page, text) in enumerate(scored)
                if index % stride == 0
            ]
            scored.sort(key=lambda item: item[1])
        selected = []
        used_chars = 0
        for _score, page_number, text in scored:
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            snippet = text[:remaining]
            selected.append((page_number, snippet))
            used_chars += len(snippet) + 12
        selected.sort(key=lambda item: item[0])
        return "\n\n".join(
            f"[Page {page_number}]\n{text}" for page_number, text in selected
        )

    def _continue_ai_action(self, action: dict) -> None:
        context = self._build_document_context(
            action.get("question"),
            int(self.api_config.get("context_chars", 32000)),
        )
        if not context:
            message = "没有检测到可提取的文字层，扫描版 PDF 需要先做 OCR。"
            if action["type"] == "summary":
                self.summary_text = message
                self._set_summary_text(message)
            else:
                self._append_to_text_widget(self.qa_view, "\n" + message)
                self.qa_send_button.configure(state="normal")
            return
        filename = self.current_path.name if self.current_path else "PDF"
        system_message = (
            "You are a careful PDF reading assistant. Use only the supplied "
            "document excerpts. Cite factual claims with page numbers in the "
            "form [p.N]. If the excerpts do not contain the answer, say so. "
            "Reply in the user's language and do not invent information."
        )
        if action["type"] == "summary":
            user_message = (
                f"Document: {filename}\n\n{context}\n\n"
                "请用中文生成结构化摘要，包含主题、关键事实、重要结论和"
                "需要留意的问题。每项尽量附页码引用，例如 [p.3]。"
            )
        else:
            self._api_current_question = str(action["question"])
            user_message = (
                f"Document: {filename}\n\n{context}\n\n"
                f"Question: {action['question']}\n"
                "请仅根据以上页面回答，并附页码引用。"
            )
        messages = [{"role": "system", "content": system_message}]
        if action["type"] == "qa":
            messages.extend(self._api_history[-6:])
        messages.append({"role": "user", "content": user_message})
        self._start_api_stream(action["type"], messages)

    def _chat_completions_url(self) -> str:
        base = str(self.api_config.get("base_url", "")).strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _start_api_stream(self, target: str, messages: list[dict]) -> None:
        self._api_generation += 1
        generation = self._api_generation
        if self._api_job is not None:
            try:
                self.after_cancel(self._api_job)
            except tk.TclError:
                pass
            self._api_job = None
        self._api_queue = queue.Queue()
        self._api_target = target
        self._api_response_text = ""
        if target == "summary":
            self.summary_text = ""
            self._set_summary_text("正在请求 AI 摘要…\n\n")
        self.qa_send_button.configure(
            state="disabled" if target == "qa" else "normal"
        )
        worker = threading.Thread(
            target=self._api_worker,
            args=(generation, messages),
            daemon=True,
        )
        worker.start()
        self._api_job = self.after(50, self._poll_api_queue, generation)

    def _api_worker(self, generation: int, messages: list[dict]) -> None:
        result_queue = self._api_queue
        try:
            payload = {
                "model": str(self.api_config.get("model", "")).strip(),
                "messages": messages,
                "stream": True,
                "max_tokens": int(self.api_config.get("max_tokens", 1200)),
                "temperature": float(
                    self.api_config.get("temperature", 0.2)
                ),
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "ShiiPDFReader/1.7",
            }
            key = str(self.api_config.get("api_key", "")).strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            request = urllib.request.Request(
                self._chat_completions_url(),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type:
                    body = response.read().decode("utf-8", errors="replace")
                    data = json.loads(body)
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if content:
                        result_queue.put(("delta", str(content)))
                    result_queue.put(("done", None))
                    return
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        break
                    data = json.loads(data_text)
                    choice = (data.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, list):
                        content = "".join(
                            str(part.get("text", ""))
                            for part in content
                            if isinstance(part, dict)
                        )
                    if content:
                        result_queue.put(("delta", str(content)))
            result_queue.put(("done", None))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")[:1200]
            except Exception:
                body = ""
            result_queue.put(
                ("error", f"API 请求失败：HTTP {exc.code}\n{body}".strip())
            )
        except urllib.error.URLError as exc:
            result_queue.put(("error", f"API 连接失败：{exc.reason}"))
        except Exception as exc:
            result_queue.put(("error", f"API 请求失败：{exc}"))

    def _poll_api_queue(self, generation: int) -> None:
        self._api_job = None
        if generation != self._api_generation:
            return
        finished = False
        request_failed = False
        try:
            while True:
                kind, value = self._api_queue.get_nowait()
                if kind == "delta":
                    chunk = str(value)
                    first_chunk = not self._api_response_text
                    self._api_response_text += chunk
                    if self._api_target == "summary":
                        if first_chunk:
                            self._set_summary_text(chunk)
                        else:
                            self._append_to_text_widget(self.summary_view, chunk)
                    else:
                        self._append_to_text_widget(self.qa_view, chunk)
                elif kind == "error":
                    self._append_to_text_widget(
                        self.summary_view if self._api_target == "summary" else self.qa_view,
                        "\n\n" + str(value),
                    )
                    self.status_var.set("API 请求失败")
                    finished = True
                    request_failed = True
                elif kind == "done":
                    finished = True
        except queue.Empty:
            pass
        if finished:
            if self._api_target == "summary":
                self.summary_text = self._api_response_text
            else:
                if self._api_current_question and not request_failed:
                    self._api_history.append(
                        {
                            "role": "user",
                            "content": self._api_current_question,
                        }
                    )
                if not request_failed:
                    self._api_history.append(
                        {
                            "role": "assistant",
                            "content": self._api_response_text,
                        }
                    )
                self._api_history = self._api_history[-8:]
                self.qa_send_button.configure(state="normal")
                self.status_var.set("AI 回答完成")
            return
        self._api_job = self.after(50, self._poll_api_queue, generation)

    def _apply_search_highlights(
        self, image: Image.Image, page_index: int, scale: float
    ) -> Image.Image:
        if not self.search_matches:
            return image
        page_matches = [
            (index, match)
            for index, match in enumerate(self.search_matches)
            if match["page"] == page_index
        ]
        if not page_matches:
            return image
        page_width, page_height = self._page_size(page_index)
        base_width = page_width * scale
        base_height = page_height * scale
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        active_index = (
            self.search_matches[self.search_index]
            if 0 <= self.search_index < len(self.search_matches)
            else None
        )
        for _global_index, match in page_matches:
            left, bottom, right, top = match["rect"]
            x1 = left * scale
            y1 = (page_height - top) * scale
            x2 = right * scale
            y2 = (page_height - bottom) * scale
            if self.rotation == 90:
                x1, y1, x2, y2 = (
                    base_height - y2,
                    x1,
                    base_height - y1,
                    x2,
                )
            elif self.rotation == 180:
                x1, y1, x2, y2 = (
                    base_width - x2,
                    base_height - y2,
                    base_width - x1,
                    base_height - y1,
                )
            elif self.rotation == 270:
                x1, y1, x2, y2 = (
                    y1,
                    base_width - x2,
                    y2,
                    base_width - x1,
                )
            x1 = max(0, min(image.width - 1, x1))
            y1 = max(0, min(image.height - 1, y1))
            x2 = max(0, min(image.width, x2))
            y2 = max(0, min(image.height, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            active = match is active_index
            draw.rectangle(
                (x1, y1, x2, y2),
                fill=(255, 203, 45, 120 if not active else 175),
                outline=(255, 129, 0, 255) if active else (220, 166, 0, 220),
                width=2 if active else 1,
            )
        return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    def _pdf_box_to_canvas(
        self,
        left: float,
        bottom: float,
        right: float,
        top: float,
        page_width: float,
        page_height: float,
        scale: float,
        rotation: int,
        offset_x: float,
        offset_y: float,
    ) -> tuple[float, float, float, float]:
        base_width = page_width * scale
        base_height = page_height * scale
        x1 = left * scale
        y1 = (page_height - top) * scale
        x2 = right * scale
        y2 = (page_height - bottom) * scale
        if rotation == 90:
            x1, y1, x2, y2 = (
                base_height - y2,
                x1,
                base_height - y1,
                x2,
            )
        elif rotation == 180:
            x1, y1, x2, y2 = (
                base_width - x2,
                base_height - y2,
                base_width - x1,
                base_height - y1,
            )
        elif rotation == 270:
            x1, y1, x2, y2 = (
                y1,
                base_width - x2,
                y2,
                base_width - x1,
            )
        return (
            offset_x + min(x1, x2),
            offset_y + min(y1, y2),
            offset_x + max(x1, x2),
            offset_y + max(y1, y2),
        )

    def _collect_page_links(
        self,
        page_index: int,
        left_offset: float,
        top_offset: float,
        scale: float,
    ) -> None:
        if self.doc is None:
            return
        page = None
        try:
            page = self.doc[page_index]
            page_width, page_height = self._page_size(page_index)
            start = ctypes.c_long(0)
            link = ctypes.POINTER(pdfium_raw.struct_fpdf_link_t__)()
            while pdfium_raw.FPDFLink_Enumerate(
                page.raw, ctypes.byref(start), ctypes.byref(link)
            ):
                rect = pdfium_raw.struct__FS_RECTF_()
                if not pdfium_raw.FPDFLink_GetAnnotRect(
                    link, ctypes.byref(rect)
                ):
                    continue
                target_page = None
                uri = None
                destination = pdfium_raw.FPDFLink_GetDest(self.doc.raw, link)
                if destination:
                    dest_index = pdfium_raw.FPDFDest_GetDestPageIndex(
                        self.doc.raw, destination
                    )
                    if dest_index >= 0:
                        target_page = int(dest_index)
                action = pdfium_raw.FPDFLink_GetAction(link)
                if (
                    action
                    and pdfium_raw.FPDFAction_GetType(action)
                    == pdfium_raw.PDFACTION_URI
                ):
                    length = pdfium_raw.FPDFAction_GetURIPath(
                        self.doc.raw, action, None, 0
                    )
                    if length > 1:
                        buffer = (ctypes.c_ubyte * length)()
                        pdfium_raw.FPDFAction_GetURIPath(
                            self.doc.raw, action, buffer, length
                        )
                        uri = bytes(buffer).rstrip(b"\0").decode(
                            "utf-8", errors="replace"
                        )
                if target_page is None and not uri:
                    continue
                x1, y1, x2, y2 = self._pdf_box_to_canvas(
                    rect.left,
                    rect.bottom,
                    rect.right,
                    rect.top,
                    page_width,
                    page_height,
                    scale,
                    self.rotation,
                    left_offset,
                    top_offset,
                )
                self.link_boxes.append(
                    {
                        "page": page_index,
                        "box": (x1, y1, x2, y2),
                        "target": target_page,
                        "uri": uri,
                    }
                )
        except Exception:
            pass
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _link_at(self, x: float, y: float) -> dict | None:
        for link in reversed(self.link_boxes):
            x1, y1, x2, y2 = link["box"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return link
        return None

    def _on_canvas_motion(self, event) -> None:
        if self._pan_start is not None or self._selecting:
            return
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if event.state & 0x0001 and self._page_rect_at(x, y) is not None:
            cursor = "xterm"
        elif self.doc is not None and self._link_at(x, y):
            cursor = "hand2"
        else:
            cursor = ""
        self.canvas.configure(cursor=cursor)

    def _activate_link_at(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        link = self._link_at(x, y)
        if not link:
            return
        target = link.get("target")
        uri = link.get("uri")
        if target is not None:
            self.go_to_page(int(target))
        elif uri:
            webbrowser.open(str(uri))

    def _page_rect_at(self, x: float, y: float) -> dict | None:
        for page_rect in self.page_rects:
            px1 = page_rect["x"]
            py1 = page_rect["y"]
            px2 = px1 + page_rect["width"]
            py2 = py1 + page_rect["height"]
            if px1 <= x <= px2 and py1 <= y <= py2:
                return page_rect
        return None

    def _clear_text_selection(self) -> None:
        self.selected_text = ""
        self._selection_pdf_rect = None
        self._selection_start = None
        self._selecting = False
        if self.selection_canvas_id is not None:
            try:
                self.canvas.delete(self.selection_canvas_id)
            except tk.TclError:
                pass
            self.selection_canvas_id = None

    def _begin_text_selection(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if self._page_rect_at(x, y) is None:
            return
        self._clear_text_selection()
        self._selecting = True
        self._selection_start = (x, y)
        self.selection_canvas_id = self.canvas.create_rectangle(
            x,
            y,
            x,
            y,
            outline=COLORS["accent"],
            fill="#4d8fc7",
            stipple="gray25",
            width=1,
            tags=("text-selection",),
        )
        self.canvas.configure(cursor="xterm")

    def _update_text_selection(self, event) -> None:
        if self._selection_start is None or self.selection_canvas_id is None:
            return
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        start_x, start_y = self._selection_start
        self.canvas.coords(
            self.selection_canvas_id,
            min(start_x, x),
            min(start_y, y),
            max(start_x, x),
            max(start_y, y),
        )

    def _finish_text_selection(self) -> None:
        if self._selection_start is None:
            return
        coords = self.canvas.coords(self.selection_canvas_id)
        self._selecting = False
        if len(coords) != 4:
            self._clear_text_selection()
            return
        x1, y1, x2, y2 = coords
        page_rect = self._page_rect_at((x1 + x2) / 2, (y1 + y2) / 2)
        if page_rect is None or x2 - x1 < 3 or y2 - y1 < 3:
            self._clear_text_selection()
            return
        self.selected_text, self._selection_pdf_rect = self._extract_text_in_rect(
            page_rect, (x1, y1, x2, y2)
        )
        if self.selected_text:
            count = len(self.selected_text)
            self.status_var.set(
                f"已选择 {count} 个字符 · Ctrl+C 复制 · 右键高亮或加便签"
            )
        else:
            self.status_var.set("所选区域没有可复制的文字")

    def _extract_text_in_rect(
        self,
        page_rect: dict,
        selection: tuple[float, float, float, float],
    ) -> tuple[str, tuple[float, float, float, float] | None]:
        if self.doc is None:
            return "", None
        page_index = int(page_rect["page"])
        page_width, page_height = self._page_size(page_index)
        page = None
        textpage = None
        try:
            page = self.doc[page_index]
            textpage = page.get_textpage()
            selected_indices: list[int] = []
            boxes: list[tuple[float, float, float, float]] = []
            sx1, sy1, sx2, sy2 = selection
            for index in range(textpage.count_chars()):
                try:
                    left, bottom, right, top = textpage.get_charbox(index)
                except Exception:
                    continue
                cx1, cy1, cx2, cy2 = self._pdf_box_to_canvas(
                    left,
                    bottom,
                    right,
                    top,
                    page_width,
                    page_height,
                    page_rect["scale"],
                    self.rotation,
                    page_rect["x"],
                    page_rect["y"],
                )
                if cx2 < sx1 or cx1 > sx2 or cy2 < sy1 or cy1 > sy2:
                    continue
                selected_indices.append(index)
                boxes.append((left, bottom, right, top))
            if not selected_indices:
                return "", None
            ranges: list[tuple[int, int]] = []
            range_start = selected_indices[0]
            previous = selected_indices[0]
            for index in selected_indices[1:]:
                if index == previous + 1:
                    previous = index
                    continue
                ranges.append((range_start, previous - range_start + 1))
                range_start = index
                previous = index
            ranges.append((range_start, previous - range_start + 1))
            text = "".join(
                textpage.get_text_range(start, count)
                for start, count in ranges
            )
            pdf_rect = (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
            return text.strip(), pdf_rect
        except Exception:
            return "", None
        finally:
            if textpage is not None:
                try:
                    textpage.close()
                except Exception:
                    pass
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _copy_selected_text(self, _event=None) -> str | None:
        focus = self.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text)):
            return None
        if not self.selected_text:
            return None
        self.clipboard_clear()
        self.clipboard_append(self.selected_text)
        self.status_var.set(f"已复制 {len(self.selected_text)} 个字符")
        return "break"

    def _show_selection_menu(self, event) -> None:
        if not self.selected_text:
            return
        menu = tk.Menu(
            self,
            tearoff=False,
            font=(FONT, 10),
            foreground=COLORS["text"],
            background=COLORS["toolbar"],
            activeforeground="#0d1720",
            activebackground=COLORS["accent_hover"],
        )
        menu.add_command(label="复制选中文字", command=self._copy_selected_text)
        menu.add_separator()
        menu.add_command(
            label="黄色高亮",
            command=lambda: self._create_annotation("yellow", ""),
        )
        menu.add_command(
            label="绿色高亮",
            command=lambda: self._create_annotation("green", ""),
        )
        menu.add_command(
            label="蓝色高亮",
            command=lambda: self._create_annotation("blue", ""),
        )
        menu.add_command(
            label="粉色便签",
            command=self.add_note_to_selection,
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def render_page(self) -> None:
        if self.doc is None:
            return
        self._cancel_render()
        if self.view_mode == "continuous":
            self._render_job = self.after(20, self._render_continuous_now)
        elif self.view_mode == "spread":
            self._render_job = self.after(20, self._render_spread_now)
        else:
            self._render_job = self.after(20, self._render_now)

    def _render_now(self) -> None:
        self._render_job = None
        if self.doc is None:
            return
        self._clear_text_selection()
        self.current_page = min(max(self.current_page, 0), self.page_count - 1)
        self.page_var.set(str(self.current_page + 1))
        self.status_var.set(f"正在渲染第 {self.current_page + 1} 页…")
        self.update_idletasks()

        page = None
        bitmap = None
        try:
            page = self.doc[self.current_page]
            raw_width, raw_height = page.get_size()
            if self.rotation in (90, 270):
                display_width, display_height = raw_height, raw_width
            else:
                display_width, display_height = raw_width, raw_height

            canvas_width = max(self.canvas.winfo_width(), 200) - 60
            canvas_height = max(self.canvas.winfo_height(), 200) - 60

            if self.fit_mode == "width":
                scale = canvas_width / display_width
            elif self.fit_mode == "page":
                scale = min(
                    canvas_width / display_width,
                    canvas_height / display_height,
                )
            else:
                scale = self.current_scale

            scale = max(0.08, min(scale, 8.0))
            max_pixels = 24_000_000
            estimated = (display_width * scale) * (display_height * scale)
            if estimated > max_pixels:
                scale = math.sqrt(max_pixels / (display_width * display_height))

            self.current_scale = scale
            self.zoom_var.set(f"{round(scale * 100)}%")
            bitmap = page.render(
                scale=scale,
                rotation=self.rotation,
                draw_annots=True,
                optimize_mode="lcd",
                rev_byteorder=True,
            )
            image = bitmap.to_pil().convert("RGB").copy()
            image = self._apply_reading_mode(image)
            image = self._apply_annotation_highlights(
                image, self.current_page, scale
            )
            image = self._apply_search_highlights(image, self.current_page, scale)

            self.photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            canvas_width = max(self.canvas.winfo_width(), image.width + 48)
            canvas_height = max(self.canvas.winfo_height(), image.height + 48)
            x = (canvas_width - image.width) / 2
            y = 24
            self.page_rects = [
                {
                    "page": self.current_page,
                    "x": x,
                    "y": y,
                    "width": image.width,
                    "height": image.height,
                    "scale": scale,
                }
            ]
            self.link_boxes = []
            self._collect_page_links(self.current_page, x, y, scale)

            self.canvas.create_rectangle(
                x - 7,
                y - 7,
                x + image.width + 7,
                y + image.height + 7,
                fill=COLORS["shadow"],
                outline="",
            )
            self.canvas.create_image(
                x,
                y,
                anchor="nw",
                image=self.photo,
                tags=("page",),
            )
            self.canvas.configure(scrollregion=(0, 0, canvas_width, canvas_height))

            size_text = format_size(self.current_path.stat().st_size) if self.current_path else ""
            name = self.current_path.name if self.current_path else ""
            self.status_var.set(
                f"{name}  ·  第 {self.current_page + 1} / {self.page_count} 页"
                f"  ·  {image.width} × {image.height} 像素  ·  {size_text}"
            )
            self.prev_button.configure(
                state="normal" if self.current_page > 0 else "disabled"
            )
            self.next_button.configure(
                state="normal"
                if self.current_page < self.page_count - 1
                else "disabled"
            )
            self._set_reading_progress(self.current_page)
            self._schedule_save_state()
        except Exception as exc:
            self.canvas.delete("all")
            self.canvas.create_text(
                max(self.canvas.winfo_width(), 400) / 2,
                max(self.canvas.winfo_height(), 300) / 2,
                text=f"这一页无法显示\n{exc}",
                justify="center",
                font=(FONT, 12),
                fill="#ef9a9a",
            )
            self.status_var.set("页面渲染失败")
        finally:
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception:
                    pass
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _render_spread_now(self) -> None:
        self._render_job = None
        if self.doc is None or self.view_mode != "spread":
            return
        self._clear_text_selection()

        self.current_page = min(max(self.current_page, 0), self.page_count - 1)
        pair_start = (self.current_page // 2) * 2
        page_indices = [pair_start]
        if pair_start + 1 < self.page_count:
            page_indices.append(pair_start + 1)

        self.page_var.set(str(self.current_page + 1))
        self.status_var.set("正在排版双页视图…")
        self.update_idletasks()
        self.canvas.delete("all")
        self.link_boxes = []
        self.page_rects = []
        self.spread_photos = []

        try:
            display_sizes = [
                self._display_page_size(index) for index in page_indices
            ]
            gap = 22
            canvas_width = max(self.canvas.winfo_width(), 500)
            canvas_height = max(self.canvas.winfo_height(), 300)
            available_width = max(canvas_width - 82 - gap, 200)
            available_height = max(canvas_height - 86, 200)
            combined_width = sum(width for width, _height in display_sizes)
            max_height = max(height for _width, height in display_sizes)

            if self.fit_mode == "width":
                scale = available_width / combined_width
            elif self.fit_mode == "page":
                scale = min(
                    available_width / combined_width,
                    available_height / max_height,
                )
            else:
                scale = self.current_scale
            scale = max(0.08, min(scale, 8.0))
            max_page_area = max(width * height for width, height in display_sizes)
            if max_page_area * scale * scale > 24_000_000:
                scale = math.sqrt(24_000_000 / max_page_area)
            self.current_scale = scale
            self.zoom_var.set(f"{round(scale * 100)}%")

            rendered: list[tuple[int, Image.Image]] = []
            for page_index in page_indices:
                page = None
                bitmap = None
                try:
                    page = self.doc[page_index]
                    bitmap = page.render(
                        scale=scale,
                        rotation=self.rotation,
                        draw_annots=True,
                        optimize_mode="lcd",
                        rev_byteorder=True,
                    )
                    rendered.append(
                        (page_index, bitmap.to_pil().convert("RGB").copy())
                    )
                finally:
                    if bitmap is not None:
                        try:
                            bitmap.close()
                        except Exception:
                            pass
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass

            total_width = sum(image.width for _index, image in rendered)
            total_width += gap * max(0, len(rendered) - 1)
            content_width = max(canvas_width, total_width + 82)
            max_image_height = max(image.height for _index, image in rendered)
            content_height = max(canvas_height, max_image_height + 86)
            x = (content_width - total_width) / 2
            y = (
                (content_height - max_image_height) / 2
                if self.fit_mode == "page"
                else 48
            )

            for page_index, image in rendered:
                image = self._apply_reading_mode(image)
                image = self._apply_annotation_highlights(
                    image, page_index, scale
                )
                image = self._apply_search_highlights(image, page_index, scale)
                self.page_rects.append(
                    {
                        "page": page_index,
                        "x": x,
                        "y": y,
                        "width": image.width,
                        "height": image.height,
                        "scale": scale,
                    }
                )
                self._collect_page_links(page_index, x, y, scale)
                self.canvas.create_text(
                    x + image.width / 2,
                    y - 12,
                    text=str(page_index + 1),
                    font=(FONT, 9),
                    fill="#76818e",
                )
                self.canvas.create_rectangle(
                    x - 7,
                    y - 7,
                    x + image.width + 7,
                    y + image.height + 7,
                    fill=COLORS["shadow"],
                    outline="",
                )
                photo = ImageTk.PhotoImage(image)
                self.spread_photos.append(photo)
                self.canvas.create_image(
                    x, y, anchor="nw", image=photo, tags=("page",)
                )
                x += image.width + gap

            self.canvas.configure(
                scrollregion=(0, 0, content_width, content_height)
            )
            pair_text = (
                f"{page_indices[0] + 1}"
                if len(page_indices) == 1
                else f"{page_indices[0] + 1}–{page_indices[-1] + 1}"
            )
            name = self.current_path.name if self.current_path else ""
            size_text = (
                format_size(self.current_path.stat().st_size)
                if self.current_path
                else ""
            )
            self.status_var.set(
                f"{name}  ·  双页并排  ·  第 {pair_text} / {self.page_count} 页"
                f"  ·  {size_text}"
            )
            self._update_page_buttons()
            self._set_reading_progress(self.current_page)
            self._schedule_save_state()
        except Exception as exc:
            self.canvas.delete("all")
            self.canvas.create_text(
                max(self.canvas.winfo_width(), 400) / 2,
                max(self.canvas.winfo_height(), 300) / 2,
                text=f"双页视图创建失败\n{exc}",
                justify="center",
                font=(FONT, 12),
                fill="#ef9a9a",
            )
            self.status_var.set("双页视图创建失败")

    def set_view_mode(self, mode: str) -> None:
        if self.doc is None:
            return
        if mode not in {"single", "spread", "continuous"}:
            return
        self.current_page = min(max(self.current_page, 0), self.page_count - 1)
        self.view_mode = mode
        self.view_mode_var.set(mode)

        self._cancel_render()
        self.continuous_pages = []
        self.photo = None
        self.spread_photos = []
        self.canvas.delete("all")
        self.render_page()

    def toggle_view_mode(self) -> None:
        if self.doc is None:
            return
        modes = ["single", "spread", "continuous"]
        current = modes.index(self.view_mode)
        self.set_view_mode(modes[(current + 1) % len(modes)])

    def _page_size(self, page_index: int) -> tuple[float, float]:
        cached = self._page_sizes.get(page_index)
        if cached is not None:
            return cached
        page = self.doc[page_index]
        try:
            width, height = page.get_size()
        finally:
            page.close()
        self._page_sizes[page_index] = (width, height)
        return width, height

    def _display_page_size(self, page_index: int) -> tuple[float, float]:
        width, height = self._page_size(page_index)
        if self.rotation in (90, 270):
            return height, width
        return width, height

    def _calculate_continuous_scale(
        self, display_sizes: list[tuple[float, float]]
    ) -> float:
        canvas_width = max(self.canvas.winfo_width(), 300) - 72
        canvas_height = max(self.canvas.winfo_height(), 300) - 72
        max_width = max(width for width, _height in display_sizes)
        max_height = max(height for _width, height in display_sizes)

        if self.fit_mode == "width":
            scale = canvas_width / max_width
        elif self.fit_mode == "page":
            scale = min(canvas_width / max_width, canvas_height / max_height)
        else:
            scale = self.current_scale

        scale = max(0.08, min(scale, 8.0))
        max_pixels = 24_000_000
        max_page_area = max(width * height for width, height in display_sizes)
        if max_page_area * scale * scale > max_pixels:
            scale = math.sqrt(max_pixels / max_page_area)
        return scale

    def _render_continuous_now(self) -> None:
        self._render_job = None
        if self.doc is None or self.view_mode != "continuous":
            return
        self._clear_text_selection()

        self.current_page = min(max(self.current_page, 0), self.page_count - 1)
        self.page_var.set(str(self.current_page + 1))
        self.status_var.set("正在排版多页视图…")
        self.update_idletasks()

        self.continuous_pages = []
        self.canvas.delete("all")
        self.link_boxes = []
        self.page_rects = []
        try:
            display_sizes = [
                self._display_page_size(index) for index in range(self.page_count)
            ]
            scale = self._calculate_continuous_scale(display_sizes)
            self.current_scale = scale
            self.zoom_var.set(f"{round(scale * 100)}%")

            canvas_width = max(self.canvas.winfo_width(), 500)
            scaled_widths = [width * scale for width, _height in display_sizes]
            content_width = max(canvas_width, max(scaled_widths) + 72)
            margin = 36
            gap = 30
            label_height = 22
            y = 24.0

            for index, (display_width, display_height) in enumerate(display_sizes):
                page_width = display_width * scale
                page_height = display_height * scale
                x = (content_width - page_width) / 2
                label_y = y
                page_y = label_y + label_height

                self.canvas.create_text(
                    content_width / 2,
                    label_y + 7,
                    text=str(index + 1),
                    font=(FONT, 9),
                    fill="#76818e",
                    tags=("page-label", f"page-{index}"),
                )
                self.canvas.create_rectangle(
                    x - 7,
                    page_y - 7,
                    x + page_width + 7,
                    page_y + page_height + 7,
                    fill=COLORS["shadow"],
                    outline="",
                    tags=("page-shadow", f"page-{index}"),
                )
                frame_id = self.canvas.create_rectangle(
                    x,
                    page_y,
                    x + page_width,
                    page_y + page_height,
                    fill="#f5f6f7",
                    outline="#343b45",
                    width=1,
                    tags=("page-frame", f"page-{index}"),
                )
                placeholder_id = self.canvas.create_text(
                    x + page_width / 2,
                    page_y + page_height / 2,
                    text=f"第 {index + 1} 页",
                    font=(FONT, 12),
                    fill="#8b96a3",
                    tags=("page-placeholder", f"page-{index}"),
                )
                self.continuous_pages.append(
                    {
                        "index": index,
                        "x": x,
                        "y": page_y,
                        "width": page_width,
                        "height": page_height,
                        "label_y": label_y,
                        "bottom": page_y + page_height,
                        "frame_id": frame_id,
                        "placeholder_id": placeholder_id,
                        "image_id": None,
                        "photo": None,
                        "rendered_key": None,
                        "error": None,
                    }
                )
                y = page_y + page_height + gap

            total_height = max(y, self.canvas.winfo_height())
            self.canvas.configure(scrollregion=(0, 0, content_width, total_height))
            self.canvas.update_idletasks()
            target = self.continuous_pages[self.current_page]
            self.canvas.yview_moveto(
                max(0.0, target["label_y"] - 12) / max(total_height, 1)
            )
            self.canvas.xview_moveto(0)
            self._render_visible_continuous()
            self._update_continuous_status()
            self._update_page_buttons()
            self._set_reading_progress(self.current_page)
            self._schedule_save_state()
        except Exception as exc:
            self.canvas.delete("all")
            self.canvas.create_text(
                max(self.canvas.winfo_width(), 400) / 2,
                max(self.canvas.winfo_height(), 300) / 2,
                text=f"多页视图创建失败\n{exc}",
                justify="center",
                font=(FONT, 12),
                fill="#ef9a9a",
            )
            self.status_var.set("多页视图创建失败")

    def _visible_continuous_pages(self) -> list[dict]:
        if not self.continuous_pages:
            return []
        top = self.canvas.canvasy(0) - 500
        bottom = top + max(self.canvas.winfo_height(), 300) + 1000
        return [
            page
            for page in self.continuous_pages
            if page["bottom"] >= top and page["y"] <= bottom
        ]

    def _render_visible_continuous(self) -> None:
        if (
            self.doc is None
            or self.view_mode != "continuous"
            or self._continuous_rendering
            or not self.continuous_pages
        ):
            return
        self._continuous_rendering = True
        try:
            visible = self._visible_continuous_pages()
            visible_indices = {page["index"] for page in visible}
            if visible_indices:
                keep_from = max(0, min(visible_indices) - 1)
                keep_to = min(self.page_count - 1, max(visible_indices) + 1)
            else:
                keep_from, keep_to = -1, -1

            for page_data in self.continuous_pages:
                if (
                    page_data["image_id"] is not None
                    and not (keep_from <= page_data["index"] <= keep_to)
                ):
                    self.canvas.delete(page_data["image_id"])
                    page_data["image_id"] = None
                    page_data["photo"] = None
                    page_data["rendered_key"] = None
                    self.link_boxes = [
                        link
                        for link in self.link_boxes
                        if link["page"] != page_data["index"]
                    ]
                    self.page_rects = [
                        rect
                        for rect in self.page_rects
                        if rect["page"] != page_data["index"]
                    ]
                    self.canvas.itemconfigure(
                        page_data["placeholder_id"], state="normal"
                    )

            rendered_any = False
            for page_data in visible:
                render_key = (round(self.current_scale, 4), self.rotation)
                if page_data["rendered_key"] == render_key:
                    continue
                if page_data["error"] is not None:
                    continue

                page = None
                bitmap = None
                try:
                    page = self.doc[page_data["index"]]
                    bitmap = page.render(
                        scale=self.current_scale,
                        rotation=self.rotation,
                        draw_annots=True,
                        optimize_mode="lcd",
                        rev_byteorder=True,
                    )
                    image = bitmap.to_pil().convert("RGB").copy()
                    image = self._apply_reading_mode(image)
                    image = self._apply_annotation_highlights(
                        image, page_data["index"], self.current_scale
                    )
                    image = self._apply_search_highlights(
                        image, page_data["index"], self.current_scale
                    )
                    photo = ImageTk.PhotoImage(image)
                    if page_data["image_id"] is not None:
                        self.canvas.delete(page_data["image_id"])
                    page_data["image_id"] = self.canvas.create_image(
                        page_data["x"],
                        page_data["y"],
                        anchor="nw",
                        image=photo,
                        tags=("page-image", f"page-{page_data['index']}"),
                    )
                    page_data["photo"] = photo
                    page_data["rendered_key"] = render_key
                    self.page_rects.append(
                        {
                            "page": page_data["index"],
                            "x": page_data["x"],
                            "y": page_data["y"],
                            "width": page_data["width"],
                            "height": page_data["height"],
                            "scale": self.current_scale,
                        }
                    )
                    self._collect_page_links(
                        page_data["index"],
                        page_data["x"],
                        page_data["y"],
                        self.current_scale,
                    )
                    self.canvas.itemconfigure(
                        page_data["placeholder_id"], state="hidden"
                    )
                    rendered_any = True
                except Exception as exc:
                    page_data["error"] = str(exc)
                    self.canvas.itemconfigure(
                        page_data["placeholder_id"],
                        text=f"第 {page_data['index'] + 1} 页无法显示",
                    )
                finally:
                    if bitmap is not None:
                        try:
                            bitmap.close()
                        except Exception:
                            pass
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass

            if rendered_any:
                self._update_continuous_status()
        finally:
            self._continuous_rendering = False

    def _schedule_continuous_refresh(self) -> None:
        if self.view_mode != "continuous" or self.doc is None:
            return
        if self._scroll_job is not None:
            try:
                self.after_cancel(self._scroll_job)
            except tk.TclError:
                pass
        self._scroll_job = self.after(70, self._refresh_continuous_view)

    def _refresh_continuous_view(self) -> None:
        self._scroll_job = None
        if self.view_mode != "continuous" or self.doc is None:
            return
        self._update_current_page_from_scroll()
        self._render_visible_continuous()
        self._update_continuous_status()
        self._update_page_buttons()

    def _update_current_page_from_scroll(self) -> None:
        if not self.continuous_pages:
            return
        viewport_center = self.canvas.canvasy(
            max(self.canvas.winfo_height(), 300) / 2
        )
        best_page = self.continuous_pages[self.current_page]
        best_distance = float("inf")
        for page_data in self.continuous_pages:
            if page_data["y"] <= viewport_center <= page_data["bottom"]:
                best_page = page_data
                break
            distance = min(
                abs(viewport_center - page_data["y"]),
                abs(viewport_center - page_data["bottom"]),
            )
            if distance < best_distance:
                best_distance = distance
                best_page = page_data
        if best_page["index"] != self.current_page:
            self.current_page = best_page["index"]
            self.page_var.set(str(self.current_page + 1))
            self._set_reading_progress(self.current_page)
            self._schedule_save_state()

    def _update_continuous_status(self) -> None:
        if self.doc is None:
            return
        name = self.current_path.name if self.current_path else ""
        size_text = format_size(self.current_path.stat().st_size) if self.current_path else ""
        self.status_var.set(
            f"{name}  ·  多页连续阅读  ·  第 {self.current_page + 1} / {self.page_count} 页"
            f"  ·  {size_text}"
        )

    def _update_page_buttons(self) -> None:
        if self.doc is None:
            return
        pair_start = (self.current_page // 2) * 2
        if self.view_mode == "spread":
            has_previous = pair_start > 0
            has_next = pair_start + 2 < self.page_count
        else:
            has_previous = self.current_page > 0
            has_next = self.current_page < self.page_count - 1
        self.prev_button.configure(
            state="normal" if has_previous else "disabled"
        )
        self.next_button.configure(
            state="normal" if has_next else "disabled"
        )

    def _scroll_to_continuous_page(self, page_index: int) -> None:
        if not self.continuous_pages:
            return
        page_index = min(max(page_index, 0), self.page_count - 1)
        page_data = self.continuous_pages[page_index]
        scrollregion = self.canvas.cget("scrollregion").split()
        total_height = float(scrollregion[3]) if len(scrollregion) == 4 else 1.0
        self.canvas.yview_moveto(
            max(0.0, page_data["label_y"] - 12) / max(total_height, 1.0)
        )
        self.canvas.xview_moveto(0)
        self.current_page = page_index
        self.page_var.set(str(page_index + 1))
        self._schedule_continuous_refresh()

    def go_to_page(self, page_index: int, *, record_history: bool = True) -> None:
        if self.doc is None:
            return
        page_index = min(max(int(page_index), 0), self.page_count - 1)
        if record_history:
            self._push_history(page_index=page_index)
        if self.view_mode == "continuous":
            self._scroll_to_continuous_page(page_index)
            self._update_page_buttons()
            return
        if self.view_mode == "spread":
            self.current_page = page_index
            self.page_var.set(str(page_index + 1))
            self.render_page()
            return
        if page_index == self.current_page:
            self.page_var.set(str(page_index + 1))
            return
        self.current_page = page_index
        self.page_var.set(str(self.current_page + 1))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.render_page()

    def previous_page(self) -> None:
        if self.view_mode == "spread":
            pair_start = (self.current_page // 2) * 2
            self.go_to_page(max(0, pair_start - 2))
            return
        self.go_to_page(self.current_page - 1)

    def next_page(self) -> None:
        if self.view_mode == "spread":
            pair_start = (self.current_page // 2) * 2
            self.go_to_page(min(self.page_count - 1, pair_start + 2))
            return
        self.go_to_page(self.current_page + 1)

    def _jump_to_entered_page(self, _event=None) -> str:
        if self.doc is None:
            return "break"
        try:
            requested = int(self.page_var.get().strip())
        except ValueError:
            self.page_var.set(str(self.current_page + 1))
            return "break"
        self.go_to_page(requested - 1)
        self.canvas.focus_set()
        return "break"

    def _restore_page_entry(self, _event=None) -> None:
        self.page_var.set(str(self.current_page + 1))

    def zoom_in(self) -> None:
        if self.doc is None:
            return
        self.fit_mode = None
        self.current_scale = min(8.0, max(0.12, self.current_scale * 1.2))
        self.render_page()

    def zoom_out(self) -> None:
        if self.doc is None:
            return
        self.fit_mode = None
        self.current_scale = max(0.08, self.current_scale / 1.2)
        self.render_page()

    def fit_width(self) -> None:
        if self.doc is None:
            return
        self.fit_mode = "width"
        self.render_page()

    def fit_page(self) -> None:
        if self.doc is None:
            return
        self.fit_mode = "page"
        self.render_page()

    def rotate_page(self) -> None:
        if self.doc is None:
            return
        self.rotation = (self.rotation + 90) % 360
        self._clear_thumbnails()
        if self.sidebar_visible:
            self._start_thumbnails()
        self.render_page()

    def toggle_night_mode(self) -> None:
        self.night_mode = bool(self.night_mode_var.get())
        self.state.setdefault("ui", {})["night_mode"] = self.night_mode
        self._save_state_now()
        self.status_var.set(
            "夜间护眼模式已开启" if self.night_mode else "夜间护眼模式已关闭"
        )
        self.render_page()

    def _apply_reading_mode(self, image: Image.Image) -> Image.Image:
        if not self.night_mode:
            return image
        grayscale = image.convert("L")
        inverted = ImageOps.invert(grayscale)
        return ImageOps.colorize(
            inverted,
            black="#191b20",
            white="#e2d7c2",
            mid="#8e877a",
        ).convert("RGB")

    def _on_canvas_configure(self, event) -> None:
        size = (event.width, event.height)
        if size == self._last_canvas_size:
            return
        self._last_canvas_size = size
        if self.doc is None:
            self.after_idle(self.show_welcome)
            return
        if self.fit_mode:
            if self._resize_job is not None:
                try:
                    self.after_cancel(self._resize_job)
                except tk.TclError:
                    pass
            self._resize_job = self.after(180, self._rerender_after_resize)

    def _rerender_after_resize(self) -> None:
        self._resize_job = None
        if self.doc is not None and self.fit_mode:
            self.render_page()

    def _on_vertical_scrollbar(self, *args) -> None:
        self.canvas.yview(*args)
        self._schedule_continuous_refresh()

    def _scroll_vertical(self, units: int) -> str:
        self.canvas.yview_scroll(units, "units")
        self._schedule_continuous_refresh()
        return "break"

    def _on_mousewheel(self, event) -> str:
        if event.state & 0x0004:
            if event.delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
        else:
            self.canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
            self._schedule_continuous_refresh()
        return "break"

    def _on_shift_mousewheel(self, event) -> str:
        self.canvas.xview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def _start_pan(self, event) -> None:
        if self.doc is not None:
            if event.state & 0x0001:
                self._begin_text_selection(event)
                return
            self._clear_text_selection()
            self._pan_start = (event.x, event.y)
            self._pan_moved = False
            self.canvas.scan_mark(event.x, event.y)

    def _drag_pan(self, event) -> None:
        if self._selecting:
            self._update_text_selection(event)
            return
        if self.doc is not None:
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            if self._pan_start is not None:
                dx = event.x - self._pan_start[0]
                dy = event.y - self._pan_start[1]
                if dx * dx + dy * dy > 16:
                    self._pan_moved = True
                    self.canvas.configure(cursor="fleur")
            self._schedule_continuous_refresh()

    def _end_pan(self, event=None) -> None:
        if self._selecting:
            self._finish_text_selection()
            self._pan_start = None
            self.canvas.configure(cursor="")
            return
        if self.doc is not None:
            if event is not None and not self._pan_moved:
                self._activate_link_at(event)
            self._pan_start = None
            self.canvas.configure(cursor="")
            self._schedule_continuous_refresh()

    def _on_left_key(self, _event=None) -> str:
        if self.focus_get() is not self.page_entry:
            self.previous_page()
        return "break"

    def _on_right_key(self, _event=None) -> str:
        if self.focus_get() is not self.page_entry:
            self.next_page()
        return "break"

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        self.fullscreen_button.configure(
            text="退出全屏" if self._fullscreen else "全屏"
        )

    def exit_fullscreen(self) -> None:
        if self._fullscreen:
            self._fullscreen = False
            self.attributes("-fullscreen", False)
            self.fullscreen_button.configure(text="全屏")

    def _on_close(self) -> None:
        self._remember_current_state()
        for job_name in (
            "_save_job",
            "_search_job",
            "_summary_job",
            "_ai_extract_job",
            "_api_job",
            "_thumb_job",
            "_render_job",
            "_resize_job",
            "_scroll_job",
        ):
            job = getattr(self, job_name, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, job_name, None)
        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass
            self.doc = None
        self.destroy()


def main() -> None:
    enable_windows_integration()
    app = SimplePDFReader()
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1])
        if candidate.suffix.lower() == ".pdf":
            app.after(120, lambda: app.open_pdf(candidate))
    app.mainloop()


if __name__ == "__main__":
    main()
