import http.cookiejar
import json
import os
import re
import time
import urllib.parse
from typing import List, Optional

from .config import settings
from .models import ArtistResponse, PlaylistResponse, SongResponse


def fix_thumbnail_url(
    url: Optional[str], proxy_base: Optional[str] = None, is_video: bool = False
) -> Optional[str]:
    """Normalise a YouTube/Google thumbnail URL to a consistent size and aspect ratio."""
    if not url:
        return None

    # Handle Google User Content (lh3.googleusercontent.com, yt3.ggpht.com)
    if "googleusercontent.com" in url or "ggpht.com" in url:
        if is_video:
            # For videos, we might want a rectangular crop if available,
            # but usually these are square source images.
            # If we want 16:9 from googleusercontent, it's tricky.
            # We'll stick to a high res square or the original if it's already rectangular.
            url = re.sub(r"=w\d+-h\d+(-[^?&]*)?$", "=w1280-h720-l90-rj", url)
            if "=w" not in url:
                url += "=w1280-h720-l90-rj"
        else:
            # Request 1000 px so full-screen art stays crisp (Square)
            url = re.sub(r"=w\d+-h\d+(-[^?&]*)?$", "=w1000-h1000-l90-rj", url)
            if "=w" not in url:
                url += "=w1000-h1000-l90-rj"
        return url

    # Handle YouTube Video Thumbnails (i.ytimg.com)
    if "i.ytimg.com" in url:
        if is_video:
            # Keep 16:9 for videos
            url = re.sub(
                r"/(?:default|mqdefault|hqdefault|sddefault|maxresdefault)\.jpg",
                "/hq720.jpg",
                url,
            )
        else:
            # Force square for songs if it's a ytimg URL (rare for songs, but happens)
            # Actually, ytimg URLs are almost always 16:9.
            # If it's a song, we prefer the hqdefault which is often padded but "squarer" in intent
            # or we let the frontend handle the center-crop.
            url = re.sub(
                r"/(?:default|mqdefault|hqdefault|sddefault|maxresdefault)\.jpg",
                "/hqdefault.jpg",
                url,
            )
        return url

    return url


def normalize_song(
    item: dict, proxy_base: Optional[str] = None
) -> Optional[SongResponse]:
    video_id = item.get("videoId")
    if not video_id:
        return None

    # Detect if it's a music video
    is_video = False
    vtype = item.get("videoType") or item.get("type") or item.get("resultType") or ""
    if isinstance(vtype, str):
        vtype = vtype.lower()
        if "video" in vtype or vtype == "omv":  # OMV = Official Music Video
            is_video = True

    artists = item.get("artists") or []
    artist_name = ", ".join(a["name"] for a in artists if a.get("name")) or "Unknown"

    album = item.get("album") or {}
    album_name = album.get("name", "") if isinstance(album, dict) else ""

    # Robust thumbnail extraction (handle thumbnails vs thumbnail, list vs dict)
    thumbnails_data = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbnails_data, dict):
        thumbnails_list = thumbnails_data.get("thumbnails") or [thumbnails_data]
    elif isinstance(thumbnails_data, list):
        thumbnails_list = thumbnails_data
    else:
        thumbnails_list = []

    raw_url = None
    if thumbnails_list and isinstance(thumbnails_list, list):
        last_thumb = thumbnails_list[-1]
        if isinstance(last_thumb, dict):
            raw_url = last_thumb.get("url")

    # Robust duration extraction (handle duration_seconds vs duration string)
    duration_seconds = item.get("duration_seconds")
    if duration_seconds is None:
        duration_str = item.get("duration")
        if duration_str and isinstance(duration_str, str) and ":" in duration_str:
            try:
                parts = duration_str.split(":")
                if len(parts) == 2:
                    duration_seconds = int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    duration_seconds = (
                        int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    )
            except Exception:
                pass

    if duration_seconds is None:
        duration_seconds = 0

    return SongResponse(
        id=video_id,
        title=item.get("title") or "Unknown",
        artist=artist_name,
        album=album_name,
        durationMs=int(duration_seconds) * 1000,
        thumbnailUrl=fix_thumbnail_url(raw_url, proxy_base, is_video=is_video),
        isVideo=is_video,
        aspectRatio=1.77 if is_video else 1.0,
    )


def normalize_album_as_song(
    item: dict, proxy_base: Optional[str] = None
) -> Optional[SongResponse]:
    """get_new_releases returns albums/singles, but we need song entities for the staggered grid."""
    # Sometimes it has a videoId directly
    video_id = item.get("videoId")
    # If not, use browseId or playlistId as fallback ID for mapping
    if not video_id:
        video_id = item.get("browseId") or item.get("playlistId") or ""

    if not video_id:
        return None

    artists = item.get("artists") or []
    artist_name = ", ".join(a["name"] for a in artists if a.get("name")) or "Unknown"

    thumbnails_data = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbnails_data, dict):
        thumbnails_list = thumbnails_data.get("thumbnails") or [thumbnails_data]
    elif isinstance(thumbnails_data, list):
        thumbnails_list = thumbnails_data
    else:
        thumbnails_list = []

    raw_url = None
    if thumbnails_list and isinstance(thumbnails_list, list):
        last_thumb = thumbnails_list[-1]
        if isinstance(last_thumb, dict):
            raw_url = last_thumb.get("url")

    return SongResponse(
        id=video_id,
        title=item.get("title") or "Unknown",
        artist=artist_name,
        album=item.get("title") or "Unknown",
        durationMs=0,
        thumbnailUrl=fix_thumbnail_url(raw_url, proxy_base),
    )


def is_artist_item(item: dict) -> bool:
    return (
        item.get("resultType") == "artist"
        or item.get("type") == "artist"
        or bool(item.get("subscribers"))
        or (not item.get("videoId") and str(item.get("browseId", "")).startswith("UC"))
    )


def normalize_artist(
    item: dict, proxy_base: Optional[str] = None
) -> Optional[ArtistResponse]:
    name = item.get("artist") or item.get("title") or item.get("name")
    if not name:
        return None

    # Robust thumbnail extraction
    thumbnails_data = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbnails_data, dict):
        thumbnails_list = thumbnails_data.get("thumbnails") or [thumbnails_data]
    elif isinstance(thumbnails_data, list):
        thumbnails_list = thumbnails_data
    else:
        thumbnails_list = []

    raw_url = None
    if thumbnails_list and isinstance(thumbnails_list, list):
        last_thumb = thumbnails_list[-1]
        if isinstance(last_thumb, dict):
            raw_url = last_thumb.get("url")

    return ArtistResponse(
        name=name, thumbnailUrl=fix_thumbnail_url(raw_url, proxy_base)
    )


def normalize_playlist(
    item: dict,
    proxy_base: Optional[str] = None,
    playlist_type: str = "yt",
    owner_code: Optional[str] = None,
) -> PlaylistResponse:
    # Robust thumbnail extraction
    thumbnails_data = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbnails_data, dict):
        thumbnails_list = thumbnails_data.get("thumbnails") or [thumbnails_data]
    elif isinstance(thumbnails_data, list):
        thumbnails_list = thumbnails_data
    else:
        thumbnails_list = []

    raw_url = None
    if thumbnails_list and isinstance(thumbnails_list, list):
        last_thumb = thumbnails_list[-1]
        if isinstance(last_thumb, dict):
            raw_url = last_thumb.get("url")

    count_str: str = str(item.get("count") or "")
    description = f"{count_str} songs" if count_str else item.get("description", "")
    track_count = 0
    parts = count_str.split()
    if parts and parts[0].isdigit():
        track_count = int(parts[0])

    # Detect album vs playlist
    item_type_raw = str(item.get("type") or item.get("resultType") or "").lower()
    is_album = item_type_raw in ("album", "single", "ep")

    # Artist name (present on album results)
    artists = item.get("artists") or []
    artist_name: Optional[str] = None
    if artists:
        artist_name = ", ".join(a["name"] for a in artists if a.get("name")) or None

    return PlaylistResponse(
        id=item.get("playlistId") or item.get("browseId") or item.get("id", ""),
        name=item.get("title") or item.get("name") or "Unknown",
        description=description,
        thumbnailUrl=fix_thumbnail_url(raw_url, proxy_base),
        trackCount=track_count,
        type=playlist_type,
        isAlbum=is_album,
        artistName=artist_name,
        ownerCode=owner_code,
    )


def write_cookie_file(auth_data: str | dict, cookie_file: str) -> bool:
    """
    Converts ytmusicapi auth JSON or Cookie string into a Netscape cookie file for yt-dlp.
    Returns True if the file was written with at least one cookie.
    """
    import logging as _logging

    _log = _logging.getLogger("uvicorn")
    try:
        data: dict = {}
        if isinstance(auth_data, str):
            if os.path.exists(auth_data):
                with open(auth_data) as f:
                    data = json.load(f)
            else:
                try:
                    data = json.loads(auth_data)
                except Exception:
                    data = {"Cookie": auth_data}
        else:
            data = dict(auth_data)

        # ── OAuth Check ──
        # If this is an OAuth token (has refresh_token), it's not a cookie list.
        # yt-dlp doesn't support using these as cookie files.
        if "refresh_token" in data:
            return False

        # ytmusicapi auth JSON uses "Cookie" header key (case-sensitive)
        cookie_str = (
            data.get("Cookie")
            or data.get("cookie")
            or data.get("headers", {}).get("Cookie")
            or ""
        )
        if not cookie_str:
            _log.warning(
                f"write_cookie_file: no Cookie field found in auth_data "
                f"(keys={list(data.keys())})"
            )
            return False

        dir_path = os.path.dirname(cookie_file)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        expiry = int(time.time()) + 31536000  # 1 year
        count = 0

        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n\n")

            for pair in cookie_str.split(";"):
                pair = pair.strip()
                if not pair or "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                name = name.strip()
                value = value.strip()
                if not name:
                    continue

                # __Host- cookies must be for the exact host (no leading dot)
                if name.startswith("__Host-"):
                    for domain in ["youtube.com", "music.youtube.com", "google.com"]:
                        f.write(
                            f"{domain}\tFALSE\t/\tTRUE\t{expiry}\t{name}\t{value}\n"
                        )
                else:
                    for domain in [".youtube.com", ".music.youtube.com", ".google.com"]:
                        f.write(f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n")
                count += 1

        _log.info(f"write_cookie_file: wrote {count} cookies to {cookie_file}")
        return count > 0

    except Exception as e:
        import logging as _logging2

        _logging2.getLogger("uvicorn").error(f"write_cookie_file failed: {e}")
        return False


def curl_to_headers(curl: str) -> str:
    curl = re.sub(r"[\^\\]\s*[\r\n]+", " ", curl)
    curl = curl.replace('^"', '"').replace('\\"', '"')
    headers = []
    for m in re.finditer(r'-H\s+([\'"])(.*?)\1', curl):
        headers.append(m.group(2))
    for m in re.finditer(r'-b\s+([\'"])(.*?)\1', curl):
        headers.append(f"cookie: {m.group(2)}")
    return "\n".join(headers)
