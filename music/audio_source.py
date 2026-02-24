from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord
import yt_dlp

log = logging.getLogger(__name__)

YTDL_OPTIONS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cookiefile": "/data/cookies.txt",
    "js_runtimes": {"node": {}},
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -ar 48000",
}


@dataclass
class TrackInfo:
    """Lightweight metadata stored in the queue — resolved to a source just-in-time."""

    title: str
    url: str  # original URL or search query (e.g. "ytsearch:Artist - Title")
    duration: int = 0  # seconds
    thumbnail: str = ""
    requester: str = ""


class YTDLSource(discord.PCMVolumeTransformer):
    """Wraps FFmpegPCMAudio with volume control and metadata."""

    def __init__(
        self, source: discord.AudioSource, *, data: dict, volume: float = 0.5
    ) -> None:
        super().__init__(source, volume)
        self.title: str = data.get("title", "Unknown")
        self.url: str = data.get("webpage_url", "")
        self.duration: int = int(data.get("duration", 0) or 0)
        self.thumbnail: str = data.get("thumbnail", "")

    @classmethod
    async def from_query(
        cls, query: str, *, loop: asyncio.AbstractEventLoop, volume: float = 0.5
    ) -> YTDLSource:
        """Create a playable source from a URL or search query."""
        ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(query, download=False)
        )

        if "entries" in data:
            data = data["entries"][0]

        stream_url = data["url"]
        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        return cls(source, data=data, volume=volume)

    @staticmethod
    async def search(
        query: str, *, loop: asyncio.AbstractEventLoop, limit: int = 5
    ) -> list[TrackInfo]:
        """Search YouTube and return lightweight TrackInfo results."""
        opts = {**YTDL_OPTIONS, "noplaylist": True, "extract_flat": "in_playlist"}
        ytdl = yt_dlp.YoutubeDL(opts)

        search_query = f"ytsearch{limit * 2}:{query}"
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search_query, download=False)
        )

        results: list[TrackInfo] = []
        for entry in data.get("entries", []) or []:
            if entry is None:
                continue
            url = entry.get("webpage_url") or entry.get("url", "")
            if "watch?v=" not in url and "youtu.be/" not in url:
                continue
            results.append(
                TrackInfo(
                    title=entry.get("title", "Unknown"),
                    url=url,
                    duration=int(entry.get("duration", 0) or 0),
                    thumbnail=entry.get("thumbnail", ""),
                )
            )
            if len(results) >= limit:
                break
        return results
