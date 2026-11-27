#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Popamp Aurora — skinned album artwork and synchronized lyrics companion.

A standalone GTK3 companion for Popamp. It follows the currently playing track
through Popamp's documented `popamp /STATUS` JSON command, reads artwork and
lyrics from the audio file (or local sidecars), and optionally looks up missing
lyrics through LRCLIB.

Arch/CachyOS dependencies:
    sudo pacman -S python-gobject gtk3 python-mutagen python-pillow

Run:
    python3 popamp_aurora.py

Configuration:
    ~/.config/popamp-aurora/config.json

License: GPL-3.0-only
"""

from __future__ import annotations

import configparser
import hashlib
import html
import io
import json
import os
import unicodedata
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import warnings
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

try:
    import mutagen
    from mutagen.id3 import ID3, ID3NoHeaderError, USLT
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    from mutagen.wave import WAVE
    HAS_MUTAGEN = True
except ImportError:
    mutagen = None
    ID3 = ID3NoHeaderError = USLT = None
    FLAC = OggVorbis = OggOpus = MP4 = WAVE = None
    HAS_MUTAGEN = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

APP_NAME = "popamp-aurora"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CACHE_DIR = Path.home() / ".cache" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
ALBUM_LIST_CONFIG = Path.home() / ".config" / "popamp-album-list" / "config.json"

DEFAULT_THEME: dict[str, object] = {
    "name": "Classic Winamp",
    "normal_bg": "#181822",
    "normal_text": "#d0d0d0",
    "current_text": "#ffffff",
    "selected_bg": "#244b80",
    "header_bg": "#282838",
    "border_light": "#555568",
    "border_dark": "#0c0c12",
    "pledit_slices": None,
}
POPAMP_VECTOR_DEFAULT: dict[str, object] = {
    "name": "Popamp Default",
    "normal_bg": "#232527",
    "normal_text": "#b8bec4",
    "current_text": "#ffffff",
    "selected_bg": "#3c4248",
    "header_bg": "#2e3134",
    "border_light": "#52565c",
    "border_dark": "#141517",
    "pledit_slices": None,
}

@dataclass
class LyricLine:
    time_ms: Optional[int]
    text: str

@dataclass
class TrackData:
    location: str
    title: str = "Unknown title"
    artist: str = ""
    album: str = ""
    year: str = ""
    duration_ms: int = 0
    cover_bytes: Optional[bytes] = None
    lyrics_text: str = ""
    lyric_lines: list[LyricLine] = field(default_factory=list)
    synced: bool = False
    lyric_source: str = "No lyrics"


def _first_tag(tags, keys: tuple[str, ...]) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def parse_lrc(text: str) -> tuple[list[LyricLine], int]:
    """Parse LRC line timestamps, multiple timestamps and [offset:] metadata."""
    lines: list[LyricLine] = []
    offset_ms = 0
    stamp_re = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        off = re.match(r"\s*\[offset\s*:\s*([+-]?\d+)\s*\]\s*$", raw, re.I)
        if off:
            offset_ms = int(off.group(1))
            continue
        stamps = list(stamp_re.finditer(raw))
        if not stamps:
            continue
        lyric = stamp_re.sub("", raw).strip()
        for match in stamps:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            millis = int(fraction.ljust(3, "0")[:3])
            lines.append(LyricLine(max(0, minutes * 60000 + seconds * 1000 + millis + offset_ms), lyric))
    lines.sort(key=lambda item: item.time_ms if item.time_ms is not None else 0)
    return lines, offset_ms


def plain_lyric_lines(text: str) -> list[LyricLine]:
    return [LyricLine(None, line.rstrip()) for line in text.replace("\r", "").split("\n")]


def _extract_cover_bytes(audio, path: Path) -> Optional[bytes]:
    """Read a front-cover image from common Mutagen container/tag formats."""
    try:
        tags = getattr(audio, "tags", None)
        if path.suffix.lower() in {".mp3", ".wav"} and tags is not None and hasattr(tags, "getall"):
            frames = tags.getall("APIC")
            for frame in frames:
                if getattr(frame, "data", None):
                    return bytes(frame.data)
        if path.suffix.lower() == ".flac" and getattr(audio, "pictures", None):
            pics = audio.pictures
            front = next((p for p in pics if getattr(p, "type", 0) == 3), pics[0])
            return bytes(front.data)
        if path.suffix.lower() in {".m4a", ".mp4", ".aac"} and tags is not None:
            covr = tags.get("covr")
            if covr:
                return bytes(covr[0])
        if tags is not None:
            for key in ("metadata_block_picture", "coverart", "COVERART"):
                try:
                    value = tags.get(key)
                except Exception:
                    value = None
                if value:
                    if isinstance(value, (list, tuple)):
                        value = value[0]
                    if isinstance(value, str):
                        import base64
                        return base64.b64decode(value)
                    if isinstance(value, bytes):
                        return value
        for picture in getattr(audio, "pictures", []) or []:
            if getattr(picture, "data", None):
                return bytes(picture.data)
    except Exception as exc:
        print(f"[Aurora] Artwork read note for {path.name}: {exc}")
    return None


def _find_folder_cover(folder: Path) -> Optional[Path]:
    names = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "front.jpg",
             "front.jpeg", "front.png", "folder.jpg", "folder.png", "album.jpg")
    try:
        files = {p.name.casefold(): p for p in folder.iterdir() if p.is_file()}
        for name in names:
            if name in files:
                return files[name]
    except OSError:
        pass
    return None


def _prepare_cover(raw: Optional[bytes]) -> Optional[bytes]:
    if not raw:
        return None
    if not HAS_PIL:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = source.convert("RGB")
            image.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=91, optimize=True)
            return out.getvalue()
    except Exception:
        return raw


def _parse_id3_sylt(audio) -> Optional[str]:
    tags = getattr(audio, "tags", None)
    if tags is None or not hasattr(tags, "getall"):
        return None
    try:
        frames = tags.getall("SYLT")
        for frame in frames:
            entries = getattr(frame, "text", None) or []
            time_format = getattr(frame, "format", 2)
            out = []
            for text, stamp in entries:
                try:
                    stamp = int(stamp)
                except (TypeError, ValueError):
                    continue
                # ID3 timestamp format 1 is MPEG frames (75 fps); format 2 is ms.
                ms = int(stamp * 1000 / 75) if time_format == 1 else stamp
                minutes, remainder = divmod(max(0, ms), 60000)
                seconds, millis = divmod(remainder, 1000)
                out.append(f"[{minutes:02d}:{seconds:02d}.{millis:03d}]{text}")
            if out:
                return "\n".join(out)
    except Exception as exc:
        print(f"[Aurora] SYLT read note: {exc}")
    return None


def _read_embedded_lyrics(audio, path: Path) -> Optional[str]:
    """Read synchronized ID3 SYLT first, then common plain/LRC tag fields."""
    if path.suffix.lower() in {".mp3", ".wav"}:
        sylt = _parse_id3_sylt(audio)
        if sylt:
            return sylt
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            try:
                frames = tags.getall("USLT")
                for frame in frames:
                    if getattr(frame, "text", "").strip():
                        return str(frame.text)
            except Exception:
                pass
    tags = getattr(audio, "tags", None)
    if tags is not None:
        candidates = (
            "LYRICS", "UNSYNCEDLYRICS", "UNSYNCED_LYRICS", "SYNCEDLYRICS",
            "SYNCED_LYRICS", "lyrics", "unsyncedlyrics", "syncedlyrics",
            "©lyr", "WM/Lyrics", "LYRICIST",
        )
        for key in candidates:
            try:
                value = tags.get(key)
            except Exception:
                value = None
            if value:
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else ""
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                text = str(value).strip()
                if text:
                    return text
    return None


def _read_sidecar(path: Path) -> Optional[tuple[str, str]]:
    for suffix, source in ((".lrc", "Local LRC"), (".txt", "Local text")):
        candidate = path.with_suffix(suffix)
        try:
            if candidate.is_file() and candidate.stat().st_size <= 2_000_000:
                text = candidate.read_text(encoding="utf-8-sig", errors="replace").strip()
                if text:
                    return text, source
        except OSError:
            continue
    return None


def read_track_data(location: str, status_title: str = "", fallback_duration_ms: int = 0) -> TrackData:
    data = TrackData(location=location, title=status_title or "Unknown title", duration_ms=fallback_duration_ms)
    path = Path(location).expanduser()
    if not path.is_file() or not HAS_MUTAGEN:
        return data
    try:
        audio = mutagen.File(str(path))
        easy = mutagen.File(str(path), easy=True)
        if audio is not None:
            data.duration_ms = int(float(getattr(getattr(audio, "info", None), "length", 0) or 0) * 1000) or data.duration_ms
            data.cover_bytes = _extract_cover_bytes(audio, path)
            lyrics = _read_embedded_lyrics(audio, path)
        else:
            lyrics = None
        tags = getattr(easy, "tags", None) if easy else None
        if tags:
            data.title = _first_tag(tags, ("title",)) or data.title
            data.artist = _first_tag(tags, ("artist", "albumartist", "album_artist"))
            data.album = _first_tag(tags, ("album",))
            data.year = _first_tag(tags, ("date", "originaldate", "year"))[:4]
            if not data.duration_ms:
                data.duration_ms = int(float(getattr(getattr(easy, "info", None), "length", 0) or 0) * 1000)
        if not data.cover_bytes:
            folder_cover = _find_folder_cover(path.parent)
            if folder_cover:
                data.cover_bytes = folder_cover.read_bytes()
        if lyrics:
            data.lyrics_text = lyrics
            synced, _offset = parse_lrc(lyrics)
            if synced:
                data.lyric_lines = synced
                data.synced = True
            else:
                data.lyric_lines = plain_lyric_lines(lyrics)
            data.lyric_source = "Embedded tags"
        else:
            sidecar = _read_sidecar(path)
            if sidecar:
                data.lyrics_text, data.lyric_source = sidecar
                synced, _offset = parse_lrc(data.lyrics_text)
                if synced:
                    data.lyric_lines = synced
                    data.synced = True
                else:
                    data.lyric_lines = plain_lyric_lines(data.lyrics_text)
    except Exception as exc:
        print(f"[Aurora] Could not read {path}: {exc}")
    if not data.artist and status_title:
        data.title = status_title
    return data


def _lyrics_match_key(value: str) -> str:
    """Normalised key used only to compare lyric provider metadata."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.casefold())


def _is_timed_lyrics(text: str) -> bool:
    return bool(parse_lrc(text or "")[0])


_LRCLIB_REQUEST_LOCK = threading.Lock()
_LRCLIB_LAST_REQUEST = 0.0


def _http_json(url: str) -> object:
    """Fetch JSON, identifying Aurora and pacing sequential LRCLIB requests."""
    global _LRCLIB_LAST_REQUEST
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PopampAurora/0.3 (GTK3 desktop lyrics companion)",
            "Accept": "application/json",
        },
    )
    is_lrclib = urllib.parse.urlparse(url).hostname == "lrclib.net"
    lock = _LRCLIB_REQUEST_LOCK if is_lrclib else threading.Lock()
    with lock:
        if is_lrclib:
            delay = 0.35 - (time.monotonic() - _LRCLIB_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = response.read(2_000_000).decode("utf-8", errors="replace")
            if is_lrclib:
                _LRCLIB_LAST_REQUEST = time.monotonic()
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
            if is_lrclib:
                _LRCLIB_LAST_REQUEST = time.monotonic()
            if is_lrclib and exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "1")
                try:
                    wait_seconds = max(1.0, min(120.0, float(retry_after)))
                except (TypeError, ValueError):
                    wait_seconds = 2.0
                time.sleep(wait_seconds)
                with urllib.request.urlopen(request, timeout=12) as response:
                    payload = response.read(2_000_000).decode("utf-8", errors="replace")
                _LRCLIB_LAST_REQUEST = time.monotonic()
                return json.loads(payload)
            raise


def fetch_lrclib_lyrics(track: TrackData) -> Optional[tuple[str, str]]:
    """Return the best LRCLIB match, explicitly preferring timestamped lyrics.

    /api/get is tried first. If it only supplies plain text, /api/search is
    checked for an exact metadata match that may have synchronized lyrics.
    """
    if not track.title or not track.artist or track.title == "Unknown title":
        return None

    query = {
        "track_name": track.title,
        "artist_name": track.artist,
    }
    if track.album:
        query["album_name"] = track.album
    duration = round(track.duration_ms / 1000) if track.duration_ms else 0
    if 1 <= duration <= 3600:
        query["duration"] = str(duration)

    plain_fallback = None
    try:
        exact_url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(query)
        payload = _http_json(exact_url)
        synced = str(payload.get("syncedLyrics") or "").strip()
        plain = str(payload.get("plainLyrics") or "").strip()
        if synced and _is_timed_lyrics(synced):
            return synced, "LRCLIB · synchronized"
        if plain:
            plain_fallback = (plain, "LRCLIB · plain")
    except Exception as exc:
        # A 404 is normal when a track is not in the catalogue.
        print(f"[LRCLIB exact] {track.artist} — {track.title}: {exc}")

    # Search can reveal an alternate release entry with synced lyrics where
    # the duration-specific exact endpoint returned only plain text.
    try:
        search_query = {
            "track_name": track.title,
            "artist_name": track.artist,
        }
        if track.album:
            search_query["album_name"] = track.album
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(search_query)
        candidates = _http_json(search_url)
        if isinstance(candidates, list):
            title_key = _lyrics_match_key(track.title)
            artist_key = _lyrics_match_key(track.artist)
            album_key = _lyrics_match_key(track.album)
            ranked = []
            for item in candidates[:20]:
                if not isinstance(item, dict):
                    continue
                if _lyrics_match_key(str(item.get("trackName") or item.get("name") or "")) != title_key:
                    continue
                if _lyrics_match_key(str(item.get("artistName") or "")) != artist_key:
                    continue
                synced = str(item.get("syncedLyrics") or "").strip()
                if not synced or not _is_timed_lyrics(synced):
                    continue
                item_album = _lyrics_match_key(str(item.get("albumName") or ""))
                album_penalty = 0 if not album_key or item_album == album_key else 1
                item_duration = float(item.get("duration") or 0)
                duration_delta = abs(item_duration - duration) if duration else 0
                if duration and duration_delta > 20:
                    continue
                ranked.append((album_penalty, duration_delta, synced))
            if ranked:
                ranked.sort(key=lambda row: (row[0], row[1]))
                return ranked[0][2], "LRCLIB · synchronized alternate match"
    except Exception as exc:
        print(f"[LRCLIB search] {track.artist} — {track.title}: {exc}")

    return plain_fallback


class _DarkLyricsTrackParser(HTMLParser):
    """Extract one track section from a DarkLyrics album page."""

    def __init__(self, wanted_title: str):
        super().__init__(convert_charrefs=True)
        self.wanted = _lyrics_match_key(wanted_title)
        self.in_heading = False
        self.heading_parts: list[str] = []
        self.capture = False
        self.parts: list[str] = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "h3":
            if self.capture:
                self.capture = False
                return
            self.in_heading = True
            self.heading_parts = []
        elif self.capture and tag == "br":
            self.parts.append("\n")
        elif self.capture and tag in {"p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "h3" and self.in_heading:
            heading = "".join(self.heading_parts).strip()
            heading = re.sub(r"^\s*\d+\s*[.)\-:]\s*", "", heading)
            if self.wanted and _lyrics_match_key(heading) == self.wanted:
                self.capture = True
                self.found = True
            self.in_heading = False
        elif self.capture and tag in {"p", "div"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_heading:
            self.heading_parts.append(data)
        elif self.capture:
            self.parts.append(data)

    def result(self) -> str:
        if not self.found:
            return ""
        text = html.unescape("".join(self.parts))
        cleaned = []
        for line in text.replace("\r", "").split("\n"):
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line:
                cleaned.append(line)
        # Do not accidentally return a navigation/footer fragment.
        result = "\n".join(cleaned).strip()
        if len(result) < 5:
            return ""
        return result[:100_000]


_DARKLYRICS_LOCK = threading.Lock()
_DARKLYRICS_LAST_REQUEST = 0.0
_DARKLYRICS_PAGE_CACHE: dict[str, tuple[float, str]] = {}


def _darklyrics_slug(value: str) -> str:
    """Build the conventional DarkLyrics artist/album URL slug."""
    value = (value or "").replace("&", " and ")
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.casefold())


def fetch_darklyrics_lyrics(track: TrackData) -> Optional[tuple[str, str]]:
    """Best-effort, rate-limited plain-lyrics fallback for metal releases.

    DarkLyrics has no official public API. The lookup makes at most one request
    to the conventional album page for a track, with a minimum three-second
    interval between requests, and never crawls an artist discography.
    """
    global _DARKLYRICS_LAST_REQUEST
    artist = _darklyrics_slug(track.artist)
    album = _darklyrics_slug(track.album)
    if not artist or not album or not track.title:
        return None

    url = f"http://www.darklyrics.com/lyrics/{artist}/{album}.html"
    try:
        cached = _DARKLYRICS_PAGE_CACHE.get(url)
        page = cached[1] if cached and (time.monotonic() - cached[0]) < 21_600 else ""
        if not page:
            with _DARKLYRICS_LOCK:
                # Recheck after acquiring the lock: another track from this
                # album may already have populated the cache.
                cached = _DARKLYRICS_PAGE_CACHE.get(url)
                page = cached[1] if cached and (time.monotonic() - cached[0]) < 21_600 else ""
                if not page:
                    wait = 3.0 - (time.monotonic() - _DARKLYRICS_LAST_REQUEST)
                    if wait > 0:
                        time.sleep(wait)
                    request = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": "PopampAurora/0.3 (personal desktop music player)",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                    )
                    # Record attempts before opening: even failures are throttled.
                    _DARKLYRICS_LAST_REQUEST = time.monotonic()
                    with urllib.request.urlopen(request, timeout=12) as response:
                        page = response.read(2_000_000).decode(
                            response.headers.get_content_charset() or "latin-1", errors="replace"
                        )
                    _DARKLYRICS_PAGE_CACHE[url] = (time.monotonic(), page)
        parser = _DarkLyricsTrackParser(track.title)
        parser.feed(page)
        lyrics = parser.result()
        if lyrics:
            return lyrics, "DarkLyrics · plain"
    except Exception as exc:
        print(f"[DarkLyrics] {track.artist} — {track.title}: {exc}")
    return None


def fetch_lyrics_ovh(track: TrackData) -> Optional[tuple[str, str]]:
    """Plain-text fallback from lyrics.ovh, after timed and metal sources."""
    if not track.artist or not track.title:
        return None
    url = "https://api.lyrics.ovh/v1/" + urllib.parse.quote(track.artist, safe="") + "/" + urllib.parse.quote(track.title, safe="")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PopampAurora/0.2 (GTK3 desktop lyrics companion)", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
        lyrics = str(payload.get("lyrics") or "").strip()
        if lyrics:
            return lyrics, "lyrics.ovh · plain"
    except Exception as exc:
        print(f"[lyrics.ovh] {track.artist} — {track.title}: {exc}")
    return None


def fetch_best_lyrics(track: TrackData) -> Optional[tuple[str, str]]:
    """Search all configured providers and prefer synchronized results."""
    lrclib_result = fetch_lrclib_lyrics(track)
    if lrclib_result and _is_timed_lyrics(lrclib_result[0]):
        return lrclib_result

    darklyrics_result = fetch_darklyrics_lyrics(track)
    if darklyrics_result:
        return darklyrics_result

    if lrclib_result:
        return lrclib_result

    return fetch_lyrics_ovh(track)


def embed_lyrics(path: Path, lyrics: str, make_backup: bool = True) -> str:
    """Embed lyrics in common audio formats without touching audio streams."""
    if not HAS_MUTAGEN:
        raise RuntimeError("Mutagen is not installed")
    suffix = path.suffix.lower()
    if suffix not in {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".wav"}:
        raise RuntimeError(f"Embedding lyrics into {suffix or 'this file'} is not supported yet")
    if make_backup:
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    audio = mutagen.File(str(path))
    if audio is None:
        raise RuntimeError("Mutagen could not open this audio file")
    if suffix == ".mp3":
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    elif suffix in {".flac", ".ogg", ".oga", ".opus"}:
        audio["LYRICS"] = [lyrics]
    elif suffix in {".m4a", ".mp4"}:
        if audio.tags is None:
            audio.add_tags()
        audio.tags["\xa9lyr"] = [lyrics]
    elif suffix == ".wav":
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    audio.save()
    return "Lyrics embedded successfully"


def format_time(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    seconds = milliseconds // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def query_popamp_status() -> dict:
    """Read the official Popamp /STATUS JSON response without blocking GTK."""
    completed = subprocess.run(
        ["popamp", "/STATUS"],
        capture_output=True,
        text=True,
        timeout=3.0,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Popamp is not running").strip()
        raise RuntimeError(message[:240])
    output = completed.stdout.strip()
    if not output:
        raise RuntimeError("Popamp returned an empty status response")
    parsed = json.loads(output)
    if not isinstance(parsed, dict):
        raise RuntimeError("Popamp status response was not a JSON object")
    return parsed


# =============================================================================
# Reused Winamp / Popamp skin parsing from Popamp Album List
# =============================================================================

def _pil_to_pixbuf(pil_img: "Image.Image") -> GdkPixbuf.Pixbuf:
    rgba = pil_img.convert("RGBA")
    width, height = rgba.size
    data = GLib.Bytes.new(rgba.tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, True, 8, width, height, width * 4
    )


def _slice_pledit_sheet(
    raw_bytes: bytes,
    is_svg: bool = False,
) -> Optional[dict[str, GdkPixbuf.Pixbuf]]:
    """Extract the classic 280px-wide Winamp PLEDIT frame regions."""
    temp_path = None
    try:
        if is_svg:
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as temp:
                temp.write(raw_bytes)
                temp_path = temp.name
            sheet_pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                temp_path, 280, -1, True
            )
            if not sheet_pb:
                return None
            sheet_width = sheet_pb.get_width()
            sheet_height = sheet_pb.get_height()

            def crop(x1: int, y1: int, x2: int, y2: int) -> GdkPixbuf.Pixbuf:
                x1 = max(0, min(sheet_width - 1, x1))
                y1 = max(0, min(sheet_height - 1, y1))
                x2 = max(x1 + 1, min(sheet_width, x2))
                y2 = max(y1 + 1, min(sheet_height, y2))
                return sheet_pb.new_subpixbuf(x1, y1, x2 - x1, y2 - y1)

        else:
            if not HAS_PIL:
                return None
            with Image.open(io.BytesIO(raw_bytes)) as source:
                sheet = source.convert("RGBA")
            if sheet.width <= 0:
                return None
            normalized_height = max(1, round(sheet.height * 280 / sheet.width))
            if sheet.width != 280 or sheet.height != normalized_height:
                sheet = sheet.resize((280, normalized_height), Image.Resampling.LANCZOS)
            sheet_width, sheet_height = sheet.size

            def crop(x1: int, y1: int, x2: int, y2: int) -> GdkPixbuf.Pixbuf:
                return _pil_to_pixbuf(sheet.crop((x1, y1, x2, y2)))

        if sheet_width < 276 or sheet_height < 110:
            print(
                "[Skin Slicer] PLEDIT sheet is not large enough for the classic "
                f"layout ({sheet_width}x{sheet_height}); using palette-only skinning."
            )
            return None

        return {
            "top_left": crop(0, 0, 25, 20),
            "top_title": crop(26, 0, 126, 20),
            "top_fill": crop(127, 0, 152, 20),
            "top_right": crop(153, 0, 178, 20),

            "left_border": crop(0, 42, 12, 71),
            "scroll_track": crop(36, 42, 44, 71),
            "right_border": crop(44, 42, 51, 71),
            "scroll_thumb": crop(52, 53, 60, 71),
            "scroll_thumb_pressed": crop(62, 53, 70, 71),

            "bottom_fill": crop(179, 0, 204, 38),
            "bottom_left_edge": crop(0, 72, 12, 110),
            "bottom_right_edge": crop(264, 72, 276, 110),
            "resize_grip": crop(257, 89, 276, 110),
        }

    except Exception as exc:
        print(f"[Skin Slicer] Could not slice PLEDIT sheet: {exc}")
        return None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def parse_skin_archive(skin_path: str) -> dict[str, object]:
    """Read skin palette and an explicitly named classic PLEDIT sprite sheet."""
    path = Path(skin_path)
    is_psz = path.suffix.lower() == ".psz"
    theme = (POPAMP_VECTOR_DEFAULT if is_psz else DEFAULT_THEME).copy()
    if not path.is_file():
        return theme

    def clean_hex(value: object, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        candidate = value.strip()
        if candidate.startswith("0x"):
            candidate = candidate[2:]
        candidate = candidate.lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{6}", candidate):
            return f"#{candidate.lower()}"
        return fallback

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            name_map = {name.lower(): name for name in names}
            manifest_data = {}
            base_prefix = ""

            manifest_name = next(
                (name for name in names if Path(name).name.lower() == "skin.toml"),
                None,
            )
            if manifest_name:
                try:
                    manifest_data = tomllib.loads(
                        archive.read(manifest_name).decode("utf-8", errors="replace")
                    )
                    base_prefix = str(Path(manifest_name).parent)
                    if base_prefix == ".":
                        base_prefix = ""
                except Exception as exc:
                    print(f"[Skin Parser] Could not read skin.toml: {exc}")

            meta = manifest_data.get("meta", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(meta, dict) and meta.get("name"):
                theme["name"] = str(meta["name"])

            playlist = manifest_data.get("playlist", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(playlist, dict):
                theme["normal_bg"] = clean_hex(playlist.get("normal_bg"), str(theme["normal_bg"]))
                theme["normal_text"] = clean_hex(playlist.get("normal"), str(theme["normal_text"]))
                theme["current_text"] = clean_hex(playlist.get("current"), str(theme["current_text"]))
                theme["selected_bg"] = clean_hex(playlist.get("selected_bg"), str(theme["selected_bg"]))

            text_section = manifest_data.get("text", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(text_section, dict) and "normal" not in playlist:
                theme["normal_text"] = clean_hex(text_section.get("color"), str(theme["normal_text"]))

            pledit_member = None
            sheets = manifest_data.get("sheets", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(sheets, dict) and isinstance(sheets.get("pledit"), str):
                requested = sheets["pledit"]
                candidate = f"{base_prefix}/{requested}".lstrip("/") if base_prefix else requested.lstrip("/")
                pledit_member = name_map.get(candidate.lower()) or name_map.get(requested.lower())

            if not pledit_member:
                pledit_member = next(
                    (
                        name for name in names
                        if Path(name).stem.lower() == "pledit"
                        and Path(name).suffix.lower() in {".bmp", ".png", ".svg"}
                    ),
                    None,
                )

            pledit_txt = next(
                (name for name in names if Path(name).name.lower() == "pledit.txt"),
                None,
            )
            if pledit_txt:
                try:
                    parser = configparser.ConfigParser(interpolation=None, strict=False)
                    parser.read_string(archive.read(pledit_txt).decode("latin-1", errors="replace"))
                    section = next(
                        (parser[section_name] for section_name in parser.sections()
                         if section_name.lower() == "text"),
                        None,
                    )
                    if section:
                        theme["normal_bg"] = clean_hex(section.get("NormalBG"), str(theme["normal_bg"]))
                        theme["normal_text"] = clean_hex(section.get("Normal"), str(theme["normal_text"]))
                        theme["current_text"] = clean_hex(section.get("Current"), str(theme["current_text"]))
                        theme["selected_bg"] = clean_hex(section.get("SelectedBG"), str(theme["selected_bg"]))
                except Exception as exc:
                    print(f"[Skin Parser] Could not parse pledit.txt: {exc}")

            if pledit_member:
                suffix = Path(pledit_member).suffix.lower()
                if suffix in {".bmp", ".png", ".svg"}:
                    theme["pledit_slices"] = _slice_pledit_sheet(
                        archive.read(pledit_member), is_svg=(suffix == ".svg")
                    )

            bg = str(theme["normal_bg"]).lstrip("#")
            red, green, blue = (int(bg[index:index + 2], 16) for index in (0, 2, 4))
            theme["header_bg"] = f"#{min(255, red + 18):02x}{min(255, green + 18):02x}{min(255, blue + 24):02x}"
            theme["border_light"] = f"#{min(255, red + 48):02x}{min(255, green + 48):02x}{min(255, blue + 58):02x}"
            theme["border_dark"] = f"#{max(0, red - 16):02x}{max(0, green - 16):02x}{max(0, blue - 16):02x}"

            if theme["name"] in {DEFAULT_THEME["name"], POPAMP_VECTOR_DEFAULT["name"]}:
                theme["name"] = path.stem

    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[Skin Parser] Could not open {skin_path}: {exc}")

    return theme


def find_popamp_skins() -> list[Path]:
    search_dirs = [
        Path.home() / ".local" / "share" / "popamp" / "skins",
        Path.home() / ".config" / "popamp" / "skins",
        Path("/usr/share/popamp/skins"),
        Path("/opt/popamp/skins"),
        Path.home() / "Downloads",
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        try:
            for file_path in directory.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in {".wsz", ".psz"}:
                    key = str(file_path.resolve())
                    if key not in seen:
                        seen.add(key)
                        found.append(file_path)
        except OSError:
            continue
    return sorted(found, key=lambda item: item.name.casefold())


# =============================================================================
# Popamp Aurora window
# =============================================================================

class AuroraWindow(Gtk.Window):
    MIN_WIDTH = 620
    MIN_HEIGHT = 300

    def __init__(self):
        super().__init__(title="Popamp Aurora")
        GLib.set_prgname("popamp-aurora")
        self.set_icon_name("popamp-aurora")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.set_wmclass("popamp-aurora", "popamp-aurora")
        except (AttributeError, TypeError):
            pass

        self.set_default_size(920, 440)
        self.set_size_request(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.set_border_width(0)
        self.set_decorated(False)
        self.set_resizable(True)

        self.config = self._load_config()
        self._geometry_max_width, self._geometry_max_height = self._display_bounds()
        self._geometry_max_width = max(self.MIN_WIDTH, self._geometry_max_width)
        self._geometry_max_height = max(self.MIN_HEIGHT, self._geometry_max_height)
        saved_size = self.config.get("window_size")
        if self._valid_pair(saved_size, 1):
            # One-time migration: the previous built-in 1020x680 default is
            # converted to the shorter current default. Any other saved size
            # (including tall user-chosen heights) is preserved.
            if list(saved_size) == [1020, 680]:
                saved_size = [920, 440]
            saved_width = max(self.MIN_WIDTH, min(int(saved_size[0]), self._geometry_max_width))
            saved_height = max(self.MIN_HEIGHT, min(int(saved_size[1]), self._geometry_max_height))
            # Legacy safety net: older builds could persist runaway heights
            # when lyrics natural-size propagation leaked into the window.
            # Only clamp geometry that exceeds the whole display. Runtime
            # propagation is still disabled below on the lyrics side.
            if int(saved_size[1]) > self._geometry_max_height:
                saved_height = min(440, self._geometry_max_height)
            saved_size = [saved_width, saved_height]
            self.config["window_size"] = saved_size
            self._save_config()
            self.set_default_size(saved_width, saved_height)
        self._supports_position = self._display_supports_position()
        self._last_size = None
        self._last_position = None
        self._geometry_ready = False
        self._geometry_source = None
        self.connect("configure-event", self._on_configure)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", self._on_destroy)

        skin_path = self.config.get("skin_path", "")
        if not skin_path and ALBUM_LIST_CONFIG.is_file():
            try:
                shared = json.loads(ALBUM_LIST_CONFIG.read_text(encoding="utf-8"))
                skin_path = str(shared.get("skin_path", ""))
            except Exception:
                pass
        self.theme = parse_skin_archive(skin_path) if skin_path and Path(skin_path).is_file() else DEFAULT_THEME.copy()
        self.config["skin_path"] = skin_path or ""

        self.current_location = ""
        self.current_title = ""
        self.current_data: Optional[TrackData] = None
        self.current_position_ms = 0
        self.status_length_ms = 0
        self.track_received_at = time.monotonic()
        self.play_state = "stopped"
        self.current_line_index = -1
        self.line_starts = []
        self.lyrics_generation = 0
        self.poll_busy = False
        self.shutting_down = False
        self.fetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="AuroraLyrics")
        self.css_provider = Gtk.CssProvider()
        self.css_installed = False

        self._build_ui()
        self._apply_theme_css()
        GLib.idle_add(self._restore_geometry)
        GLib.timeout_add(850, self._schedule_status_poll)
        GLib.timeout_add(150, self._update_playback_clock)
        self._schedule_status_poll()

    # ------------------------------------------------------------------ config
    @staticmethod
    def _display_bounds():
        """Return the full monitor bounding box using the GTK 3.22 monitor model."""
        rects = []
        try:
            display = Gdk.Display.get_default()
            model = display.get_monitors() if display is not None else None
            if model is not None:
                for index in range(model.get_n_items()):
                    monitor = model.get_item(index)
                    if monitor is not None:
                        rects.append(monitor.get_geometry())
        except Exception:
            rects = []
        if rects:
            left = min(rect.x for rect in rects)
            top = min(rect.y for rect in rects)
            right = max(rect.x + rect.width for rect in rects)
            bottom = max(rect.y + rect.height for rect in rects)
            return right - left, bottom - top
        try:
            screen = Gdk.Screen.get_default()
            if screen is not None:
                return screen.get_width(), screen.get_height()
        except Exception:
            pass
        return 1920, 1080

    @staticmethod
    def _display_supports_position() -> bool:
        display = Gdk.Display.get_default()
        if display is None:
            return False
        backend = " ".join((type(display).__name__, getattr(getattr(display, "__gtype__", None), "name", ""), display.get_name() or "")).lower()
        return "wayland" not in backend

    @staticmethod
    def _valid_pair(value, minimum=0):
        return isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and v >= minimum for v in value)

    def _load_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        defaults = {
            "skin_path": "",
            "window_size": [920, 440],
            "window_position": None,
            "auto_fetch_lyrics": True,
            "auto_embed_fetched_lyrics": False,
            "auto_scroll": True,
            "font_size": 15,
            "splitter_position": 320,
        }
        try:
            value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                defaults.update(value)
        except (OSError, json.JSONDecodeError):
            pass
        return defaults

    def _save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp = CONFIG_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
            os.replace(temp, CONFIG_FILE)
        except OSError as exc:
            print(f"[Aurora Config] {exc}")

    def _on_configure(self, _window, event):
        if (
            not self._geometry_ready
            or event.width < self.MIN_WIDTH or event.height < self.MIN_HEIGHT
            or event.width > self._geometry_max_width
            or event.height > self._geometry_max_height
        ):
            return False
        self._last_size = (int(event.width), int(event.height))
        if self._supports_position:
            try:
                x, y = self.get_position()
                self._last_position = (int(x), int(y))
            except Exception:
                pass
        if self._geometry_source:
            GLib.source_remove(self._geometry_source)
        self._geometry_source = GLib.timeout_add(350, self._save_geometry)
        return False

    def _restore_geometry(self):
        size = self.config.get("window_size")
        if self._valid_pair(size, 1):
            width = max(self.MIN_WIDTH, min(int(size[0]), self._geometry_max_width))
            height = max(self.MIN_HEIGHT, min(int(size[1]), self._geometry_max_height))
            self.resize(width, height)
            self._last_size = (width, height)
        if self._supports_position:
            pos = self.config.get("window_position")
            if self._valid_pair(pos, -100000):
                x, y = int(pos[0]), int(pos[1])
                display = Gdk.Display.get_default()
                monitors = []
                primary_rect = None
                if display is not None:
                    model = display.get_monitors()
                    for index in range(model.get_n_items()):
                        monitor = model.get_item(index)
                        if monitor is None:
                            continue
                        rect = monitor.get_geometry()
                        monitors.append(rect)
                        try:
                            if monitor.is_primary():
                                primary_rect = rect
                        except Exception:
                            pass
                width, height = self.get_size()
                if monitors:
                    visible = any(
                        max(0, min(x + width, rect.x + rect.width) - max(x, rect.x)) >= min(100, width)
                        and max(0, min(y + height, rect.y + rect.height) - max(y, rect.y)) >= min(70, height)
                        for rect in monitors
                    )
                    if not visible:
                        rect = primary_rect or monitors[0]
                        x = max(rect.x - width + 100, min(x, rect.x + rect.width - 100))
                        y = max(rect.y, min(y, rect.y + rect.height - 80))
                self.move(x, y)
                self._last_position = (x, y)
        self._geometry_ready = True
        return False

    def _save_geometry(self):
        self._geometry_source = None
        try:
            if self.get_mapped():
                width, height = self.get_size()
                if (
                    width >= self.MIN_WIDTH and height >= self.MIN_HEIGHT
                    and width <= self._geometry_max_width
                    and height <= self._geometry_max_height
                ):
                    self._last_size = (int(width), int(height))
                if self._supports_position:
                    x, y = self.get_position()
                    self._last_position = (int(x), int(y))
        except Exception:
            pass
        if self._last_size:
            self.config["window_size"] = list(self._last_size)
        if self._supports_position and self._last_position:
            self.config["window_position"] = list(self._last_position)
        if hasattr(self, "main_paned"):
            try:
                position = int(self.main_paned.get_position())
                if position > 0:
                    self.config["splitter_position"] = position
            except Exception:
                pass
        self._save_config()
        return False

    def _on_delete(self, *_args):
        self._save_geometry()
        return False

    def _on_destroy(self, *_args):
        self.shutting_down = True
        if self._geometry_source:
            GLib.source_remove(self._geometry_source)
            self._geometry_source = None
        self._save_geometry()
        self.fetch_executor.shutdown(wait=False, cancel_futures=True)
        Gtk.main_quit()

    # ------------------------------------------------------------------ theme
    def _apply_theme_css(self):
        t = self.theme
        slices = t.get("pledit_slices")
        has_slices = bool(slices)
        if has_slices:
            self.top_chrome.set_size_request(-1, slices["top_left"].get_height())
            self.left_chrome.set_size_request(slices["left_border"].get_width(), -1)
            self.right_chrome.set_size_request(slices["right_border"].get_width(), -1)
            self.bottom_chrome.set_size_request(-1, slices["bottom_fill"].get_height())
            self.minimize_button.set_size_request(9, 20)
            self.close_button.set_size_request(9, 20)
            self.minimize_button_label.set_text("")
            self.close_button_label.set_text("")
            external = getattr(Gtk.PolicyType, "EXTERNAL", Gtk.PolicyType.AUTOMATIC)
            self.lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, external)
            self.lyrics_scroll_rail.show()
        else:
            self.top_chrome.set_size_request(-1, 22)
            self.left_chrome.set_size_request(8, -1)
            self.right_chrome.set_size_request(8, -1)
            self.bottom_chrome.set_size_request(-1, 24)
            self.minimize_button.set_size_request(19, 16)
            self.close_button.set_size_request(19, 16)
            self.minimize_button_label.set_text("—")
            self.close_button_label.set_text("×")
            self.lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.lyrics_scroll_rail.hide()

        bar_bg = "transparent" if has_slices else t["header_bg"]
        side_bg = "transparent" if has_slices else t["header_bg"]
        side_l = "none" if has_slices else f"1px solid {t['border_light']}"
        side_r = "none" if has_slices else f"1px solid {t['border_dark']}"
        toolbar_bottom = "none" if has_slices else f"1px solid {t['border_dark']}"
        footer_bg = "transparent" if has_slices else t["header_bg"]
        footer_border = "none" if has_slices else f"1px solid {t['border_dark']}"
        css = f"""
        window, .aurora-root {{ background-color: {t['normal_bg']}; color: {t['normal_text']}; }}
        .aurora-titlebar {{ background-color: {bar_bg}; padding: 0px 0px 0px 5px; min-height: 20px; border: none; }}
        .aurora-title {{ color: {t['current_text']}; font-weight: bold; font-size: 10px; letter-spacing: 1px; }}
        .aurora-toolbar {{ background-color: {t['normal_bg']}; border-bottom: {toolbar_bottom}; padding: 2px 3px; }}
        .aurora-side {{ background-color: {side_bg}; border-left: {side_l}; border-right: {side_r}; }}
        button.aurora-button, button.aurora-toggle {{ background-image: none; background-color: {t['header_bg']}; color: {t['current_text']}; border-top: 1px solid {t['border_light']}; border-left: 1px solid {t['border_light']}; border-right: 1px solid {t['border_dark']}; border-bottom: 1px solid {t['border_dark']}; border-radius: 0; padding: 1px 4px; min-height: 15px; min-width: 0; font-size: 8px; font-weight: bold; box-shadow: none; }}
        button.aurora-button:hover, button.aurora-toggle:hover {{ background-color: {t['selected_bg']}; }}
        button.aurora-button:active, button.aurora-toggle:checked, button.aurora-toggle:active {{ background-color: {t['selected_bg']}; border-top: 1px solid {t['border_dark']}; border-left: 1px solid {t['border_dark']}; border-right: 1px solid {t['border_light']}; border-bottom: 1px solid {t['border_light']}; }}
        label.window-control-label {{ color: {t['current_text']}; font-size: 10px; font-weight: bold; padding: 0; margin: 0; }}
        .aurora-footer {{ background-color: {footer_bg}; border-top: {footer_border}; color: {t['normal_text']}; padding: 2px 7px; font-size: 10px; }}
        .aurora-muted {{ color: {t['normal_text']}; font-size: 10px; }}
        .aurora-toolbar label {{ font-size: 8px; }}
        .aurora-track-title {{ color: {t['current_text']}; font-weight: bold; font-size: 15px; }}
        .aurora-track-artist {{ color: {t['normal_text']}; font-size: 12px; }}
        .aurora-lyrics-title {{ color: {t['current_text']}; font-weight: bold; font-size: 11px; }}
        textview, textview text {{ background-color: {t['normal_bg']}; color: {t['normal_text']}; font-family: monospace; font-size: {int(self.config.get('font_size', 15))}px; }}
        textview text selection {{ background-color: {t['selected_bg']}; color: {t['current_text']}; }}
        entry {{ background-color: {t['border_dark']}; color: {t['normal_text']}; border: 1px solid {t['border_light']}; border-radius: 0; padding: 3px 5px; }}
        progressbar trough {{ background-color: {t['border_dark']}; border: 1px solid {t['border_light']}; border-radius: 0; min-height: 5px; }}
        progressbar progress {{ background-color: {t['current_text']}; border-radius: 0; min-height: 5px; }}
        scrolledwindow, viewport {{ background-color: {t['normal_bg']}; border: none; }}
        /* Invisible splitter between artwork and lyrics: still draggable,
           but nothing is painted. Default handle size (~6px) is fine. */
        paned.aurora-paned separator {{
            background-color: transparent;
            background-image: none;
            border: none;
            box-shadow: none;
            outline: none;
        }}
        """
        self.css_provider.load_from_data(css.encode("utf-8"))
        screen = Gdk.Screen.get_default()
        if screen and not self.css_installed:
            Gtk.StyleContext.add_provider_for_screen(screen, self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.css_installed = True
        self.footer.set_margin_start(14 if has_slices else 6)
        self.footer.set_margin_end(22 if has_slices else 6)
        for rail in (self.left_chrome, self.right_chrome):
            rail.set_margin_start(0)
            rail.set_margin_end(0)
            rail.set_halign(Gtk.Align.FILL)
        if hasattr(self, "current_tag"):
            self.current_tag.set_property("foreground", str(t["current_text"]))
            self.current_tag.set_property("background", str(t["selected_bg"]))
        for widget in (self.top_chrome, self.left_chrome, self.right_chrome, self.bottom_chrome, self.resize_grip):
            widget.queue_draw()

    @staticmethod
    def _paint(cr, pixbuf, x, y):
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, x, y)
        cr.paint()

    def _on_minimize_hit(self, _widget, event):
        if event.button == 1:
            self.iconify()
            return True
        return False

    def _on_close_hit(self, _widget, event):
        if event.button == 1:
            self.close()
            return True
        return False

    def _draw_top(self, _widget, cr):
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False
        width = self.top_chrome.get_allocated_width()
        left, title, fill, right = (slices[k] for k in ("top_left", "top_title", "top_fill", "top_right"))
        for x in range(0, width, max(1, fill.get_width())):
            self._paint(cr, fill, x, 0)
        if width > left.get_width() + right.get_width():
            self._paint(cr, title, (width - title.get_width()) // 2, 0)
        self._paint(cr, left, 0, 0)
        self._paint(cr, right, max(0, width - right.get_width()), 0)
        return False

    def _draw_side(self, widget, cr, side):
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False

        pix = slices["left_border" if side == "left" else "right_border"]
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        tile_h = max(1, pix.get_height())

        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        y = 0
        while y < height:
            self._paint(cr, pix, 0, y)
            y += tile_h
        cr.restore()
        return False

    def _scroll_thumb_metrics(self):
        if not hasattr(self, "lyrics_scroll_rail"):
            return 0, 0
        height = max(1, self.lyrics_scroll_rail.get_allocated_height())
        slices = self.theme.get("pledit_slices") or {}
        thumb = slices.get("scroll_thumb")
        thumb_h = min(height, thumb.get_height() if thumb else 18)
        adjustment = self.lyrics_scroll.get_vadjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        page = adjustment.get_page_size()
        max_value = max(lower, upper - page)
        usable = max(1, height - thumb_h)
        fraction = 0.0 if max_value <= lower else (adjustment.get_value() - lower) / (max_value - lower)
        fraction = max(0.0, min(1.0, fraction))
        return int(round(usable * fraction)), thumb_h

    def _draw_lyrics_scroll_rail(self, widget, cr):
        slices = self.theme.get("pledit_slices") or {}
        track = slices.get("scroll_track")
        thumb = slices.get("scroll_thumb")
        if not track or not thumb:
            return False
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        tile_h = max(1, track.get_height())
        for y in range(0, height, tile_h):
            self._paint(cr, track, 0, y)
        thumb_y, thumb_h = self._scroll_thumb_metrics()
        thumb_pix = slices.get("scroll_thumb_pressed") if self.scroll_dragging else thumb
        if thumb_pix:
            self._paint(cr, thumb_pix, 0, thumb_y)
        cr.restore()
        return False

    def _scroll_rail_press(self, widget, event):
        if event.button != 1:
            return False
        thumb_y, thumb_h = self._scroll_thumb_metrics()
        if thumb_y <= event.y <= thumb_y + thumb_h:
            self.scroll_dragging = True
            self.scroll_drag_offset = event.y - thumb_y
        else:
            adjustment = self.lyrics_scroll.get_vadjustment()
            step = max(1.0, adjustment.get_page_size() * 0.85)
            adjustment.set_value(adjustment.get_value() + (step if event.y > thumb_y else -step))
            self.scroll_dragging = True
            self.scroll_drag_offset = thumb_h / 2
        widget.queue_draw()
        return True

    def _scroll_rail_motion(self, widget, event):
        if not self.scroll_dragging:
            return False
        height = max(1, widget.get_allocated_height())
        _old_y, thumb_h = self._scroll_thumb_metrics()
        usable = max(1, height - thumb_h)
        fraction = max(0.0, min(1.0, (event.y - self.scroll_drag_offset) / usable))
        adjustment = self.lyrics_scroll.get_vadjustment()
        lower = adjustment.get_lower()
        maximum = max(lower, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(lower + fraction * (maximum - lower))
        widget.queue_draw()
        return True

    def _scroll_rail_release(self, widget, _event):
        self.scroll_dragging = False
        widget.queue_draw()
        return False

    def _draw_bottom(self, _widget, cr):
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False
        width = self.bottom_chrome.get_allocated_width()
        fill = slices["bottom_fill"]
        for x in range(0, width, max(1, fill.get_width())):
            self._paint(cr, fill, x, 0)
        self._paint(cr, slices["bottom_left_edge"], 0, 0)
        right_edge = slices.get("bottom_right_edge")
        if right_edge:
            self._paint(cr, right_edge, max(0, width - right_edge.get_width()), 0)
        grip = slices["resize_grip"]
        self._paint(cr, grip, max(0, width - grip.get_width()), max(0, self.bottom_chrome.get_allocated_height() - grip.get_height()))
        return False

    def _draw_grip(self, widget, cr):
        if self.theme.get("pledit_slices"):
            return False
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
        cr.set_line_width(1)
        for offset in (5, 9, 13):
            cr.move_to(w - offset, h - 1)
            cr.line_to(w - 1, h - offset)
        cr.stroke()
        return False

    # ------------------------------------------------------------------ UI
    def _button(self, label, tooltip, callback):
        button = Gtk.Button(label=label)
        button.set_tooltip_text(tooltip)
        button.get_style_context().add_class("aurora-button")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.connect("clicked", callback)
        return button

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("aurora-root")
        self.add(root)

        top = Gtk.Overlay()
        self.top_chrome = Gtk.DrawingArea()
        self.top_chrome.set_size_request(-1, 20)
        self.top_chrome.connect("draw", self._draw_top)
        top.add(self.top_chrome)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.get_style_context().add_class("aurora-titlebar")
        title = Gtk.Label(label="POPAMP AURORA")
        title.get_style_context().add_class("aurora-title")
        header.pack_start(title, False, False, 4)
        drag = Gtk.EventBox()
        drag.set_visible_window(False)
        drag.set_hexpand(True)
        drag.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        drag.connect("button-press-event", self._on_drag)
        header.pack_start(drag, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        has_skin_controls = bool(self.theme.get("pledit_slices"))

        self.minimize_button = Gtk.EventBox()
        self.minimize_button.set_visible_window(False)
        self.minimize_button.set_can_focus(False)
        self.minimize_button.set_tooltip_text("Minimize Popamp Aurora")
        self.minimize_button_label = Gtk.Label(label="" if has_skin_controls else "—")
        self.minimize_button_label.get_style_context().add_class("window-control-label")
        self.minimize_button.add(self.minimize_button_label)
        self.minimize_button.set_size_request(9 if has_skin_controls else 19, 20 if has_skin_controls else 16)
        self.minimize_button.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.minimize_button.connect("button-release-event", self._on_minimize_hit)
        controls.pack_start(self.minimize_button, False, False, 0)

        self.close_button = Gtk.EventBox()
        self.close_button.set_visible_window(False)
        self.close_button.set_can_focus(False)
        self.close_button.set_tooltip_text("Close Popamp Aurora")
        self.close_button_label = Gtk.Label(label="" if has_skin_controls else "×")
        self.close_button_label.get_style_context().add_class("window-control-label")
        self.close_button.add(self.close_button_label)
        self.close_button.set_size_request(9 if has_skin_controls else 19, 20 if has_skin_controls else 16)
        self.close_button.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.close_button.connect("button-release-event", self._on_close_hit)
        controls.pack_start(self.close_button, False, False, 0)
        header.pack_end(controls, False, False, 0)
        top.add_overlay(header)
        root.pack_start(top, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.get_style_context().add_class("aurora-toolbar")
        self.search_button = self._button("SEARCH", "Search lyrics providers; synchronized lyrics are preferred", self._on_manual_search)
        toolbar.pack_start(self.search_button, False, False, 0)
        self.embed_button = self._button("EMBED", "Embed the displayed lyrics into the current audio file", self._on_embed_lyrics)
        self.embed_button.set_sensitive(False)
        toolbar.pack_start(self.embed_button, False, False, 0)
        self.export_button = self._button("SAVE LRC", "Export synchronized lyrics as an .lrc sidecar file", self._on_export_lrc)
        self.export_button.set_sensitive(False)
        toolbar.pack_start(self.export_button, False, False, 0)
        skin_button = self._button("SKIN", "Load a Popamp (.psz) or Winamp (.wsz) skin", self._on_choose_skin)
        toolbar.pack_end(skin_button, False, False, 0)
        self.auto_embed = Gtk.ToggleButton(label="AUTO SAVE")
        self.auto_embed.get_style_context().add_class("aurora-toggle")
        self.auto_embed.set_tooltip_text("Automatically embed newly found lyrics; a .bak is created before the audio file is changed")
        self.auto_embed.set_active(bool(self.config.get("auto_embed_fetched_lyrics", False)))
        self.auto_embed.connect("toggled", self._on_auto_embed_toggled)
        toolbar.pack_end(self.auto_embed, False, False, 0)
        self.auto_fetch = Gtk.ToggleButton(label="AUTO SEARCH")
        self.auto_fetch.get_style_context().add_class("aurora-toggle")
        self.auto_fetch.set_tooltip_text("Search all lyric providers automatically; synchronized lyrics take priority, DarkLyrics is a plain-text fallback")
        self.auto_fetch.set_active(bool(self.config.get("auto_fetch_lyrics", True)))
        self.auto_fetch.connect("toggled", self._on_auto_fetch_toggled)
        toolbar.pack_end(self.auto_fetch, False, False, 0)

        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.left_chrome = Gtk.DrawingArea()
        self.left_chrome.set_size_request(8, -1)
        self.left_chrome.get_style_context().add_class("aurora-side")
        self.left_chrome.connect("draw", self._draw_side, "left")
        middle.pack_start(self.left_chrome, False, False, 0)

        center_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        center_column.set_hexpand(True)
        center_column.set_vexpand(True)
        center_column.pack_start(toolbar, False, False, 0)

        # Invisible draggable splitter between artwork and lyrics. _draw_cover
        # already scales the pixbuf to the current allocation, so the cover
        # refits automatically as the splitter moves. Gtk.Paned reports the
        # max (not sum) of child natural heights, and the lyrics side keeps
        # propagate_natural_height disabled, so long lyrics never grow the
        # window.
        main = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        main.get_style_context().add_class("aurora-root")
        main.get_style_context().add_class("aurora-paned")
        main.set_border_width(3)
        main.set_wide_handle(False)
        self.main_paned = main
        try:
            initial_position = int(self.config.get("splitter_position", 320))
        except (TypeError, ValueError):
            initial_position = 320
        main.set_position(max(1, initial_position))

        # Left: artwork only. No frame, no border — the scaled cover is the
        # entire widget. A small minimum keeps the splitter from collapsing it.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.set_size_request(80, -1)
        left.set_hexpand(False)
        left.set_vexpand(True)
        self.cover_overlay = Gtk.Overlay()
        self.cover_overlay.set_hexpand(True)
        self.cover_overlay.set_vexpand(True)
        self.cover_canvas = Gtk.DrawingArea()
        self.cover_canvas.set_size_request(1, 1)
        self.cover_canvas.set_hexpand(True)
        self.cover_canvas.set_vexpand(True)
        self.cover_canvas.connect("draw", self._draw_cover)
        self.cover_overlay.add(self.cover_canvas)
        self.cover_placeholder = Gtk.Label(label="WAITING FOR POPAMP")
        self.cover_placeholder.set_xalign(0.5)
        self.cover_placeholder.set_yalign(0.5)
        self.cover_placeholder.set_line_wrap(True)
        self.cover_placeholder.get_style_context().add_class("aurora-muted")
        self.cover_overlay.add_overlay(self.cover_placeholder)
        left.pack_start(self.cover_overlay, True, True, 0)
        main.pack1(left, resize=False, shrink=False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        right.set_size_request(260, -1)
        right.set_hexpand(True)
        right.set_vexpand(True)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lyric_title = Gtk.Label(label="LYRICS")
        lyric_title.get_style_context().add_class("aurora-lyrics-title")
        heading.pack_start(lyric_title, False, False, 0)
        self.lyrics_source_label = Gtk.Label(label="Waiting for playback")
        self.lyrics_source_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.lyrics_source_label.set_max_width_chars(30)
        self.lyrics_source_label.set_width_chars(22)
        self.lyrics_source_label.get_style_context().add_class("aurora-muted")
        heading.pack_end(self.lyrics_source_label, False, False, 0)
        right.pack_start(heading, False, False, 0)

        self.lyrics_scroll = Gtk.ScrolledWindow()
        self.lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.lyrics_scroll.set_hexpand(True)
        self.lyrics_scroll.set_vexpand(True)
        # Crucial: prevent long lyrics from propagating their natural height
        # up into the window. This is what previously caused the window to
        # grow taller on tracks with many lines.
        self.lyrics_scroll.set_propagate_natural_width(False)
        self.lyrics_scroll.set_propagate_natural_height(False)
        self.lyrics_scroll.set_min_content_width(0)
        self.lyrics_scroll.set_min_content_height(0)
        self.lyrics_view = Gtk.TextView()
        self.lyrics_view.set_editable(False)
        self.lyrics_view.set_cursor_visible(False)
        self.lyrics_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.lyrics_view.set_hexpand(True)
        self.lyrics_view.set_vexpand(True)
        self.lyrics_view.set_size_request(0, 0)
        self.lyrics_view.set_left_margin(6)
        self.lyrics_view.set_right_margin(4)
        self.lyrics_view.set_top_margin(10)
        self.lyrics_view.set_bottom_margin(10)
        self.lyrics_buffer = self.lyrics_view.get_buffer()
        self.current_tag = self.lyrics_buffer.create_tag(
            "current-line", foreground=str(self.theme["current_text"]),
            background=str(self.theme["selected_bg"]), weight=Pango.Weight.BOLD,
        )
        self.lyrics_scroll.add(self.lyrics_view)
        self.scroll_dragging = False
        self.scroll_drag_offset = 0.0
        lyrics_scroll_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        lyrics_scroll_row.pack_start(self.lyrics_scroll, True, True, 0)
        self.lyrics_scroll_rail = Gtk.DrawingArea()
        self.lyrics_scroll_rail.set_size_request(8, -1)
        self.lyrics_scroll_rail.set_no_show_all(True)
        self.lyrics_scroll_rail.connect("draw", self._draw_lyrics_scroll_rail)
        self.lyrics_scroll_rail.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.lyrics_scroll_rail.connect("button-press-event", self._scroll_rail_press)
        self.lyrics_scroll_rail.connect("motion-notify-event", self._scroll_rail_motion)
        self.lyrics_scroll_rail.connect("button-release-event", self._scroll_rail_release)
        lyrics_scroll_row.pack_end(self.lyrics_scroll_rail, False, False, 0)
        adjustment = self.lyrics_scroll.get_vadjustment()
        adjustment.connect("value-changed", lambda *_: self.lyrics_scroll_rail.queue_draw())
        adjustment.connect("changed", lambda *_: self.lyrics_scroll_rail.queue_draw())
        right.pack_start(lyrics_scroll_row, True, True, 0)
        self.status_detail = Gtk.Label(label="Popamp /STATUS · synchronized lyrics preferred")
        self.status_detail.set_xalign(0.0)
        self.status_detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_detail.set_max_width_chars(48)
        self.status_detail.set_width_chars(32)
        self.status_detail.get_style_context().add_class("aurora-muted")
        right.pack_start(self.status_detail, False, False, 0)
        main.pack2(right, resize=True, shrink=True)
        center_column.pack_start(main, True, True, 0)
        middle.pack_start(center_column, True, True, 0)

        self.right_chrome = Gtk.DrawingArea()
        self.right_chrome.set_size_request(8, -1)
        self.right_chrome.get_style_context().add_class("aurora-side")
        self.right_chrome.connect("draw", self._draw_side, "right")
        middle.pack_end(self.right_chrome, False, False, 0)
        root.pack_start(middle, True, True, 0)

        bottom = Gtk.Overlay()
        self.bottom_chrome = Gtk.DrawingArea()
        self.bottom_chrome.set_size_request(-1, 26)
        self.bottom_chrome.connect("draw", self._draw_bottom)
        bottom.add(self.bottom_chrome)
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.footer.get_style_context().add_class("aurora-footer")
        self.footer_status = Gtk.Label(label="Waiting for Popamp playback")
        self.footer_status.set_xalign(0.0)
        self.footer_status.set_ellipsize(Pango.EllipsizeMode.END)
        self.footer_status.set_max_width_chars(80)
        self.footer.pack_start(self.footer_status, True, True, 0)
        self.time_label = Gtk.Label(label="00:00 / 00:00")
        self.footer.pack_end(self.time_label, False, False, 0)
        bottom.add_overlay(self.footer)
        self.resize_grip = Gtk.EventBox()
        self.resize_grip.set_visible_window(False)
        self.resize_grip.set_halign(Gtk.Align.END)
        self.resize_grip.set_valign(Gtk.Align.END)
        self.resize_grip.set_size_request(16, 16)
        self.resize_grip.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.resize_grip.connect("button-press-event", self._on_resize)
        self.resize_grip.connect("draw", self._draw_grip)
        bottom.add_overlay(self.resize_grip)
        root.pack_start(bottom, False, False, 0)

        self.connect("key-press-event", self._on_key_press)
        self._set_placeholder("Waiting for Popamp playback…")

    def _set_placeholder(self, message):
        self.cover_pixbuf = None
        self.cover_placeholder.set_text(message)
        self.cover_placeholder.show()
        if hasattr(self, "cover_canvas"):
            self.cover_canvas.queue_draw()

    def _draw_cover(self, widget, cr):
        """Draw the cover into a responsive square without changing requisition."""
        pixbuf = getattr(self, "cover_pixbuf", None)
        if pixbuf is None:
            return False
        allocation = widget.get_allocation()
        available = max(1, min(allocation.width, allocation.height))
        source_w = max(1, pixbuf.get_width())
        source_h = max(1, pixbuf.get_height())
        scale = min(available / source_w, available / source_h)
        dest_w = max(1, int(round(source_w * scale)))
        dest_h = max(1, int(round(source_h * scale)))
        if (dest_w, dest_h) == (source_w, source_h):
            scaled = pixbuf
        else:
            scaled = pixbuf.scale_simple(dest_w, dest_h, GdkPixbuf.InterpType.BILINEAR)
        x = (allocation.width - dest_w) / 2.0
        y = (allocation.height - dest_h) / 2.0
        Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
        cr.paint()
        return False

    def _set_cover(self, raw: Optional[bytes]):
        if not raw:
            self._set_placeholder("NO ARTWORK")
            return
        try:
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(raw)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is None:
                self._set_placeholder("ARTWORK UNAVAILABLE")
                return
            self.cover_pixbuf = pixbuf.copy()
            self.cover_placeholder.hide()
            self.cover_canvas.queue_draw()
        except Exception as exc:
            print(f"[Aurora] Could not display artwork: {exc}")
            self._set_placeholder("ARTWORK UNAVAILABLE")

    def _render_lyrics(self, data: TrackData):
        lines = data.lyric_lines or plain_lyric_lines(data.lyrics_text)
        self.lyrics_buffer.set_text("\n".join(line.text for line in lines))
        self.lyric_lines = lines
        self.line_starts = []
        offset = 0
        for line in lines:
            self.line_starts.append(offset)
            offset += len(line.text) + 1
        self.current_line_index = -1
        self.lyrics_source_label.set_text(data.lyric_source + (" · SYNCED" if data.synced else " · PLAIN"))
        self.embed_button.set_sensitive(bool(data.lyrics_text and Path(data.location).is_file()))
        self.export_button.set_sensitive(bool(data.lyrics_text and data.synced and Path(data.location).is_file()))
        if not data.lyrics_text:
            self.lyrics_buffer.set_text("No lyrics found.\n\nUse SEARCH to check the enabled online providers, or add an .lrc sidecar beside the audio file.")
            self.embed_button.set_sensitive(False)
            self.export_button.set_sensitive(False)
        self._highlight_current_line(force=True)

    # ----------------------------------------------------------- Popamp polling
    def _schedule_status_poll(self):
        if self.shutting_down or self.poll_busy:
            return True
        self.poll_busy = True
        future = self.fetch_executor.submit(query_popamp_status)
        future.add_done_callback(lambda done: GLib.idle_add(self._on_status_result, done))
        return True

    def _on_status_result(self, future):
        self.poll_busy = False
        if self.shutting_down:
            return False
        try:
            status = future.result()
        except Exception as exc:
            message = str(exc)
            self.footer_status.set_text("Waiting for Popamp · " + message[:120])
            return False
        self.play_state = str(status.get("state", "stopped")).lower()
        self.current_position_ms = max(0, int(status.get("position_ms") or 0))
        self.status_length_ms = max(0, int(status.get("length_ms") or 0))
        self.track_received_at = time.monotonic()
        location = str(status.get("location") or "")
        title = str(status.get("title") or "")
        self._update_time_label()
        state_caption = self.play_state.upper()
        if location and (location != self.current_location or title != self.current_title or self.current_data is None):
            self.current_location = location
            self.current_title = title
            self.lyrics_generation += 1
            generation = self.lyrics_generation
            self._set_placeholder("LOADING ARTWORK…")
            self.lyrics_buffer.set_text("Loading embedded and local lyrics…")
            self.lyrics_source_label.set_text("Reading metadata")
            self.embed_button.set_sensitive(False)
            self.export_button.set_sensitive(False)
            duration_ms = self.status_length_ms
            future = self.fetch_executor.submit(self._load_track_worker, location, title, duration_ms)
            future.add_done_callback(lambda done, gen=generation: GLib.idle_add(self._on_track_loaded, gen, done))
        elif not location:
            self.footer_status.set_text(f"Popamp {state_caption.lower()} · No local track selected")
        else:
            self.footer_status.set_text(f"Popamp {state_caption.lower()} · {self.current_data.lyric_source if self.current_data else 'Loading track'}")
        return False

    def _load_track_worker(self, location, title, duration_ms):
        data = read_track_data(location, title, duration_ms)
        data.cover_bytes = _prepare_cover(data.cover_bytes)
        return data

    @staticmethod
    def _auto_fetch_worker(data: TrackData, auto_embed: bool):
        result = fetch_best_lyrics(data)
        if not result:
            return None, ""
        lyrics, source = result
        is_timed = _is_timed_lyrics(lyrics)
        has_local_text = bool(data.lyrics_text)
        should_embed = auto_embed and (not has_local_text or (not data.synced and is_timed))
        embed_note = ""
        if should_embed and data.location and Path(data.location).is_file():
            try:
                embed_lyrics(Path(data.location), lyrics, make_backup=True)
                embed_note = " · automatically embedded (backup created if needed)"
            except Exception as exc:
                embed_note = f" · auto-embed failed: {exc}"
        return result, embed_note

    def _on_track_loaded(self, generation, future):
        if self.shutting_down or generation != self.lyrics_generation:
            return False
        try:
            data = future.result()
        except Exception as exc:
            self.status_detail.set_text(f"Could not load track: {exc}")
            return False
        self.current_data = data
        self._set_cover(data.cover_bytes)
        self._render_lyrics(data)
        self.status_detail.set_text(data.location)
        self.footer_status.set_text(f"{data.lyric_source} · {self.play_state.upper()}")
        self._update_time_label()

        if (not data.lyrics_text or not data.synced) and bool(self.config.get("auto_fetch_lyrics", True)):
            if data.lyrics_text:
                self.lyrics_source_label.set_text(f"{data.lyric_source} · checking timed sources")
                self.footer_status.set_text("Looking for synchronized lyrics; current local text remains visible")
            else:
                self.lyrics_buffer.set_text("No embedded or local lyrics.\n\nSearching lyric providers…")
                self.lyrics_source_label.set_text("Searching timed and plain sources")
            generation = self.lyrics_generation
            auto_embed = bool(self.config.get("auto_embed_fetched_lyrics", False))
            online = self.fetch_executor.submit(self._auto_fetch_worker, data, auto_embed)
            online.add_done_callback(lambda done, gen=generation: GLib.idle_add(self._on_auto_fetch_complete, gen, done))
        return False

    def _on_auto_fetch_complete(self, generation, future):
        if self.shutting_down or generation != self.lyrics_generation:
            return False
        try:
            result, embed_note = future.result()
        except Exception as exc:
            result, embed_note = None, f" · {exc}"
        if not result:
            if self.current_data and self.current_data.lyrics_text:
                self.lyrics_source_label.set_text(self.current_data.lyric_source + " · local plain lyrics")
                self.footer_status.set_text("No synchronized upgrade found; keeping local lyrics")
            else:
                self.lyrics_buffer.set_text("No lyrics found in embedded tags, local sidecars or online providers.\n\nAdd an .lrc file beside the audio track or try again later.")
                self.lyrics_source_label.set_text("No lyrics found")
                self.footer_status.set_text("No lyrics provider returned a match")
            return False
        lyrics, source = result
        timed, _ = parse_lrc(lyrics)
        if self.current_data and self.current_data.lyrics_text and not self.current_data.synced and not timed:
            self.lyrics_source_label.set_text(self.current_data.lyric_source + " · local plain lyrics")
            self.footer_status.set_text("No synchronized upgrade found; keeping local lyrics")
            return False
        self.current_data.lyrics_text = lyrics
        self.current_data.lyric_source = source + (" · auto-embedded" if "automatically embedded" in embed_note else "")
        self.current_data.synced = bool(timed)
        self.current_data.lyric_lines = timed or plain_lyric_lines(lyrics)
        self._render_lyrics(self.current_data)
        self.footer_status.set_text(f"Lyrics found: {source}{embed_note}")
        return False

    def _update_playback_clock(self):
        if self.shutting_down:
            return False
        self._update_time_label()
        self._highlight_current_line()
        return True

    def _position_now(self):
        position = self.current_position_ms
        if self.play_state == "playing":
            position += int((time.monotonic() - self.track_received_at) * 1000)
        duration_limit = getattr(self, "status_length_ms", 0)
        if duration_limit:
            position = min(position, duration_limit)
        return max(0, position)

    def _update_time_label(self):
        position = self._position_now()
        duration = getattr(self, "status_length_ms", 0) or (self.current_data.duration_ms if self.current_data else 0)
        self.time_label.set_text(f"{format_time(position)} / {format_time(duration)}")

    def _highlight_current_line(self, force=False):
        data = self.current_data
        if not data or not data.synced or not self.line_starts:
            return
        position = self._position_now()
        active = -1
        for index, line in enumerate(data.lyric_lines):
            if line.time_ms is not None and line.time_ms <= position:
                active = index
            elif line.time_ms is not None and line.time_ms > position:
                break
        if active == self.current_line_index and not force:
            return
        self.current_line_index = active
        start = self.lyrics_buffer.get_start_iter()
        end = self.lyrics_buffer.get_end_iter()
        self.lyrics_buffer.remove_tag(self.current_tag, start, end)
        if active >= 0 and active < len(self.line_starts):
            line_start = self.lyrics_buffer.get_iter_at_offset(self.line_starts[active])
            line_end = line_start.copy()
            line_end.forward_to_line_end()
            self.lyrics_buffer.apply_tag(self.current_tag, line_start, line_end)
            if bool(self.config.get("auto_scroll", True)):
                self.lyrics_view.scroll_to_iter(line_start, 0.35, True, 0.5, 0.48)

    # -------------------------------------------------------------- lyric tools
    def _on_manual_search(self, _button):
        data = self.current_data
        if not data:
            self.footer_status.set_text("No track loaded yet")
            return
        self.search_button.set_sensitive(False)
        self.footer_status.set_text("Searching lyric providers (synchronized results first)…")
        generation = self.lyrics_generation
        future = self.fetch_executor.submit(fetch_best_lyrics, data)
        future.add_done_callback(lambda done, gen=generation: GLib.idle_add(self._on_manual_search_result, gen, done))

    def _on_manual_search_result(self, generation, future):
        self.search_button.set_sensitive(True)
        if self.shutting_down or generation != self.lyrics_generation:
            return False
        try:
            result = future.result()
        except Exception as exc:
            result = None
            self.footer_status.set_text(f"Lyrics search failed: {exc}")
        if not result:
            self.footer_status.set_text("No lyric provider found a match")
            return False
        lyrics, source = result
        synced, _ = parse_lrc(lyrics)
        if self.current_data.synced and not synced:
            self.footer_status.set_text("Keeping existing synchronized lyrics; providers returned only plain text")
            return False
        self.current_data.lyrics_text = lyrics
        self.current_data.lyric_source = source
        self.current_data.synced = bool(synced)
        self.current_data.lyric_lines = synced or plain_lyric_lines(lyrics)
        self._render_lyrics(self.current_data)
        self.footer_status.set_text(f"Found lyrics: {source}")
        return False

    def _on_embed_lyrics(self, _button):
        data = self.current_data
        if not data or not data.lyrics_text or not Path(data.location).is_file():
            return
        dialog = Gtk.Dialog(title="Embed lyrics into audio file", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Embed", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label=f"Embed lyrics into:\n{Path(data.location).name}"), False, False, 0)
        backup = Gtk.CheckButton(label="Create a .bak backup before modifying tags")
        backup.set_active(True)
        box.pack_start(backup, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        make_backup = backup.get_active()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        path = Path(data.location)
        lyrics = data.lyrics_text
        self.embed_button.set_sensitive(False)
        self.footer_status.set_text("Embedding lyrics into audio tags…")
        generation = self.lyrics_generation
        future = self.fetch_executor.submit(embed_lyrics, path, lyrics, make_backup)
        future.add_done_callback(lambda done, gen=generation: GLib.idle_add(self._on_embed_done, gen, done))

    def _on_embed_done(self, generation, future):
        if generation != self.lyrics_generation:
            return False
        try:
            result = future.result()
            self.current_data.lyric_source = "Embedded tags"
            self.lyrics_source_label.set_text("Embedded tags" + (" · SYNCED" if self.current_data.synced else " · PLAIN"))
            self.footer_status.set_text(result)
        except Exception as exc:
            self.footer_status.set_text(f"Could not embed lyrics: {exc}")
        self.embed_button.set_sensitive(bool(self.current_data and self.current_data.lyrics_text))
        return False

    def _on_export_lrc(self, _button):
        data = self.current_data
        if not data or not data.lyrics_text or not Path(data.location).is_file():
            return
        path = Path(data.location).with_suffix(".lrc" if data.synced else ".txt")
        if path.exists():
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       buttons=Gtk.ButtonsType.YES_NO, text=f"Replace existing {path.name}?")
            answer = dialog.run()
            dialog.destroy()
            if answer != Gtk.ResponseType.YES:
                return
        try:
            path.write_text(data.lyrics_text, encoding="utf-8")
            self.footer_status.set_text(f"Saved {path.name}")
        except OSError as exc:
            self.footer_status.set_text(f"Could not save sidecar: {exc}")

    def _on_auto_fetch_toggled(self, button):
        self.config["auto_fetch_lyrics"] = bool(button.get_active())
        self._save_config()

    def _on_auto_embed_toggled(self, button):
        self.config["auto_embed_fetched_lyrics"] = bool(button.get_active())
        self._save_config()

    # --------------------------------------------------------------- skin menu
    def _on_choose_skin(self, _button):
        dialog = Gtk.FileChooserDialog(title="Load Popamp / Winamp skin", parent=self,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        filt = Gtk.FileFilter()
        filt.set_name("Popamp and Winamp skins (*.psz, *.wsz)")
        filt.add_pattern("*.psz")
        filt.add_pattern("*.PSZ")
        filt.add_pattern("*.wsz")
        filt.add_pattern("*.WSZ")
        dialog.add_filter(filt)
        current = self.config.get("skin_path", "")
        if current and Path(current).is_file():
            dialog.set_filename(current)
        else:
            skin_dirs = [Path.home() / ".local/share/popamp/skins", Path.home() / ".config/popamp/skins", Path("/usr/share/popamp/skins")]
            for folder in skin_dirs:
                if folder.is_dir():
                    dialog.set_current_folder(str(folder))
                    break
        response = dialog.run()
        filename = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if filename:
            self.config["skin_path"] = filename
            self.theme = parse_skin_archive(filename)
            self._save_config()
            self._apply_theme_css()
            self.footer_status.set_text(f"Loaded skin: {self.theme['name']}")

    # --------------------------------------------------------------- windowing
    def _on_drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _on_resize(self, _widget, event):
        if event.button == 1:
            self.begin_resize_drag(Gdk.WindowEdge.SOUTH_EAST, event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _on_key_press(self, _widget, event):
        if event.keyval == Gdk.KEY_F5:
            self._on_manual_search(None)
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


def main():
    try:
        if not HAS_MUTAGEN:
            print("Popamp Aurora requires Mutagen. Install it with: sudo pacman -S python-mutagen")
            raise SystemExit(1)
        win = AuroraWindow()
        win.show_all()
        Gtk.main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()