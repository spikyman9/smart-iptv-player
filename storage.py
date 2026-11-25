"""
Storage & Settings Manager for Smart IPTV Player
Handles persistent favorites, recent playlists, and user preferences.
"""

import os
import sys
import json
import uuid
import shutil
import urllib.parse
from typing import List, Dict, Set


class StorageManager:
    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.is_portable = (
            os.path.exists(os.path.join(self.base_dir, "portable.txt"))
            or os.path.exists(os.path.join(self.base_dir, "portable.flag"))
        )

        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
            host_data_dir = os.path.join(appdata, "SmartIPTVPlayer", "userdata")
        else:
            host_data_dir = os.path.join(os.path.expanduser("~"), ".smart_iptv_player", "userdata")

        if self.is_portable:
            self.data_dir = os.path.join(self.base_dir, "userdata")
            try:
                os.makedirs(self.data_dir, exist_ok=True)
            except Exception:
                self.data_dir = host_data_dir
                os.makedirs(self.data_dir, exist_ok=True)
        else:
            self.data_dir = host_data_dir
            try:
                os.makedirs(self.data_dir, exist_ok=True)
            except Exception:
                self.data_dir = os.path.join(self.base_dir, "userdata")
                os.makedirs(self.data_dir, exist_ok=True)

            # Migrate legacy userdata from app folder once if target is empty
            legacy_dir = os.path.join(self.base_dir, "userdata")
            if os.path.abspath(legacy_dir) != os.path.abspath(self.data_dir) and os.path.isdir(legacy_dir):
                for fname in ("settings.json", "favorites.json", "recent_playlists.json", "servers.json"):
                    src = os.path.join(legacy_dir, fname)
                    dst = os.path.join(self.data_dir, fname)
                    if os.path.exists(src) and not os.path.exists(dst):
                        try:
                            shutil.copy2(src, dst)
                        except Exception:
                            pass

        self.settings_file = os.path.join(self.data_dir, "settings.json")
        self.favorites_file = os.path.join(self.data_dir, "favorites.json")
        self.recent_file = os.path.join(self.data_dir, "recent_playlists.json")
        self.servers_file = os.path.join(self.data_dir, "servers.json")

        self.settings = self._load_settings()
        self.favorites: Set[str] = self._load_favorites()
        self.recent_playlists: List[str] = self._load_recent()
        self.servers: List[Dict] = self._load_servers()

    def _load_settings(self) -> dict:
        if self.is_portable:
            default_record_dir = os.path.join(self.base_dir, "recordings")
        else:
            default_record_dir = os.path.join(os.path.expanduser("~"), "Videos", "IPTV_Recordings")
        default_settings = {
            "volume": 85,
            "record_format": "mp4",
            "recordings_dir": default_record_dir,
            "aspect_ratio": "default",
            "last_playlist": "",
            "theme": "dark"
        }
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    default_settings.update(data)
            except Exception:
                pass

        # Validate recordings_dir exists or can be created; fallback if invalid/inaccessible
        r_dir = default_settings.get("recordings_dir", "")
        if not r_dir or not os.path.exists(os.path.dirname(r_dir)):
            default_settings["recordings_dir"] = default_record_dir

        return default_settings

    def save_settings(self):
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def get_setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key: str, value):
        self.settings[key] = value
        self.save_settings()

    def _load_favorites(self) -> Set[str]:
        if os.path.exists(self.favorites_file):
            try:
                with open(self.favorites_file, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

    def save_favorites(self):
        try:
            with open(self.favorites_file, "w", encoding="utf-8") as f:
                json.dump(list(self.favorites), f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def is_favorite(self, stream_url: str) -> bool:
        return stream_url in self.favorites

    def toggle_favorite(self, stream_url: str) -> bool:
        if stream_url in self.favorites:
            self.favorites.remove(stream_url)
            is_fav = False
        else:
            self.favorites.add(stream_url)
            is_fav = True
        self.save_favorites()
        return is_fav

    def _load_recent(self) -> List[str]:
        if os.path.exists(self.recent_file):
            try:
                with open(self.recent_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def add_recent_playlist(self, path_or_url: str):
        if not path_or_url:
            return
        if path_or_url in self.recent_playlists:
            self.recent_playlists.remove(path_or_url)
        self.recent_playlists.insert(0, path_or_url)
        self.recent_playlists = self.recent_playlists[:10]  # keep top 10
        try:
            with open(self.recent_file, "w", encoding="utf-8") as f:
                json.dump(self.recent_playlists, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def _load_servers(self) -> List[Dict]:
        if os.path.exists(self.servers_file):
            try:
                with open(self.servers_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def save_servers(self):
        try:
            with open(self.servers_file, "w", encoding="utf-8") as f:
                json.dump(self.servers, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def get_servers(self) -> List[Dict]:
        return list(self.servers)

    def add_server(
        self,
        name: str,
        url: str,
        server_type: str = "m3u",
        server_url: str = "",
        username: str = "",
        password: str = ""
    ) -> Dict:
        clean_url = url.strip()
        clean_name = name.strip()
        if not clean_name:
            host = urllib.parse.urlparse(server_url or clean_url).netloc
            clean_name = host or "IPTV Server"

        for i, srv in enumerate(self.servers):
            if srv.get("url") == clean_url:
                srv["name"] = clean_name
                srv["type"] = server_type
                srv["server_url"] = server_url.strip()
                srv["username"] = username.strip()
                srv["password"] = password
                self.servers.pop(i)
                self.servers.insert(0, srv)
                self.save_servers()
                return srv

        new_srv = {
            "id": str(uuid.uuid4()),
            "name": clean_name,
            "url": clean_url,
            "type": server_type,
            "server_url": server_url.strip(),
            "username": username.strip(),
            "password": password,
        }
        self.servers.insert(0, new_srv)
        self.save_servers()
        return new_srv

    def delete_server(self, server_id: str) -> bool:
        for i, srv in enumerate(self.servers):
            if srv.get("id") == server_id:
                self.servers.pop(i)
                self.save_servers()
                return True
        return False

    def save_cache(self, mode: str, channels_data: list):
        """Cache playlist items to disk for instant subsequent startup."""
        cache_path = os.path.join(self.data_dir, f"cache_{mode}.json")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(channels_data, f, ensure_ascii=False)
        except Exception:
            pass

    def load_cache(self, mode: str) -> list:
        """Load cached playlist items from disk."""
        cache_path = os.path.join(self.data_dir, f"cache_{mode}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def clear_cache(self):
        """Clear cached playlists on manual refresh."""
        for mode in ("live", "vod", "series"):
            p = os.path.join(self.data_dir, f"cache_{mode}.json")
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
