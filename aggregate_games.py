#!/usr/bin/env python3
"""
Game Library Aggregator (Steam + Heroic Games Launcher)
Aggregates owned games from Steam and Heroic (Epic, GOG, Amazon, Sideloaded),
enriches them with IGDB (critic score, user score, genres, themes),
Steam User Reviews, Steam Deck Verified status, and ProtonDB tiers,
and generates an interactive offline HTML dashboard + CSV export.

Uses Python 3 standard library only (zero pip dependencies).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import html
import json
import os
import pathlib
import re
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "GameLibraryAggregator/1.0 (Linux; Python stdlib)"
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_CACHE_TTL_DAYS = 30

# Known non-game Steam tools / runtimes / redistributables to skip in local mode
STEAM_SKIP_APPIDS = {
    228980,   # Steamworks Common Redistributables
    1070560,  # Steam Linux Runtime 1.0 (scout)
    1391110,  # Steam Linux Runtime 2.0 (soldier)
    1628350,  # Steam Linux Runtime 3.0 (sniper)
    1493710,  # Proton Experimental
    2180100,  # Proton Hotfix
    1887720,  # Proton 7.0
    2348590,  # Proton 8.0
    2805730,  # Proton 9.0
    3658110,  # Proton 10.0
    1580130,  # Proton EasyAntiCheat Runtime
    1161040,  # Proton BattlEye Runtime
}

STEAM_SKIP_NAME_PATTERNS = (
    re.compile(r"^proton(\s+\d|\s+experimental|\s+hotfix|\s+easyanticheat|\s+battleye)", re.I),
    re.compile(r"^steam linux runtime", re.I),
    re.compile(r"^steamworks common redistributables$", re.I),
)

HEROIC_SKIP_APP_NAMES = {
    "gog-redist",
}

HEROIC_SKIP_TITLE_PATTERNS = (
    re.compile(r"common redistributables$", re.I),
    re.compile(r"\b(original\s+)?soundtrack\b$", re.I),
    re.compile(r"\b(digital\s+)?art\s*book\b$", re.I),
    re.compile(r"\bdedicated server\b$", re.I),
    re.compile(r"\bmod\s*(kit|tools?)\b$", re.I),
)

EDITION_SUFFIX_RE = re.compile(
    r"\s*[-:–—]?\s*\b("
    r"game of the year|goty|definitive|complete|ultimate|deluxe|enhanced|"
    r"director'?s cut|gold|standard|special|collector'?s|anniversary|"
    r"legendary|royal|premium|digital|classic"
    r")\s*(edition|version|collection|bundle)?\s*$",
    re.I,
)


@dataclass
class GameEntry:
    title: str
    stores: set[str] = field(default_factory=set)
    steam_appid: int | None = None
    heroic_ids: dict[str, str] = field(default_factory=dict)
    is_installed: bool = False
    cover_wide: str | None = None
    cover_tall: str | None = None

    # IGDB metadata
    igdb_id: int | None = None
    igdb_name: str | None = None
    igdb_url: str | None = None
    critic_score: int | None = None
    critic_count: int = 0
    igdb_user_score: int | None = None
    igdb_user_count: int = 0
    genres: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)

    # Steam Reviews metadata
    steam_user_score: int | None = None
    steam_user_count: int = 0
    steam_review_desc: str | None = None

    # Compatibility
    deck_status: str | None = None      # Verified | Playable | Unsupported | Unknown
    protondb_tier: str | None = None    # Native | Platinum | Gold | Silver | Bronze | Borked

    @property
    def user_score(self) -> int | None:
        if self.steam_user_score is not None:
            return self.steam_user_score
        return self.igdb_user_score

    @property
    def user_score_source(self) -> str | None:
        if self.steam_user_score is not None:
            return "Steam"
        if self.igdb_user_score is not None:
            return "IGDB"
        return None

    @property
    def user_review_count(self) -> int:
        if self.steam_user_score is not None:
            return self.steam_user_count
        if self.igdb_user_score is not None:
            return self.igdb_user_count
        return 0

    @property
    def steam_url(self) -> str | None:
        if self.steam_appid:
            return f"https://store.steampowered.com/app/{self.steam_appid}/"
        return None


# ---------------------------------------------------------------------------
# Environment / Config helpers
# ---------------------------------------------------------------------------

def load_dotenv(env_path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def get_cfg(key: str, dotenv: dict[str, str], default: str = "") -> str:
    return os.environ.get(key) or dotenv.get(key) or default


def normalize_title(title: str, strip_edition: bool = False) -> str:
    s = title.strip()
    s = s.replace("®", "").replace("™", "").replace("©", "")
    # Leading symbol quirk like ">observer_"
    s = s.lstrip(">_ ").rstrip("_ ")
    if strip_edition:
        prev = None
        while prev != s:
            prev = s
            s = EDITION_SUFFIX_RE.sub("", s).strip()
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def clean_search_title(title: str) -> str:
    s = title.strip()
    s = s.replace("®", "").replace("™", "").replace("©", "")
    s = s.lstrip(">_ ").rstrip("_ ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# HTTP helper with retry/backoff
# ---------------------------------------------------------------------------

def http_request_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 20,
    max_retries: int = 3,
    allow_404: bool = False,
) -> Any:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < max_retries:
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if (retry_after and retry_after.isdigit()) else (1.5 * (2 ** attempt))
                time.sleep(min(wait, 12.0))
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt + 1 < max_retries:
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc
    return None


# ---------------------------------------------------------------------------
# SQLite Cache
# ---------------------------------------------------------------------------

class MetadataCache:
    def __init__(self, db_path: pathlib.Path, ttl_days: int = DEFAULT_CACHE_TTL_DAYS):
        self.db_path = db_path
        self.ttl_seconds = ttl_days * 86400
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_tables()

    def _init_tables(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                """
            )
            self.conn.commit()

    def get(self, namespace: str, key: str, ignore_ttl: bool = False) -> tuple[bool, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT payload, fetched_at FROM cache WHERE namespace = ? AND key = ?",
                (namespace, str(key)),
            ).fetchone()
        if not row:
            return False, None
        payload_str, fetched_at = row
        if not ignore_ttl and (time.time() - fetched_at) > self.ttl_seconds:
            return False, None
        try:
            return True, json.loads(payload_str)
        except json.JSONDecodeError:
            return False, None

    def set(self, namespace: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, payload, fetched_at) VALUES (?, ?, ?, ?)",
                (namespace, str(key), payload, now),
            )
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


# ---------------------------------------------------------------------------
# Library Discovery: Heroic Games Launcher
# ---------------------------------------------------------------------------

def find_heroic_dir(override: str = "") -> pathlib.Path | None:
    candidates = []
    if override:
        candidates.append(pathlib.Path(override).expanduser())
    home = pathlib.Path.home()
    candidates.extend([
        home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
        home / ".config/heroic",
    ])
    for path in candidates:
        if (path / "store_cache").is_dir() or (path / "sideload_apps").is_dir():
            return path
    return None


def _should_skip_heroic(app_name: str, title: str) -> bool:
    if app_name.lower() in HEROIC_SKIP_APP_NAMES:
        return True
    for pat in HEROIC_SKIP_TITLE_PATTERNS:
        if pat.search(title):
            return True
    return False


def load_heroic_games(heroic_dir: pathlib.Path) -> list[GameEntry]:
    games: list[GameEntry] = []
    store_cache = heroic_dir / "store_cache"

    sources = [
        (store_cache / "legendary_library.json", "Epic", ("library", "games")),
        (store_cache / "gog_library.json", "GOG", ("games", "library")),
        (store_cache / "nile_library.json", "Amazon", ("library", "games")),
        (heroic_dir / "sideload_apps" / "library.json", "Sideloaded", ("games", "library")),
    ]

    for json_path, store_label, list_keys in sources:
        if not json_path.exists():
            continue
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] Could not parse {json_path}: {exc}", file=sys.stderr)
            continue

        items: list[dict[str, Any]] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            for k in list_keys:
                if isinstance(raw.get(k), list):
                    items = raw[k]
                    break

        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            app_name = str(item.get("app_name") or "").strip()
            if not title:
                continue
            if _should_skip_heroic(app_name, title):
                continue
            is_installed = bool(item.get("is_installed", False))
            art_cover = (item.get("art_cover") or "").strip() or None
            art_square = (item.get("art_square") or "").strip() or None
            entry = GameEntry(
                title=title,
                stores={store_label},
                heroic_ids={store_label: app_name} if app_name else {},
                is_installed=is_installed,
                cover_wide=art_cover,
                cover_tall=art_square or art_cover,
            )
            games.append(entry)
            count += 1
        print(f"  Heroic ({store_label}): found {count} games")

    return games


# ---------------------------------------------------------------------------
# Library Discovery: Steam
# ---------------------------------------------------------------------------

def find_steam_dir(override: str = "") -> pathlib.Path | None:
    candidates = []
    if override:
        candidates.append(pathlib.Path(override).expanduser())
    home = pathlib.Path.home()
    candidates.extend([
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ])
    for path in candidates:
        if (path / "steamapps").is_dir():
            return path
    return None


def detect_steam_id(steam_dir: pathlib.Path | None) -> str | None:
    if not steam_dir:
        return None
    loginusers = steam_dir / "config" / "loginusers.vdf"
    if not loginusers.exists():
        return None
    txt = loginusers.read_text(encoding="utf-8", errors="ignore")
    # Match user blocks to prefer MostRecent "1"
    blocks = re.finditer(r'"(7656119\d{10})"\s*\{([^}]*)\}', txt, re.S)
    fallback_id = None
    for m in blocks:
        sid = m.group(1)
        body = m.group(2)
        if fallback_id is None:
            fallback_id = sid
        if re.search(r'"MostRecent"\s*"1"', body, re.I):
            return sid
    return fallback_id


def _should_skip_steam(appid: int, name: str) -> bool:
    if appid in STEAM_SKIP_APPIDS:
        return True
    for pat in STEAM_SKIP_NAME_PATTERNS:
        if pat.search(name):
            return True
    return False


def load_steam_games_api(api_key: str, steam_id: str) -> list[GameEntry]:
    params = urllib.parse.urlencode({
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": "1",
        "include_played_free_games": "1",
        "format": "json",
    })
    url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?{params}"
    data = http_request_json(url)
    raw_games = (data or {}).get("response", {}).get("games", [])
    if not raw_games:
        print(
            "[warn] Steam Web API returned 0 games. Make sure your Steam Profile's "
            "'Game details' privacy setting is set to Public.",
            file=sys.stderr,
        )
        return []

    games: list[GameEntry] = []
    for item in raw_games:
        appid = int(item["appid"])
        name = (item.get("name") or f"Steam App {appid}").strip()
        if _should_skip_steam(appid, name):
            continue
        games.append(GameEntry(title=name, stores={"Steam"}, steam_appid=appid))
    print(f"  Steam (Web API): found {len(games)} owned games")
    return games


def _parse_binary_vdf(
    buf: bytes,
    pos: int,
    end_limit: int | None = None,
    string_table: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    obj: dict[str, Any] = {}
    lim = len(buf) if end_limit is None else end_limit
    while pos < lim:
        t = buf[pos]
        pos += 1
        if t == 8:
            break
        if string_table is not None:
            idx = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
            key = string_table[idx]
        else:
            e = buf.index(b"\x00", pos)
            key = buf[pos:e].decode("utf-8", errors="replace")
            pos = e + 1

        if t == 0:
            val, pos = _parse_binary_vdf(buf, pos, end_limit, string_table)
        elif t == 1:
            e = buf.index(b"\x00", pos)
            val = buf[pos:e].decode("utf-8", errors="replace")
            pos = e + 1
        elif t == 2:
            val = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif t == 3:
            val = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif t == 7:
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        else:
            raise ValueError(f"Unsupported binary VDF type {t} at offset {pos}")
        obj[key] = val
    return obj, pos


def load_steam_games_local_owned(steam_dir: pathlib.Path) -> list[GameEntry]:
    """
    Reads the full owned Steam library directly from local Steam cache files
    (appcache/packageinfo.vdf + appcache/appinfo.vdf) — no Steam API key needed!
    """
    pkg_path = steam_dir / "appcache" / "packageinfo.vdf"
    app_path = steam_dir / "appcache" / "appinfo.vdf"
    if not pkg_path.exists() or not app_path.exists():
        return []

    # 1. Collect owned AppIDs from packageinfo.vdf (skip package 0 = Steam default/Spacewar)
    pb = pkg_path.read_bytes()
    pmagic, _ = struct.unpack_from("<II", pb, 0)
    pos = 8
    owned_appids: set[int] = set()
    while pos + 4 <= len(pb):
        pid = struct.unpack_from("<I", pb, pos)[0]
        pos += 4
        if pid == 0xFFFFFFFF:
            break
        pos += 32 if pmagic >= 0x06565528 else 24
        kv, pos = _parse_binary_vdf(pb, pos)
        if pid == 0:
            continue
        inner = next(iter(kv.values()), {}) if isinstance(kv, dict) else {}
        apps = inner.get("appids", {}) if isinstance(inner, dict) else {}
        for v in apps.values():
            if isinstance(v, int):
                owned_appids.add(v)

    # 2. Resolve AppID -> name & type from appinfo.vdf, keeping type == "game"
    ab = app_path.read_bytes()
    amagic, _ = struct.unpack_from("<II", ab, 0)
    pos = 8
    string_table: list[str] | None = None
    if amagic >= 0x07564429:
        st_offset = struct.unpack_from("<q", ab, pos)[0]
        pos += 8
        sp = st_offset
        st_count = struct.unpack_from("<I", ab, sp)[0]
        sp += 4
        string_table = []
        for _ in range(st_count):
            e = ab.index(b"\x00", sp)
            string_table.append(ab[sp:e].decode("utf-8", errors="replace"))
            sp = e + 1

    app_names: dict[int, str] = {}
    while pos + 4 <= len(ab):
        aid = struct.unpack_from("<I", ab, pos)[0]
        pos += 4
        if aid == 0:
            break
        sz = struct.unpack_from("<I", ab, pos)[0]
        pos += 4
        entry_end = pos + sz
        if aid in owned_appids:
            hdr = 60 if amagic >= 0x07564428 else 40
            kv, _ = _parse_binary_vdf(ab, pos + hdr, entry_end, string_table)
            common = kv.get("appinfo", {}).get("common", {})
            if isinstance(common, dict) and str(common.get("type", "")).lower() == "game":
                name = str(common.get("name") or "").strip()
                if name:
                    app_names[aid] = name
        pos = entry_end

    # Also detect installed AppIDs so we can mark is_installed accurately
    installed_ids = {g.steam_appid for g in load_steam_games_local(steam_dir, quiet=True) if g.steam_appid}

    games: list[GameEntry] = []
    for aid, name in sorted(app_names.items(), key=lambda kv: kv[1].lower()):
        if _should_skip_steam(aid, name):
            continue
        games.append(
            GameEntry(
                title=name,
                stores={"Steam"},
                steam_appid=aid,
                is_installed=(aid in installed_ids),
            )
        )
    print(f"  Steam (local packageinfo/appinfo): found {len(games)} owned games")
    return games


def load_steam_games_local(steam_dir: pathlib.Path, quiet: bool = False) -> list[GameEntry]:
    lib_vdf = steam_dir / "steamapps" / "libraryfolders.vdf"
    library_paths: list[pathlib.Path] = [steam_dir]
    if lib_vdf.exists():
        txt = lib_vdf.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
            p = pathlib.Path(m.group(1).replace("\\\\", "\\"))
            if p not in library_paths and (p / "steamapps").is_dir():
                library_paths.append(p)

    seen_appids: set[int] = set()
    games: list[GameEntry] = []
    for lib in library_paths:
        steamapps = lib / "steamapps"
        if not steamapps.is_dir():
            continue
        for acf in sorted(steamapps.glob("appmanifest_*.acf")):
            txt = acf.read_text(encoding="utf-8", errors="ignore")
            m_id = re.search(r'"appid"\s*"(\d+)"', txt)
            m_name = re.search(r'"name"\s*"([^"]+)"', txt)
            if not m_id or not m_name:
                continue
            appid = int(m_id.group(1))
            name = m_name.group(1).strip()
            if appid in seen_appids or _should_skip_steam(appid, name):
                continue
            seen_appids.add(appid)
            games.append(
                GameEntry(title=name, stores={"Steam"}, steam_appid=appid, is_installed=True)
            )
    if not quiet:
        print(f"  Steam (local installed): found {len(games)} games")
    return games


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def merge_entries(target: GameEntry, incoming: GameEntry) -> None:
    target.stores.update(incoming.stores)
    target.heroic_ids.update(incoming.heroic_ids)
    target.is_installed = target.is_installed or incoming.is_installed
    if target.steam_appid is None and incoming.steam_appid is not None:
        target.steam_appid = incoming.steam_appid
    if target.cover_wide is None and incoming.cover_wide:
        target.cover_wide = incoming.cover_wide
    if target.cover_tall is None and incoming.cover_tall:
        target.cover_tall = incoming.cover_tall
    # Prefer cleaner title if one has weird leading symbol
    if target.title.startswith(">") and not incoming.title.startswith(">"):
        target.title = incoming.title


def deduplicate_games(games: list[GameEntry]) -> list[GameEntry]:
    by_steam_id: dict[int, GameEntry] = {}
    by_norm_title: dict[str, GameEntry] = {}
    result: list[GameEntry] = []

    for g in games:
        existing: GameEntry | None = None
        if g.steam_appid is not None and g.steam_appid in by_steam_id:
            existing = by_steam_id[g.steam_appid]
        else:
            norm = normalize_title(g.title, strip_edition=False)
            if norm and norm in by_norm_title:
                existing = by_norm_title[norm]

        if existing is not None:
            merge_entries(existing, g)
            if existing.steam_appid is not None:
                by_steam_id[existing.steam_appid] = existing
        else:
            result.append(g)
            if g.steam_appid is not None:
                by_steam_id[g.steam_appid] = g
            norm = normalize_title(g.title, strip_edition=False)
            if norm:
                by_norm_title[norm] = g

    return result


def deduplicate_after_enrichment(games: list[GameEntry]) -> list[GameEntry]:
    """Merge Heroic + Steam entries that IGDB resolved to the same IGDB ID or Steam AppID."""
    by_igdb: dict[int, GameEntry] = {}
    by_steam: dict[int, GameEntry] = {}
    merged: list[GameEntry] = []

    for g in games:
        existing: GameEntry | None = None
        if g.steam_appid and g.steam_appid in by_steam:
            existing = by_steam[g.steam_appid]
        elif g.igdb_id and g.igdb_id in by_igdb:
            existing = by_igdb[g.igdb_id]

        if existing is not None:
            merge_entries(existing, g)
            # Keep richer metadata if target missed any
            if existing.igdb_id is None and g.igdb_id is not None:
                existing.igdb_id = g.igdb_id
                existing.igdb_name = g.igdb_name
                existing.igdb_url = g.igdb_url
                existing.critic_score = g.critic_score
                existing.critic_count = g.critic_count
                existing.igdb_user_score = g.igdb_user_score
                existing.igdb_user_count = g.igdb_user_count
                existing.genres = g.genres
                existing.themes = g.themes
            if existing.steam_appid:
                by_steam[existing.steam_appid] = existing
            if existing.igdb_id:
                by_igdb[existing.igdb_id] = existing
        else:
            merged.append(g)
            if g.steam_appid:
                by_steam[g.steam_appid] = g
            if g.igdb_id:
                by_igdb[g.igdb_id] = g

    return merged


# ---------------------------------------------------------------------------
# IGDB Client (Twitch OAuth + v4 API)
# ---------------------------------------------------------------------------

class IGDBClient:
    BASE_URL = "https://api.igdb.com/v4"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    GAME_FIELDS = (
        "fields id,name,url,aggregated_rating,aggregated_rating_count,"
        "rating,rating_count,genres.name,themes.name,platforms,"
        "external_games.external_game_source,external_games.uid;"
    )

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: str | None = None
        self._last_req = 0.0

    def authenticate(self) -> None:
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        resp = http_request_json(
            self.TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        token = (resp or {}).get("access_token")
        if not token:
            raise RuntimeError("Failed to obtain Twitch OAuth token for IGDB.")
        self.access_token = token

    def _throttle(self) -> None:
        # IGDB limit is 4 requests/sec; keep comfortable ~0.28s spacing
        elapsed = time.monotonic() - self._last_req
        if elapsed < 0.28:
            time.sleep(0.28 - elapsed)
        self._last_req = time.monotonic()

    def query(self, endpoint: str, apicalypse: str) -> list[dict[str, Any]]:
        if not self.access_token:
            self.authenticate()
        self._throttle()
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "text/plain",
        }
        res = http_request_json(url, method="POST", headers=headers, data=apicalypse.encode("utf-8"))
        return res if isinstance(res, list) else []

    def batch_lookup_by_steam_appids(self, appids: list[int]) -> dict[int, dict[str, Any]]:
        """Return mapping steam_appid -> IGDB game dict using batched v4 queries."""
        steam_to_igdb_id: dict[int, int] = {}
        chunk_size = 80
        for i in range(0, len(appids), chunk_size):
            chunk = appids[i : i + chunk_size]
            uid_list = ",".join(f'"{aid}"' for aid in chunk)
            q = (
                f"fields game,uid,external_game_source; "
                f"where external_game_source = 1 & uid = ({uid_list}); "
                f"limit 500;"
            )
            rows = self.query("external_games", q)
            for r in rows:
                uid = str(r.get("uid") or "")
                gid = r.get("game")
                if uid.isdigit() and isinstance(gid, int):
                    # First match or overwrite cleanly
                    steam_to_igdb_id.setdefault(int(uid), gid)

        igdb_ids = sorted(set(steam_to_igdb_id.values()))
        igdb_games: dict[int, dict[str, Any]] = {}
        for i in range(0, len(igdb_ids), chunk_size):
            chunk = igdb_ids[i : i + chunk_size]
            id_list = ",".join(str(gid) for gid in chunk)
            q = f"{self.GAME_FIELDS} where id = ({id_list}); limit 500;"
            rows = self.query("games", q)
            for g in rows:
                if isinstance(g.get("id"), int):
                    igdb_games[g["id"]] = g

        result: dict[int, dict[str, Any]] = {}
        for aid, gid in steam_to_igdb_id.items():
            if gid in igdb_games:
                result[aid] = igdb_games[gid]
        return result

    def search_by_title(self, title: str) -> dict[str, Any] | None:
        cleaned = clean_search_title(title)
        if not cleaned:
            return None

        # Try exact search first; if no good match and title has edition suffix, try stripped title
        candidates = self._search_raw(cleaned)
        best = _pick_best_igdb_candidate(title, candidates)
        if best is not None:
            return best

        stripped = EDITION_SUFFIX_RE.sub("", cleaned).strip()
        if stripped and stripped.lower() != cleaned.lower():
            candidates2 = self._search_raw(stripped)
            best = _pick_best_igdb_candidate(stripped, candidates2)
        return best

    def _search_raw(self, query_text: str) -> list[dict[str, Any]]:
        escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
        q = f'{self.GAME_FIELDS} search "{escaped}"; limit 10;'
        return self.query("games", q)


def _pick_best_igdb_candidate(title: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    target_exact = normalize_title(title, strip_edition=False)
    target_base = normalize_title(title, strip_edition=True)
    target_tokens = set(target_base.split())

    best_score = -1.0
    best_cand: dict[str, Any] | None = None

    for c in candidates:
        cname = c.get("name") or ""
        c_exact = normalize_title(cname, strip_edition=False)
        c_base = normalize_title(cname, strip_edition=True)
        c_tokens = set(c_base.split())

        score = 0.0
        if c_exact == target_exact:
            score += 100.0
        elif c_base == target_base and target_base:
            score += 85.0
        elif target_tokens and c_tokens:
            overlap = len(target_tokens & c_tokens) / max(len(target_tokens | c_tokens), 1)
            if overlap < 0.55:
                continue
            score += overlap * 65.0
        else:
            continue

        platforms = c.get("platforms") or []
        if 6 in platforms:  # PC (Microsoft Windows)
            score += 10.0
        if c.get("aggregated_rating") is not None:
            score += 4.0
        if c.get("rating") is not None:
            score += 2.0
        # Slight bonus if Steam external_game exists
        for ext in c.get("external_games") or []:
            if isinstance(ext, dict) and ext.get("external_game_source") == 1:
                score += 3.0
                break

        if score > best_score:
            best_score = score
            best_cand = c

    return best_cand if best_score >= 45.0 else None


def extract_igdb_summary(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    steam_appid: int | None = None
    for ext in raw.get("external_games") or []:
        if isinstance(ext, dict) and ext.get("external_game_source") == 1:
            uid = str(ext.get("uid") or "")
            if uid.isdigit():
                steam_appid = int(uid)
                break

    genres = [g["name"] for g in (raw.get("genres") or []) if isinstance(g, dict) and g.get("name")]
    themes = [t["name"] for t in (raw.get("themes") or []) if isinstance(t, dict) and t.get("name")]

    agg = raw.get("aggregated_rating")
    usr = raw.get("rating")

    return {
        "igdb_id": raw.get("id"),
        "igdb_name": raw.get("name"),
        "igdb_url": raw.get("url"),
        "critic_score": round(agg) if isinstance(agg, (int, float)) else None,
        "critic_count": int(raw.get("aggregated_rating_count") or 0),
        "igdb_user_score": round(usr) if isinstance(usr, (int, float)) else None,
        "igdb_user_count": int(raw.get("rating_count") or 0),
        "genres": genres,
        "themes": themes,
        "steam_appid": steam_appid,
    }


def apply_igdb_summary(game: GameEntry, summary: dict[str, Any] | None) -> None:
    if not summary:
        return
    game.igdb_id = summary.get("igdb_id")
    game.igdb_name = summary.get("igdb_name")
    game.igdb_url = summary.get("igdb_url")
    game.critic_score = summary.get("critic_score")
    game.critic_count = int(summary.get("critic_count") or 0)
    game.igdb_user_score = summary.get("igdb_user_score")
    game.igdb_user_count = int(summary.get("igdb_user_count") or 0)
    game.genres = list(summary.get("genres") or [])
    game.themes = list(summary.get("themes") or [])
    if game.steam_appid is None and summary.get("steam_appid"):
        game.steam_appid = int(summary["steam_appid"])


# ---------------------------------------------------------------------------
# Steam Reviews, Steam Deck Verified & ProtonDB Enrichment
# ---------------------------------------------------------------------------

DECK_CATEGORY_MAP = {
    3: "Verified",
    2: "Playable",
    1: "Unsupported",
    0: "Unknown",
}

PROTONDB_TIER_MAP = {
    "native": "Native",
    "platinum": "Platinum",
    "gold": "Gold",
    "silver": "Silver",
    "bronze": "Bronze",
    "borked": "Borked",
}


def fetch_steam_store_batch(appids: list[int]) -> dict[int, dict[str, Any]]:
    """
    Batch-fetch Steam User Reviews + Steam Deck compatibility via Valve's
    official IStoreBrowseService/GetItems/v1 WebAPI (no API key required).
    """
    results: dict[int, dict[str, Any]] = {}
    chunk_size = 50
    for i in range(0, len(appids), chunk_size):
        chunk = appids[i : i + chunk_size]
        payload = json.dumps({
            "ids": [{"appid": aid} for aid in chunk],
            "context": {"language": "english", "country_code": "US", "steam_realm": 1},
            "data_request": {"include_reviews": True, "include_platforms": True},
        })
        url = (
            "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?"
            + urllib.parse.urlencode({"input_json": payload})
        )
        data = http_request_json(url, timeout=20)
        items = (data or {}).get("response", {}).get("store_items") or []
        by_id = {int(it["appid"]): it for it in items if isinstance(it, dict) and it.get("appid")}

        for aid in chunk:
            it = by_id.get(aid) or {}
            rev_summary = (
                (it.get("reviews") or {}).get("summary_filtered")
                or (it.get("reviews") or {}).get("summary_unfiltered")
                or {}
            )
            total = int(rev_summary.get("review_count") or 0)
            pct = rev_summary.get("percent_positive")
            score = int(pct) if (total > 0 and isinstance(pct, (int, float))) else None
            desc = rev_summary.get("review_score_label") or None

            deck_cat = (it.get("platforms") or {}).get("steam_deck_compat_category")
            deck_status = DECK_CATEGORY_MAP.get(deck_cat, "Unknown") if isinstance(deck_cat, int) else None

            results[aid] = {
                "reviews": {"score": score, "total": total, "desc": desc},
                "deck": deck_status,
            }
        if i + chunk_size < len(appids):
            time.sleep(0.25)
    return results


def fetch_protondb_tier(appid: int) -> str | None:
    url = f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
    data = http_request_json(url, timeout=15, allow_404=True)
    if not isinstance(data, dict):
        return None
    raw_tier = (data.get("tier") or "").strip().lower()
    return PROTONDB_TIER_MAP.get(raw_tier)


def enrich_igdb(
    games: list[GameEntry],
    igdb: IGDBClient,
    cache: MetadataCache,
    refresh: bool = False,
) -> None:
    # 1. Batch lookup Steam-AppID games not yet cached
    steam_to_fetch: list[int] = []
    for g in games:
        if g.steam_appid is None:
            continue
        hit, cached = cache.get("igdb_steam", str(g.steam_appid), ignore_ttl=False)
        if hit and not refresh:
            apply_igdb_summary(g, cached)
        else:
            steam_to_fetch.append(g.steam_appid)

    steam_to_fetch = sorted(set(steam_to_fetch))
    if steam_to_fetch:
        print(f"Fetching IGDB metadata for {len(steam_to_fetch)} Steam games (batched)...")
        found = igdb.batch_lookup_by_steam_appids(steam_to_fetch)
        for aid in steam_to_fetch:
            summary = extract_igdb_summary(found.get(aid))
            cache.set("igdb_steam", str(aid), summary)
        for g in games:
            if g.steam_appid in steam_to_fetch:
                _, cached = cache.get("igdb_steam", str(g.steam_appid), ignore_ttl=True)
                apply_igdb_summary(g, cached)

    # 2. Title lookup for games without IGDB match yet (Heroic titles + any Steam appids missing in external_games)
    title_targets: list[GameEntry] = []
    for g in games:
        if g.igdb_id is not None:
            continue
        key = normalize_title(g.title, strip_edition=False)
        if not key:
            continue
        hit, cached = cache.get("igdb_title", key, ignore_ttl=False)
        if hit and not refresh:
            apply_igdb_summary(g, cached)
        else:
            title_targets.append(g)

    if title_targets:
        print(f"Searching IGDB by title for {len(title_targets)} games...")
        for idx, g in enumerate(title_targets, 1):
            key = normalize_title(g.title, strip_edition=False)
            # Check again in case duplicate normalized title was fetched earlier in loop
            hit, cached = cache.get("igdb_title", key, ignore_ttl=False)
            if hit and not refresh:
                apply_igdb_summary(g, cached)
                continue
            try:
                raw = igdb.search_by_title(g.title)
                summary = extract_igdb_summary(raw)
                cache.set("igdb_title", key, summary)
                apply_igdb_summary(g, summary)
            except Exception as exc:
                print(f"  [warn] IGDB lookup failed for '{g.title}': {exc}", file=sys.stderr)
            if idx % 20 == 0 or idx == len(title_targets):
                print(f"  IGDB title progress: {idx}/{len(title_targets)}")


def enrich_steam_and_compat(
    games: list[GameEntry],
    cache: MetadataCache,
    refresh: bool = False,
    include_compat: bool = True,
) -> None:
    appids = sorted({g.steam_appid for g in games if g.steam_appid is not None})
    if not appids:
        return

    need_steam_store: list[int] = []
    need_proton: list[int] = []

    for aid in appids:
        hit_r, _ = cache.get("steam_reviews", str(aid))
        hit_d, _ = cache.get("steam_deck", str(aid))
        if refresh or not hit_r or (include_compat and not hit_d):
            need_steam_store.append(aid)
        if include_compat:
            hit_p, _ = cache.get("protondb", str(aid))
            if refresh or not hit_p:
                need_proton.append(aid)

    if need_steam_store:
        print(f"Fetching Steam Reviews & Deck status for {len(need_steam_store)} games (batched)...")
        try:
            batch_data = fetch_steam_store_batch(need_steam_store)
            for aid, info in batch_data.items():
                cache.set("steam_reviews", str(aid), info["reviews"])
                if include_compat:
                    cache.set("steam_deck", str(aid), info["deck"])
        except Exception as exc:
            print(f"  [warn] Steam batch lookup failed: {exc}", file=sys.stderr)

    def _run_pool(label: str, items: list[int], fn, ns: str, workers: int = 4, delay: float = 0.08) -> None:
        if not items:
            return
        print(f"Fetching {label} for {len(items)} games...")
        done = 0
        lock = threading.Lock()

        def worker(aid: int) -> None:
            nonlocal done
            try:
                val = fn(aid)
                cache.set(ns, str(aid), val)
            except Exception as exc:
                print(f"  [warn] {label} failed for AppID {aid}: {exc}", file=sys.stderr)
            time.sleep(delay)
            with lock:
                done += 1
                if done % 40 == 0 or done == len(items):
                    print(f"  {label} progress: {done}/{len(items)}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(worker, items))

    if include_compat:
        _run_pool("ProtonDB tiers", need_proton, fetch_protondb_tier, "protondb", workers=4, delay=0.10)

    # Apply cached values to GameEntry objects
    for g in games:
        if g.steam_appid is None:
            continue
        aid_str = str(g.steam_appid)
        hit_r, rev = cache.get("steam_reviews", aid_str, ignore_ttl=True)
        if hit_r and isinstance(rev, dict):
            g.steam_user_score = rev.get("score")
            g.steam_user_count = int(rev.get("total") or 0)
            g.steam_review_desc = rev.get("desc")
        if include_compat:
            hit_d, deck = cache.get("steam_deck", aid_str, ignore_ttl=True)
            if hit_d and isinstance(deck, str):
                g.deck_status = deck
            hit_p, pdb = cache.get("protondb", aid_str, ignore_ttl=True)
            if hit_p and isinstance(pdb, str):
                g.protondb_tier = pdb


# ---------------------------------------------------------------------------
# Export: CSV
# ---------------------------------------------------------------------------

def write_csv(games: list[GameEntry], out_path: pathlib.Path) -> None:
    fieldnames = [
        "Title",
        "Stores",
        "Genres",
        "Themes",
        "Critic Score",
        "Critic Reviews",
        "User Score",
        "User Score Source",
        "User Reviews",
        "Steam Deck",
        "ProtonDB",
        "Steam AppID",
        "IGDB URL",
        "Steam URL",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for g in sorted(games, key=lambda x: (x.critic_score is None, -(x.critic_score or 0), x.title.lower())):
            writer.writerow({
                "Title": g.title,
                "Stores": ", ".join(sorted(g.stores)),
                "Genres": ", ".join(g.genres),
                "Themes": ", ".join(g.themes),
                "Critic Score": g.critic_score if g.critic_score is not None else "",
                "Critic Reviews": g.critic_count if g.critic_score is not None else "",
                "User Score": g.user_score if g.user_score is not None else "",
                "User Score Source": g.user_score_source or "",
                "User Reviews": g.user_review_count if g.user_score is not None else "",
                "Steam Deck": g.deck_status or "",
                "ProtonDB": g.protondb_tier or "",
                "Steam AppID": g.steam_appid or "",
                "IGDB URL": g.igdb_url or "",
                "Steam URL": g.steam_url or "",
            })


# ---------------------------------------------------------------------------
# Export: Interactive Self-Contained HTML Dashboard
# ---------------------------------------------------------------------------

def write_html(games: list[GameEntry], out_path: pathlib.Path) -> None:
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    records = []
    for g in games:
        steam_tall = (
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{g.steam_appid}/library_600x900.jpg"
            if g.steam_appid
            else None
        )
        steam_wide = (
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{g.steam_appid}/header.jpg"
            if g.steam_appid
            else None
        )
        cover_tall = steam_tall or g.cover_tall or g.cover_wide
        cover_wide = steam_wide or g.cover_wide or g.cover_tall
        cover_fallback = steam_wide if steam_tall else (g.cover_wide if g.cover_wide != cover_tall else None)

        records.append({
            "title": g.title,
            "stores": sorted(g.stores),
            "genres": g.genres,
            "themes": g.themes,
            "critic": g.critic_score,
            "criticCount": g.critic_count,
            "user": g.user_score,
            "userSource": g.user_score_source,
            "userCount": g.user_review_count,
            "deck": g.deck_status,
            "proton": g.protondb_tier,
            "steamUrl": g.steam_url,
            "igdbUrl": g.igdb_url,
            "steamAppId": g.steam_appid,
            "coverTall": cover_tall,
            "coverWide": cover_wide,
            "coverFallback": cover_fallback,
        })

    # Escape </script> safely inside embedded JSON
    payload_json = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Game Library Aggregator</title>
<style>
  :root {{
    --bg: #0f1218;
    --panel: #171c26;
    --panel-2: #1e2533;
    --border: #2a3345;
    --text: #e7ecf5;
    --muted: #93a0b8;
    --accent: #66c0f4;
    --good: #34d399;
    --mid: #fbbf24;
    --bad: #f87171;
    --chip: #252f42;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }}
  .wrap {{
    max-width: 1440px;
    margin: 0 auto;
    padding: 20px 20px 48px;
  }}
  header {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: flex-end;
    gap: 12px;
    margin-bottom: 18px;
  }}
  h1 {{
    margin: 0;
    font-size: 1.55rem;
    letter-spacing: -0.02em;
  }}
  .subtitle {{
    color: var(--muted);
    font-size: 0.88rem;
    margin-top: 2px;
  }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
  }}
  .stat {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
  }}
  .stat .num {{
    font-size: 1.35rem;
    font-weight: 700;
  }}
  .stat .lbl {{
    font-size: 0.78rem;
    color: var(--muted);
  }}
  .controls {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px;
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .ctrl-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }}
  input[type="search"], select {{
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 11px;
    font-size: 0.9rem;
    outline: none;
  }}
  input[type="search"] {{
    flex: 1 1 240px;
  }}
  input[type="search"]:focus, select:focus {{
    border-color: var(--accent);
  }}
  .btn {{
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 0.86rem;
    cursor: pointer;
  }}
  .btn:hover {{
    border-color: var(--accent);
  }}
  .btn.primary {{
    background: #1b3650;
    border-color: #366a99;
    color: #dbeeff;
  }}
  .pills {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }}
  .pill {{
    background: var(--chip);
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 999px;
    padding: 4px 11px;
    font-size: 0.8rem;
    cursor: pointer;
    user-select: none;
  }}
  .pill.active {{
    background: #1d4363;
    border-color: var(--accent);
    color: #fff;
  }}
  .table-wrap {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow-x: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }}
  thead th {{
    position: sticky;
    top: 0;
    background: #141923;
    border-bottom: 1px solid var(--border);
    text-align: left;
    padding: 11px 12px;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    white-space: nowrap;
  }}
  thead th.sortable {{
    cursor: pointer;
    user-select: none;
  }}
  thead th.sortable:hover {{
    color: var(--text);
  }}
  thead th.sorted {{
    color: var(--accent);
  }}
  tbody td {{
    border-bottom: 1px solid #1f2634;
    padding: 10px 12px;
    vertical-align: middle;
  }}
  tbody tr:hover {{
    background: #1b2230;
  }}
  .game-title {{
    font-weight: 600;
  }}
  .game-title a {{
    color: var(--text);
    text-decoration: none;
  }}
  .game-title a:hover {{
    color: var(--accent);
    text-decoration: underline;
  }}
  .links {{
    display: inline-flex;
    gap: 6px;
    margin-left: 6px;
    font-size: 0.72rem;
  }}
  .links a {{
    color: var(--muted);
    text-decoration: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
  }}
  .links a:hover {{
    color: var(--accent);
    border-color: var(--accent);
  }}
  .badge-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }}
  .badge {{
    display: inline-block;
    font-size: 0.74rem;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--chip);
    border: 1px solid var(--border);
    color: #cbd5e1;
    white-space: nowrap;
  }}
  .badge.clickable {{
    cursor: pointer;
  }}
  .badge.clickable:hover {{
    border-color: var(--accent);
    color: #fff;
  }}
  .store-Steam {{ background: #162e45; border-color: #2c5b85; color: #b9e0ff; }}
  .store-Epic {{ background: #26262b; border-color: #4f4f5c; color: #e4e4e7; }}
  .store-GOG {{ background: #311d3f; border-color: #6b3b8a; color: #e9d5ff; }}
  .store-Amazon {{ background: #332714; border-color: #8a6423; color: #fde68a; }}
  .store-Sideloaded {{ background: #1d3228; border-color: #356e52; color: #bbf7d0; }}

  .score-cell {{
    white-space: nowrap;
  }}
  .score-pill {{
    display: inline-block;
    min-width: 42px;
    text-align: center;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 7px;
    font-size: 0.88rem;
  }}
  .score-high {{ background: rgba(52, 211, 153, 0.16); color: #6ee7b7; border: 1px solid rgba(52, 211, 153, 0.35); }}
  .score-mid  {{ background: rgba(251, 191, 36, 0.16); color: #fcd34d; border: 1px solid rgba(251, 191, 36, 0.35); }}
  .score-low  {{ background: rgba(248, 113, 113, 0.16); color: #fca5a5; border: 1px solid rgba(248, 113, 113, 0.35); }}
  .score-none {{ color: #64748b; font-weight: 500; }}
  .meta-sub {{
    display: block;
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 2px;
  }}

  .deck-Verified {{ background: rgba(34, 197, 94, 0.16); border-color: rgba(34, 197, 94, 0.4); color: #86efac; }}
  .deck-Playable {{ background: rgba(234, 179, 8, 0.16); border-color: rgba(234, 179, 8, 0.4); color: #fde047; }}
  .deck-Unsupported {{ background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5; }}
  .deck-Unknown {{ background: #202736; border-color: #364156; color: #94a3b8; }}

  .pdb-Native   {{ background: rgba(56, 189, 248, 0.16); border-color: rgba(56, 189, 248, 0.45); color: #7dd3fc; }}
  .pdb-Platinum {{ background: rgba(196, 181, 253, 0.16); border-color: rgba(196, 181, 253, 0.45); color: #ddd6fe; }}
  .pdb-Gold     {{ background: rgba(250, 204, 21, 0.16); border-color: rgba(250, 204, 21, 0.45); color: #fef08a; }}
  .pdb-Silver   {{ background: rgba(203, 213, 225, 0.15); border-color: rgba(203, 213, 225, 0.4); color: #e2e8f0; }}
  .pdb-Bronze   {{ background: rgba(217, 119, 6, 0.18); border-color: rgba(217, 119, 6, 0.45); color: #fdba74; }}
  .pdb-Borked   {{ background: rgba(239, 68, 68, 0.16); border-color: rgba(239, 68, 68, 0.45); color: #fca5a5; }}

  .empty {{
    text-align: center;
    padding: 36px;
    color: var(--muted);
  }}
  .header-actions {{
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .view-toggle {{
    display: inline-flex;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 2px;
  }}
  .view-toggle button {{
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 0.84rem;
    cursor: pointer;
    font-weight: 600;
  }}
  .view-toggle button.active {{
    background: #1d4363;
    color: #fff;
  }}

  /* Table thumbnail */
  .title-cell {{
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 240px;
  }}
  .thumb-wrap {{
    width: 92px;
    height: 43px;
    flex: 0 0 92px;
    border-radius: 6px;
    overflow: hidden;
    background: linear-gradient(135deg, #1b2434, #26344d);
    border: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .thumb-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }}
  .thumb-fallback {{
    font-size: 0.66rem;
    color: var(--muted);
    text-align: center;
    padding: 2px 4px;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }}

  /* Steam-style Grid View */
  .grid-wrap {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
    gap: 16px;
  }}
  .game-card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 11px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
  }}
  .game-card:hover {{
    transform: translateY(-3px);
    border-color: var(--accent);
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.45);
  }}
  .cover-box {{
    position: relative;
    width: 100%;
    aspect-ratio: 2 / 3;
    background: linear-gradient(150deg, #1a2333 0%, #0f1622 100%);
    overflow: hidden;
    display: block;
  }}
  .cover-box img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 0.25s ease;
  }}
  .game-card:hover .cover-box img {{
    transform: scale(1.04);
  }}
  .cover-placeholder {{
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 16px;
    font-weight: 700;
    font-size: 0.98rem;
    color: #cbd5e1;
    background: radial-gradient(circle at 30% 20%, #263650, #131924 75%);
  }}
  .cover-overlay-top {{
    position: absolute;
    top: 8px;
    left: 8px;
    right: 8px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 4px;
    pointer-events: none;
  }}
  .cover-overlay-top .badge {{
    backdrop-filter: blur(6px);
    box-shadow: 0 2px 6px rgba(0,0,0,0.45);
  }}
  .cover-overlay-bottom {{
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    padding: 26px 8px 8px;
    background: linear-gradient(to top, rgba(10,13,19,0.95) 20%, rgba(10,13,19,0.55) 65%, transparent 100%);
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 6px;
  }}
  .mini-score {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 1px;
  }}
  .mini-score.right {{
    align-items: flex-end;
  }}
  .mini-score .lbl {{
    font-size: 0.64rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #cbd5e1;
  }}
  .mini-score .score-pill {{
    padding: 2px 7px;
    font-size: 0.82rem;
    min-width: 36px;
  }}
  .card-body {{
    padding: 10px 11px 12px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    flex: 1;
  }}
  .card-title {{
    font-weight: 650;
    font-size: 0.92rem;
    line-height: 1.28;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .card-title a {{
    color: var(--text);
    text-decoration: none;
  }}
  .card-title a:hover {{
    color: var(--accent);
  }}
  .card-links {{
    display: flex;
    gap: 5px;
    margin-top: auto;
    padding-top: 4px;
    font-size: 0.72rem;
  }}
  .card-links a {{
    color: var(--muted);
    text-decoration: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 6px;
  }}
  .card-links a:hover {{
    color: var(--accent);
    border-color: var(--accent);
  }}
  .hidden {{ display: none !important; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Game Library Aggregator</h1>
      <div class="subtitle">Steam + Heroic (Epic / GOG / Amazon) &bull; IGDB + Steam Reviews + Deck &amp; ProtonDB &bull; Generated {html.escape(generated_at)}</div>
    </div>
    <div class="header-actions">
      <div class="view-toggle" role="group" aria-label="View mode">
        <button id="viewGridBtn" class="active">Tiles</button>
        <button id="viewTableBtn">Table</button>
      </div>
      <button class="btn primary" id="exportCsvBtn">Export Filtered CSV</button>
    </div>
  </header>

  <div class="stats" id="statsBar"></div>

  <div class="controls">
    <div class="ctrl-row">
      <input type="search" id="searchInput" placeholder="Search title, genre, or theme..." />
      <select id="sortSelect" title="Sort by">
        <option value="critic">Sort: Critic Score</option>
        <option value="user">Sort: User Score</option>
        <option value="compat">Sort: Deck / ProtonDB</option>
        <option value="title">Sort: Title (A-Z)</option>
      </select>
      <button class="btn" id="sortDirBtn" title="Toggle sort direction">↓ Desc</button>
      <select id="genreSelect"><option value="">All Genres</option></select>
      <select id="themeSelect"><option value="">All Themes</option></select>
      <select id="compatSelect">
        <option value="">All Deck / ProtonDB</option>
        <option value="deck_verified">Steam Deck: Verified</option>
        <option value="deck_playable_plus">Steam Deck: Verified or Playable</option>
        <option value="pdb_plat_plus">ProtonDB: Native or Platinum</option>
        <option value="pdb_gold_plus">ProtonDB: Gold or better</option>
      </select>
      <select id="criticFilter">
        <option value="">All Critic Status</option>
        <option value="has">Has Critic Score</option>
        <option value="missing">No Critic Score (Niche / Unrated)</option>
      </select>
      <button class="btn" id="resetBtn">Reset</button>
    </div>
    <div class="ctrl-row">
      <span style="font-size:0.8rem;color:var(--muted);">Storefront:</span>
      <div class="pills" id="storePills"></div>
    </div>
  </div>

  <div id="gridView" class="grid-wrap"></div>

  <div id="tableView" class="table-wrap hidden">
    <table>
      <thead>
        <tr>
          <th class="sortable" data-sort="title">Title</th>
          <th>Store(s)</th>
          <th>Genres</th>
          <th>Themes</th>
          <th class="sortable" data-sort="compat">Deck / ProtonDB</th>
          <th class="sortable" data-sort="critic">Critic Score</th>
          <th class="sortable" data-sort="user">User Score</th>
        </tr>
      </thead>
      <tbody id="tableBody"></tbody>
    </table>
  </div>
</div>

<script>
const GAMES = {payload_json};

const state = {{
  view: "grid",
  search: "",
  store: "ALL",
  genre: "",
  theme: "",
  compat: "",
  criticFilter: "",
  sortBy: "critic",
  sortDir: "desc",
}};

function handleImgError(img) {{
  const fb = img.dataset.fallback;
  if (fb && img.src !== fb) {{
    img.dataset.fallback = "";
    img.src = fb;
    return;
  }}
  const parent = img.parentElement;
  if (!parent) return;
  img.remove();
  const ph = document.createElement("div");
  ph.className = img.dataset.placeholderClass || "cover-placeholder";
  ph.textContent = img.alt || "";
  parent.prepend(ph);
}}

const DECK_RANK = {{ Verified: 4, Playable: 3, Unknown: 2, Unsupported: 1 }};
const PDB_RANK = {{ Native: 6, Platinum: 5, Gold: 4, Silver: 3, Bronze: 2, Borked: 1 }};

function compatRank(g) {{
  const p = PDB_RANK[g.proton] || 0;
  const d = DECK_RANK[g.deck] || 0;
  if (!p && !d) return null;
  return d * 10 + p;
}}

function fmtCount(n) {{
  if (!n) return "";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}}

function scoreClass(val) {{
  if (val === null || val === undefined) return "score-none";
  if (val >= 80) return "score-high";
  if (val >= 60) return "score-mid";
  return "score-low";
}}

function esc(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }}[c]));
}}

function populateFilters() {{
  const stores = new Set();
  const genres = new Set();
  const themes = new Set();
  for (const g of GAMES) {{
    g.stores.forEach(s => stores.add(s));
    g.genres.forEach(x => genres.add(x));
    g.themes.forEach(x => themes.add(x));
  }}

  const pillsEl = document.getElementById("storePills");
  ["ALL", ...Array.from(stores).sort()].forEach(st => {{
    const btn = document.createElement("button");
    btn.className = "pill" + (st === state.store ? " active" : "");
    btn.textContent = st === "ALL" ? "All Stores" : st;
    btn.dataset.store = st;
    btn.addEventListener("click", () => {{
      state.store = st;
      pillsEl.querySelectorAll(".pill").forEach(p => p.classList.toggle("active", p.dataset.store === st));
      render();
    }});
    pillsEl.appendChild(btn);
  }});

  const gSel = document.getElementById("genreSelect");
  Array.from(genres).sort().forEach(x => {{
    const opt = document.createElement("option");
    opt.value = x;
    opt.textContent = x;
    gSel.appendChild(opt);
  }});

  const tSel = document.getElementById("themeSelect");
  Array.from(themes).sort().forEach(x => {{
    const opt = document.createElement("option");
    opt.value = x;
    opt.textContent = x;
    tSel.appendChild(opt);
  }});
}}

function getFiltered() {{
  const q = state.search.trim().toLowerCase();
  return GAMES.filter(g => {{
    if (state.store !== "ALL" && !g.stores.includes(state.store)) return false;
    if (state.genre && !g.genres.includes(state.genre)) return false;
    if (state.theme && !g.themes.includes(state.theme)) return false;
    if (state.criticFilter === "has" && g.critic === null) return false;
    if (state.criticFilter === "missing" && g.critic !== null) return false;

    if (state.compat === "deck_verified" && g.deck !== "Verified") return false;
    if (state.compat === "deck_playable_plus" && !["Verified", "Playable"].includes(g.deck)) return false;
    if (state.compat === "pdb_plat_plus" && !["Native", "Platinum"].includes(g.proton)) return false;
    if (state.compat === "pdb_gold_plus" && !["Native", "Platinum", "Gold"].includes(g.proton)) return false;

    if (q) {{
      const hay = (g.title + " " + g.genres.join(" ") + " " + g.themes.join(" ")).toLowerCase();
      if (!hay.includes(q)) return false;
    }}
    return true;
  }});
}}

function sortGames(list) {{
  const dir = state.sortDir === "asc" ? 1 : -1;
  return list.slice().sort((a, b) => {{
    if (state.sortBy === "title") {{
      return a.title.localeCompare(b.title) * dir;
    }}
    let av = null, bv = null;
    if (state.sortBy === "critic") {{ av = a.critic; bv = b.critic; }}
    else if (state.sortBy === "user") {{ av = a.user; bv = b.user; }}
    else if (state.sortBy === "compat") {{ av = compatRank(a); bv = compatRank(b); }}

    // Always keep missing values at the bottom regardless of sort direction
    if (av === null && bv === null) return a.title.localeCompare(b.title);
    if (av === null) return 1;
    if (bv === null) return -1;
    if (av !== bv) return (av - bv) * dir;
    // Tie-breaker by review count for user/critic
    if (state.sortBy === "user" && a.userCount !== b.userCount) {{
      return (a.userCount - b.userCount) * dir;
    }}
    if (state.sortBy === "critic" && a.criticCount !== b.criticCount) {{
      return (a.criticCount - b.criticCount) * dir;
    }}
    return a.title.localeCompare(b.title);
  }});
}}

function renderStats(filtered) {{
  const withCritic = filtered.filter(g => g.critic !== null).length;
  const withUser = filtered.filter(g => g.user !== null).length;
  const deckGood = filtered.filter(g => ["Verified", "Playable"].includes(g.deck)).length;
  const pdbGood = filtered.filter(g => ["Native", "Platinum", "Gold"].includes(g.proton)).length;
  document.getElementById("statsBar").innerHTML = `
    <div class="stat"><div class="num">${{filtered.length}} <span style="font-size:0.8rem;color:var(--muted);font-weight:400">/ ${{GAMES.length}}</span></div><div class="lbl">Games Shown</div></div>
    <div class="stat"><div class="num">${{withCritic}}</div><div class="lbl">With Critic Score</div></div>
    <div class="stat"><div class="num">${{withUser}}</div><div class="lbl">With User Score</div></div>
    <div class="stat"><div class="num">${{deckGood}}</div><div class="lbl">Deck Verified / Playable</div></div>
    <div class="stat"><div class="num">${{pdbGood}}</div><div class="lbl">ProtonDB Gold+</div></div>
  `;
}}

function render() {{
  const filtered = sortGames(getFiltered());
  renderStats(filtered);

  document.getElementById("sortSelect").value = state.sortBy;
  document.getElementById("sortDirBtn").textContent = state.sortDir === "asc" ? "↑ Asc" : "↓ Desc";
  document.getElementById("viewGridBtn").classList.toggle("active", state.view === "grid");
  document.getElementById("viewTableBtn").classList.toggle("active", state.view === "table");
  document.getElementById("gridView").classList.toggle("hidden", state.view !== "grid");
  document.getElementById("tableView").classList.toggle("hidden", state.view !== "table");

  document.querySelectorAll("thead th.sortable").forEach(th => {{
    const active = th.dataset.sort === state.sortBy;
    th.classList.toggle("sorted", active);
    const base = th.textContent.replace(/ [▲▼]$/, "");
    th.textContent = active ? base + (state.sortDir === "asc" ? " ▲" : " ▼") : base;
  }});

  if (state.view === "grid") {{
    renderGrid(filtered);
  }} else {{
    renderTable(filtered);
  }}
}}

function renderGrid(filtered) {{
  const grid = document.getElementById("gridView");
  if (!filtered.length) {{
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1;">No games match the current filters.</div>';
    return;
  }}

  const cards = filtered.map(g => {{
    const mainUrl = g.steamUrl || g.igdbUrl;
    const coverImg = g.coverTall
      ? `<img src="${{esc(g.coverTall)}}" data-fallback="${{esc(g.coverFallback || "")}}" data-placeholder-class="cover-placeholder" alt="${{esc(g.title)}}" loading="lazy" decoding="async" onerror="handleImgError(this)" />`
      : `<div class="cover-placeholder">${{esc(g.title)}}</div>`;

    const storesHtml = g.stores.map(s => `<span class="badge store-${{esc(s)}}">${{esc(s)}}</span>`).join("");

    const criticOverlay = g.critic !== null
      ? `<div class="mini-score"><span class="lbl">Critic</span><span class="score-pill ${{scoreClass(g.critic)}}">${{g.critic}}</span></div>`
      : `<div class="mini-score"><span class="lbl">Critic</span><span class="score-pill score-none" style="background:rgba(15,18,24,0.75);border:1px solid var(--border);">—</span></div>`;

    const userOverlay = g.user !== null
      ? `<div class="mini-score right"><span class="lbl">${{esc(g.userSource || "User")}}${{g.userCount ? " · " + fmtCount(g.userCount) : ""}}</span><span class="score-pill ${{scoreClass(g.user)}}">${{g.user}}%</span></div>`
      : `<div class="mini-score right"><span class="lbl">User</span><span class="score-pill score-none" style="background:rgba(15,18,24,0.75);border:1px solid var(--border);">—</span></div>`;

    const compatBadges = [];
    if (g.deck) compatBadges.push(`<span class="badge deck-${{esc(g.deck)}}">Deck: ${{esc(g.deck)}}</span>`);
    if (g.proton) compatBadges.push(`<span class="badge pdb-${{esc(g.proton)}}">PDB: ${{esc(g.proton)}}</span>`);

    const tags = [
      ...g.genres.slice(0, 3).map(x => `<span class="badge clickable" data-filter-genre="${{esc(x)}}">${{esc(x)}}</span>`),
      ...g.themes.slice(0, 2).map(x => `<span class="badge clickable" style="opacity:0.85;" data-filter-theme="${{esc(x)}}">${{esc(x)}}</span>`),
    ];

    const extLinks = [];
    if (g.steamUrl) extLinks.push(`<a href="${{esc(g.steamUrl)}}" target="_blank" rel="noopener">Steam</a>`);
    if (g.igdbUrl) extLinks.push(`<a href="${{esc(g.igdbUrl)}}" target="_blank" rel="noopener">IGDB</a>`);
    if (g.steamAppId) extLinks.push(`<a href="https://www.protondb.com/app/${{g.steamAppId}}" target="_blank" rel="noopener">PDB</a>`);

    const coverTag = mainUrl
      ? `<a class="cover-box" href="${{esc(mainUrl)}}" target="_blank" rel="noopener">${{coverImg}}<div class="cover-overlay-top"><div class="badge-list">${{storesHtml}}</div></div><div class="cover-overlay-bottom">${{criticOverlay}}${{userOverlay}}</div></a>`
      : `<div class="cover-box">${{coverImg}}<div class="cover-overlay-top"><div class="badge-list">${{storesHtml}}</div></div><div class="cover-overlay-bottom">${{criticOverlay}}${{userOverlay}}</div></div>`;

    const titleHtml = mainUrl
      ? `<a href="${{esc(mainUrl)}}" target="_blank" rel="noopener" title="${{esc(g.title)}}">${{esc(g.title)}}</a>`
      : esc(g.title);

    return `<article class="game-card">
      ${{coverTag}}
      <div class="card-body">
        <div class="card-title">${{titleHtml}}</div>
        ${{compatBadges.length ? `<div class="badge-list">${{compatBadges.join("")}}</div>` : ""}}
        ${{tags.length ? `<div class="badge-list">${{tags.join("")}}</div>` : ""}}
        ${{extLinks.length ? `<div class="card-links">${{extLinks.join("")}}</div>` : ""}}
      </div>
    </article>`;
  }});

  grid.innerHTML = cards.join("");
}}

function renderTable(filtered) {{
  const tbody = document.getElementById("tableBody");
  if (!filtered.length) {{
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No games match the current filters.</td></tr>';
    return;
  }}

  const rows = filtered.map(g => {{
    const mainUrl = g.steamUrl || g.igdbUrl;
    const titleHtml = mainUrl
      ? `<a href="${{esc(mainUrl)}}" target="_blank" rel="noopener">${{esc(g.title)}}</a>`
      : esc(g.title);

    const thumbHtml = g.coverWide
      ? `<div class="thumb-wrap"><img src="${{esc(g.coverWide)}}" data-fallback="${{esc(g.coverTall || "")}}" data-placeholder-class="thumb-fallback" alt="${{esc(g.title)}}" loading="lazy" decoding="async" onerror="handleImgError(this)" /></div>`
      : `<div class="thumb-wrap"><div class="thumb-fallback">${{esc(g.title)}}</div></div>`;

    const extLinks = [];
    if (g.steamUrl) extLinks.push(`<a href="${{esc(g.steamUrl)}}" target="_blank" rel="noopener">Steam</a>`);
    if (g.igdbUrl) extLinks.push(`<a href="${{esc(g.igdbUrl)}}" target="_blank" rel="noopener">IGDB</a>`);
    if (g.steamAppId) extLinks.push(`<a href="https://www.protondb.com/app/${{g.steamAppId}}" target="_blank" rel="noopener">PDB</a>`);

    const storesHtml = g.stores.map(s => `<span class="badge store-${{esc(s)}}">${{esc(s)}}</span>`).join("");
    const genresHtml = g.genres.length
      ? g.genres.map(x => `<span class="badge clickable" data-filter-genre="${{esc(x)}}">${{esc(x)}}</span>`).join("")
      : '<span class="score-none">—</span>';
    const themesHtml = g.themes.length
      ? g.themes.map(x => `<span class="badge clickable" data-filter-theme="${{esc(x)}}">${{esc(x)}}</span>`).join("")
      : '<span class="score-none">—</span>';

    const compatBadges = [];
    if (g.deck) compatBadges.push(`<span class="badge deck-${{esc(g.deck)}}" title="Steam Deck Compatibility">Deck: ${{esc(g.deck)}}</span>`);
    if (g.proton) compatBadges.push(`<span class="badge pdb-${{esc(g.proton)}}" title="ProtonDB Tier">PDB: ${{esc(g.proton)}}</span>`);
    const compatHtml = compatBadges.length ? `<div class="badge-list">${{compatBadges.join("")}}</div>` : '<span class="score-none">—</span>';

    const criticHtml = g.critic !== null
      ? `<span class="score-pill ${{scoreClass(g.critic)}}">${{g.critic}}</span><span class="meta-sub">${{g.criticCount ? g.criticCount + " critics" : "IGDB"}}</span>`
      : '<span class="score-none">—</span>';

    const userHtml = g.user !== null
      ? `<span class="score-pill ${{scoreClass(g.user)}}">${{g.user}}%</span><span class="meta-sub">${{esc(g.userSource || "")}}${{g.userCount ? " · " + fmtCount(g.userCount) : ""}}</span>`
      : '<span class="score-none">—</span>';

    return `<tr>
      <td><div class="title-cell">${{thumbHtml}}<div><div class="game-title">${{titleHtml}}</div><div class="links" style="margin-left:0;margin-top:3px;">${{extLinks.join("")}}</div></div></div></td>
      <td><div class="badge-list">${{storesHtml}}</div></td>
      <td><div class="badge-list">${{genresHtml}}</div></td>
      <td><div class="badge-list">${{themesHtml}}</div></td>
      <td>${{compatHtml}}</td>
      <td class="score-cell">${{criticHtml}}</td>
      <td class="score-cell">${{userHtml}}</td>
    </tr>`;
  }});

  tbody.innerHTML = rows.join("");
}}

function exportFilteredCsv() {{
  const rows = sortGames(getFiltered());
  const header = ["Title","Stores","Genres","Themes","Critic Score","Critic Reviews","User Score","User Source","User Reviews","Steam Deck","ProtonDB","Steam URL","IGDB URL"];
  const csvEsc = v => '"' + String(v ?? "").replace(/"/g, '""') + '"';
  const lines = [header.map(csvEsc).join(",")];
  for (const g of rows) {{
    lines.push([
      g.title, g.stores.join("; "), g.genres.join("; "), g.themes.join("; "),
      g.critic ?? "", g.critic !== null ? g.criticCount : "",
      g.user ?? "", g.userSource ?? "", g.user !== null ? g.userCount : "",
      g.deck ?? "", g.proton ?? "", g.steamUrl ?? "", g.igdbUrl ?? ""
    ].map(csvEsc).join(","));
  }}
  const blob = new Blob([lines.join("\\n")], {{ type: "text/csv;charset=utf-8;" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "games_filtered.csv";
  a.click();
}}

populateFilters();
render();

document.getElementById("viewGridBtn").addEventListener("click", () => {{ state.view = "grid"; render(); }});
document.getElementById("viewTableBtn").addEventListener("click", () => {{ state.view = "table"; render(); }});
document.getElementById("sortSelect").addEventListener("change", e => {{
  state.sortBy = e.target.value;
  state.sortDir = state.sortBy === "title" ? "asc" : "desc";
  render();
}});
document.getElementById("sortDirBtn").addEventListener("click", () => {{
  state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
  render();
}});
document.getElementById("searchInput").addEventListener("input", e => {{ state.search = e.target.value; render(); }});
document.getElementById("genreSelect").addEventListener("change", e => {{ state.genre = e.target.value; render(); }});
document.getElementById("themeSelect").addEventListener("change", e => {{ state.theme = e.target.value; render(); }});
document.getElementById("compatSelect").addEventListener("change", e => {{ state.compat = e.target.value; render(); }});
document.getElementById("criticFilter").addEventListener("change", e => {{ state.criticFilter = e.target.value; render(); }});
document.getElementById("exportCsvBtn").addEventListener("click", exportFilteredCsv);
document.getElementById("resetBtn").addEventListener("click", () => {{
  state.search = ""; state.store = "ALL"; state.genre = ""; state.theme = ""; state.compat = ""; state.criticFilter = "";
  document.getElementById("searchInput").value = "";
  document.getElementById("genreSelect").value = "";
  document.getElementById("themeSelect").value = "";
  document.getElementById("compatSelect").value = "";
  document.getElementById("criticFilter").value = "";
  document.querySelectorAll("#storePills .pill").forEach(p => p.classList.toggle("active", p.dataset.store === "ALL"));
  render();
}});

document.querySelectorAll("thead th.sortable").forEach(th => {{
  th.addEventListener("click", () => {{
    const key = th.dataset.sort;
    if (state.sortBy === key) {{
      state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
    }} else {{
      state.sortBy = key;
      state.sortDir = key === "title" ? "asc" : "desc";
    }}
    render();
  }});
}});

function handleBadgeFilterClick(e) {{
  const gBadge = e.target.closest("[data-filter-genre]");
  if (gBadge) {{
    state.genre = gBadge.dataset.filterGenre;
    document.getElementById("genreSelect").value = state.genre;
    render();
    return;
  }}
  const tBadge = e.target.closest("[data-filter-theme]");
  if (tBadge) {{
    state.theme = tBadge.dataset.filterTheme;
    document.getElementById("themeSelect").value = state.theme;
    render();
  }}
}}
document.getElementById("tableBody").addEventListener("click", handleBadgeFilterClick);
document.getElementById("gridView").addEventListener("click", handleBadgeFilterClick);
</script>
</body>
</html>
"""
    out_path.write_text(html_doc, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Aggregate Steam & Heroic libraries with IGDB, Steam Reviews, Deck & ProtonDB metadata."
    )
    p.add_argument("--env", type=pathlib.Path, default=SCRIPT_DIR / ".env", help="Path to .env config file")
    p.add_argument("--output-dir", type=pathlib.Path, default=SCRIPT_DIR / "output", help="Directory for games.html and games.csv")
    p.add_argument("--cache", type=pathlib.Path, default=SCRIPT_DIR / "cache.db", help="Path to SQLite metadata cache")
    p.add_argument("--steam-local", action="store_true", help="Read installed Steam games locally instead of Steam Web API")
    p.add_argument("--skip-steam", action="store_true", help="Skip Steam library")
    p.add_argument("--skip-heroic", action="store_true", help="Skip Heroic library")
    p.add_argument("--no-compat", action="store_true", help="Skip Steam Deck & ProtonDB lookups")
    p.add_argument("--refresh", action="store_true", help="Force re-fetching cached metadata")
    p.add_argument("--limit", type=int, default=0, help="Limit total games processed (useful for quick testing)")
    p.add_argument("--dry-run", action="store_true", help="Discover and deduplicate libraries without calling external APIs")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dotenv = load_dotenv(args.env)

    print("1. Discovering libraries...")
    all_games: list[GameEntry] = []

    if not args.skip_heroic:
        heroic_dir = find_heroic_dir(get_cfg("HEROIC_CONFIG_DIR", dotenv))
        if heroic_dir:
            print(f"  Using Heroic config: {heroic_dir}")
            all_games.extend(load_heroic_games(heroic_dir))
        else:
            print("  [warn] Heroic config directory not found.", file=sys.stderr)

    if not args.skip_steam:
        steam_dir = find_steam_dir(get_cfg("STEAM_DIR", dotenv))
        steam_api_key = get_cfg("STEAM_API_KEY", dotenv)
        steam_id = get_cfg("STEAM_ID", dotenv) or detect_steam_id(steam_dir)

        if not args.steam_local and steam_api_key and steam_id:
            print(f"  Using Steam Web API (SteamID64: {steam_id})")
            try:
                all_games.extend(load_steam_games_api(steam_api_key, steam_id))
            except Exception as exc:
                print(f"  [warn] Steam Web API failed ({exc}); falling back to local Steam cache.", file=sys.stderr)
                if steam_dir:
                    owned = load_steam_games_local_owned(steam_dir)
                    all_games.extend(owned if owned else load_steam_games_local(steam_dir))
        else:
            if steam_dir:
                owned: list[GameEntry] = []
                if not args.steam_local:
                    try:
                        owned = load_steam_games_local_owned(steam_dir)
                    except Exception as exc:
                        print(f"  [warn] Local packageinfo/appinfo parse failed ({exc}); using installed games.", file=sys.stderr)
                all_games.extend(owned if owned else load_steam_games_local(steam_dir))
            else:
                print("  [warn] Steam directory not found.", file=sys.stderr)

    games = deduplicate_games(all_games)
    print(f"Total unique games after initial deduplication: {len(games)}")

    if args.limit > 0:
        games = games[: args.limit]
        print(f"Limiting to first {len(games)} games (--limit {args.limit}).")

    if not games:
        print("No games found. Exiting.")
        return 1

    if args.dry_run:
        print("\nDry run sample (first 15 games):")
        for g in games[:15]:
            stores = ", ".join(sorted(g.stores))
            sid = f" [Steam:{g.steam_appid}]" if g.steam_appid else ""
            print(f"  - {g.title} ({stores}){sid}")
        return 0

    cache = MetadataCache(args.cache)
    try:
        igdb_id = get_cfg("IGDB_CLIENT_ID", dotenv)
        igdb_secret = get_cfg("IGDB_CLIENT_SECRET", dotenv)

        print("\n2. Enriching metadata...")
        if igdb_id and igdb_secret:
            igdb = IGDBClient(igdb_id, igdb_secret)
            enrich_igdb(games, igdb, cache, refresh=args.refresh)
            before = len(games)
            games = deduplicate_after_enrichment(games)
            if len(games) < before:
                print(f"  Merged {before - len(games)} cross-store duplicates matched via IGDB.")
        else:
            print(
                "  [warn] IGDB_CLIENT_ID / IGDB_CLIENT_SECRET not set in .env — skipping IGDB\n"
                "         (Critic Scores, Genres, Themes, and Heroic->Steam matching need IGDB keys).\n"
                "         See README.md for 2-minute free Twitch/IGDB setup."
            )

        enrich_steam_and_compat(
            games,
            cache,
            refresh=args.refresh,
            include_compat=not args.no_compat,
        )
    finally:
        cache.close()

    print("\n3. Writing outputs...")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    html_path = args.output_dir / "games.html"
    csv_path = args.output_dir / "games.csv"
    write_html(games, html_path)
    write_csv(games, csv_path)

    print(f"\nDone! Generated:")
    print(f"  HTML dashboard: {html_path}")
    print(f"  CSV export:     {csv_path}")
    print(f"  Metadata cache: {args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
