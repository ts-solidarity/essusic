from __future__ import annotations

import asyncio
import logging
import struct
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
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1"
        " -reconnect_on_http_error 5xx -reconnect_delay_max 5"
    ),
    "options": "-vn -ar 48000 -bufsize 64k",
}

AUDIO_FILTERS: dict[str, str] = {
    "bassboost": "bass=g=10,acompressor=threshold=0.5",
    "nightcore": "aresample=48000,asetrate=48000*1.25",
    "vaporwave": "aresample=48000,asetrate=48000*0.8",
    "8d": "apulsator=hz=0.08",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
}

# ── Equalizer ────────────────────────────────────────────────────────────

EQ_BANDS = [
    ("31Hz", 31), ("63Hz", 63), ("125Hz", 125), ("250Hz", 250),
    ("500Hz", 500), ("1kHz", 1000), ("2kHz", 2000), ("4kHz", 4000),
    ("8kHz", 8000), ("16kHz", 16000),
]

EQ_PRESETS: dict[str, list[float]] = {
    "flat": [0.0] * 10,
    "bass_heavy": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "treble_heavy": [0, 0, 0, 0, 0, 0, 2, 4, 5, 6],
    "vocal": [-2, -1, 0, 2, 4, 4, 2, 0, -1, -2],
    "electronic": [4, 3, 0, -2, -1, 0, 2, 3, 4, 5],
}


def build_eq_filter(bands: list[float]) -> str:
    parts = []
    for i, (_, freq) in enumerate(EQ_BANDS):
        gain = max(-12.0, min(12.0, bands[i]))
        if gain != 0.0:
            parts.append(f"equalizer=f={freq}:t=o:w=1:g={gain}")
    return ",".join(parts) if parts else ""


@dataclass
class TrackInfo:
    """Lightweight metadata stored in the queue — resolved to a source just-in-time."""

    title: str
    url: str  # original URL or search query (e.g. "ytsearch:Artist - Title")
    duration: int = 0  # seconds
    thumbnail: str = ""
    requester: str = ""
    is_live: bool = False
    artist: str = ""
    requester_id: int = 0


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
        eq_bands: list[float] | None = None,
        is_live: bool = False,
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
                          speed=speed, normalize=normalize,
                          eq_bands=eq_bands, is_live=is_live)

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
        eq_bands: list[float] | None = None,
        is_live: bool = False,
    ) -> YTDLSource:
        """Rebuild an FFmpeg source from a cached stream URL (no yt-dlp fetch)."""
        return cls._build(stream_url, data=data, volume=volume,
                          filter_name=filter_name, seek_seconds=seek_seconds,
                          speed=speed, normalize=normalize,
                          eq_bands=eq_bands, is_live=is_live)

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
        eq_bands: list[float] | None = None,
        is_live: bool = False,
    ) -> YTDLSource:
        before = FFMPEG_OPTIONS["before_options"]
        opts = FFMPEG_OPTIONS["options"]

        if is_live:
            before = (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1"
                " -reconnect_on_http_error 5xx -reconnect_delay_max 10"
            )
            seek_seconds = 0
        elif seek_seconds > 0:
            before = f"-ss {seek_seconds} " + before

        af_parts: list[str] = []
        if filter_name and filter_name in AUDIO_FILTERS:
            af_parts.append(AUDIO_FILTERS[filter_name])
        if speed != 1.0:
            af_parts.append(f"atempo={speed}")
        if normalize:
            af_parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        if eq_bands and any(g != 0.0 for g in eq_bands):
            eq_str = build_eq_filter(eq_bands)
            if eq_str:
                af_parts.append(eq_str)
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


class CrossfadeSource(discord.AudioSource):
    """Mixes two PCM sources with a linear crossfade.

    Discord sends 20ms frames at 48kHz stereo 16-bit = 3840 bytes/frame.
    50 frames/sec, so 5s crossfade = 250 frames.
    """

    FRAME_SIZE = 3840  # bytes per 20ms frame
    SAMPLES_PER_FRAME = FRAME_SIZE // 2  # 16-bit samples

    def __init__(
        self,
        outgoing: discord.AudioSource,
        incoming: discord.AudioSource,
        crossfade_seconds: int = 5,
    ) -> None:
        self.outgoing = outgoing
        self.incoming = incoming
        self.total_frames = crossfade_seconds * 50
        self.frame_count = 0
        self.finished = False

    def read(self) -> bytes:
        if self.finished:
            return self.incoming.read()

        out_data = self.outgoing.read()
        in_data = self.incoming.read()

        if not out_data and not in_data:
            return b""
        if not out_data:
            self.finished = True
            return in_data or b""
        if not in_data:
            self.finished = True
            return b""

        self.frame_count += 1
        progress = min(self.frame_count / max(self.total_frames, 1), 1.0)

        if progress >= 1.0:
            self.finished = True
            return in_data

        out_gain = 1.0 - progress
        in_gain = progress

        # Pad to equal length
        max_len = max(len(out_data), len(in_data))
        out_data = out_data.ljust(max_len, b"\x00")
        in_data = in_data.ljust(max_len, b"\x00")

        num_samples = max_len // 2
        out_samples = struct.unpack(f"<{num_samples}h", out_data[:num_samples * 2])
        in_samples = struct.unpack(f"<{num_samples}h", in_data[:num_samples * 2])

        mixed = []
        for o, i in zip(out_samples, in_samples):
            val = int(o * out_gain + i * in_gain)
            val = max(-32768, min(32767, val))
            mixed.append(val)

        return struct.pack(f"<{num_samples}h", *mixed)

    def cleanup(self) -> None:
        if hasattr(self.outgoing, "cleanup"):
            self.outgoing.cleanup()
        if hasattr(self.incoming, "cleanup"):
            self.incoming.cleanup()

    def is_opus(self) -> bool:
        return False
