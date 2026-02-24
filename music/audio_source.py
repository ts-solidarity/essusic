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

AUDIO_FILTERS: dict[str, str] = {
    "bassboost": "bass=g=10,acompressor=threshold=0.5",
    "nightcore": "aresample=48000,asetrate=48000*1.25",
    "vaporwave": "aresample=48000,asetrate=48000*0.8",
    "8d": "apulsator=hz=0.08",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
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
        self,
        source: discord.AudioSource,
        *,
        data: dict,
        volume: float = 0.5,
        stream_url: str = "",
    ) -> None:
        super().__init__(source, volume)
        self.title: str = data.get("title", "Unknown")
        self.url: str = data.get("webpage_url", "")
        self.duration: int = int(data.get("duration", 0) or 0)
        self.thumbnail: str = data.get("thumbnail", "")
        self.stream_url: str = stream_url
        self._data: dict = data

    @classmethod
    async def from_query(
        cls,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop,
        volume: float = 0.5,
        filter_name: str | None = None,
        seek_seconds: int = 0,
        speed: float = 1.0,
        normalize: bool = False,
    ) -> YTDLSource:
        """Create a playable source from a URL or search query."""
        ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(query, download=False)
        )

        if "entries" in data:
            data = data["entries"][0]

        url = data["url"]
        return cls._build(url, data=data, volume=volume,
                          filter_name=filter_name, seek_seconds=seek_seconds,
                          speed=speed, normalize=normalize)

    @classmethod
    def from_stream_url(
        cls,
        stream_url: str,
        *,
        data: dict,
        volume: float = 0.5,
        filter_name: str | None = None,
        seek_seconds: int = 0,
        speed: float = 1.0,
        normalize: bool = False,
    ) -> YTDLSource:
        """Rebuild an FFmpeg source from a cached stream URL (no yt-dlp fetch)."""
        return cls._build(stream_url, data=data, volume=volume,
                          filter_name=filter_name, seek_seconds=seek_seconds,
                          speed=speed, normalize=normalize)

    @classmethod
    def _build(
        cls,
        stream_url: str,
        *,
        data: dict,
        volume: float,
        filter_name: str | None,
        seek_seconds: int,
        speed: float = 1.0,
        normalize: bool = False,
    ) -> YTDLSource:
        before = FFMPEG_OPTIONS["before_options"]
        opts = FFMPEG_OPTIONS["options"]

        if seek_seconds > 0:
            before = f"-ss {seek_seconds} " + before

        af_parts: list[str] = []
        if filter_name and filter_name in AUDIO_FILTERS:
            af_parts.append(AUDIO_FILTERS[filter_name])
        if speed != 1.0:
            af_parts.append(f"atempo={speed}")
        if normalize:
            af_parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        if af_parts:
            opts = opts + " -af " + ",".join(af_parts)

        source = discord.FFmpegPCMAudio(
            stream_url, before_options=before, options=opts
        )
        return cls(source, data=data, volume=volume, stream_url=stream_url)

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
