"""
IPTV Stream Recorder Engine
Supports instant one-click recording to MP4 or TS formats using FFmpeg
with robust timestamp alignment (-avoid_negative_ts make_zero) and universal audio compatibility.
"""

import os
import sys
import glob
import time
import shutil
import subprocess
import threading
import re
from typing import Optional, Callable


class StreamRecorder:
    def __init__(self, output_dir: Optional[str] = None):
        """
        Initialize the stream recorder.
        :param output_dir: Target directory for recorded videos.
        """
        if output_dir:
            self.output_dir = output_dir
        else:
            user_videos = os.path.join(os.path.expanduser("~"), "Videos", "IPTV_Recordings")
            self.output_dir = user_videos

        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception:
            self.output_dir = os.path.join(os.path.expanduser("~"), "Videos", "IPTV_Recordings")
            try:
                os.makedirs(self.output_dir, exist_ok=True)
            except Exception:
                self.output_dir = os.path.join(os.getcwd(), "recordings")
                os.makedirs(self.output_dir, exist_ok=True)

        self.ffmpeg_path = self._find_ffmpeg()
        self.is_recording = False
        self.current_file: Optional[str] = None
        self.channel_name: str = ""
        self.stream_url: str = ""
        self.start_time: float = 0.0

        self._process: Optional[subprocess.Popen] = None
        self._temp_part_file: Optional[str] = None
        self._py_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stop_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_stats_update: Optional[Callable[[str, str], None]] = None
        self.on_finished: Optional[Callable[[str, bool, str], None]] = None

    def _find_ffmpeg(self) -> Optional[str]:
        """Auto-detect FFmpeg binary across common Windows locations."""
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        bundle_dir = getattr(sys, "_MEIPASS", exe_dir)

        candidates = [
            shutil.which("ffmpeg"),
            os.path.join(exe_dir, "ffmpeg.exe"),
            os.path.join(bundle_dir, "ffmpeg.exe"),
            os.path.join(os.path.dirname(__file__), "ffmpeg.exe"),
            r"C:\Program Files (x86)\FormatFactory\ffmpeg.exe",
            r"C:\Program Files (x86)\Digiarty\Winxvideo AI\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
        ]

        # Expand WinGet package glob patterns
        winget_pattern = os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe"
        )
        candidates.extend(glob.glob(winget_pattern))

        for path in candidates:
            if path and os.path.exists(path):
                return path

        return None

    def start_recording(
        self,
        stream_url: str,
        channel_name: str,
        file_format: str = "mp4",
        http_headers: Optional[dict] = None
    ) -> bool:
        """
        Start recording stream immediately in the background.
        :param stream_url: The media stream URL.
        :param channel_name: Name of the channel for filename generation.
        :param file_format: 'mp4' or 'ts'.
        :param http_headers: Optional custom headers (User-Agent, Referer).
        """
        if self.is_recording:
            return False

        self.stream_url = stream_url
        self.channel_name = channel_name or "IPTV_Channel"
        self._stop_event.clear()

        # Sanitize channel name for safe Windows filename
        safe_name = re.sub(r'[\\/*?:"<>| ]', '_', self.channel_name)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        ext = file_format.lower().strip(".")
        if ext not in ("mp4", "ts", "mkv"):
            ext = "mp4"

        filename = f"{safe_name}_{timestamp}.{ext}"
        self.current_file = os.path.join(self.output_dir, filename)
        self.start_time = time.time()
        self.is_recording = True

        if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
            self._start_ffmpeg_recording(http_headers, ext)
        else:
            self._start_python_recording(http_headers)

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        return True

    def _start_ffmpeg_recording(self, headers: Optional[dict], ext: str):
        """Record using FFmpeg with robust audio normalization and timestamp alignment."""
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel", "error"
        ]

        ua_set = False
        if headers:
            for k, v in headers.items():
                k_lower = k.lower()
                if "user-agent" in k_lower:
                    cmd.extend(["-user_agent", str(v)])
                    ua_set = True
                elif "refer" in k_lower:
                    cmd.extend(["-headers", f"Referer: {v}\r\n"])

        if not ua_set:
            cmd.extend(["-user_agent", "IPTVSmartersPro/2.2.1.2 (Linux; Android 10)"])

        # Input URL
        cmd.extend(["-i", self.stream_url])

        self._temp_part_file = self.current_file + ".part" if ext == "mp4" else self.current_file

        if ext == "mp4":
            # Record in crash-proof fragmented MP4 mode, normalize audio to AAC for universal playback
            cmd.extend([
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
                self._temp_part_file
            ])
        else:
            # Transport Stream: direct copy
            cmd.extend([
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-f", "mpegts",
                self._temp_part_file
            ])

        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags
        )

    def _start_python_recording(self, http_headers: Optional[dict] = None):
        """Fallback direct stream recording using requests."""
        def run_py_rec():
            import requests
            try:
                headers = {"User-Agent": "IPTVSmartersPro/2.2.1.2 (Linux; Android 10)"}
                if http_headers:
                    for k, v in http_headers.items():
                        k_lower = k.lower()
                        if "user-agent" in k_lower:
                            headers["User-Agent"] = str(v)
                        elif "refer" in k_lower:
                            headers["Referer"] = str(v)
                        else:
                            headers[str(k)] = str(v)
                with requests.get(self.stream_url, headers=headers, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    with open(self.current_file, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if self._stop_event.is_set():
                                break
                            if chunk:
                                f.write(chunk)
            except Exception:
                pass

        self._py_thread = threading.Thread(target=run_py_rec, daemon=True)
        self._py_thread.start()

    def _monitor_loop(self):
        """Continuously update elapsed recording time and file size."""
        while self.is_recording and not self._stop_event.is_set():
            time.sleep(1.0)
            if not self.is_recording:
                break

            if self._process is not None and self._process.poll() is not None:
                self.stop_recording()
                break
            if self._process is None and self._py_thread is not None and not self._py_thread.is_alive():
                self.stop_recording()
                break

            elapsed = int(time.time() - self.start_time)
            hours = elapsed // 3600
            minutes = (elapsed % 3600) // 60
            seconds = elapsed % 60
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            size_mb = 0.0
            check_file = self._temp_part_file or self.current_file
            if check_file and os.path.exists(check_file):
                try:
                    size_bytes = os.path.getsize(check_file)
                    size_mb = size_bytes / (1024 * 1024)
                except Exception:
                    pass

            size_str = f"{size_mb:.1f} MB"

            if self.on_stats_update:
                try:
                    self.on_stats_update(duration_str, size_str)
                except Exception:
                    pass

    def stop_recording(self) -> Optional[str]:
        """Gracefully stop recording and finalize media container."""
        with self._stop_lock:
            if not self.is_recording:
                return None

            self.is_recording = False
            self._stop_event.set()

        # Stop FFmpeg process safely
        if self._process:
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

        if self._py_thread:
            self._py_thread.join(timeout=2.0)
            self._py_thread = None

        # If recorded to a temporary fragmented .part file, remux into standard faststart MP4
        if self._temp_part_file and self._temp_part_file != self.current_file and os.path.exists(self._temp_part_file):
            if os.path.getsize(self._temp_part_file) > 1024:
                remux_done = False
                if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
                    remux_cmd = [
                        self.ffmpeg_path,
                        "-y",
                        "-hide_banner",
                        "-loglevel", "error",
                        "-i", self._temp_part_file,
                        "-c", "copy",
                        "-movflags", "+faststart",
                        self.current_file
                    ]
                    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    try:
                        subprocess.run(
                            remux_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=cflags,
                            timeout=10
                        )
                        if os.path.exists(self.current_file) and os.path.getsize(self.current_file) > 1024:
                            remux_done = True
                            try:
                                os.remove(self._temp_part_file)
                            except Exception:
                                pass
                    except Exception:
                        pass

                if not remux_done:
                    try:
                        if os.path.exists(self.current_file):
                            os.remove(self.current_file)
                        os.rename(self._temp_part_file, self.current_file)
                    except Exception:
                        pass
            else:
                try:
                    os.remove(self._temp_part_file)
                except Exception:
                    pass

        recorded_path = self.current_file
        file_valid = bool(recorded_path and os.path.exists(recorded_path) and os.path.getsize(recorded_path) > 1024)

        if self.on_finished:
            try:
                msg = f"Recording saved successfully!\nSize: {os.path.getsize(recorded_path)/(1024*1024):.1f} MB" if file_valid else "Failed to capture recording data."
                self.on_finished(recorded_path or "", file_valid, msg)
            except Exception:
                pass

        return recorded_path if file_valid else None

    def open_recordings_folder(self):
        """Open the recordings directory in Windows File Explorer."""
        if os.path.exists(self.output_dir):
            if sys.platform == "win32":
                os.startfile(self.output_dir)
            else:
                subprocess.Popen(["xdg-open", self.output_dir])
