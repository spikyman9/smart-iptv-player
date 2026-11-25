"""
VLC Player Engine for Smart IPTV Player
Handles media playback, audio/video synchronization, embedded window rendering,
low-latency IPTV buffer tuning, volume, and aspect ratio controls.
"""

import os
import sys
import vlc
from typing import Optional, Callable


class VLCPlayerEngine:
    def __init__(self, hwnd: Optional[int] = None):
        """
        Initialize VLC instance with IPTV-optimized arguments.
        :param hwnd: Windows HWND handle where the video will be rendered.
        """
        self.hwnd = hwnd
        self._current_url = ""
        self._current_aspect_ratio = "default"
        self._is_muted = False
        self._volume = 85
        self.on_state_change: Optional[Callable[[str], None]] = None

        # Tune libvlc for fast, low-latency live IPTV streams
        vlc_args = [
            "--network-caching=800",
            "--live-caching=800",
            "--file-caching=800",
            "--no-video-title-show",
            "--no-snapshot-preview",
            "--quiet",
            "--no-xlib",
            "--avcodec-hw=any",
            "--no-stats",
            "--no-osd",
            "--drop-late-frames",
            "--skip-frames",
            "--clock-jitter=0",
            "--clock-synchro=0"
        ]

        # Portable & System VLC Discovery
        search_dirs = []
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
            bundle_dir = getattr(sys, "_MEIPASS", base_dir)
            search_dirs.extend([
                os.path.join(bundle_dir, "vlc"),
                bundle_dir,
                os.path.join(base_dir, "vlc"),
                os.path.join(base_dir, "_internal", "vlc"),
                os.path.join(base_dir, "_internal"),
                base_dir,
            ])
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            search_dirs.extend([
                os.path.join(base_dir, "vlc"),
                base_dir,
            ])

        search_dirs.extend([
            r"C:\Program Files\VideoLAN\VLC",
            r"C:\Program Files (x86)\VideoLAN\VLC",
        ])

        found_vlc = None
        for d in search_dirs:
            if d and os.path.exists(os.path.join(d, "libvlc.dll")):
                found_vlc = d
                break

        if found_vlc:
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(found_vlc)
                except Exception:
                    pass
            plugins_dir = os.path.join(found_vlc, "plugins")
            if os.path.exists(plugins_dir):
                os.environ["VLC_PLUGIN_PATH"] = plugins_dir

        try:
            self.instance = vlc.Instance(" ".join(vlc_args))
            self.player = self.instance.media_player_new()
        except Exception:
            self.instance = None
            self.player = None

        if self.player:
            try:
                self.player.video_set_mouse_input(False)
                self.player.video_set_key_input(False)
                self.player.video_set_scale(0)
            except Exception:
                pass

        if self.hwnd is not None and self.player:
            self.set_window_handle(self.hwnd)

        self.set_volume(self._volume)

    def is_available(self) -> bool:
        """Return True if libvlc initialized successfully."""
        return self.player is not None and self.instance is not None

    def set_window_handle(self, hwnd: int):
        """Assign native window handle for video rendering."""
        self.hwnd = hwnd
        if not self.player:
            return
        if sys.platform == "win32":
            self.player.set_hwnd(hwnd)
        elif sys.platform.startswith("linux"):
            self.player.set_xwindow(hwnd)
        elif sys.platform == "darwin":
            self.player.set_nsobject(hwnd)

    def play_stream(self, url: str, headers: Optional[dict] = None) -> bool:
        """
        Play a live stream or VOD media URL.
        Supports passing custom HTTP referer and user-agent.
        """
        if not url or not self.player or not self.instance:
            return False

        self.stop()
        self._current_url = url

        media: vlc.Media = self.instance.media_new(url)

        # Apply custom HTTP headers if provided by playlist
        if headers:
            for k, v in headers.items():
                if "user-agent" in k:
                    media.add_option(f":http-user-agent={v}")
                elif "refer" in k:
                    media.add_option(f":http-referrer={v}")

        # Fast low-latency IPTV buffering
        media.add_option(":network-caching=800")
        media.add_option(":live-caching=800")
        media.add_option(":file-caching=800")

        self.player.set_media(media)

        if self.hwnd is not None:
            self.set_window_handle(self.hwnd)

        try:
            self.player.video_set_scale(0)
        except Exception:
            pass

        self.player.play()
        return True

    def pause(self):
        """Toggle pause state."""
        if not self.player:
            return
        self.player.pause()

    def resume(self):
        """Resume playback if paused."""
        if not self.player:
            return
        if not self.is_playing():
            self.player.play()

    def stop(self):
        """Stop playback completely."""
        if not self.player:
            return
        try:
            self.player.stop()
        except Exception:
            pass

    def is_playing(self) -> bool:
        """Check if media is currently actively playing."""
        if not self.player:
            return False
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False

    def get_state(self) -> str:
        """Return human-readable player state."""
        if not self.player:
            return "غير متاح"
        state = self.player.get_state()
        state_map = {
            vlc.State.NothingSpecial: "خامل",
            vlc.State.Opening: "جاري الفتح...",
            vlc.State.Buffering: "جاري التحميل...",
            vlc.State.Playing: "شغال",
            vlc.State.Paused: "متوقف مؤقتاً",
            vlc.State.Stopped: "متوقف",
            vlc.State.Ended: "انتهى",
            vlc.State.Error: "خطأ في البث"
        }
        return state_map.get(state, "غير معروف")

    def get_time(self) -> int:
        """Get current playback position in milliseconds."""
        try:
            return self.player.get_time()
        except Exception:
            return 0

    def get_length(self) -> int:
        """Get total media length in milliseconds."""
        try:
            return self.player.get_length()
        except Exception:
            return 0

    def set_time(self, time_ms: int):
        """Set playback position in milliseconds."""
        try:
            self.player.set_time(max(0, time_ms))
        except Exception:
            pass

    def get_position(self) -> float:
        """Get playback position as a fraction from 0.0 to 1.0."""
        try:
            return max(0.0, self.player.get_position())
        except Exception:
            return 0.0

    def set_position(self, pos: float):
        """Set playback position as a fraction from 0.0 to 1.0."""
        try:
            self.player.set_position(max(0.0, min(1.0, pos)))
        except Exception:
            pass

    def seek_relative(self, seconds: int):
        """Seek forward or backward by a given number of seconds."""
        try:
            curr = self.player.get_time()
            total = self.player.get_length()
            if curr >= 0:
                target = max(0, curr + (seconds * 1000))
                if total > 0:
                    target = min(total - 1000, target)
                self.player.set_time(target)
        except Exception:
            pass

    def set_volume(self, volume: int):
        """Set volume between 0 and 100."""
        self._volume = max(0, min(100, volume))
        if self.player:
            self.player.audio_set_volume(self._volume)

    def get_volume(self) -> int:
        return self._volume

    def toggle_mute(self) -> bool:
        """Toggle mute state. Returns current muted status."""
        self._is_muted = not self._is_muted
        if self.player:
            self.player.audio_set_mute(self._is_muted)
        return self._is_muted

    def set_mute(self, muted: bool):
        """Explicitly set mute state."""
        self._is_muted = bool(muted)
        if self.player:
            self.player.audio_set_mute(self._is_muted)

    def is_muted(self) -> bool:
        return self._is_muted

    def set_aspect_ratio(self, ratio_str: str):
        """Set video aspect ratio: 'default', 'Fill', '16:9', '4:3' or None."""
        self._current_aspect_ratio = ratio_str
        if not self.player:
            return
        if not ratio_str or ratio_str in ("default", "original"):
            self.player.video_set_aspect_ratio(None)
        elif ratio_str == "Fill":
            try:
                import ctypes
                w = ctypes.windll.user32.GetSystemMetrics(0)
                h = ctypes.windll.user32.GetSystemMetrics(1)
                if w and h:
                    self.player.video_set_aspect_ratio(f"{w}:{h}".encode("utf-8"))
                else:
                    self.player.video_set_aspect_ratio(b"16:9")
            except Exception:
                self.player.video_set_aspect_ratio(b"16:9")
        else:
            self.player.video_set_aspect_ratio(ratio_str.encode("utf-8"))

    def cycle_aspect_ratio(self) -> str:
        """Cycle through common aspect ratios: Fill, 16:9, 4:3, Default."""
        ratios = ["Fill", "16:9", "4:3", "default"]
        curr_idx = 0
        if self._current_aspect_ratio in ratios:
            curr_idx = ratios.index(self._current_aspect_ratio)
        next_ratio = ratios[(curr_idx + 1) % len(ratios)]

        self.set_aspect_ratio(next_ratio)
        self._current_aspect_ratio = next_ratio
        return next_ratio

    def get_media_info(self) -> dict:
        """Get stream resolution and metadata."""
        if not self.player:
            return {"width": 0, "height": 0, "resolution": "No VLC"}
        size = self.player.video_get_size(0)
        width, height = (size[0], size[1]) if size else (0, 0)
        return {
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else "HD Live"
        }

    def release(self):
        """Release all VLC resources on app exit."""
        try:
            self.stop()
            if self.player:
                self.player.release()
            if self.instance:
                self.instance.release()
        except Exception:
            pass
