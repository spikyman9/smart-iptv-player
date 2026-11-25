"""
Smart IPTV Player & Recorder Pro
High-performance desktop IPTV player supporting Live TV, Movies (VOD), and Series,
featuring instant disk caching, fast timeline seek bar, double-click fullscreen, and stream recording.
"""

import os
import sys
import queue
import threading
import time
import urllib.parse
from typing import Optional
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

from m3u_parser import M3UParser, Channel
from player_engine import VLCPlayerEngine
from recorder import StreamRecorder
from storage import StorageManager

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")
UI_FONT = "Segoe UI"
ctk.ThemeManager.theme["CTkFont"]["family"] = UI_FONT
ctk.deactivate_automatic_dpi_awareness()

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = APP_DIR


def format_ms(ms: int) -> str:
    """Format milliseconds into HH:MM:SS or MM:SS."""
    if ms < 0:
        return "00:00"
    total_sec = int(ms // 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class SmartIPTVApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Smart IPTV Player & Recorder Pro")
        self.geometry("1340x860")
        self.minsize(1060, 680)

        # Windows taskbar AppUserModelID + custom app icon
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SmartIPTVPlayer.Pro.App.1")
            except Exception:
                pass

        icon_path = os.path.join(BUNDLE_DIR, "app_icon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(APP_DIR, "app_icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
                self.after(250, lambda p=icon_path: self.iconbitmap(p))
            except Exception:
                pass

        # Thread-safe message queue for UI events
        self.msg_queue = queue.Queue()

        # Storage & Recorder
        self.storage = StorageManager(APP_DIR)
        record_dir = self.storage.get_setting("recordings_dir")
        self.recorder = StreamRecorder(output_dir=record_dir)

        # Active Mode: 'live', 'vod', or 'series'
        self.current_mode: str = "live"

        # Content Cache: { 'live': {...}, 'vod': {...}, 'series': {...} }
        self.content_cache = {
            "live": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False},
            "vod": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False},
            "series": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False}
        }

        # Ephemeral State
        self.current_playlist_target: str = ""
        self.displayed_items: list[Channel] = []
        self.current_channel: Channel | None = None
        self.current_category: str = "All"
        self.cat_search_query: str = ""
        self.chan_search_query: str = ""
        self.is_fullscreen = False
        self.sidebar_visible = True
        self.record_format = self.storage.get_setting("record_format", "mp4")
        self.displayed_limit = 40
        self.displayed_cat_limit = 50
        self._cat_search_timer = None
        self._chan_search_timer = None
        self._vol_save_timer = None
        self._load_request_id = 0
        self._cache_generation = 0
        self._prefetch_started_for: str = ""

        # Category and channel rows cache & widget pools for zero-destroy O(1) rendering
        self.cat_rows: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self.channel_rows: dict[int, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self._cat_pool: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        self._chan_pool: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        self._cat_more_btn: Optional[tk.Label] = None
        self._chan_more_btn: Optional[tk.Label] = None
        self._chan_empty_lbl: Optional[tk.Label] = None
        self._hovered_cat = None
        self._hovered_chan = None

        # Seek bar dragging state flag
        self.is_seeking = False

        # Floating fullscreen OSD state
        self._fs_osd = None
        self._fs_osd_visible = False
        self._fs_osd_poll_id = None
        self._fs_osd_hide_at = 0.0

        # Series sub-view state
        self.in_episodes_view = False
        self.current_series_name = ""

        # Build UI layout
        self._setup_ui()

        # Initialize VLC Engine
        self.update_idletasks()
        self.player = VLCPlayerEngine(hwnd=self.video_frame.winfo_id())

        # Set initial volume & aspect ratio
        saved_vol = self.storage.get_setting("volume", 85)
        self.vol_slider.set(saved_vol)
        self.player.set_volume(saved_vol)
        self.vol_label.configure(text=f"{saved_vol}%")
        if self._fs_osd is not None:
            self.fs_vol_slider.set(saved_vol)
            self.fs_vol_label.configure(text=f"{saved_vol}%")

        saved_aspect = self.storage.get_setting("aspect_ratio", "default")
        self.player.set_aspect_ratio(saved_aspect)
        self.btn_aspect.configure(text=f"📐 {saved_aspect}")
        if self._fs_osd is not None:
            self.fs_btn_aspect.configure(text=f"📐 {saved_aspect}")

        # Connect recorder callbacks
        self.recorder.on_stats_update = lambda d, s: self.msg_queue.put(("stats", d, s))
        self.recorder.on_finished = lambda p, s, m: self.msg_queue.put(("rec_finished", p, s, m))

        # Start periodic tasks
        self._start_queue_poller()
        self._start_timeline_timer()

        # Bind keyboard shortcuts
        self._bind_shortcuts()

        # Load initial playlist (from disk cache for instant startup)
        self.after(100, self._load_initial_playlist)

    def _setup_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # 1. Top Navigation Bar
        self._create_top_bar()

        # 2. Dual Sidebar (Categories + Channels/Items)
        self._create_sidebar()

        # 3. Main Center Area (Video Viewport & Info Strip)
        self._create_main_viewport()

        # 4. Bottom Controls Bar with Seek Slider
        self._create_bottom_bar()

        # 5. Auto-hiding Floating Fullscreen OSD Bar
        self._create_fullscreen_osd()

    def _create_top_bar(self):
        self.top_bar = ctk.CTkFrame(self, height=54, corner_radius=0, fg_color="#0e1118")
        self.top_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.top_bar.grid_propagate(False)

        # Brand / Logo
        brand_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        brand_frame.pack(side="left", padx=14, pady=8)

        logo_lbl = ctk.CTkLabel(
            brand_frame,
            text="📺 SMART IPTV PRO",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#00d2c4"
        )
        logo_lbl.pack(side="left")

        # Mode Selector Buttons: LIVE TV | MOVIES | SERIES
        mode_frame = ctk.CTkFrame(self.top_bar, fg_color="#181c28", corner_radius=8)
        mode_frame.pack(side="left", padx=16, pady=8)

        self.btn_mode_live = ctk.CTkButton(
            mode_frame,
            text="📺 LIVE TV",
            width=95,
            height=30,
            corner_radius=6,
            fg_color="#00adb5",
            hover_color="#008f96",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_mode("live")
        )
        self.btn_mode_live.pack(side="left", padx=2, pady=2)

        self.btn_mode_vod = ctk.CTkButton(
            mode_frame,
            text="🎬 MOVIES",
            width=90,
            height=30,
            corner_radius=6,
            fg_color="transparent",
            hover_color="#262d40",
            text_color="#94a3b8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_mode("vod")
        )
        self.btn_mode_vod.pack(side="left", padx=2, pady=2)

        self.btn_mode_series = ctk.CTkButton(
            mode_frame,
            text="🍿 SERIES",
            width=90,
            height=30,
            corner_radius=6,
            fg_color="transparent",
            hover_color="#262d40",
            text_color="#94a3b8",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_mode("series")
        )
        self.btn_mode_series.pack(side="left", padx=2, pady=2)

        # Total Count Badge
        self.channel_count_badge = ctk.CTkLabel(
            self.top_bar,
            text="0 items",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#1c2130",
            corner_radius=8,
            padx=10,
            pady=3,
            text_color="#38bdf8"
        )
        self.channel_count_badge.pack(side="left", padx=8)

        # Right Action Buttons
        actions_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        actions_frame.pack(side="right", padx=14, pady=8)

        # Refresh Button
        btn_refresh = ctk.CTkButton(
            actions_frame,
            text="🔄 Refresh",
            width=76,
            height=32,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self._refresh_current_playlist
        )
        btn_refresh.pack(side="left", padx=2)

        # Toggle Sidebar button
        self.btn_toggle_sidebar = ctk.CTkButton(
            actions_frame,
            text="Sidebar ☰",
            width=78,
            height=32,
            fg_color="#1c2130",
            hover_color="#283045",
            command=self._toggle_sidebar
        )
        self.btn_toggle_sidebar.pack(side="left", padx=2)

        # Open File Button
        btn_open_file = ctk.CTkButton(
            actions_frame,
            text="📂 M3U",
            width=74,
            height=32,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self._open_file_dialog
        )
        btn_open_file.pack(side="left", padx=2)

        # Open URL / Xtream Button
        self.btn_open_url = ctk.CTkButton(
            actions_frame,
            text="🌐 Xtream / M3U",
            width=120,
            height=32,
            fg_color="#00adb5",
            hover_color="#00939a",
            font=ctk.CTkFont(weight="bold"),
            command=self._open_url_dialog
        )
        self.btn_open_url.pack(side="left", padx=2)

        # Saved Servers Button
        self.btn_servers = ctk.CTkButton(
            actions_frame,
            text="🖥️ Servers",
            width=88,
            height=32,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(weight="bold"),
            command=self._open_servers_dialog
        )
        self.btn_servers.pack(side="left", padx=2)

        # Open Recordings Folder Button
        btn_recordings = ctk.CTkButton(
            actions_frame,
            text="📁 Recs",
            width=74,
            height=32,
            fg_color="#1c2130",
            hover_color="#283045",
            command=self.recorder.open_recordings_folder
        )
        btn_recordings.pack(side="left", padx=2)

        # Settings Button
        btn_settings = ctk.CTkButton(
            actions_frame,
            text="⚙️",
            width=38,
            height=32,
            fg_color="#1c2130",
            hover_color="#283045",
            command=self._open_settings_dialog
        )
        btn_settings.pack(side="left", padx=2)

    def _create_sidebar(self):
        """Creates dual-column sidebar: Left for Categories, Right for Items."""
        self.sidebar = ctk.CTkFrame(self, width=490, corner_radius=0, fg_color="#11141d")
        self.sidebar.grid(row=1, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_columnconfigure(1, weight=1)
        self.sidebar.grid_rowconfigure(0, weight=1)

        # ----------------- 1. Categories Column (Left) -----------------
        self.cat_panel = ctk.CTkFrame(self.sidebar, corner_radius=0, fg_color="#131722")
        self.cat_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 1))

        cat_header_frame = ctk.CTkFrame(self.cat_panel, fg_color="transparent")
        cat_header_frame.pack(fill="x", padx=8, pady=(8, 4))

        self.lbl_cat_title = ctk.CTkLabel(
            cat_header_frame,
            text="CATEGORIES (0)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
            anchor="w"
        )
        self.lbl_cat_title.pack(fill="x", pady=(0, 4))

        self.cat_search_entry = ctk.CTkEntry(
            cat_header_frame,
            placeholder_text="Filter Categories... 🔍",
            height=30,
            corner_radius=6,
            fg_color="#1c2130",
            border_width=1,
            border_color="#2a3349",
            font=ctk.CTkFont(size=12)
        )
        self.cat_search_entry.pack(fill="x")
        self.cat_search_entry.bind("<KeyRelease>", self._on_cat_search_change)

        self.cat_scroll = ctk.CTkScrollableFrame(self.cat_panel, fg_color="transparent", corner_radius=0)
        self.cat_scroll.pack(fill="both", expand=True, padx=4, pady=4)
        self.cat_scroll.bind("<Leave>", lambda e: self._clear_cat_hover())

        # Synchronize canvas rendering with scrollbar drag to eliminate tearing / ghosting
        def smooth_cat_scroll(*args):
            self.cat_scroll._parent_canvas.yview(*args)
            self.cat_scroll._parent_canvas.update_idletasks()

        self.cat_scroll._scrollbar.configure(command=smooth_cat_scroll)
        self.cat_scroll._parent_canvas.configure(yscrollincrement=2)

        # ----------------- 2. Channels/Items Column (Right) -----------------
        self.chan_panel = ctk.CTkFrame(self.sidebar, corner_radius=0, fg_color="#161b28")
        self.chan_panel.grid(row=0, column=1, sticky="nsew")

        chan_header_frame = ctk.CTkFrame(self.chan_panel, fg_color="transparent")
        chan_header_frame.pack(fill="x", padx=8, pady=(8, 4))

        head_row = ctk.CTkFrame(chan_header_frame, fg_color="transparent")
        head_row.pack(fill="x", pady=(0, 4))

        self.btn_back_series = ctk.CTkButton(
            head_row,
            text="◀ Back",
            width=55,
            height=24,
            corner_radius=4,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._exit_episodes_view
        )

        self.lbl_chan_head = ctk.CTkLabel(
            head_row,
            text="CHANNELS (0)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f8fafc",
            anchor="w"
        )
        self.lbl_chan_head.pack(side="left", fill="x", expand=True)

        self.chan_search_entry = ctk.CTkEntry(
            chan_header_frame,
            placeholder_text="Search by name... 🔍",
            height=30,
            corner_radius=6,
            fg_color="#202738",
            border_width=1,
            border_color="#2d374d",
            font=ctk.CTkFont(size=12)
        )
        self.chan_search_entry.pack(fill="x")
        self.chan_search_entry.bind("<KeyRelease>", self._on_chan_search_change)

        self.channel_scroll = ctk.CTkScrollableFrame(self.chan_panel, fg_color="transparent", corner_radius=0)
        self.channel_scroll.pack(fill="both", expand=True, padx=4, pady=4)
        self.channel_scroll.bind("<Leave>", lambda e: self._clear_chan_hover())

        # Synchronize canvas rendering with scrollbar drag to eliminate tearing / ghosting
        def smooth_chan_scroll(*args):
            self.channel_scroll._parent_canvas.yview(*args)
            self.channel_scroll._parent_canvas.update_idletasks()

        self.channel_scroll._scrollbar.configure(command=smooth_chan_scroll)
        self.channel_scroll._parent_canvas.configure(yscrollincrement=2)

    def _create_main_viewport(self):
        self.viewport_container = ctk.CTkFrame(self, fg_color="#07080d", corner_radius=0)
        self.viewport_container.grid(row=1, column=1, sticky="nsew")
        self.viewport_container.grid_rowconfigure(1, weight=1)
        self.viewport_container.grid_columnconfigure(0, weight=1)
        self.viewport_container.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # Top Information Strip
        self.top_info_strip = ctk.CTkFrame(self.viewport_container, height=40, corner_radius=0, fg_color="#0e111a")
        self.top_info_strip.grid(row=0, column=0, sticky="ew")
        self.top_info_strip.grid_propagate(False)

        self.top_info_strip.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.top_info_strip.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.lbl_live_badge = ctk.CTkLabel(
            self.top_info_strip,
            text="🔴 LIVE",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#00d2c4",
            padx=12
        )
        self.lbl_live_badge.pack(side="left")
        self.lbl_live_badge.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.lbl_live_badge.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.lbl_channel_title = ctk.CTkLabel(
            self.top_info_strip,
            text="Select a channel or movie to start playing",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#f8fafc",
            anchor="w"
        )
        self.lbl_channel_title.pack(side="left", fill="x", expand=True, padx=6)
        self.lbl_channel_title.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.lbl_channel_title.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.lbl_stream_quality = ctk.CTkLabel(
            self.top_info_strip,
            text="HD Stream",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#181e2b",
            corner_radius=6,
            padx=8,
            pady=1,
            text_color="#38bdf8"
        )
        self.lbl_stream_quality.pack(side="right", padx=12)
        self.lbl_stream_quality.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # Native Frame for VLC Video Output (Win32 HWND)
        self.video_frame = tk.Frame(self.viewport_container, bg="black", highlightthickness=0)
        self.video_frame.grid(row=1, column=0, sticky="nsew")

        # Double click on video frame triggers Fullscreen; scroll wheel controls volume
        self.video_frame.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.video_frame.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # Placeholder message placed on viewport_container (independent of video_frame Win32 HWND)
        self.placeholder_frame = ctk.CTkFrame(self.viewport_container, fg_color="#07080d", corner_radius=0)
        self.placeholder_frame.grid(row=1, column=0, sticky="nsew")
        self.placeholder_frame.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.placeholder_frame.bind("<MouseWheel>", self._on_video_mouse_wheel)
        self.placeholder_frame.lift()

        center_ph = ctk.CTkFrame(self.placeholder_frame, fg_color="transparent")
        center_ph.place(relx=0.5, rely=0.5, anchor="center")
        center_ph.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        center_ph.bind("<MouseWheel>", self._on_video_mouse_wheel)

        icon_ph = ctk.CTkLabel(
            center_ph,
            text="📺",
            font=ctk.CTkFont(size=56)
        )
        icon_ph.pack(pady=(0, 10))
        icon_ph.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        icon_ph.bind("<MouseWheel>", self._on_video_mouse_wheel)

        text_ph = ctk.CTkLabel(
            center_ph,
            text="Smart IPTV Player Pro\nDouble-click video or channel to toggle Fullscreen (or press F / F11)\nSeek bar below allows fast-forward & rewind for Movies & Series",
            font=ctk.CTkFont(size=14),
            text_color="#64748b",
            justify="center"
        )
        text_ph.pack()
        text_ph.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        text_ph.bind("<MouseWheel>", self._on_video_mouse_wheel)

    def _create_bottom_bar(self):
        """Creates the bottom bar including Seek Timeline Bar and playback controls."""
        self.bottom_bar = ctk.CTkFrame(self, height=96, corner_radius=0, fg_color="#0e111a")
        self.bottom_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.bottom_bar.grid_propagate(False)
        self.bottom_bar.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # ---------------- 1. Timeline / Seek Bar Row ----------------
        timeline_row = ctk.CTkFrame(self.bottom_bar, height=30, fg_color="transparent")
        timeline_row.pack(fill="x", padx=16, pady=(4, 0))

        self.lbl_curr_time = ctk.CTkLabel(
            timeline_row,
            text="00:00",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            width=50
        )
        self.lbl_curr_time.pack(side="left")

        # -10s Rewind Button
        self.btn_seek_back = ctk.CTkButton(
            timeline_row,
            text="⏪ -10s",
            width=48,
            height=22,
            corner_radius=4,
            fg_color="#181e2b",
            hover_color="#263045",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: self.player.seek_relative(-10)
        )
        self.btn_seek_back.pack(side="left", padx=4)

        # Seek Slider (0 to 1000)
        self.seek_slider = ctk.CTkSlider(
            timeline_row,
            from_=0,
            to=1000,
            height=14,
            progress_color="#00adb5",
            button_color="#00d2c4",
            button_hover_color="#38bdf8",
            command=self._on_seek_slider_move
        )
        self.seek_slider.set(0)
        self.seek_slider.pack(side="left", fill="x", expand=True, padx=6)
        self.seek_slider.bind("<ButtonPress-1>", lambda e: setattr(self, "is_seeking", True))
        self.seek_slider.bind("<ButtonRelease-1>", self._on_seek_slider_release)
        self.seek_slider._canvas.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # +10s Forward Button
        self.btn_seek_fwd = ctk.CTkButton(
            timeline_row,
            text="⏩ +10s",
            width=48,
            height=22,
            corner_radius=4,
            fg_color="#181e2b",
            hover_color="#263045",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: self.player.seek_relative(10)
        )
        self.btn_seek_fwd.pack(side="left", padx=4)

        self.lbl_total_time = ctk.CTkLabel(
            timeline_row,
            text="00:00",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            width=55
        )
        self.lbl_total_time.pack(side="right")

        # ---------------- 2. Main Controls Row ----------------
        controls_row = ctk.CTkFrame(self.bottom_bar, height=48, fg_color="transparent")
        controls_row.pack(fill="x", padx=16, pady=(2, 6))

        # Playback controls
        left_controls = ctk.CTkFrame(controls_row, fg_color="transparent")
        left_controls.pack(side="left")

        self.btn_play_pause = ctk.CTkButton(
            left_controls,
            text="▶ Play",
            width=80,
            height=34,
            fg_color="#00adb5",
            hover_color="#00939a",
            font=ctk.CTkFont(weight="bold"),
            command=self._toggle_play_pause
        )
        self.btn_play_pause.pack(side="left", padx=3)

        self.btn_stop = ctk.CTkButton(
            left_controls,
            text="⏹ Stop",
            width=68,
            height=34,
            fg_color="#242b3b",
            hover_color="#313b52",
            command=self._stop_playback
        )
        self.btn_stop.pack(side="left", padx=3)

        # Volume Controls
        self.btn_mute = ctk.CTkButton(
            left_controls,
            text="🔊",
            width=36,
            height=34,
            fg_color="#1c2130",
            hover_color="#272f44",
            command=self._toggle_mute
        )
        self.btn_mute.pack(side="left", padx=(10, 3))

        self.vol_slider = ctk.CTkSlider(
            left_controls,
            from_=0,
            to=100,
            width=100,
            command=self._on_volume_change
        )
        self.vol_slider.pack(side="left", padx=3)
        self.vol_slider._canvas.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.vol_label = ctk.CTkLabel(
            left_controls,
            text="85%",
            width=34,
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8"
        )
        self.vol_label.pack(side="left", padx=2)

        # Center Section: Record Button
        center_controls = ctk.CTkFrame(controls_row, fg_color="transparent")
        center_controls.pack(side="left", expand=True, padx=15)

        self.btn_record = ctk.CTkButton(
            center_controls,
            text="⏺ Record Stream",
            width=165,
            height=36,
            fg_color="#e02424",
            hover_color="#b91c1c",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._toggle_recording
        )
        self.btn_record.pack(side="left", padx=6)

        self.lbl_record_stats = ctk.CTkLabel(
            center_controls,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f87171"
        )
        self.lbl_record_stats.pack(side="left", padx=6)

        # Right Section: Aspect ratio, Fullscreen
        right_controls = ctk.CTkFrame(controls_row, fg_color="transparent")
        right_controls.pack(side="right")

        self.btn_aspect = ctk.CTkButton(
            right_controls,
            text="📐 16:9",
            width=72,
            height=34,
            fg_color="#1c2130",
            hover_color="#272f44",
            command=self._cycle_aspect_ratio
        )
        self.btn_aspect.pack(side="left", padx=3)

        self.btn_fullscreen = ctk.CTkButton(
            right_controls,
            text="⛶ Fullscreen",
            width=95,
            height=34,
            fg_color="#1c2130",
            hover_color="#272f44",
            command=self._toggle_fullscreen
        )
        self.btn_fullscreen.pack(side="left", padx=3)

    def _create_fullscreen_osd(self):
        """Create a floating bottom control bar (OSD) shown on bottom hover during Fullscreen."""
        self._fs_osd = ctk.CTkToplevel(self)
        self._fs_osd.overrideredirect(True)
        self._fs_osd.configure(fg_color="#0e111a")
        try:
            self._fs_osd.attributes("-topmost", True)
            self._fs_osd.attributes("-alpha", 0.92)
        except Exception:
            pass
        self._fs_osd.withdraw()

        # Mirror playback shortcuts when focus is inside the floating OSD
        self._fs_osd.bind("<Escape>", lambda e: self._exit_fullscreen())
        self._fs_osd.bind("<space>", lambda e: self._toggle_play_pause())
        self._fs_osd.bind("<f>", lambda e: self._toggle_fullscreen())
        self._fs_osd.bind("<F>", lambda e: self._toggle_fullscreen())
        self._fs_osd.bind("<F11>", lambda e: self._toggle_fullscreen())
        self._fs_osd.bind("<a>", lambda e: self._cycle_aspect_ratio())
        self._fs_osd.bind("<A>", lambda e: self._cycle_aspect_ratio())
        self._fs_osd.bind("<r>", lambda e: self._toggle_recording())
        self._fs_osd.bind("<R>", lambda e: self._toggle_recording())
        self._fs_osd.bind("<m>", lambda e: self._toggle_mute())
        self._fs_osd.bind("<M>", lambda e: self._toggle_mute())
        self._fs_osd.bind("<Up>", lambda e: self._adjust_volume(5))
        self._fs_osd.bind("<Down>", lambda e: self._adjust_volume(-5))
        self._fs_osd.bind("<Left>", lambda e: self.player.seek_relative(-10))
        self._fs_osd.bind("<Right>", lambda e: self.player.seek_relative(10))
        self._fs_osd.bind("<MouseWheel>", self._on_video_mouse_wheel)

        # Top accent border
        tk.Frame(self._fs_osd, bg="#00adb5", height=2).pack(fill="x", side="top")

        # Row 1: Timeline / Seek Bar
        fs_timeline = ctk.CTkFrame(self._fs_osd, height=30, fg_color="transparent")
        fs_timeline.pack(fill="x", padx=20, pady=(6, 0))

        self.fs_lbl_curr_time = ctk.CTkLabel(
            fs_timeline,
            text="00:00",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            width=55
        )
        self.fs_lbl_curr_time.pack(side="left")

        self.fs_btn_seek_back = ctk.CTkButton(
            fs_timeline,
            text="⏪ -10s",
            width=52,
            height=22,
            corner_radius=4,
            fg_color="#181e2b",
            hover_color="#263045",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: self.player.seek_relative(-10)
        )
        self.fs_btn_seek_back.pack(side="left", padx=4)

        self.fs_seek_slider = ctk.CTkSlider(
            fs_timeline,
            from_=0,
            to=1000,
            height=14,
            progress_color="#00adb5",
            button_color="#00d2c4",
            button_hover_color="#38bdf8",
            command=self._on_fs_seek_slider_move
        )
        self.fs_seek_slider.set(0)
        self.fs_seek_slider.pack(side="left", fill="x", expand=True, padx=8)
        self.fs_seek_slider.bind("<ButtonPress-1>", lambda e: setattr(self, "is_seeking", True))
        self.fs_seek_slider.bind("<ButtonRelease-1>", self._on_fs_seek_slider_release)
        self.fs_seek_slider._canvas.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.fs_btn_seek_fwd = ctk.CTkButton(
            fs_timeline,
            text="⏩ +10s",
            width=52,
            height=22,
            corner_radius=4,
            fg_color="#181e2b",
            hover_color="#263045",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: self.player.seek_relative(10)
        )
        self.fs_btn_seek_fwd.pack(side="left", padx=4)

        self.fs_lbl_total_time = ctk.CTkLabel(
            fs_timeline,
            text="00:00",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8",
            width=60
        )
        self.fs_lbl_total_time.pack(side="right")

        # Row 2: Playback, Volume, Title, Aspect Ratio, Exit
        fs_controls = ctk.CTkFrame(self._fs_osd, height=48, fg_color="transparent")
        fs_controls.pack(fill="x", padx=20, pady=(4, 8))

        fs_left = ctk.CTkFrame(fs_controls, fg_color="transparent")
        fs_left.pack(side="left")

        self.fs_btn_play_pause = ctk.CTkButton(
            fs_left,
            text="▶ Play",
            width=84,
            height=34,
            fg_color="#00adb5",
            hover_color="#00939a",
            font=ctk.CTkFont(weight="bold"),
            command=self._toggle_play_pause
        )
        self.fs_btn_play_pause.pack(side="left", padx=3)

        self.fs_btn_stop = ctk.CTkButton(
            fs_left,
            text="⏹ Stop",
            width=72,
            height=34,
            fg_color="#242b3b",
            hover_color="#313b52",
            command=self._stop_playback
        )
        self.fs_btn_stop.pack(side="left", padx=3)

        self.fs_btn_mute = ctk.CTkButton(
            fs_left,
            text="🔊",
            width=38,
            height=34,
            fg_color="#1c2130",
            hover_color="#272f44",
            command=self._toggle_mute
        )
        self.fs_btn_mute.pack(side="left", padx=(12, 3))

        self.fs_vol_slider = ctk.CTkSlider(
            fs_left,
            from_=0,
            to=100,
            width=115,
            command=self._on_volume_change
        )
        self.fs_vol_slider.pack(side="left", padx=4)
        self.fs_vol_slider._canvas.bind("<MouseWheel>", self._on_video_mouse_wheel)

        self.fs_vol_label = ctk.CTkLabel(
            fs_left,
            text="85%",
            width=36,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#94a3b8"
        )
        self.fs_vol_label.pack(side="left", padx=2)

        fs_right = ctk.CTkFrame(fs_controls, fg_color="transparent")
        fs_right.pack(side="right")

        self.fs_lbl_record_stats = ctk.CTkLabel(
            fs_right,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#f87171"
        )
        self.fs_lbl_record_stats.pack(side="left", padx=(0, 6))

        self.fs_btn_record = ctk.CTkButton(
            fs_right,
            text="⏺ Record",
            width=96,
            height=34,
            fg_color="#e02424",
            hover_color="#b91c1c",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._toggle_recording
        )
        self.fs_btn_record.pack(side="left", padx=4)

        self.fs_btn_aspect = ctk.CTkButton(
            fs_right,
            text="📐 Fill",
            width=82,
            height=34,
            fg_color="#1c2130",
            hover_color="#272f44",
            command=self._cycle_aspect_ratio
        )
        self.fs_btn_aspect.pack(side="left", padx=4)

        self.fs_btn_exit_fs = ctk.CTkButton(
            fs_right,
            text="⛶ Exit",
            width=84,
            height=34,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(weight="bold"),
            command=self._exit_fullscreen
        )
        self.fs_btn_exit_fs.pack(side="left", padx=3)

        self.fs_lbl_title = ctk.CTkLabel(
            fs_controls,
            text="",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e2e8f0"
        )
        self.fs_lbl_title.pack(side="left", fill="x", expand=True, padx=16)

    def _on_video_mouse_wheel(self, event):
        """Adjust volume up/down smoothly when scrolling mouse wheel over video."""
        delta = getattr(event, "delta", 0)
        if delta != 0:
            step = 5 if delta > 0 else -5
            self._adjust_volume(step)
        return "break"

    def _on_app_mouse_wheel(self, event):
        """Route mouse wheel to volume when in fullscreen or hovering over video/bottom controls."""
        try:
            if self.is_fullscreen:
                return self._on_video_mouse_wheel(event)
            mx, my = self.winfo_pointerxy()
            for area in (self.viewport_container, self.bottom_bar):
                ax = area.winfo_rootx()
                ay = area.winfo_rooty()
                aw = area.winfo_width()
                ah = area.winfo_height()
                if ax <= mx <= ax + aw and ay <= my <= ay + ah:
                    return self._on_video_mouse_wheel(event)
        except Exception:
            pass

    def _is_typing_in_entry(self) -> bool:
        """Return True if keyboard focus is currently inside a text input widget."""
        try:
            focused = self.focus_get()
            return isinstance(focused, (tk.Entry, tk.Text))
        except Exception:
            return False

    def _bind_shortcuts(self):
        def guard(fn):
            return lambda e: None if self._is_typing_in_entry() else fn()

        self.bind("<space>", guard(self._toggle_play_pause))
        self.bind("<f>", guard(self._toggle_fullscreen))
        self.bind("<F>", guard(self._toggle_fullscreen))
        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.bind("<a>", guard(self._cycle_aspect_ratio))
        self.bind("<A>", guard(self._cycle_aspect_ratio))
        self.bind("<Escape>", lambda e: self._exit_fullscreen())
        self.bind("<r>", guard(self._toggle_recording))
        self.bind("<R>", guard(self._toggle_recording))
        self.bind("<m>", guard(self._toggle_mute))
        self.bind("<M>", guard(self._toggle_mute))
        self.bind("<Up>", guard(lambda: self._adjust_volume(5)))
        self.bind("<Down>", guard(lambda: self._adjust_volume(-5)))
        self.bind("<Left>", guard(lambda: self.player.seek_relative(-10)))
        self.bind("<Right>", guard(lambda: self.player.seek_relative(10)))
        self.bind("<MouseWheel>", self._on_app_mouse_wheel)

    def _start_queue_poller(self):
        try:
            while not self.msg_queue.empty():
                item = self.msg_queue.get_nowait()
                tag = item[0]
                if tag == "loaded":
                    _, req_id, mode, target, channels = item
                    if req_id != self._load_request_id:
                        continue
                    self._on_content_loaded(mode, target, channels)
                elif tag == "loaded_indexed":
                    _, req_id, mode, target, indexed, save_to_disk = item
                    if req_id != self._load_request_id:
                        continue
                    self._on_content_loaded(mode, target, indexed["channels"], save_to_disk=save_to_disk, indexed=indexed)
                elif tag == "prefetched":
                    _, gen, target, other_mode, indexed = item
                    if gen != self._cache_generation or target != self.current_playlist_target:
                        continue
                    if not self.content_cache.get(other_mode, {}).get("_loaded"):
                        self.content_cache[other_mode] = indexed
                        if self.current_mode == other_mode:
                            self.current_category = "All"
                            self.displayed_limit = 40
                            self._render_category_rows()
                            self._filter_and_render_items()
                            total_cnt = len(indexed["channels"])
                            lbl = "Channels" if other_mode == "live" else ("Movies" if other_mode == "vod" else "Series")
                            self.channel_count_badge.configure(text=f"{total_cnt:,} {lbl}")
                elif tag == "episodes_loaded":
                    _, episodes = item
                    self._on_episodes_loaded(episodes)
                elif tag == "error":
                    _, req_id, err_msg = item
                    if req_id is not None and req_id != self._load_request_id:
                        continue
                    self.channel_count_badge.configure(text="Load Error")
                    messagebox.showerror("Loading Error", f"Failed to load stream playlist:\n{err_msg}")
                elif tag == "stats":
                    _, dur_str, sz_str = item
                    if self.recorder.is_recording:
                        stats_txt = f"⏱️ {dur_str}  💾 {sz_str}"
                        self.lbl_record_stats.configure(text=stats_txt)
                        if self._fs_osd is not None:
                            self.fs_lbl_record_stats.configure(text=stats_txt)
                elif tag == "rec_finished":
                    _, filepath, success, msg = item
                    self._handle_recording_finished(filepath, success, msg)
        except Exception:
            pass
        finally:
            self.after(150, self._start_queue_poller)

    def _start_timeline_timer(self):
        """Update timeline seek slider and duration labels smoothly."""
        def tick():
            try:
                if self.player.is_playing():
                    if self.btn_play_pause.cget("text") != "⏸ Pause":
                        self.btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")
                    if self.is_fullscreen and self._fs_osd_visible and self.fs_btn_play_pause.cget("text") != "⏸ Pause":
                        self.fs_btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")

                    if self.current_channel:
                        expected_badge = "🔴 LIVE" if self.current_channel.content_type == "live" else ("🎬 MOVIE" if self.current_channel.content_type == "vod" else "🍿 EPISODE")
                        if self.lbl_live_badge.cget("text") != expected_badge:
                            self.lbl_live_badge.configure(text=expected_badge)

                    info = self.player.get_media_info()
                    if info.get("width") and info.get("height"):
                        res_str = info["resolution"]
                        if self.lbl_stream_quality.cget("text") != res_str:
                            self.lbl_stream_quality.configure(text=res_str)

                    total_ms = self.player.get_length()
                    curr_ms = self.player.get_time()

                    if total_ms > 0:
                        if not self.is_seeking:
                            progress = max(0.0, min(1000.0, (curr_ms / total_ms) * 1000))
                            self.seek_slider.set(progress)
                            if self.is_fullscreen and self._fs_osd_visible:
                                self.fs_seek_slider.set(progress)

                        cur_str = format_ms(curr_ms)
                        tot_str = format_ms(total_ms)
                        if self.lbl_curr_time.cget("text") != cur_str:
                            self.lbl_curr_time.configure(text=cur_str)
                        if self.lbl_total_time.cget("text") != tot_str:
                            self.lbl_total_time.configure(text=tot_str)
                        if str(self.btn_seek_back.cget("state")) != "normal":
                            self.btn_seek_back.configure(state="normal")
                            self.btn_seek_fwd.configure(state="normal")

                        if self.is_fullscreen and self._fs_osd_visible:
                            if self.fs_lbl_curr_time.cget("text") != cur_str:
                                self.fs_lbl_curr_time.configure(text=cur_str)
                            if self.fs_lbl_total_time.cget("text") != tot_str:
                                self.fs_lbl_total_time.configure(text=tot_str)
                            if str(self.fs_btn_seek_back.cget("state")) != "normal":
                                self.fs_btn_seek_back.configure(state="normal")
                                self.fs_btn_seek_fwd.configure(state="normal")
                    else:
                        if self.lbl_curr_time.cget("text") != "LIVE":
                            self.lbl_curr_time.configure(text="LIVE")
                            self.lbl_total_time.configure(text="🔴 LIVE")
                            self.seek_slider.set(1000)
                            self.btn_seek_back.configure(state="disabled")
                            self.btn_seek_fwd.configure(state="disabled")
                        if self.is_fullscreen and self._fs_osd_visible and self.fs_lbl_curr_time.cget("text") != "LIVE":
                            self.fs_lbl_curr_time.configure(text="LIVE")
                            self.fs_lbl_total_time.configure(text="🔴 LIVE")
                            self.fs_seek_slider.set(1000)
                            self.fs_btn_seek_back.configure(state="disabled")
                            self.fs_btn_seek_fwd.configure(state="disabled")
                elif self.current_channel:
                    state = self.player.get_state()
                    if state in ("جاري الفتح...", "جاري التحميل..."):
                        if self.lbl_live_badge.cget("text") != "⏳ Loading...":
                            self.lbl_live_badge.configure(text="⏳ Loading...")
                    elif state == "خطأ في البث":
                        if self.btn_play_pause.cget("text") != "▶ Play":
                            self.btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
                        if self.is_fullscreen and self._fs_osd_visible and self.fs_btn_play_pause.cget("text") != "▶ Play":
                            self.fs_btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
                        if self.lbl_live_badge.cget("text") != "⚠️ Stream Error":
                            self.lbl_live_badge.configure(text="⚠️ Stream Error")
                    elif state == "انتهى":
                        if self.btn_play_pause.cget("text") != "▶ Play":
                            self.btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
                        if self.is_fullscreen and self._fs_osd_visible and self.fs_btn_play_pause.cget("text") != "▶ Play":
                            self.fs_btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
                        if self.lbl_live_badge.cget("text") != "⏹ Ended":
                            self.lbl_live_badge.configure(text="⏹ Ended")
            except Exception:
                pass

            self.after(500, tick)

        self.after(500, tick)

    def _on_seek_slider_move(self, val):
        total_ms = self.player.get_length()
        if total_ms > 0:
            target_ms = int((float(val) / 1000.0) * total_ms)
            self.lbl_curr_time.configure(text=format_ms(target_ms))

    def _on_seek_slider_release(self, event):
        self.is_seeking = False
        val = self.seek_slider.get()
        total_ms = self.player.get_length()
        if total_ms > 0:
            target_ms = int((val / 1000.0) * total_ms)
            self.player.set_time(target_ms)

    def _on_fs_seek_slider_move(self, val):
        total_ms = self.player.get_length()
        if total_ms > 0:
            target_ms = int((float(val) / 1000.0) * total_ms)
            t_str = format_ms(target_ms)
            self.fs_lbl_curr_time.configure(text=t_str)
            self.lbl_curr_time.configure(text=t_str)

    def _on_fs_seek_slider_release(self, event):
        self.is_seeking = False
        val = self.fs_seek_slider.get()
        total_ms = self.player.get_length()
        if total_ms > 0:
            target_ms = int((val / 1000.0) * total_ms)
            self.player.set_time(target_ms)
            self.seek_slider.set(val)

    @staticmethod
    def _build_mode_index(channels: list[Channel]) -> dict:
        """Build category counts, lookup dict, and top 30 recently added items off the UI thread."""
        cat_counts = M3UParser.get_category_counts(channels)

        by_cat = {}
        for ch in channels:
            grp = ch.group or "General"
            if grp not in by_cat:
                by_cat[grp] = []
            by_cat[grp].append(ch)

        if any(getattr(ch, "added", 0) > 0 for ch in channels):
            recent = sorted(
                channels,
                key=lambda c: (getattr(c, "added", 0), int(c.item_id) if str(c.item_id).isdigit() else 0),
                reverse=True,
            )[:30]
        else:
            recent = list(reversed(channels[-30:]))

        return {
            "channels": channels,
            "cat_counts": cat_counts,
            "by_cat": by_cat,
            "recent": recent,
            "_loaded": True,
        }

    @staticmethod
    def _fetch_channels_for_mode(path_or_url: str, mode: str) -> list[Channel]:
        """Fetch and filter channels/movies/series for a given playlist target and mode."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return M3UParser.parse_url(path_or_url, content_type=mode)

        channels = M3UParser.parse_file(path_or_url)
        if mode in ("vod", "series"):
            return [ch for ch in channels if ch.content_type == mode]
        if mode == "live":
            live_only = [ch for ch in channels if ch.content_type == "live"]
            if live_only:
                return live_only
        return channels

    def _switch_mode(self, mode: str):
        """Switch between LIVE TV, MOVIES (VOD), and SERIES with instant caching."""
        if self.current_mode == mode and not self.in_episodes_view:
            return

        self.current_mode = mode
        self.in_episodes_view = False
        self.btn_back_series.pack_forget()

        self.btn_mode_live.configure(
            fg_color="#00adb5" if mode == "live" else "transparent",
            text_color="#ffffff" if mode == "live" else "#94a3b8"
        )
        self.btn_mode_vod.configure(
            fg_color="#00adb5" if mode == "vod" else "transparent",
            text_color="#ffffff" if mode == "vod" else "#94a3b8"
        )
        self.btn_mode_series.configure(
            fg_color="#00adb5" if mode == "series" else "transparent",
            text_color="#ffffff" if mode == "series" else "#94a3b8"
        )

        # Reset scrollbars cleanly to top when switching modes
        try:
            self.cat_scroll._parent_canvas.yview_moveto(0)
            self.channel_scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

        cached = self.content_cache.get(mode, {})
        if cached.get("_loaded") or cached.get("channels"):
            self.current_category = "All"
            self.displayed_limit = 40
            self._render_category_rows()
            self._filter_and_render_items()
            total_cnt = len(cached["channels"])
            lbl = "Channels" if mode == "live" else ("Movies" if mode == "vod" else "Series")
            self.channel_count_badge.configure(text=f"{total_cnt:,} {lbl}")
        elif self.current_playlist_target:
            self._load_mode_from_cache_or_source_async(self.current_playlist_target, mode=mode)

    def _load_initial_playlist(self):
        last_playlist = self.storage.get_setting("last_playlist")
        sample_file = os.path.join(BUNDLE_DIR, "sample_playlist.m3u")
        if not os.path.exists(sample_file):
            sample_file = os.path.join(APP_DIR, "sample_playlist.m3u")

        target = None
        if last_playlist and (os.path.exists(last_playlist) or last_playlist.startswith("http")):
            target = last_playlist
        elif os.path.exists(sample_file):
            target = sample_file

        if target:
            self._load_mode_from_cache_or_source_async(target, mode="live")

    def _load_mode_from_cache_or_source_async(self, path_or_url: str, mode: str = "live"):
        """Load disk cache or remote/local playlist off the UI thread so switching is instant."""
        self._load_playlist_async(path_or_url, mode=mode, skip_disk_cache=False)

    def _load_playlist_async(self, path_or_url: str, mode: str = "live", skip_disk_cache: bool = True):
        self._load_request_id += 1
        req_id = self._load_request_id
        gen = self._cache_generation
        self.current_playlist_target = path_or_url
        self.channel_count_badge.configure(text="Loading...")
        self.lbl_cat_title.configure(text=f"Loading {mode.upper()}...")

        def worker():
            try:
                if not skip_disk_cache:
                    cached_disk = self.storage.load_cache(mode)
                    if cached_disk:
                        channels = [Channel.from_dict(d) for d in cached_disk]
                        indexed = self._build_mode_index(channels)
                        self.msg_queue.put(("loaded_indexed", req_id, mode, path_or_url, indexed, False))
                        return

                channels = self._fetch_channels_for_mode(path_or_url, mode)
                indexed = self._build_mode_index(channels)
                self.msg_queue.put(("loaded_indexed", req_id, mode, path_or_url, indexed, True))
            except Exception as e:
                if gen == self._cache_generation:
                    self.msg_queue.put(("error", req_id, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _prefetch_other_modes_async(self, target: str, skip_mode: str):
        """Pre-warm remaining modes (VOD/Series/Live) in background so tab switching is instantaneous."""
        gen = self._cache_generation

        def worker():
            for other_mode in ("vod", "series", "live"):
                if other_mode == skip_mode:
                    continue
                if gen != self._cache_generation or target != self.current_playlist_target:
                    return
                if self.content_cache.get(other_mode, {}).get("_loaded"):
                    continue
                try:
                    cached_disk = self.storage.load_cache(other_mode)
                    if cached_disk:
                        channels = [Channel.from_dict(d) for d in cached_disk]
                    else:
                        channels = self._fetch_channels_for_mode(target, other_mode)
                        if gen == self._cache_generation and target == self.current_playlist_target:
                            self.storage.save_cache(other_mode, [ch.to_dict() for ch in channels])

                    indexed = self._build_mode_index(channels)
                    self.msg_queue.put(("prefetched", gen, target, other_mode, indexed))
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _reset_content_cache(self):
        """Clear both in-memory and disk playlist caches safely."""
        self._cache_generation += 1
        self._prefetch_started_for = ""
        self.storage.clear_cache()
        self.content_cache = {
            "live": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False},
            "vod": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False},
            "series": {"channels": [], "cat_counts": [], "by_cat": {}, "recent": [], "_loaded": False}
        }
        self.current_channel = None

    def _refresh_current_playlist(self):
        """Manually clear disk cache and re-fetch from server."""
        self._reset_content_cache()
        if self.current_playlist_target:
            self._load_playlist_async(self.current_playlist_target, mode=self.current_mode, skip_disk_cache=True)

    def _on_content_loaded(self, mode: str, target: str, channels: list[Channel], save_to_disk: bool = True, indexed: Optional[dict] = None):
        if indexed is None:
            indexed = self._build_mode_index(channels)

        self.content_cache[mode] = indexed

        if save_to_disk or target != self.storage.get_setting("last_playlist"):
            self.storage.set_setting("last_playlist", target)
            self.storage.add_recent_playlist(target)

        # Save to disk cache in background
        if save_to_disk:
            def bg_save():
                data = [ch.to_dict() for ch in channels]
                self.storage.save_cache(mode, data)
            threading.Thread(target=bg_save, daemon=True).start()

        if self.current_mode == mode:
            self.current_category = "All"
            self.displayed_limit = 40
            self._render_category_rows()
            self._filter_and_render_items()
            lbl = "Channels" if mode == "live" else ("Movies" if mode == "vod" else "Series")
            self.channel_count_badge.configure(text=f"{len(channels):,} {lbl}")

            if mode == "live" and channels and not self.current_channel:
                self.current_channel = channels[0]
                self.lbl_channel_title.configure(text=f"{channels[0].name}  [{channels[0].group}]")

        if target and self._prefetch_started_for != target:
            self._prefetch_started_for = target
            self._prefetch_other_modes_async(target, skip_mode=mode)

    # ---------------- High-Speed Category Panel ----------------
    def _get_or_create_cat_slot(self, idx: int) -> tuple[tk.Frame, tk.Label, tk.Label]:
        """Return a pooled category card slot, creating widgets only when pool grows."""
        while len(self._cat_pool) <= idx:
            row = tk.Frame(self.cat_scroll, bg="#1c2130", highlightbackground="#283248", highlightthickness=1, cursor="hand2")
            row._cat_key = ""

            l1 = tk.Label(row, text="", bg="#1c2130", fg="#f8fafc", font=("Segoe UI", 9, "normal"), anchor="w", cursor="hand2")
            l1.pack(side="left", fill="x", expand=True, padx=4, pady=3)

            l2 = tk.Label(row, text="", bg="#1c2130", fg="#94a3b8", font=("Segoe UI", 8), cursor="hand2")
            l2.pack(side="right", padx=6, pady=3)

            def on_enter(e, r=row, a=l1, b=l2):
                cat_key = r._cat_key
                if not cat_key or self._hovered_cat == cat_key:
                    return
                if self._hovered_cat and self._hovered_cat != self.current_category and self._hovered_cat in self.cat_rows:
                    old_r, old_l1, old_l2 = self.cat_rows[self._hovered_cat]
                    try:
                        old_r.configure(bg="#1c2130")
                        old_l1.configure(bg="#1c2130")
                        old_l2.configure(bg="#1c2130")
                    except Exception:
                        pass
                if cat_key != self.current_category:
                    try:
                        r.configure(bg="#273044")
                        a.configure(bg="#273044")
                        b.configure(bg="#273044")
                    except Exception:
                        pass
                self._hovered_cat = cat_key

            def on_click(e, r=row):
                if r._cat_key:
                    self._on_select_category(r._cat_key)

            row.bind("<Enter>", on_enter)
            l1.bind("<Enter>", on_enter)
            l2.bind("<Enter>", on_enter)

            row.bind("<Button-1>", on_click)
            l1.bind("<Button-1>", on_click)
            l2.bind("<Button-1>", on_click)

            self._cat_pool.append((row, l1, l2))

        return self._cat_pool[idx]

    def _render_category_rows(self):
        """High-performance pooled category rendering with zero widget destruction."""
        if self._cat_more_btn is not None and self._cat_more_btn.winfo_ismapped():
            self._cat_more_btn.pack_forget()

        self.cat_rows.clear()
        self._hovered_cat = None
        cached = self.content_cache.get(self.current_mode, {})
        channels = cached.get("channels", [])
        cat_counts = cached.get("cat_counts", [])

        self.lbl_cat_title.configure(text=f"CATEGORIES ({len(cat_counts)})")

        entries: list[tuple[str, int, str]] = []

        # 1. Favorites Category
        fav_count = 0 if not self.storage.favorites else sum(1 for ch in channels if ch.url in self.storage.favorites)
        entries.append(("⭐ Favorites", fav_count, "Favorites"))

        # 2. All Items Category
        item_type = "Channels" if self.current_mode == "live" else ("Movies" if self.current_mode == "vod" else "Series")
        entries.append((f"📺 All {item_type}", len(channels), "All"))

        # 3. Recently Added (latest 30) for Movies & Series
        if self.current_mode in ("vod", "series"):
            recent_items = cached.get("recent", [])
            entries.append(("✦ Recently Added", len(recent_items), "Recently Added"))

        # 4. Category rows (paginated for instant UI responsiveness)
        query = self.cat_search_query.lower()
        matched = []
        for cat_name, count in cat_counts:
            if query and query not in cat_name.lower():
                continue
            matched.append((cat_name, count))

        limit_cats = matched[:self.displayed_cat_limit]
        for cat_name, count in limit_cats:
            entries.append((cat_name, count, cat_name))

        for i, (label, count, cat_key) in enumerate(entries):
            row, l1, l2 = self._get_or_create_cat_slot(i)
            row._cat_key = cat_key

            is_selected = (self.current_category == cat_key)
            bg = "#242c3f" if is_selected else "#1c2130"
            border_color = "#00d2c4" if is_selected else "#283248"
            if is_selected:
                fg = "#00d2c4"
            elif cat_key == "Favorites":
                fg = "#f59e0b"
            elif cat_key == "Recently Added":
                fg = "#38bdf8"
            else:
                fg = "#f8fafc"
            count_fg = "#38bdf8" if is_selected else "#94a3b8"

            row.configure(bg=bg, highlightbackground=border_color)
            l1.configure(text=f" {label}", bg=bg, fg=fg, font=("Segoe UI", 9, "bold" if is_selected else "normal"))
            l2.configure(text=f"{count:,} ", bg=bg, fg=count_fg)

            if not row.winfo_ismapped():
                row.pack(fill="x", padx=2, pady=1)

            self.cat_rows[cat_key] = (row, l1, l2)

        for j in range(len(entries), len(self._cat_pool)):
            unused_row = self._cat_pool[j][0]
            if unused_row.winfo_ismapped():
                unused_row.pack_forget()

        if len(matched) > len(limit_cats):
            rem = len(matched) - len(limit_cats)
            if self._cat_more_btn is None:
                self._cat_more_btn = tk.Label(
                    self.cat_scroll,
                    text="",
                    bg="#1e293b",
                    fg="#f8fafc",
                    font=("Segoe UI", 9, "bold"),
                    pady=5,
                    cursor="hand2"
                )
                self._cat_more_btn.bind("<Enter>", lambda e: self._cat_more_btn.configure(bg="#334155"))
                self._cat_more_btn.bind("<Leave>", lambda e: self._cat_more_btn.configure(bg="#1e293b"))
                self._cat_more_btn.bind("<Button-1>", lambda e: self._load_more_categories())
            self._cat_more_btn.configure(text=f"+ More Categories ({rem})")
            self._cat_more_btn.pack(fill="x", padx=4, pady=4)

    def _load_more_categories(self):
        self.displayed_cat_limit += 60
        self._render_category_rows()

    def _clear_cat_hover(self):
        if self._hovered_cat and self._hovered_cat != self.current_category and self._hovered_cat in self.cat_rows:
            old_r, old_l1, old_l2 = self.cat_rows[self._hovered_cat]
            try:
                old_r.configure(bg="#1c2130", highlightbackground="#283248")
                old_l1.configure(bg="#1c2130")
                old_l2.configure(bg="#1c2130")
            except Exception:
                pass
        self._hovered_cat = None

    def _on_select_category(self, cat_key: str):
        """Instant O(1) category selection without destroying widgets."""
        old_key = self.current_category
        self.current_category = cat_key

        # Update old row styling
        if old_key in self.cat_rows:
            r, l1, l2 = self.cat_rows[old_key]
            r.configure(bg="#1c2130", highlightbackground="#283248")
            if old_key == "Favorites" or "Favorites" in old_key:
                old_fg = "#f59e0b"
            elif old_key == "Recently Added":
                old_fg = "#38bdf8"
            else:
                old_fg = "#f8fafc"
            l1.configure(bg="#1c2130", fg=old_fg, font=("Segoe UI", 9, "normal"))
            l2.configure(bg="#1c2130", fg="#94a3b8")

        # Update new row styling
        if cat_key in self.cat_rows:
            r, l1, l2 = self.cat_rows[cat_key]
            r.configure(bg="#242c3f", highlightbackground="#00d2c4")
            l1.configure(bg="#242c3f", fg="#00d2c4", font=("Segoe UI", 9, "bold"))
            l2.configure(bg="#242c3f", fg="#38bdf8")

        self.displayed_limit = 40
        self.in_episodes_view = False
        self.btn_back_series.pack_forget()
        self._filter_and_render_items()

    def _on_cat_search_change(self, event=None):
        if self._cat_search_timer:
            self.after_cancel(self._cat_search_timer)
        self._cat_search_timer = self.after(200, self._apply_cat_search)

    def _apply_cat_search(self):
        self._cat_search_timer = None
        self.cat_search_query = self.cat_search_entry.get().strip()
        self.displayed_cat_limit = 50
        self._render_category_rows()

    # ---------------- Items Panel (Channels / VOD / Series) ----------------
    def _filter_and_render_items(self):
        """Instant O(1) category lookup and rapid item card rendering."""
        cached = self.content_cache.get(self.current_mode, {})
        all_channels = cached.get("channels", [])
        by_cat = cached.get("by_cat", {})

        is_fav = (self.current_category == "Favorites")
        is_all = (self.current_category == "All")

        if is_fav:
            filtered = [ch for ch in all_channels if self.storage.is_favorite(ch.url)]
        elif is_all:
            filtered = all_channels
        elif self.current_category == "Recently Added":
            filtered = cached.get("recent", [])
        else:
            filtered = by_cat.get(self.current_category, [])

        if self.chan_search_query:
            q = self.chan_search_query.lower()
            filtered = [ch for ch in filtered if q in ch.name.lower()]

        self.displayed_items = filtered
        self.lbl_chan_head.configure(text=f"{self.current_category} ({len(filtered):,})")
        self._render_item_cards()

    def _clear_chan_hover(self):
        if self._hovered_chan and (not self.current_channel or self._hovered_chan != id(self.current_channel)) and self._hovered_chan in self.channel_rows:
            old_r, old_n, old_f = self.channel_rows[self._hovered_chan]
            try:
                old_r.configure(bg="#1c2130")
                old_n.configure(bg="#1c2130")
                old_f.configure(bg="#1c2130")
            except Exception:
                pass
        self._hovered_chan = None

    def _get_or_create_chan_slot(self, idx: int) -> tuple[tk.Frame, tk.Label, tk.Label]:
        """Return a pooled channel/item card slot, creating widgets only when pool grows."""
        while len(self._chan_pool) <= idx:
            row = tk.Frame(self.channel_scroll, bg="#1c2130", highlightbackground="#283248", highlightthickness=1, cursor="hand2")
            row._channel = None
            row._ch_key = 0

            lbl_fav = tk.Label(row, text="☆", bg="#1c2130", fg="#64748b", font=("Segoe UI", 11), width=3, cursor="hand2")
            lbl_fav.pack(side="left", padx=2)

            lbl_name = tk.Label(row, text="", bg="#1c2130", fg="#f8fafc", font=("Segoe UI", 9, "normal"), anchor="w", cursor="hand2")
            lbl_name.pack(side="left", fill="x", expand=True, padx=4, pady=3)

            def toggle_fav(e, r=row):
                if r._channel is not None:
                    self._toggle_item_favorite(r._channel)
            lbl_fav.bind("<Button-1>", toggle_fav)

            def on_chan_enter(e, r=row, n=lbl_name, f=lbl_fav):
                c_key = r._ch_key
                if not c_key or self._hovered_chan == c_key:
                    return
                if self._hovered_chan and (not self.current_channel or self._hovered_chan != id(self.current_channel)) and self._hovered_chan in self.channel_rows:
                    old_r, old_n, old_f = self.channel_rows[self._hovered_chan]
                    try:
                        old_r.configure(bg="#1c2130")
                        old_n.configure(bg="#1c2130")
                        old_f.configure(bg="#1c2130")
                    except Exception:
                        pass
                if not (self.current_channel and id(self.current_channel) == c_key):
                    try:
                        r.configure(bg="#273044")
                        n.configure(bg="#273044")
                        f.configure(bg="#273044")
                    except Exception:
                        pass
                self._hovered_chan = c_key

            row.bind("<Enter>", on_chan_enter)
            lbl_name.bind("<Enter>", on_chan_enter)
            lbl_fav.bind("<Enter>", on_chan_enter)

            def on_click(e, r=row):
                c = r._channel
                if c is None:
                    return
                if c.content_type == "series":
                    self._open_series_episodes(c)
                else:
                    self._play_item(c)

            def on_double_click(e, r=row):
                c = r._channel
                if c is None:
                    return
                if self.current_channel is c:
                    self._toggle_fullscreen()
                else:
                    if c.content_type == "series":
                        self._open_series_episodes(c)
                    else:
                        self._play_item(c)
                        self._toggle_fullscreen()

            row.bind("<Button-1>", on_click)
            lbl_name.bind("<Button-1>", on_click)

            row.bind("<Double-Button-1>", on_double_click)
            lbl_name.bind("<Double-Button-1>", on_double_click)

            self._chan_pool.append((row, lbl_name, lbl_fav))

        return self._chan_pool[idx]

    def _render_item_cards(self):
        """Render items using pooled tk cards for near-instant mode and category switching."""
        if self._chan_more_btn is not None and self._chan_more_btn.winfo_ismapped():
            self._chan_more_btn.pack_forget()
        if self._chan_empty_lbl is not None and self._chan_empty_lbl.winfo_ismapped():
            self._chan_empty_lbl.pack_forget()

        self.channel_rows.clear()
        self._hovered_chan = None

        if not self.displayed_items:
            for row, _, _ in self._chan_pool:
                if row.winfo_ismapped():
                    row.pack_forget()
            if self._chan_empty_lbl is None:
                self._chan_empty_lbl = tk.Label(
                    self.channel_scroll,
                    text="No items found in this category",
                    bg="#161b28",
                    fg="#64748b",
                    font=("Segoe UI", 10),
                    pady=30
                )
            self._chan_empty_lbl.pack()
            return

        limit = min(self.displayed_limit, len(self.displayed_items))
        for i in range(limit):
            ch = self.displayed_items[i]
            ch_key = id(ch)
            is_active = (self.current_channel is ch)
            is_fav = self.storage.is_favorite(ch.url)

            bg_color = "#242c3f" if is_active else "#1c2130"
            border_color = "#00d2c4" if is_active else "#283248"
            fav_text = "★" if is_fav else "☆"
            fav_color = "#f59e0b" if is_fav else "#64748b"

            row, lbl_name, lbl_fav = self._get_or_create_chan_slot(i)
            row._channel = ch
            row._ch_key = ch_key

            row.configure(bg=bg_color, highlightbackground=border_color)
            lbl_fav.configure(text=fav_text, bg=bg_color, fg=fav_color)
            lbl_name.configure(
                text=ch.name,
                bg=bg_color,
                fg="#00d2c4" if is_active else "#f8fafc",
                font=("Segoe UI", 9, "bold" if is_active else "normal")
            )

            if not row.winfo_ismapped():
                row.pack(fill="x", padx=2, pady=1)

            self.channel_rows[ch_key] = (row, lbl_name, lbl_fav)

        for j in range(limit, len(self._chan_pool)):
            unused_row = self._chan_pool[j][0]
            if unused_row.winfo_ismapped():
                unused_row.pack_forget()

        # "+ Load More" button for large categories
        if limit < len(self.displayed_items):
            rem = len(self.displayed_items) - limit
            if self._chan_more_btn is None:
                self._chan_more_btn = tk.Label(
                    self.channel_scroll,
                    text="",
                    bg="#1e293b",
                    fg="#f8fafc",
                    font=("Segoe UI", 9, "bold"),
                    pady=6,
                    cursor="hand2"
                )
                self._chan_more_btn.bind("<Enter>", lambda e: self._chan_more_btn.configure(bg="#334155"))
                self._chan_more_btn.bind("<Leave>", lambda e: self._chan_more_btn.configure(bg="#1e293b"))
                self._chan_more_btn.bind("<Button-1>", lambda e: self._load_more_items())
            self._chan_more_btn.configure(text=f"+ Load More ({rem:,} remaining)")
            self._chan_more_btn.pack(fill="x", padx=4, pady=6)

    def _load_more_items(self):
        self.displayed_limit += 60
        self._render_item_cards()

    def _on_chan_search_change(self, event=None):
        if self._chan_search_timer:
            self.after_cancel(self._chan_search_timer)
        self._chan_search_timer = self.after(200, self._apply_chan_search)

    def _apply_chan_search(self):
        self._chan_search_timer = None
        self.chan_search_query = self.chan_search_entry.get().strip()
        self.displayed_limit = 40
        self._filter_and_render_items()

    # ---------------- Series Episodes Navigation ----------------
    def _open_series_episodes(self, series_channel: Channel):
        url = self.current_playlist_target
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        user = qs.get("username", [None])[0]
        pwd = qs.get("password", [None])[0]
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        # Non-Xtream playlists (e.g. local M3U) have direct stream URLs instead of series_id
        if not (user and pwd and series_channel.item_id and series_channel.url.startswith("series://")):
            self._play_item(series_channel)
            return

        self.current_series_name = series_channel.name
        self.in_episodes_view = True
        self.btn_back_series.pack(side="left", padx=(0, 6))
        self.lbl_chan_head.configure(text=f"Loading {series_channel.name}...")

        def worker():
            try:
                episodes = M3UParser.fetch_series_episodes(base_url, user, pwd, series_channel.item_id)
                self.msg_queue.put(("episodes_loaded", episodes))
            except Exception as e:
                self.msg_queue.put(("error", None, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_episodes_loaded(self, episodes: list[Channel]):
        self.displayed_items = episodes
        self.lbl_chan_head.configure(text=f"{self.current_series_name} ({len(episodes)} Episodes)")
        self.displayed_limit = 100
        self._render_item_cards()

    def _exit_episodes_view(self):
        self.in_episodes_view = False
        self.btn_back_series.pack_forget()
        self._filter_and_render_items()

    # ---------------- Playback Engine ----------------
    def _play_item(self, channel: Channel):
        if not self.player.is_available():
            messagebox.showerror(
                "VLC Not Found",
                "VLC media engine (libvlc.dll) could not be loaded.\nPlease make sure VLC is installed or placed in the 'vlc' folder."
            )
            return

        old_channel = self.current_channel
        self.current_channel = channel
        self.placeholder_frame.grid_remove()
        self.video_frame.lift()

        badge = "🔴 LIVE" if channel.content_type == "live" else ("🎬 MOVIE" if channel.content_type == "vod" else "🍿 EPISODE")
        self.lbl_live_badge.configure(text=badge)
        self.lbl_channel_title.configure(text=f"{channel.name}  [{channel.group}]")

        self.player.play_stream(channel.url, headers=channel.http_headers)
        self.btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")
        if self._fs_osd is not None:
            self.fs_btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")
            self.fs_lbl_title.configure(text=channel.name)

        self._highlight_active_channel(old_channel, channel)

    def _highlight_active_channel(self, old_ch: Optional[Channel], new_ch: Channel):
        """Instant O(1) channel selection highlight without destroying widgets."""
        if old_ch and id(old_ch) in self.channel_rows:
            r, n, f = self.channel_rows[id(old_ch)]
            try:
                r.configure(bg="#1c2130", highlightbackground="#283248")
                n.configure(bg="#1c2130", fg="#f8fafc", font=("Segoe UI", 9, "normal"))
                f.configure(bg="#1c2130")
            except Exception:
                pass

        if new_ch and id(new_ch) in self.channel_rows:
            r, n, f = self.channel_rows[id(new_ch)]
            try:
                r.configure(bg="#242c3f", highlightbackground="#00d2c4")
                n.configure(bg="#242c3f", fg="#00d2c4", font=("Segoe UI", 9, "bold"))
                f.configure(bg="#242c3f")
            except Exception:
                pass

    def _toggle_play_pause(self):
        if self.player.is_playing():
            self.player.pause()
            self.btn_play_pause.configure(text="▶ Resume", fg_color="#00adb5")
            if self._fs_osd is not None:
                self.fs_btn_play_pause.configure(text="▶ Resume", fg_color="#00adb5")
            self.lbl_live_badge.configure(text="⏸ Paused")
        else:
            if self.current_channel:
                self.player.resume()
                self.btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")
                if self._fs_osd is not None:
                    self.fs_btn_play_pause.configure(text="⏸ Pause", fg_color="#2563eb")
                badge = "🔴 LIVE" if self.current_channel.content_type == "live" else "▶ PLAYING"
                self.lbl_live_badge.configure(text=badge)
            elif self.displayed_items:
                self._play_item(self.displayed_items[0])

    def _stop_playback(self):
        self.player.stop()
        self.btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
        self.lbl_channel_title.configure(text="Playback Stopped")
        self.lbl_live_badge.configure(text="⏹ Stopped")
        self.seek_slider.set(0)
        self.lbl_curr_time.configure(text="00:00")
        self.lbl_total_time.configure(text="00:00")
        if self._fs_osd is not None:
            self.fs_btn_play_pause.configure(text="▶ Play", fg_color="#00adb5")
            self.fs_lbl_title.configure(text="Playback Stopped")
            self.fs_seek_slider.set(0)
            self.fs_lbl_curr_time.configure(text="00:00")
            self.fs_lbl_total_time.configure(text="00:00")
        if self.is_fullscreen:
            self._exit_fullscreen()
        self.placeholder_frame.grid()
        self.placeholder_frame.lift()

    def _toggle_recording(self):
        if self.recorder.is_recording:
            self.btn_record.configure(
                text="⏳ Saving...",
                state="disabled"
            )
            if self._fs_osd is not None:
                self.fs_btn_record.configure(
                    text="⏳ Saving...",
                    state="disabled"
                )
            threading.Thread(target=self.recorder.stop_recording, daemon=True).start()
        else:
            if not self.current_channel:
                messagebox.showwarning("Notice", "Please select and play a channel or stream before recording.")
                return

            success = self.recorder.start_recording(
                stream_url=self.current_channel.url,
                channel_name=self.current_channel.name,
                file_format=self.record_format,
                http_headers=self.current_channel.http_headers
            )

            if success:
                self.btn_record.configure(
                    text="⏹ Stop Recording",
                    fg_color="#991b1b",
                    hover_color="#7f1d1d"
                )
                self.lbl_record_stats.configure(text="⏱️ 00:00:00  💾 0.0 MB")
                if self._fs_osd is not None:
                    self.fs_btn_record.configure(
                        text="⏹ Stop Rec",
                        fg_color="#991b1b",
                        hover_color="#7f1d1d"
                    )
                    self.fs_lbl_record_stats.configure(text="⏱️ 00:00:00  💾 0.0 MB")
            else:
                messagebox.showerror("Error", "Could not start stream recording.")

    def _handle_recording_finished(self, filepath: str, success: bool, msg: str):
        self.btn_record.configure(
            state="normal",
            text="⏺ Record Stream",
            fg_color="#e02424",
            hover_color="#b91c1c"
        )
        self.lbl_record_stats.configure(text="")
        if self._fs_osd is not None:
            self.fs_btn_record.configure(
                state="normal",
                text="⏺ Record",
                fg_color="#e02424",
                hover_color="#b91c1c"
            )
            self.fs_lbl_record_stats.configure(text="")
        if success:
            res = messagebox.askyesno(
                "Recording Saved Successfully",
                f"{msg}\n\nFile Location:\n{filepath}\n\nDo you want to open the recordings directory now?"
            )
            if res:
                self.recorder.open_recordings_folder()
        else:
            messagebox.showwarning("Recording Notice", msg)

    def _toggle_item_favorite(self, channel: Channel):
        is_fav = self.storage.toggle_favorite(channel.url)
        if id(channel) in self.channel_rows:
            r, n, f = self.channel_rows[id(channel)]
            try:
                f.configure(text="★" if is_fav else "☆", fg="#f59e0b" if is_fav else "#64748b")
            except Exception:
                pass
        channels = self.content_cache.get(self.current_mode, {}).get("channels", [])
        fav_count = sum(1 for ch in channels if self.storage.is_favorite(ch.url))
        if "Favorites" in self.cat_rows:
            try:
                self.cat_rows["Favorites"][2].configure(text=f"{fav_count:,} ")
            except Exception:
                pass
        if self.current_category == "Favorites":
            self._filter_and_render_items()

    def _on_volume_change(self, val):
        vol = int(val)
        if vol > 0 and self.player.is_muted():
            self.player.set_mute(False)
        self.player.set_volume(vol)
        mute_icon = "🔇" if vol == 0 else "🔊"
        self.vol_label.configure(text=f"{vol}%")
        self.btn_mute.configure(text=mute_icon)
        if int(self.vol_slider.get()) != vol:
            self.vol_slider.set(vol)
        if self._fs_osd is not None:
            self.fs_vol_label.configure(text=f"{vol}%")
            self.fs_btn_mute.configure(text=mute_icon)
            if int(self.fs_vol_slider.get()) != vol:
                self.fs_vol_slider.set(vol)
        if self._vol_save_timer:
            self.after_cancel(self._vol_save_timer)
        self._vol_save_timer = self.after(400, lambda v=vol: self.storage.set_setting("volume", v))

    def _adjust_volume(self, delta: int):
        curr = self.player.get_volume()
        new_vol = max(0, min(100, curr + delta))
        self.vol_slider.set(new_vol)
        self._on_volume_change(new_vol)

    def _toggle_mute(self):
        is_muted = self.player.toggle_mute()
        icon = "🔇" if is_muted else "🔊"
        self.btn_mute.configure(text=icon)
        if self._fs_osd is not None:
            self.fs_btn_mute.configure(text=icon)

    def _cycle_aspect_ratio(self):
        new_ratio = self.player.cycle_aspect_ratio()
        self.storage.set_setting("aspect_ratio", new_ratio)
        self.btn_aspect.configure(text=f"📐 {new_ratio}")
        if self._fs_osd is not None:
            self.fs_btn_aspect.configure(text=f"📐 {new_ratio}")

    def _position_fullscreen_osd(self):
        """Anchor floating OSD bar cleanly to the bottom edge of the fullscreen window."""
        if self._fs_osd is None:
            return
        try:
            wx = self.winfo_rootx()
            wy = self.winfo_rooty()
            ww = self.winfo_width()
            wh = self.winfo_height()
            osd_h = 98
            self._fs_osd.geometry(f"{max(400, ww)}x{osd_h}+{wx}+{wy + wh - osd_h}")
        except Exception:
            pass

    def _sync_fullscreen_osd(self):
        """Sync all OSD controls to match current player and bottom bar state."""
        if self._fs_osd is None:
            return
        try:
            self.fs_btn_play_pause.configure(
                text=self.btn_play_pause.cget("text"),
                fg_color=self.btn_play_pause.cget("fg_color")
            )
            self.fs_btn_mute.configure(text=self.btn_mute.cget("text"))
            vol = self.player.get_volume()
            self.fs_vol_slider.set(vol)
            self.fs_vol_label.configure(text=f"{vol}%")
            self.fs_btn_aspect.configure(text=self.btn_aspect.cget("text"))
            title_txt = self.current_channel.name if self.current_channel else "No stream selected"
            self.fs_lbl_title.configure(text=title_txt)
            self.fs_seek_slider.set(self.seek_slider.get())
            self.fs_lbl_curr_time.configure(text=self.lbl_curr_time.cget("text"))
            self.fs_lbl_total_time.configure(text=self.lbl_total_time.cget("text"))
            seek_state = str(self.btn_seek_back.cget("state"))
            self.fs_btn_seek_back.configure(state=seek_state)
            self.fs_btn_seek_fwd.configure(state=seek_state)
            if self.recorder.is_recording:
                self.fs_btn_record.configure(state="normal", text="⏹ Stop Rec", fg_color="#991b1b", hover_color="#7f1d1d")
            elif str(self.btn_record.cget("state")) == "disabled":
                self.fs_btn_record.configure(state="disabled", text="⏳ Saving...")
            else:
                self.fs_btn_record.configure(state="normal", text="⏺ Record", fg_color="#e02424", hover_color="#b91c1c")
            self.fs_lbl_record_stats.configure(text=self.lbl_record_stats.cget("text"))
        except Exception:
            pass

    def _show_fullscreen_osd(self):
        if self._fs_osd is None:
            return
        self._sync_fullscreen_osd()
        self._position_fullscreen_osd()
        if not self._fs_osd_visible:
            try:
                self._fs_osd.deiconify()
                self._fs_osd.lift()
                self._fs_osd.attributes("-topmost", True)
                self._fs_osd_visible = True
            except Exception:
                pass

    def _hide_fullscreen_osd(self):
        if self._fs_osd is not None and self._fs_osd_visible:
            try:
                self._fs_osd.withdraw()
            except Exception:
                pass
            self._fs_osd_visible = False

    def _poll_fullscreen_osd(self):
        """Show floating OSD on bottom hover and hide immediately as soon as pointer leaves it."""
        self._fs_osd_poll_id = None
        if not self.is_fullscreen:
            self._hide_fullscreen_osd()
            return
        try:
            mx, my = self.winfo_pointerxy()
            wx = self.winfo_rootx()
            wy = self.winfo_rooty()
            ww = self.winfo_width()
            wh = self.winfo_height()
            osd_top = (wy + wh - 98) if self._fs_osd_visible else (wy + wh - 75)
            in_bottom_zone = (wx <= mx <= wx + ww) and (osd_top <= my <= wy + wh + 5)

            if in_bottom_zone or self.is_seeking:
                self._show_fullscreen_osd()
            elif self._fs_osd_visible:
                self._hide_fullscreen_osd()
        except Exception:
            pass

        if self.is_fullscreen:
            self._fs_osd_poll_id = self.after(80, self._poll_fullscreen_osd)

    def _toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        self.attributes("-fullscreen", self.is_fullscreen)
        if self.is_fullscreen:
            self.top_bar.grid_remove()
            self.sidebar.grid_remove()
            self.bottom_bar.grid_remove()
            self.top_info_strip.grid_remove()

            # Make viewport container and video frame take 100% of the entire window/screen
            self.viewport_container.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
            self.video_frame.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
            self.video_frame.lift()

            try:
                self.player.player.video_set_scale(0)
                # Auto-fill screen without black borders
                self.player.set_aspect_ratio("Fill")
            except Exception:
                pass

            self.btn_aspect.configure(text="📐 Fill")
            if self._fs_osd is not None:
                self.fs_btn_aspect.configure(text="📐 Fill")
            self.update_idletasks()
            self._sync_fullscreen_osd()
            if self._fs_osd_poll_id:
                try:
                    self.after_cancel(self._fs_osd_poll_id)
                except Exception:
                    pass
                self._fs_osd_poll_id = None
            self._poll_fullscreen_osd()
        else:
            if self._fs_osd_poll_id:
                try:
                    self.after_cancel(self._fs_osd_poll_id)
                except Exception:
                    pass
                self._fs_osd_poll_id = None
            self._hide_fullscreen_osd()

            self.video_frame.place_forget()
            self.viewport_container.place_forget()

            self.viewport_container.grid(row=1, column=1, sticky="nsew")
            self.video_frame.grid(row=1, column=0, sticky="nsew")

            self.top_bar.grid()
            if self.sidebar_visible:
                self.sidebar.grid()
            self.bottom_bar.grid()
            self.top_info_strip.grid()

            # Restore original aspect ratio
            saved_aspect = self.storage.get_setting("aspect_ratio", "default")
            self.player.set_aspect_ratio(saved_aspect)
            self.btn_aspect.configure(text=f"📐 {saved_aspect}")
            if self._fs_osd is not None:
                self.fs_btn_aspect.configure(text=f"📐 {saved_aspect}")

    def _exit_fullscreen(self):
        if self.is_fullscreen:
            self._toggle_fullscreen()


    def _toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
        if self.sidebar_visible:
            self.sidebar.grid()
            self.btn_toggle_sidebar.configure(fg_color="#1c2130")
        else:
            self.sidebar.grid_remove()
            self.btn_toggle_sidebar.configure(fg_color="#334155")

    def _open_file_dialog(self):
        filetypes = [
            ("M3U Playlist Files", "*.m3u;*.m3u8;*.txt"),
            ("All Files", "*.*")
        ]
        chosen = filedialog.askopenfilename(title="Select M3U Playlist File", filetypes=filetypes)
        if chosen:
            self._reset_content_cache()
            self._load_playlist_async(chosen, mode="live")

    @staticmethod
    def _build_xtream_url(server_url: str, username: str, password: str) -> str:
        """Normalize Xtream Codes server URL and build authenticated M3U playlist URL."""
        clean_server = server_url.strip().rstrip("/")
        if clean_server and not clean_server.startswith(("http://", "https://")):
            clean_server = f"http://{clean_server}"
        u_enc = urllib.parse.quote(username.strip(), safe="")
        p_enc = urllib.parse.quote(password, safe="")
        return f"{clean_server}/get.php?username={u_enc}&password={p_enc}&type=m3u_plus&output=ts"

    def _switch_to_live_and_load(self, playlist_url: str):
        """Reset caches, highlight LIVE TV tab, and load playlist URL."""
        self.current_mode = "live"
        self.in_episodes_view = False
        self.btn_back_series.pack_forget()
        self.btn_mode_live.configure(fg_color="#00adb5", text_color="#ffffff")
        self.btn_mode_vod.configure(fg_color="transparent", text_color="#94a3b8")
        self.btn_mode_series.configure(fg_color="transparent", text_color="#94a3b8")

        self._reset_content_cache()
        self._load_playlist_async(playlist_url.strip(), mode="live", skip_disk_cache=True)

    def _connect_server(self, srv: dict):
        """Connect to a saved server dictionary and load its Live TV list."""
        url = srv.get("url", "").strip()
        if not url:
            return
        self._switch_to_live_and_load(url)

    def _open_url_dialog(self, on_saved=None, parent_win=None):
        master = parent_win if (parent_win is not None and parent_win.winfo_exists()) else self
        win = ctk.CTkToplevel(master)
        win.title("Add IPTV Server (Xtream / M3U)")
        win.geometry("500x450")
        win.resizable(False, False)
        win.transient(master)
        win.grab_set()

        def close_win():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if parent_win is not None:
                try:
                    if parent_win.winfo_exists():
                        parent_win.grab_set()
                        parent_win.focus_force()
                except Exception:
                    pass

        win.protocol("WM_DELETE_WINDOW", close_win)

        ctk.CTkLabel(
            win,
            text="🌐 Add Xtream Server or M3U URL",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#00d2c4"
        ).pack(pady=(14, 6))

        tabview = ctk.CTkTabview(win, height=275, segmented_button_selected_color="#00adb5", segmented_button_selected_hover_color="#00939a")
        tabview.pack(fill="both", expand=True, padx=20, pady=(4, 8))

        tab_xtream = tabview.add("Xtream Codes")
        tab_url = tabview.add("M3U / Direct URL")

        # --- Tab 1: Xtream Codes ---
        ctk.CTkLabel(tab_xtream, text="IPTV Name:", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(6, 1))
        xt_name = ctk.CTkEntry(tab_xtream, placeholder_text="e.g. My IPTV Server", height=32)
        xt_name.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(tab_xtream, text="Server URL:", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(2, 1))
        xt_server = ctk.CTkEntry(tab_xtream, placeholder_text="e.g. http://server.com:8080", height=32)
        xt_server.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(tab_xtream, text="Username:", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(2, 1))
        xt_user = ctk.CTkEntry(tab_xtream, placeholder_text="Username", height=32)
        xt_user.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(tab_xtream, text="Password:", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(2, 1))
        pwd_row = ctk.CTkFrame(tab_xtream, fg_color="transparent")
        pwd_row.pack(fill="x", padx=10, pady=(0, 4))

        xt_pass = ctk.CTkEntry(pwd_row, placeholder_text="Password", show="•", height=32)
        xt_pass.pack(side="left", fill="x", expand=True, padx=(0, 6))

        pwd_visible = {"shown": False}

        def toggle_pwd():
            pwd_visible["shown"] = not pwd_visible["shown"]
            xt_pass.configure(show="" if pwd_visible["shown"] else "•")
            btn_eye.configure(text="🙈" if pwd_visible["shown"] else "👁")

        btn_eye = ctk.CTkButton(pwd_row, text="👁", width=36, height=32, fg_color="#1e293b", hover_color="#334155", command=toggle_pwd)
        btn_eye.pack(side="right")

        # --- Tab 2: M3U / Direct URL ---
        ctk.CTkLabel(tab_url, text="IPTV Name (Optional):", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(14, 2))
        url_name = ctk.CTkEntry(tab_url, placeholder_text="e.g. Sports Playlist", height=32)
        url_name.pack(fill="x", padx=10, pady=(0, 12))

        ctk.CTkLabel(tab_url, text="M3U / Xtream URL:", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(2, 2))
        url_entry = ctk.CTkEntry(tab_url, placeholder_text="http://server.com/get.php?username=...&password=...", height=34)
        url_entry.pack(fill="x", padx=10, pady=(0, 8))

        # --- Save Checkbox & Buttons ---
        save_var = ctk.BooleanVar(value=True)
        chk_save = ctk.CTkCheckBox(win, text="Save to Servers list", variable=save_var, font=ctk.CTkFont(size=12))
        chk_save.pack(anchor="w", padx=24, pady=(0, 8))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 14))

        def on_connect():
            active_tab = tabview.get()
            if active_tab == "Xtream Codes":
                name_val = xt_name.get().strip()
                srv_val = xt_server.get().strip()
                usr_val = xt_user.get().strip()
                pwd_val = xt_pass.get()

                if not srv_val or not usr_val or not pwd_val:
                    messagebox.showerror("Missing Details", "Please enter Server URL, Username, and Password.", parent=win)
                    return

                clean_srv = srv_val.rstrip("/")
                if not clean_srv.startswith(("http://", "https://")):
                    clean_srv = f"http://{clean_srv}"

                final_url = self._build_xtream_url(clean_srv, usr_val, pwd_val)
                if not name_val:
                    name_val = urllib.parse.urlparse(clean_srv).netloc or "Xtream Server"

                if save_var.get():
                    self.storage.add_server(
                        name=name_val,
                        url=final_url,
                        server_type="xtream",
                        server_url=clean_srv,
                        username=usr_val,
                        password=pwd_val
                    )
            else:
                name_val = url_name.get().strip()
                raw_url = url_entry.get().strip()
                if not raw_url.startswith(("http://", "https://")):
                    messagebox.showerror("Invalid URL", "URL must start with http:// or https://", parent=win)
                    return

                final_url = raw_url
                if not name_val:
                    name_val = urllib.parse.urlparse(final_url).netloc or "M3U Server"

                if save_var.get():
                    self.storage.add_server(
                        name=name_val,
                        url=final_url,
                        server_type="m3u"
                    )

            if on_saved:
                try:
                    on_saved()
                except Exception:
                    pass

            close_win()
            if parent_win is not None:
                try:
                    if parent_win.winfo_exists():
                        parent_win.destroy()
                except Exception:
                    pass

            self._switch_to_live_and_load(final_url)

        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=110,
            height=34,
            fg_color="#1e293b",
            hover_color="#334155",
            command=close_win
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            btn_row,
            text="Connect & Load",
            width=150,
            height=34,
            fg_color="#00adb5",
            hover_color="#00939a",
            font=ctk.CTkFont(weight="bold"),
            command=on_connect
        ).pack(side="right")

        win._widgets_ref = {
            "tabview": tabview,
            "xt_name": xt_name,
            "xt_server": xt_server,
            "xt_user": xt_user,
            "xt_pass": xt_pass,
            "url_name": url_name,
            "url_entry": url_entry,
            "save_var": save_var,
            "on_connect": on_connect,
        }
        return win

    def _open_servers_dialog(self):
        srv_win = ctk.CTkToplevel(self)
        srv_win.title("Saved IPTV Servers")
        srv_win.geometry("560x470")
        srv_win.minsize(500, 380)
        srv_win.transient(self)
        srv_win.grab_set()

        header = ctk.CTkFrame(srv_win, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(14, 8))

        ctk.CTkLabel(
            header,
            text="🖥️ Saved IPTV Servers",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#00d2c4"
        ).pack(side="left")

        scroll_list = ctk.CTkScrollableFrame(srv_win, fg_color="#0e111a", corner_radius=8)
        scroll_list.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        def refresh_list():
            for child in scroll_list.winfo_children():
                child.destroy()

            servers = self.storage.get_servers()
            if not servers:
                ctk.CTkLabel(
                    scroll_list,
                    text="No saved servers yet.\nClick '+ Add Server' to add an Xtream account or M3U URL.",
                    font=ctk.CTkFont(size=13),
                    text_color="#64748b",
                    justify="center"
                ).pack(pady=80)
                return

            for srv in servers:
                is_active = (srv.get("url", "") == self.current_playlist_target and bool(self.current_playlist_target))
                card = ctk.CTkFrame(
                    scroll_list,
                    fg_color="#1c2130",
                    border_width=1,
                    border_color="#00d2c4" if is_active else "#283248",
                    corner_radius=8
                )
                card.pack(fill="x", padx=4, pady=4)

                info_col = ctk.CTkFrame(card, fg_color="transparent")
                info_col.pack(side="left", fill="both", expand=True, padx=12, pady=10)

                title_row = ctk.CTkFrame(info_col, fg_color="transparent")
                title_row.pack(fill="x")

                srv_type = (srv.get("type") or "m3u").upper()
                type_color = "#38bdf8" if srv_type == "XTREAM" else "#a78bfa"
                ctk.CTkLabel(
                    title_row,
                    text=srv_type,
                    font=ctk.CTkFont(size=10, weight="bold"),
                    fg_color="#131722",
                    text_color=type_color,
                    corner_radius=4,
                    padx=6,
                    pady=1
                ).pack(side="left", padx=(0, 8))

                ctk.CTkLabel(
                    title_row,
                    text=srv.get("name", "IPTV Server"),
                    font=ctk.CTkFont(size=13, weight="bold"),
                    text_color="#00d2c4" if is_active else "#f8fafc",
                    anchor="w"
                ).pack(side="left")

                if is_active:
                    ctk.CTkLabel(
                        title_row,
                        text="● ACTIVE",
                        font=ctk.CTkFont(size=10, weight="bold"),
                        text_color="#34d399",
                        padx=8
                    ).pack(side="left")

                if srv.get("type") == "xtream":
                    sub_txt = f"{srv.get('server_url', '')}  •  User: {srv.get('username', '')}"
                else:
                    raw_u = srv.get("url", "")
                    sub_txt = raw_u if len(raw_u) <= 58 else raw_u[:55] + "..."

                ctk.CTkLabel(
                    info_col,
                    text=sub_txt,
                    font=ctk.CTkFont(size=11),
                    text_color="#94a3b8",
                    anchor="w"
                ).pack(fill="x", pady=(3, 0))

                btns_col = ctk.CTkFrame(card, fg_color="transparent")
                btns_col.pack(side="right", padx=10, pady=10)

                def do_connect(s=srv):
                    srv_win.destroy()
                    self._connect_server(s)

                def do_delete(s=srv):
                    if messagebox.askyesno("Delete Server", f"Remove server '{s.get('name', '')}'?", parent=srv_win):
                        self.storage.delete_server(s.get("id", ""))
                        refresh_list()

                ctk.CTkButton(
                    btns_col,
                    text="▶ Connect",
                    width=88,
                    height=30,
                    fg_color="#00adb5",
                    hover_color="#00939a",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    command=do_connect
                ).pack(side="left", padx=(0, 6))

                ctk.CTkButton(
                    btns_col,
                    text="🗑️",
                    width=34,
                    height=30,
                    fg_color="#2a1c22",
                    hover_color="#991b1b",
                    command=do_delete
                ).pack(side="left")

        ctk.CTkButton(
            header,
            text="+ Add Server",
            width=115,
            height=32,
            fg_color="#00adb5",
            hover_color="#00939a",
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self._open_url_dialog(on_saved=refresh_list, parent_win=srv_win)
        ).pack(side="right")

        refresh_list()
        srv_win._refresh_list = refresh_list
        return srv_win

    def _open_settings_dialog(self):
        settings_win = ctk.CTkToplevel(self)
        settings_win.title("Player & Recording Settings")
        settings_win.geometry("520x360")
        settings_win.grab_set()

        ctk.CTkLabel(
            settings_win,
            text="⚙️ Player & Recording Settings",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(pady=15)

        fmt_frame = ctk.CTkFrame(settings_win, fg_color="transparent")
        fmt_frame.pack(fill="x", padx=25, pady=8)

        ctk.CTkLabel(fmt_frame, text="Recording Output Format:", font=ctk.CTkFont(size=13)).pack(side="left")

        fmt_menu = ctk.CTkOptionMenu(
            fmt_frame,
            values=["mp4 (Recommended)", "ts (Transport Stream)"],
            width=180
        )
        fmt_menu.pack(side="right")
        current_fmt = self.storage.get_setting("record_format", "mp4")
        fmt_menu.set("mp4 (Recommended)" if current_fmt == "mp4" else "ts (Transport Stream)")

        dir_frame = ctk.CTkFrame(settings_win, fg_color="transparent")
        dir_frame.pack(fill="x", padx=25, pady=12)

        ctk.CTkLabel(dir_frame, text="Recordings Save Directory:", font=ctk.CTkFont(size=13)).pack(anchor="w")

        dir_sub = ctk.CTkFrame(dir_frame, fg_color="transparent")
        dir_sub.pack(fill="x", pady=5)

        curr_dir = self.recorder.output_dir
        dir_entry = ctk.CTkEntry(dir_sub)
        dir_entry.insert(0, curr_dir)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def browse_folder():
            f = filedialog.askdirectory(title="Choose Recordings Directory")
            if f:
                dir_entry.delete(0, "end")
                dir_entry.insert(0, f)

        ctk.CTkButton(dir_sub, text="Browse 📁", width=90, command=browse_folder).pack(side="right")

        ff_frame = ctk.CTkFrame(settings_win, fg_color="#181c28", corner_radius=8)
        ff_frame.pack(fill="x", padx=25, pady=10)

        ff_found = bool(self.recorder.ffmpeg_path)
        ff_status_text = f"Recording Engine: FFmpeg Ready ({os.path.basename(self.recorder.ffmpeg_path)})" if ff_found else "Recording Engine: Direct Native Stream Capture"
        ctk.CTkLabel(
            ff_frame,
            text=ff_status_text,
            text_color="#34d399" if ff_found else "#fbbf24",
            font=ctk.CTkFont(size=12)
        ).pack(padx=10, pady=8)

        def save_and_close():
            chosen_val = fmt_menu.get()
            new_fmt = "mp4" if "mp4" in chosen_val else "ts"
            new_dir = dir_entry.get().strip()

            if new_dir:
                try:
                    os.makedirs(new_dir, exist_ok=True)
                    self.recorder.output_dir = new_dir
                    self.storage.set_setting("recordings_dir", new_dir)
                except Exception as e:
                    messagebox.showerror("Invalid Directory", f"Could not create or access directory:\n{e}")
                    return

            self.record_format = new_fmt
            self.storage.set_setting("record_format", new_fmt)

            settings_win.destroy()

        ctk.CTkButton(
            settings_win,
            text="Save Settings",
            width=140,
            height=36,
            fg_color="#00adb5",
            hover_color="#00939a",
            command=save_and_close
        ).pack(pady=15)

    def destroy(self):
        if self._fs_osd_poll_id:
            try:
                self.after_cancel(self._fs_osd_poll_id)
            except Exception:
                pass
            self._fs_osd_poll_id = None
        if self._fs_osd is not None:
            try:
                self._fs_osd.destroy()
            except Exception:
                pass
            self._fs_osd = None
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        self.player.release()
        super().destroy()


if __name__ == "__main__":
    app = SmartIPTVApp()
    app.mainloop()
