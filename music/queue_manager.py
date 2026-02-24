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

    def add(self, track: TrackInfo) -> int | None:
        """Add a track and return its position (1-indexed), or None if queue is full."""
        if len(self.queue) >= self.max_queue:
            return None
        self.queue.append(track)
        return len(self.queue)

    def next_track(self) -> TrackInfo | None:
        """Advance the queue respecting loop mode. Returns the next TrackInfo or None."""
        if self.loop_mode == LoopMode.SINGLE and self.current is not None:
            return self.current

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
        self.loop_mode = LoopMode.OFF


_SETTINGS_KEYS = ("volume", "search_mode", "max_queue", "autoplay")


class QueueManager:
    """Holds per-guild queues."""

    def __init__(self, settings_path: str = "/data/settings.json") -> None:
        self._guilds: dict[int, GuildQueue] = {}
        self._settings_path = Path(settings_path)
        self._settings: dict[str, dict] = {}
        self._load_settings()

    def _load_settings(self) -> None:
        if self._settings_path.exists():
            try:
                self._settings = json.loads(self._settings_path.read_text())
            except Exception as exc:
                log.warning("Failed to load settings: %s", exc)

    def save_settings(self) -> None:
        for guild_id, gq in self._guilds.items():
            self._settings[str(guild_id)] = {
                k: getattr(gq, k) for k in _SETTINGS_KEYS
            }
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
                        setattr(gq, k, saved[k])
            self._guilds[guild_id] = gq
        return self._guilds[guild_id]

    def remove(self, guild_id: int) -> None:
        self._guilds.pop(guild_id, None)
