"""
M3U / M3U8 & Xtream Codes Parser for Smart IPTV Player
Supports parsing local files, remote M3U URLs, and Xtream Codes servers
for Live TV, Movies (VOD), and Series.
"""

import os
import re
import urllib.parse
from typing import List, Dict, Optional, Tuple
import requests


def _parse_ts(val) -> int:
    """Safely parse numeric or string timestamps into int."""
    if val is None or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


class Channel:
    def __init__(
        self,
        name: str,
        url: str,
        group: str = "General",
        logo: str = "",
        tvg_id: str = "",
        content_type: str = "live",
        item_id: str = "",
        http_headers: Optional[Dict[str, str]] = None,
        added: int = 0
    ):
        self.name = name.strip()
        self.url = url.strip()
        self.group = group.strip() if group else "General"
        self.logo = logo.strip() if logo else ""
        self.tvg_id = tvg_id.strip() if tvg_id else ""
        self.content_type = content_type  # 'live', 'vod', or 'series'
        self.item_id = str(item_id)
        self.http_headers = http_headers or {}
        self.added = _parse_ts(added)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "group": self.group,
            "logo": self.logo,
            "tvg_id": self.tvg_id,
            "content_type": self.content_type,
            "item_id": self.item_id,
            "http_headers": self.http_headers,
            "added": self.added
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        return cls(
            name=d.get("name", ""),
            url=d.get("url", ""),
            group=d.get("group", "General"),
            logo=d.get("logo", ""),
            tvg_id=d.get("tvg_id", ""),
            content_type=d.get("content_type", "live"),
            item_id=d.get("item_id", ""),
            http_headers=d.get("http_headers", {}),
            added=d.get("added", 0)
        )

    def __repr__(self):
        return f"<Channel: {self.name} [{self.group}] ({self.content_type})>"


class M3UParser:
    ATTR_PATTERNS = {
        "group": re.compile(r'group-title=["\'](.*?)["\']', re.IGNORECASE),
        "logo": re.compile(r'tvg-logo=["\'](.*?)["\']', re.IGNORECASE),
        "id": re.compile(r'tvg-id=["\'](.*?)["\']', re.IGNORECASE),
        "name": re.compile(r'tvg-name=["\'](.*?)["\']', re.IGNORECASE),
        "user_agent": re.compile(r'user-agent=["\'](.*?)["\']', re.IGNORECASE),
        "referer": re.compile(r'http-referrer=["\'](.*?)["\']', re.IGNORECASE)
    }

    @staticmethod
    def parse_file(file_path: str) -> List[Channel]:
        """Parse playlist from a local file path."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Playlist file not found: {file_path}")

        encodings = ["utf-8", "utf-8-sig", "latin1", "cp1256", "iso-8859-1"]
        content = None
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if content is None:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

        return M3UParser.parse_content(content)

    @staticmethod
    def parse_url(url: str, content_type: str = "live", timeout: int = 20) -> List[Channel]:
        """
        Fetch and parse playlist from a remote URL.
        Detects Xtream Codes server credentials in URL parameters.
        content_type can be 'live', 'vod', or 'series'.
        """
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        user = qs.get("username", [None])[0]
        pwd = qs.get("password", [None])[0]
        output_fmt = qs.get("output", ["ts"])[0]

        if user and pwd:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            try:
                channels = M3UParser.fetch_xtream(base_url, user, pwd, content_type=content_type, output_fmt=output_fmt, timeout=timeout)
                if channels:
                    return channels
            except Exception:
                pass

        # Standard M3U HTTP fetch fallback
        headers = {
            "User-Agent": "IPTVSmartersPro/2.2.1.2 (Linux; Android 10)",
            "Accept": "*/*"
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return M3UParser.parse_content(resp.text)

    @staticmethod
    def fetch_xtream(
        base_url: str,
        user: str,
        pwd: str,
        content_type: str = "live",
        output_fmt: str = "ts",
        timeout: int = 20
    ) -> List[Channel]:
        """Fetch categories and channels/movies/series via Xtream Codes API."""
        headers = {
            "User-Agent": "IPTVSmartersPro/2.2.1.2 (Linux; Android 10)",
            "Accept": "application/json"
        }

        # Determine API endpoints based on content_type
        if content_type == "vod":
            cat_action = "get_vod_categories"
            stream_action = "get_vod_streams"
        elif content_type == "series":
            cat_action = "get_series_categories"
            stream_action = "get_series"
        else:
            cat_action = "get_live_categories"
            stream_action = "get_live_streams"

        u_enc = urllib.parse.quote(str(user), safe="")
        p_enc = urllib.parse.quote(str(pwd), safe="")

        # 1. Fetch categories
        cat_url = f"{base_url}/player_api.php?username={u_enc}&password={p_enc}&action={cat_action}"
        r_cats = requests.get(cat_url, headers=headers, timeout=timeout)
        r_cats.raise_for_status()
        cat_data = r_cats.json()

        cat_map = {}
        cat_order: Dict[str, int] = {}
        if isinstance(cat_data, list):
            for idx, cat in enumerate(cat_data):
                cid = str(cat.get("category_id", ""))
                cname = cat.get("category_name", "").strip()
                if cid and cname:
                    cat_map[cid] = cname
                if cid and cid not in cat_order:
                    cat_order[cid] = idx

        # 2. Fetch streams or series
        streams_url = f"{base_url}/player_api.php?username={u_enc}&password={p_enc}&action={stream_action}"
        r_streams = requests.get(streams_url, headers=headers, timeout=timeout)
        r_streams.raise_for_status()
        items_data = r_streams.json()

        ordered_channels: List[Tuple[int, int, int, Channel]] = []
        default_cat_rank = len(cat_order)
        if isinstance(items_data, list):
            for raw_idx, item in enumerate(items_data):
                name = item.get("name", "").strip()
                cat_id = str(item.get("category_id", ""))
                group_name = cat_map.get(cat_id, "General")
                logo = item.get("stream_icon") or item.get("cover") or ""

                raw_num = item.get("num")
                try:
                    item_num = int(raw_num) if raw_num not in (None, "") else raw_idx
                except (ValueError, TypeError):
                    item_num = raw_idx
                cat_rank = cat_order.get(cat_id, default_cat_rank)

                ch: Optional[Channel] = None
                if content_type == "live":
                    sid = item.get("stream_id")
                    if not sid:
                        continue
                    ext = "m3u8" if output_fmt == "m3u8" else "ts"
                    stream_url = f"{base_url}/live/{u_enc}/{p_enc}/{sid}.{ext}"
                    ch = Channel(
                        name=name or f"Live Channel {sid}",
                        url=stream_url,
                        group=group_name,
                        logo=logo,
                        tvg_id=str(item.get("epg_channel_id", "") or ""),
                        content_type="live",
                        item_id=str(sid),
                        added=_parse_ts(item.get("added"))
                    )

                elif content_type == "vod":
                    sid = item.get("stream_id")
                    if not sid:
                        continue
                    ext = item.get("container_extension", "mp4") or "mp4"
                    stream_url = f"{base_url}/movie/{u_enc}/{p_enc}/{sid}.{ext}"
                    ch = Channel(
                        name=name or f"Movie {sid}",
                        url=stream_url,
                        group=group_name,
                        logo=logo,
                        content_type="vod",
                        item_id=str(sid),
                        added=_parse_ts(item.get("added"))
                    )

                elif content_type == "series":
                    series_id = item.get("series_id")
                    if not series_id:
                        continue
                    # For series, we store series_id. The actual playable URL is loaded on episode click
                    ch = Channel(
                        name=name or f"Series {series_id}",
                        url=f"series://{series_id}",
                        group=group_name,
                        logo=logo,
                        content_type="series",
                        item_id=str(series_id),
                        added=_parse_ts(item.get("last_modified") or item.get("added"))
                    )

                if ch is not None:
                    ordered_channels.append((cat_rank, item_num, raw_idx, ch))

        ordered_channels.sort(key=lambda t: (t[0], t[1], t[2]))
        return [t[3] for t in ordered_channels]

    @staticmethod
    def fetch_series_episodes(base_url: str, user: str, pwd: str, series_id: str, timeout: int = 15) -> List[Channel]:
        """Fetch all playable episodes for a given series."""
        headers = {
            "User-Agent": "IPTVSmartersPro/2.2.1.2 (Linux; Android 10)",
            "Accept": "application/json"
        }
        u_enc = urllib.parse.quote(str(user), safe="")
        p_enc = urllib.parse.quote(str(pwd), safe="")
        url = f"{base_url}/player_api.php?username={u_enc}&password={p_enc}&action=get_series_info&series_id={series_id}"
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        episodes_map = data.get("episodes", {})
        episode_channels = []

        if isinstance(episodes_map, dict):
            for season_num in sorted(episodes_map.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                ep_list = episodes_map[season_num]
                if isinstance(ep_list, list):
                    for ep in ep_list:
                        ep_id = ep.get("id")
                        ep_title = ep.get("title", "").strip() or f"Episode {ep.get('episode_num', '')}"
                        ext = ep.get("container_extension", "mp4") or "mp4"
                        stream_url = f"{base_url}/series/{u_enc}/{p_enc}/{ep_id}.{ext}"
                        logo = ""
                        info = ep.get("info")
                        if isinstance(info, dict):
                            logo = info.get("movie_image", "")

                        episode_channels.append(
                            Channel(
                                name=f"S{season_num} - {ep_title}",
                                url=stream_url,
                                group=f"Season {season_num}",
                                logo=logo,
                                content_type="vod",
                                item_id=str(ep_id)
                            )
                        )

        return episode_channels

    @staticmethod
    def parse_content(content: str) -> List[Channel]:
        """Parse string content of an M3U file into Channel objects."""
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        channels: List[Channel] = []

        current_metadata = {}
        headers_dict = {}

        for line in lines:
            if line.startswith("#EXTM3U"):
                continue

            if line.startswith("#EXTVLCOPT:") or line.startswith("#EXTHTTP:"):
                val = line.split(":", 1)[1].strip()
                if "=" in val:
                    k, v = val.split("=", 1)
                    headers_dict[k.strip().lower()] = v.strip()
                continue

            if line.startswith("#EXTINF:"):
                current_metadata = M3UParser._extract_extinf(line)
                continue

            if not line.startswith("#"):
                stream_url = line
                channel_name = current_metadata.get("name", "")
                if not channel_name:
                    parts = stream_url.split("/")[-1].split("?")[0]
                    channel_name = parts if parts else "Unknown Channel"

                group = current_metadata.get("group", "General")
                logo = current_metadata.get("logo", "")
                tvg_id = current_metadata.get("id", "")

                # Detect if VOD or Series from URL / group
                c_type = "live"
                lower_url = stream_url.lower()
                lower_grp = group.lower()
                if "/movie/" in lower_url or "vod" in lower_grp or "movies" in lower_grp:
                    c_type = "vod"
                elif "/series/" in lower_url or "series" in lower_grp:
                    c_type = "series"

                channels.append(
                    Channel(
                        name=channel_name,
                        url=stream_url,
                        group=group,
                        logo=logo,
                        tvg_id=tvg_id,
                        content_type=c_type,
                        http_headers=headers_dict.copy()
                    )
                )

                current_metadata = {}
                headers_dict = {}

        return channels

    @staticmethod
    def _extract_extinf(line: str) -> dict:
        """Extract attributes and channel name from #EXTINF line."""
        info = {
            "name": "",
            "group": "General",
            "logo": "",
            "id": ""
        }

        for key, pattern in M3UParser.ATTR_PATTERNS.items():
            match = pattern.search(line)
            if match:
                info[key] = match.group(1).strip()

        if "," in line:
            match_title = re.search(r'^#EXTINF:[^,]*?(?:\s+[\w-]+=(?:"[^"]*"|\'[^\']*\'))*\s*,(.*)$', line)
            name_part = (match_title.group(1) if match_title else line.split(",", 1)[1]).strip()
            if name_part:
                info["name"] = name_part
        elif not info["name"] and "tvg-name" in info:
            info["name"] = info["tvg-name"]

        info["name"] = info["name"].strip()
        if not info["name"]:
            info["name"] = "Unknown Channel"

        return info

    @staticmethod
    def get_category_counts(channels: List[Channel]) -> List[Tuple[str, int]]:
        """Return list of (category_name, count) tuples preserving order."""
        counts = {}
        order = []
        for ch in channels:
            grp = ch.group or "General"
            if grp not in counts:
                counts[grp] = 0
                order.append(grp)
            counts[grp] += 1

        return [(grp, counts[grp]) for grp in order]
