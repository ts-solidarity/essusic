from __future__ import annotations

import random
from collections import deque
from enum import Enum, auto

from .audio_source import TrackInfo


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


class QueueManager:
    """Holds per-guild queues."""

    def __init__(self) -> None:
        self._guilds: dict[int, GuildQueue] = {}

    def get(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._guilds:
            self._guilds[guild_id] = GuildQueue()
        return self._guilds[guild_id]

    def remove(self, guild_id: int) -> None:
        self._guilds.pop(guild_id, None)
