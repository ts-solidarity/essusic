from __future__ import annotations

import json
import logging
import os
import random
import re
from collections import Counter, deque
from enum import Enum, auto
from pathlib import Path

from .audio_source import TrackInfo

log = logging.getLogger(__name__)

_ARTIST_SEP_RE = re.compile(r"\s+[-–—|]\s+")


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically using a temp file + rename to avoid corruption on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("Failed to save %s: %s", path, exc)
        tmp.unlink(missing_ok=True)


def _extract_artist(title: str) -> str:
    """Extract artist from 'Artist - Title' style strings."""
    parts = _ARTIST_SEP_RE.split(title, maxsplit=1)
    return parts[0].strip().lower() if len(parts) > 1 else ""


class LoopMode(Enum):
    OFF = auto()
    SINGLE = auto()
    QUEUE = auto()

    def next(self) -> LoopMode:
        order = [LoopMode.OFF, LoopMode.SINGLE, LoopMode.QUEUE]
        idx = order.index(self)
        return order[(idx + 1) % len(order)]

    def label(self) -> str:
        return {
            LoopMode.OFF: "off",
            LoopMode.SINGLE: "single track",
            LoopMode.QUEUE: "whole queue",
        }[self]


class GuildQueue:
    """Per-guild playback state."""

    def __init__(self) -> None:
        self.queue: deque[TrackInfo] = deque()
        self.current: TrackInfo | None = None
        self.loop_mode: LoopMode = LoopMode.OFF
        self.volume: float = 0.5
        self.search_mode: str = "youtube"
        self.max_queue: int = 50
        self.play_start_time: float = 0.0
        self.autoplay: bool = False
        self.filter_name: str | None = None
        self.previous: TrackInfo | None = None
        self.dj_role_id: int | None = None
        self.stay_connected: bool = False
        self.speed: float = 1.0
        self.normalize: bool = False
        self.text_channel_id: int | None = None
        self._restarting: bool = False
        self.skip_votes: set[int] = set()

        # EQ
        self.eq_bands: list[float] = [0.0] * 10

        # Radio mode
        self.radio_mode: bool = False
        self.radio_seed: str | None = None
        self.radio_history: set[str] = set()

        # DJ queue mode
        self.dj_queue_mode: bool = False
        self.pending_requests: deque[TrackInfo] = deque()

        # Crossfade
        self.crossfade_seconds: int = 0

        # Undo stack (in-memory only)
        self._undo_stack: list[tuple[list[TrackInfo], str]] = []

        # Locale
        self.locale: str = "en"

        # Now-playing channel (persisted: np_channel_id; runtime: np_message_id)
        self.np_channel_id: int | None = None
        self.np_message_id: int | None = None  # not persisted

        # Per-user queue limit (0 = unlimited)
        self.max_per_user: int = 0

    def add(self, track: TrackInfo) -> int | None:
        """Add a track and return its position (1-indexed), or None if queue is full."""
        if len(self.queue) >= self.max_queue:
            return None
        self.queue.append(track)
        return len(self.queue)

    def next_track(self) -> TrackInfo | None:
        """Advance the queue respecting loop mode. Returns the next TrackInfo or None."""
        self.skip_votes.clear()
        if self.loop_mode == LoopMode.SINGLE and self.current is not None:
            return self.current

        self.previous = self.current

        if self.loop_mode == LoopMode.QUEUE and self.current is not None:
            self.queue.append(self.current)

        if not self.queue:
            self.current = None
            return None

        self.current = self.queue.popleft()
        return self.current

    def remove_at(self, index: int) -> TrackInfo | None:
        """Remove and return the track at 0-based index, or None if out of range."""
        if index < 0 or index >= len(self.queue):
            return None
        items = list(self.queue)
        removed = items.pop(index)
        self.queue = deque(items)
        return removed

    def move(self, from_idx: int, to_idx: int) -> TrackInfo | None:
        """Move track from from_idx to to_idx (both 0-based). Returns the moved track or None."""
        if from_idx < 0 or from_idx >= len(self.queue):
            return None
        items = list(self.queue)
        track = items.pop(from_idx)
        to_idx = max(0, min(to_idx, len(items)))
        items.insert(to_idx, track)
        self.queue = deque(items)
        return track

    def skip_to(self, index: int) -> TrackInfo | None:
        """Drop all tracks before 0-based index and return the track at that position."""
        if index < 0 or index >= len(self.queue):
            return None
        items = list(self.queue)
        self.queue = deque(items[index:])
        return self.queue[0]

    def shuffle(self) -> None:
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    def smart_shuffle(self) -> None:
        """Shuffle the queue avoiding back-to-back tracks from the same artist."""
        items = list(self.queue)
        if len(items) < 2:
            return

        # Group by artist
        groups: dict[str, list[TrackInfo]] = {}
        for track in items:
            artist = track.artist or _extract_artist(track.title) or f"__unknown_{id(track)}"
            groups.setdefault(artist, []).append(track)

        # Shuffle within each group
        for g in groups.values():
            random.shuffle(g)

        # Interleave: always pick from the largest group that differs from the last
        result: list[TrackInfo] = []
        last_artist = ""
        remaining = {k: deque(v) for k, v in groups.items()}

        while remaining:
            # Find candidate groups (different from last artist)
            candidates = {k: v for k, v in remaining.items() if k != last_artist and v}
            if not candidates:
                # Forced to pick from same artist (only one group left)
                candidates = {k: v for k, v in remaining.items() if v}
            if not candidates:
                break

            # Pick from largest group
            best_key = max(candidates, key=lambda k: len(candidates[k]))
            track = remaining[best_key].popleft()
            result.append(track)
            last_artist = best_key

            if not remaining[best_key]:
                del remaining[best_key]

        self.queue = deque(result)

    def has_duplicate(self, track: TrackInfo) -> bool:
        """Check if a track URL is already in the queue or currently playing."""
        if self.current and self.current.url == track.url:
            return True
        return any(t.url == track.url for t in self.queue)

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.previous = None
        self.loop_mode = LoopMode.OFF
        self.radio_mode = False
        self.radio_seed = None
        self.radio_history.clear()
        self.skip_votes.clear()
        self.pending_requests.clear()
        self.play_start_time = 0.0
        self._restarting = False
        self._undo_stack.clear()

    # ── Undo ──────────────────────────────────────────────────────────────

    def snapshot(self, description: str) -> None:
        """Save a snapshot of the current queue for undo."""
        self._undo_stack.append((list(self.queue), description))
        if len(self._undo_stack) > 10:
            self._undo_stack.pop(0)

    def undo(self) -> str | None:
        """Restore the last queue snapshot. Returns description or None."""
        if not self._undo_stack:
            return None
        items, description = self._undo_stack.pop()
        self.queue = deque(items)
        return description


_SETTINGS_KEYS = (
    "volume", "search_mode", "max_queue", "autoplay", "filter_name",
    "dj_role_id", "stay_connected", "speed", "normalize", "loop_mode",
    "eq_bands", "crossfade_seconds", "locale", "np_channel_id", "max_per_user",
)


class QueueManager:
    """Holds per-guild queues."""

    def __init__(self, settings_path: str = "/data/settings.json") -> None:
        self._guilds: dict[int, GuildQueue] = {}
        self._settings_path = Path(settings_path)
        self._settings: dict[str, dict] = {}
        self._load_settings()
        self._queue_state_path = Path("/data/queue_state.json")
        self._queue_state: dict[str, dict] = {}
        if self._queue_state_path.exists():
            try:
                self._queue_state = json.loads(self._queue_state_path.read_text())
            except Exception as exc:
                log.warning("Failed to load queue state: %s", exc)

    def _load_settings(self) -> None:
        if self._settings_path.exists():
            try:
                self._settings = json.loads(self._settings_path.read_text())
            except Exception as exc:
                log.warning("Failed to load settings: %s", exc)

    def save_settings(self) -> None:
        for guild_id, gq in self._guilds.items():
            data = {k: getattr(gq, k) for k in _SETTINGS_KEYS}
            data["loop_mode"] = gq.loop_mode.name
            self._settings[str(guild_id)] = data
        _atomic_write(self._settings_path, self._settings)

    def get(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._guilds:
            gq = GuildQueue()
            saved = self._settings.get(str(guild_id))
            if saved:
                for k in _SETTINGS_KEYS:
                    if k in saved:
                        if k == "loop_mode":
                            try:
                                gq.loop_mode = LoopMode[saved[k]]
                            except KeyError:
                                pass
                        elif k == "eq_bands":
                            bands = saved[k]
                            if isinstance(bands, list):
                                # Ensure exactly 10 elements regardless of stored length
                                bands = (bands + [0.0] * 10)[:10]
                                gq.eq_bands = [float(b) for b in bands]
                        else:
                            setattr(gq, k, saved[k])
            self._restore_queue_state(guild_id, gq)
            self._guilds[guild_id] = gq
        return self._guilds[guild_id]

    def save_queue_state(self, guild_id: int) -> None:
        """Persist current track + queue to disk for crash recovery."""
        gq = self._guilds.get(guild_id)
        if gq is None:
            return
        key = str(guild_id)

        def _track_dict(t: TrackInfo) -> dict:
            return {"title": t.title, "url": t.url, "duration": t.duration,
                    "thumbnail": t.thumbnail, "requester": t.requester}

        state: dict = {"queue": [_track_dict(t) for t in gq.queue],
                       "loop_mode": gq.loop_mode.name}
        if gq.current:
            state["current"] = _track_dict(gq.current)
            import time
            elapsed = int((time.time() - gq.play_start_time) * gq.speed) if gq.play_start_time else 0
            state["elapsed"] = elapsed
        self._queue_state[key] = state
        self._write_queue_state()

    def clear_queue_state(self, guild_id: int) -> None:
        """Remove saved queue state (called on /stop and auto-disconnect)."""
        key = str(guild_id)
        if key in self._queue_state:
            del self._queue_state[key]
            self._write_queue_state()

    def _write_queue_state(self) -> None:
        _atomic_write(self._queue_state_path, self._queue_state)

    def _restore_queue_state(self, guild_id: int, gq: GuildQueue) -> None:
        """Restore saved queue into a freshly created GuildQueue."""
        saved = self._queue_state.get(str(guild_id))
        if not saved:
            return
        if "current" in saved:
            d = saved["current"]
            gq.queue.appendleft(TrackInfo(
                title=d["title"], url=d["url"], duration=d.get("duration", 0),
                thumbnail=d.get("thumbnail", ""), requester=d.get("requester", ""),
            ))
        for d in saved.get("queue", []):
            gq.queue.append(TrackInfo(
                title=d["title"], url=d["url"], duration=d.get("duration", 0),
                thumbnail=d.get("thumbnail", ""), requester=d.get("requester", ""),
            ))
        if "loop_mode" in saved:
            try:
                gq.loop_mode = LoopMode[saved["loop_mode"]]
            except KeyError:
                pass

    def remove(self, guild_id: int) -> None:
        self._guilds.pop(guild_id, None)


class HistoryManager:
    """Tracks play history per guild, capped at 500 entries."""

    def __init__(self, path: str = "/data/history.json") -> None:
        self._path = Path(path)
        self._data: dict[str, list[dict]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as exc:
                log.warning("Failed to load history: %s", exc)

    def _save(self) -> None:
        _atomic_write(self._path, self._data)

    def record(
        self,
        guild_id: int,
        track: TrackInfo,
        requester_id: int = 0,
        duration: int = 0,
    ) -> None:
        import time

        key = str(guild_id)
        entries = self._data.setdefault(key, [])
        entry: dict = {"title": track.title, "url": track.url, "ts": time.time()}
        if requester_id:
            entry["user"] = requester_id
        if duration:
            entry["dur"] = duration
        entries.append(entry)
        if len(entries) > 500:
            self._data[key] = entries[-500:]
        self._save()

    def top(self, guild_id: int, limit: int = 10) -> list[tuple[str, str, int]]:
        """Return top tracks as (title, url, count) sorted by play count."""
        entries = self._data.get(str(guild_id), [])
        counts: Counter[str] = Counter()
        url_map: dict[str, str] = {}
        for e in entries:
            title = e["title"]
            counts[title] += 1
            url_map[title] = e.get("url", "")
        return [(t, url_map[t], c) for t, c in counts.most_common(limit)]

    def user_stats(self, guild_id: int, user_id: int) -> dict:
        """Return stats for a specific user in a guild."""
        entries = self._data.get(str(guild_id), [])
        user_entries = [e for e in entries if e.get("user") == user_id]
        total_plays = len(user_entries)
        total_time = sum(e.get("dur", 0) for e in user_entries)

        track_counts: Counter[str] = Counter()
        for e in user_entries:
            track_counts[e["title"]] += 1

        top_tracks = track_counts.most_common(10)
        return {
            "total_plays": total_plays,
            "total_time_seconds": total_time,
            "top_tracks": top_tracks,
        }

    def server_stats(self, guild_id: int) -> dict:
        """Return aggregate stats for a guild."""
        entries = self._data.get(str(guild_id), [])
        total_plays = len(entries)
        total_time = sum(e.get("dur", 0) for e in entries)

        track_counts: Counter[str] = Counter()
        user_counts: Counter[int] = Counter()
        unique_urls: set[str] = set()
        for e in entries:
            track_counts[e["title"]] += 1
            unique_urls.add(e.get("url", ""))
            uid = e.get("user")
            if uid:
                user_counts[uid] += 1

        return {
            "total_plays": total_plays,
            "total_time_seconds": total_time,
            "unique_tracks": len(unique_urls),
            "top_tracks": track_counts.most_common(10),
            "top_users": user_counts.most_common(10),
        }


class FavoritesManager:
    """Per-user favorites, max 50 per user."""

    def __init__(self, path: str = "/data/favorites.json") -> None:
        self._path = Path(path)
        self._data: dict[str, list[dict]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as exc:
                log.warning("Failed to load favorites: %s", exc)

    def _save(self) -> None:
        _atomic_write(self._path, self._data)

    def add(self, user_id: int, track: TrackInfo, guild_id: int = 0) -> bool:
        """Add a track. Returns False if already at max or duplicate."""
        key = str(user_id)
        favs = self._data.setdefault(key, [])
        if len(favs) >= 50:
            return False
        if any(f["url"] == track.url for f in favs):
            return False
        favs.append({
            "title": track.title,
            "url": track.url,
            "duration": track.duration,
            "thumbnail": track.thumbnail,
            "guild_id": guild_id,
        })
        self._save()
        return True

    def remove(self, user_id: int, index: int) -> dict | None:
        """Remove by 0-based index. Returns the removed entry or None."""
        key = str(user_id)
        favs = self._data.get(key, [])
        if index < 0 or index >= len(favs):
            return None
        removed = favs.pop(index)
        self._save()
        return removed

    def list(self, user_id: int) -> list[dict]:
        return self._data.get(str(user_id), [])

    def list_for_guild(self, user_id: int, guild_id: int) -> list[dict]:
        """Return only favorites saved from a specific guild."""
        return [f for f in self.list(user_id) if f.get("guild_id") == guild_id]

    def as_tracks(self, user_id: int, requester: str = "") -> list[TrackInfo]:
        return [
            TrackInfo(
                title=f["title"],
                url=f["url"],
                duration=f.get("duration", 0),
                thumbnail=f.get("thumbnail", ""),
                requester=requester,
            )
            for f in self.list(user_id)
        ]

    def as_tracks_for_guild(
        self, user_id: int, guild_id: int, requester: str = ""
    ) -> list[TrackInfo]:
        """Return favorites from a specific guild as playable TrackInfo objects."""
        return [
            TrackInfo(
                title=f["title"],
                url=f["url"],
                duration=f.get("duration", 0),
                thumbnail=f.get("thumbnail", ""),
                requester=requester,
            )
            for f in self.list_for_guild(user_id, guild_id)
        ]


class PlaylistManager:
    """Per-guild saved playlists, max 25 per guild, 200 tracks per playlist."""

    MAX_PLAYLISTS = 25
    MAX_TRACKS = 200

    def __init__(self, path: str = "/data/playlists.json") -> None:
        self._path = Path(path)
        self._data: dict[str, dict[str, dict]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as exc:
                log.warning("Failed to load playlists: %s", exc)

    def _write(self) -> None:
        _atomic_write(self._path, self._data)

    def _get_playlist(self, guild_id: int, name: str) -> dict | None:
        guild_pls = self._data.get(str(guild_id), {})
        return guild_pls.get(name.lower())

    def save(
        self, guild_id: int, name: str, tracks: list[TrackInfo], created_by: str
    ) -> str | None:
        """Save a playlist. Returns error message or None on success."""
        import time as _time

        key = str(guild_id)
        guild_pls = self._data.setdefault(key, {})
        name_key = name.lower()
        if name_key not in guild_pls and len(guild_pls) >= self.MAX_PLAYLISTS:
            return f"Server already has {self.MAX_PLAYLISTS} playlists."
        track_list = [
            {"title": t.title, "url": t.url, "duration": t.duration,
             "thumbnail": t.thumbnail}
            for t in tracks[:self.MAX_TRACKS]
        ]
        existing = guild_pls.get(name_key, {})
        guild_pls[name_key] = {
            "name": name,
            "tracks": track_list,
            "created_by": created_by,
            "created_at": _time.time(),
            "collaborators": existing.get("collaborators", []),
        }
        self._write()
        return None

    def load(self, guild_id: int, name: str) -> list[TrackInfo] | None:
        """Load a playlist by name. Returns None if not found."""
        guild_pls = self._data.get(str(guild_id), {})
        entry = guild_pls.get(name.lower())
        if entry is None:
            return None
        return [
            TrackInfo(
                title=t["title"], url=t["url"],
                duration=t.get("duration", 0),
                thumbnail=t.get("thumbnail", ""),
            )
            for t in entry["tracks"]
        ]

    def delete(self, guild_id: int, name: str) -> bool:
        """Delete a playlist. Returns True if found and deleted."""
        guild_pls = self._data.get(str(guild_id), {})
        if name.lower() not in guild_pls:
            return False
        del guild_pls[name.lower()]
        self._write()
        return True

    def list_all(self, guild_id: int) -> list[dict]:
        """Return all playlists for a guild as a list of metadata dicts."""
        guild_pls = self._data.get(str(guild_id), {})
        return list(guild_pls.values())

    def names(self, guild_id: int) -> list[str]:
        """Return all playlist names for autocomplete."""
        guild_pls = self._data.get(str(guild_id), {})
        return [v["name"] for v in guild_pls.values()]

    # ── Collaborative playlist methods ────────────────────────────────────

    def get_creator(self, guild_id: int, name: str) -> str | None:
        """Return the creator of a playlist, or None if not found."""
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return None
        return entry.get("created_by")

    def get_collaborators(self, guild_id: int, name: str) -> list[int]:
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return []
        return entry.get("collaborators", [])

    def is_collaborator(self, guild_id: int, name: str, user_id: int) -> bool:
        return user_id in self.get_collaborators(guild_id, name)

    def add_collaborator(self, guild_id: int, name: str, user_id: int) -> bool:
        """Add a collaborator. Returns False if not found or already added."""
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return False
        collabs = entry.setdefault("collaborators", [])
        if user_id in collabs:
            return False
        collabs.append(user_id)
        self._write()
        return True

    def remove_collaborator(self, guild_id: int, name: str, user_id: int) -> bool:
        """Remove a collaborator. Returns False if not found."""
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return False
        collabs = entry.get("collaborators", [])
        if user_id not in collabs:
            return False
        collabs.remove(user_id)
        self._write()
        return True

    def add_track_to_playlist(
        self, guild_id: int, name: str, track: TrackInfo
    ) -> str | None:
        """Add a track to a playlist. Returns error string or None on success."""
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return "Playlist not found."
        tracks = entry.get("tracks", [])
        if len(tracks) >= self.MAX_TRACKS:
            return f"Playlist is full ({self.MAX_TRACKS} tracks max)."
        tracks.append({
            "title": track.title, "url": track.url,
            "duration": track.duration, "thumbnail": track.thumbnail,
        })
        self._write()
        return None

    def remove_track_from_playlist(
        self, guild_id: int, name: str, index: int
    ) -> dict | None:
        """Remove a track by 0-based index. Returns removed track dict or None."""
        entry = self._get_playlist(guild_id, name)
        if entry is None:
            return None
        tracks = entry.get("tracks", [])
        if index < 0 or index >= len(tracks):
            return None
        removed = tracks.pop(index)
        self._write()
        return removed


class RatingsManager:
    """Per-guild track ratings with up/down votes."""

    def __init__(self, path: str = "/data/ratings.json") -> None:
        self._path = Path(path)
        self._data: dict[str, dict[str, dict]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as exc:
                log.warning("Failed to load ratings: %s", exc)

    def _save(self) -> None:
        _atomic_write(self._path, self._data)

    def vote(
        self, guild_id: int, track_url: str, title: str,
        user_id: int, direction: str,
    ) -> tuple[int, int]:
        """Toggle a vote. direction is 'up' or 'down'. Returns (up_count, down_count)."""
        key = str(guild_id)
        guild_data = self._data.setdefault(key, {})
        entry = guild_data.setdefault(track_url, {
            "title": title, "up": 0, "down": 0, "voters": {},
        })
        voters = entry.setdefault("voters", {})
        uid = str(user_id)

        prev = voters.get(uid)
        if prev == direction:
            # Toggle off
            entry[direction] = max(0, entry[direction] - 1)
            del voters[uid]
        else:
            # Remove old vote if switching
            if prev:
                entry[prev] = max(0, entry[prev] - 1)
            entry[direction] += 1
            voters[uid] = direction

        self._save()
        return entry["up"], entry["down"]

    def get_rating(self, guild_id: int, track_url: str) -> tuple[int, int]:
        entry = self._data.get(str(guild_id), {}).get(track_url)
        if entry is None:
            return 0, 0
        return entry.get("up", 0), entry.get("down", 0)

    def top_rated(self, guild_id: int, limit: int = 10) -> list[tuple[str, str, int, int]]:
        """Return top rated tracks as (title, url, up, down) sorted by net score."""
        guild_data = self._data.get(str(guild_id), {})
        items = [
            (v["title"], url, v.get("up", 0), v.get("down", 0))
            for url, v in guild_data.items()
        ]
        items.sort(key=lambda x: x[2] - x[3], reverse=True)
        return items[:limit]
