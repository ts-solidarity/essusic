from __future__ import annotations

import json
import logging
import random
from collections import deque
from enum import Enum, auto
from pathlib import Path

from .audio_source import TrackInfo

log = logging.getLogger(__name__)


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

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.previous = None
        self.loop_mode = LoopMode.OFF


_SETTINGS_KEYS = ("volume", "search_mode", "max_queue", "autoplay", "filter_name", "dj_role_id", "stay_connected", "speed", "normalize", "loop_mode")


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
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(json.dumps(self._settings, indent=2))
        except Exception as exc:
            log.warning("Failed to save settings: %s", exc)

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
        try:
            self._queue_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._queue_state_path.write_text(json.dumps(self._queue_state, indent=2))
        except Exception as exc:
            log.warning("Failed to save queue state: %s", exc)

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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            log.warning("Failed to save history: %s", exc)

    def record(self, guild_id: int, track: TrackInfo) -> None:
        import time

        key = str(guild_id)
        entries = self._data.setdefault(key, [])
        entries.append({"title": track.title, "url": track.url, "ts": time.time()})
        if len(entries) > 500:
            self._data[key] = entries[-500:]
        self._save()

    def top(self, guild_id: int, limit: int = 10) -> list[tuple[str, str, int]]:
        """Return top tracks as (title, url, count) sorted by play count."""
        from collections import Counter

        entries = self._data.get(str(guild_id), [])
        counts: Counter[str] = Counter()
        url_map: dict[str, str] = {}
        for e in entries:
            title = e["title"]
            counts[title] += 1
            url_map[title] = e.get("url", "")
        return [(t, url_map[t], c) for t, c in counts.most_common(limit)]


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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            log.warning("Failed to save favorites: %s", exc)

    def add(self, user_id: int, track: TrackInfo) -> bool:
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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            log.warning("Failed to save playlists: %s", exc)

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
        guild_pls[name_key] = {
            "name": name,
            "tracks": track_list,
            "created_by": created_by,
            "created_at": _time.time(),
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
