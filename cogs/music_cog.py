from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from music.audio_source import AUDIO_FILTERS, TrackInfo, YTDLSource
from music.queue_manager import (
    FavoritesManager,
    HistoryManager,
    QueueManager,
)
from music.spotify_resolver import SpotifyResolver
from music.url_parser import InputType, classify

log = logging.getLogger(__name__)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "LIVE"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_bar(elapsed: int, total: int, length: int = 12) -> str:
    if total <= 0:
        return f"{format_duration(elapsed)} / LIVE"
    elapsed = min(elapsed, total)
    filled = round(length * elapsed / total)
    bar = "▬" * filled + "🔘" + "▬" * (length - filled)
    return f"{format_duration(elapsed)} {bar} {format_duration(total)}"


def parse_time(value: str) -> int | None:
    """Parse '90', '1:30', or '1:30:00' into seconds. Returns None on failure."""
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


class SearchView(discord.ui.View):
    """Buttons for /search results."""

    def __init__(
        self, results: list[TrackInfo], cog: MusicCog, interaction: discord.Interaction
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.original_interaction = interaction

        for i, track in enumerate(results):
            label = f"{i + 1}. {track.title}"
            if len(label) > 80:
                label = label[:77] + "..."
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            button.callback = self._make_callback(track)
            self.add_item(button)

    def _make_callback(self, track: TrackInfo):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            track.requester = interaction.user.display_name

            vc = await self.cog._ensure_voice(interaction)
            if vc is None:
                return

            if vc.is_playing() or vc.is_paused():
                gq = self.cog.queues.get(interaction.guild.id)  # type: ignore[union-attr]
                gq.current = None
                gq.queue.appendleft(track)
                vc.stop()  # triggers _play_next → pops our track from front
                await interaction.followup.send(f"Now playing: **{track.title}**")
            else:
                await self.cog._enqueue_and_play(interaction, track)

        return callback

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await self.original_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class MixConfirmView(discord.ui.View):
    """Asks the user whether to play a YouTube Mix or just the single video."""

    def __init__(self, cog: MusicCog, interaction: discord.Interaction, url: str) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.original_interaction = interaction
        self.url = url

    @discord.ui.button(label="Play just this video", style=discord.ButtonStyle.primary)
    async def play_video(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self._disable_all()
        await self.original_interaction.edit_original_response(view=self)
        # Strip list= params to get just the video URL
        parsed = urlparse(self.url)
        params = parse_qs(parsed.query)
        video_id = params.get("v", [None])[0]
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            video_url = self.url
        # Re-invoke play logic as a YouTube URL
        await self.cog._play_single_url(interaction, video_url)

    @discord.ui.button(label="Load the mix anyway", style=discord.ButtonStyle.secondary)
    async def play_mix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self._disable_all()
        await self.original_interaction.edit_original_response(view=self)
        await self.cog._play_youtube_playlist(interaction, self.url)

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    async def on_timeout(self) -> None:
        self._disable_all()
        try:
            await self.original_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class VoteSkipView(discord.ui.View):
    """Vote-skip button that tracks unique voters."""

    def __init__(self, cog: MusicCog, guild: discord.Guild, required: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.guild = guild
        self.required = required
        self.voters: set[int] = set()

    @discord.ui.button(label="Skip (0/0)", style=discord.ButtonStyle.danger)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.voters.add(interaction.user.id)
        # Also record in the guild queue
        gq = self.cog.queues.get(self.guild.id)
        gq.skip_votes = self.voters

        count = len(self.voters)
        button.label = f"Skip ({count}/{self.required})"

        if count >= self.required:
            button.disabled = True
            await interaction.response.edit_message(
                content=f"Vote skip passed ({count}/{self.required})! Skipping...",
                view=self,
            )
            vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
            if vc and vc.is_playing():
                vc.stop()
        else:
            await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.queues = QueueManager()
        self.spotify = SpotifyResolver()
        self.history = HistoryManager()
        self.favorites = FavoritesManager()

    # ── helpers ──────────────────────────────────────────────────────────

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> Optional[discord.VoiceClient]:
        """Join the user's voice channel if needed. Returns the VoiceClient or None on failure."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel.", ephemeral=True
            )
            return None

        channel = interaction.user.voice.channel
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[assignment]

        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)

        return vc

    def _check_idle(self, guild: discord.Guild) -> None:
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc and not vc.is_playing() and not vc.is_paused():
            asyncio.run_coroutine_threadsafe(vc.disconnect(), self.bot.loop)
            self.queues.remove(guild.id)
            asyncio.run_coroutine_threadsafe(
                self._update_presence(None), self.bot.loop
            )

    def _after_play(self, guild: discord.Guild, error: Exception | None) -> None:
        if error:
            log.error("Playback error in guild %s: %s", guild.id, error)
        gq = self.queues.get(guild.id)
        if gq._restarting:
            return  # restart handles its own playback
        asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

    async def _play_next(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None:
            gq.clear()
            return

        track = gq.next_track()
        if track is None:
            # Autoplay: recommend a track based on what just played
            if gq.autoplay and self.spotify.available and gq.current is not None:
                try:
                    rec = await self.bot.loop.run_in_executor(
                        None, self.spotify.recommend, gq.current.title
                    )
                    if rec:
                        gq.add(rec)
                        track = gq.next_track()
                except Exception as exc:
                    log.warning("Autoplay recommendation failed: %s", exc)

            if track is None:
                await self._update_presence(None)
                self.bot.loop.call_later(300, self._check_idle, guild)
                return

        try:
            source = await YTDLSource.from_query(
                track.url, loop=self.bot.loop, volume=gq.volume,
                filter_name=gq.filter_name,
            )
        except Exception as exc:
            log.error("Failed to create source for %s: %s", track.title, exc)
            # Skip broken tracks
            await self._play_next(guild)
            return

        gq.play_start_time = time.time()
        self.history.record(guild.id, track)
        vc.play(source, after=lambda e: self._after_play(guild, e))
        await self._update_presence(track)

    async def _update_presence(self, track: TrackInfo | None) -> None:
        if track:
            activity = discord.Activity(
                type=discord.ActivityType.listening, name=track.title
            )
        else:
            activity = None
        await self.bot.change_presence(activity=activity)

    async def _restart_playback(
        self, guild: discord.Guild, seek_seconds: int = 0
    ) -> None:
        """Restart current track with the active filter and/or seek position."""
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            return

        # Get the stream URL from the current source if available
        current_source = vc.source
        stream_url = None
        if isinstance(current_source, YTDLSource):
            stream_url = current_source.stream_url
            data = current_source._data

        gq._restarting = True
        vc.stop()

        if stream_url:
            source = YTDLSource.from_stream_url(
                stream_url,
                data=data,
                volume=gq.volume,
                filter_name=gq.filter_name,
                seek_seconds=seek_seconds,
            )
        else:
            source = await YTDLSource.from_query(
                gq.current.url,
                loop=self.bot.loop,
                volume=gq.volume,
                filter_name=gq.filter_name,
                seek_seconds=seek_seconds,
            )

        gq.play_start_time = time.time() - seek_seconds
        gq._restarting = False
        vc.play(source, after=lambda e: self._after_play(guild, e))

    async def _enqueue_and_play(
        self, interaction: discord.Interaction, track: TrackInfo
    ) -> None:
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        pos = gq.add(track)

        if pos is None:
            msg = f"Queue is full ({gq.max_queue} tracks max)."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]
            msg = f"Now playing: **{track.title}**"
        else:
            msg = f"Queued **{track.title}** at position #{pos}"

        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)

    async def _play_youtube_playlist(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        """Fetch a YouTube playlist and queue all its tracks."""
        try:
            import yt_dlp
            from music.audio_source import YTDL_OPTIONS

            ytdl = yt_dlp.YoutubeDL(
                {
                    **YTDL_OPTIONS,
                    "noplaylist": False,
                    "extract_flat": "in_playlist",
                    "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
                }
            )
            data = await self.bot.loop.run_in_executor(
                None, lambda: ytdl.extract_info(url, download=False)
            )
        except Exception as exc:
            await interaction.followup.send(f"Could not load playlist: {exc}")
            return

        entries = data.get("entries") or []
        if not entries:
            await interaction.followup.send("No tracks found in that playlist.")
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        count = 0
        skipped = 0
        for entry in entries:
            if entry is None:
                continue
            video_id = entry.get("id", "")
            if video_id:
                entry_url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                entry_url = entry.get("webpage_url") or entry.get("url", "")
            track = TrackInfo(
                title=entry.get("title", "Unknown"),
                url=entry_url,
                duration=int(entry.get("duration", 0) or 0),
                thumbnail=entry.get("thumbnail", ""),
                requester=interaction.user.display_name,
            )
            if gq.add(track) is None:
                skipped = sum(1 for e in entries if e is not None) - count
                break
            count += 1

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        playlist_title = data.get("title", "YouTube playlist")
        msg = f"Queued **{count} tracks** from **{playlist_title}**."
        if skipped:
            msg += f" ({skipped} skipped — queue full)"
        await interaction.followup.send(msg)

    async def _play_single_url(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        """Resolve a single YouTube URL or search query and queue it."""
        try:
            import yt_dlp
            from music.audio_source import YTDL_OPTIONS

            ytdl = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "skip_download": True})
            data = await self.bot.loop.run_in_executor(
                None, lambda: ytdl.extract_info(url, download=False)
            )
            if "entries" in data:
                data = data["entries"][0]

            track = TrackInfo(
                title=data.get("title", "Unknown"),
                url=data.get("webpage_url", url),
                duration=int(data.get("duration", 0) or 0),
                thumbnail=data.get("thumbnail", ""),
                requester=interaction.user.display_name,
            )
        except Exception as exc:
            await interaction.followup.send(f"Could not find anything: {exc}")
            return

        await self._enqueue_and_play(interaction, track)

    # ── commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play from a YouTube/Spotify URL or search keywords")
    @app_commands.describe(query="YouTube URL, Spotify URL, or search keywords")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        input_type, value = classify(query)

        # Spotify resolution
        if input_type in (
            InputType.SPOTIFY_TRACK,
            InputType.SPOTIFY_PLAYLIST,
            InputType.SPOTIFY_ALBUM,
        ):
            if not self.spotify.available:
                await interaction.response.send_message(
                    "Spotify credentials are not configured.", ephemeral=True
                )
                return

            await interaction.response.defer()

            resolver_map = {
                InputType.SPOTIFY_TRACK: self.spotify.resolve_track,
                InputType.SPOTIFY_PLAYLIST: self.spotify.resolve_playlist,
                InputType.SPOTIFY_ALBUM: self.spotify.resolve_album,
            }
            try:
                search_strings = await self.bot.loop.run_in_executor(
                    None, resolver_map[input_type], value
                )
            except Exception as exc:
                await interaction.followup.send(f"Spotify error: {exc}")
                return

            if not search_strings:
                await interaction.followup.send("No tracks found from that Spotify link.")
                return

            vc = await self._ensure_voice(interaction)
            if vc is None:
                return

            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            count = 0
            for s in search_strings:
                track = TrackInfo(
                    title=s,
                    url=f"ytsearch:{s}",
                    requester=interaction.user.display_name,
                )
                if gq.add(track) is None:
                    break
                count += 1

            if not vc.is_playing() and not vc.is_paused():
                await self._play_next(interaction.guild)  # type: ignore[arg-type]

            msg = f"Queued **{count} track{'s' if count != 1 else ''}** from Spotify."
            if count < len(search_strings):
                msg += f" ({len(search_strings) - count} skipped — queue full)"
            await interaction.followup.send(msg)
            return

        # YouTube playlist
        if input_type == InputType.YOUTUBE_PLAYLIST:
            # Detect YouTube Mix (list=RD...) — these are personalized
            params = parse_qs(urlparse(value).query)
            list_id = params.get("list", [""])[0]
            if list_id.startswith("RD"):
                await interaction.response.send_message(
                    "This is a **YouTube Mix** — its contents are personalized and "
                    "may differ from what you see in your browser.\n"
                    "What would you like to do?",
                    view=MixConfirmView(self, interaction, value),
                )
                return

            await interaction.response.defer()
            await self._play_youtube_playlist(interaction, value)
            return

        # YouTube URL or search
        await interaction.response.defer()
        if input_type == InputType.SEARCH_QUERY:
            url = f"ytsearch:{value}"
        else:
            url = value
        await self._play_single_url(interaction, url)

    @app_commands.command(name="stop", description="Stop playback, clear queue, and disconnect")
    async def stop(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.clear()
        vc.stop()
        await vc.disconnect()
        self.queues.remove(interaction.guild.id)  # type: ignore[union-attr]
        await self._update_presence(None)
        await interaction.response.send_message("Stopped and disconnected.")

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        title = gq.current.title if gq.current else "current track"
        vc.stop()  # triggers _after_play → _play_next
        await interaction.response.send_message(f"Skipped **{title}**.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]

        if not gq.current and not gq.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return

        lines: list[str] = []
        if gq.current:
            lines.append(f"**Now playing:** {gq.current.title} [{format_duration(gq.current.duration)}]")

        for i, track in enumerate(gq.queue):
            if i >= 20:
                lines.append(f"... and {len(gq.queue) - 20} more")
                break
            lines.append(f"`{i + 1}.` {track.title} [{format_duration(track.duration)}]")

        lines.append(f"\nLoop: **{gq.loop_mode.label()}** | Volume: **{int(gq.volume * 100)}%**")

        embed = discord.Embed(
            title="Queue",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        vc.pause()
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_paused():
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)
            return
        vc.resume()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        track = gq.current
        elapsed = int(time.time() - gq.play_start_time) if gq.play_start_time else 0

        embed = discord.Embed(
            title="Now Playing",
            description=f"**{track.title}**\n{progress_bar(elapsed, track.duration)}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Requested by", value=track.requester or "Unknown")
        embed.add_field(name="Loop", value=gq.loop_mode.label())
        if gq.autoplay:
            embed.add_field(name="Autoplay", value="on")
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        if track.url:
            embed.url = track.url

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Adjust volume (1-100)")
    @app_commands.describe(level="Volume level from 1 to 100")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        if not 1 <= level <= 100:
            await interaction.response.send_message(
                "Volume must be between 1 and 100.", ephemeral=True
            )
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.volume = level / 100

        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume

        self.queues.save_settings()
        await interaction.response.send_message(f"Volume set to **{level}%**.")

    async def _do_youtube_search(self, interaction: discord.Interaction, query: str) -> None:
        results = await YTDLSource.search(query, loop=self.bot.loop, limit=5)
        if not results:
            await interaction.followup.send("No results found.")
            return

        lines = [
            f"**{i + 1}.** {t.title} [{format_duration(t.duration)}]"
            for i, t in enumerate(results)
        ]
        embed = discord.Embed(
            title=f"YouTube results for: {query}",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    async def _do_spotify_search(self, interaction: discord.Interaction, query: str) -> None:
        if not self.spotify.available:
            await interaction.followup.send(
                "Spotify credentials are not configured.", ephemeral=True
            )
            return

        results = await self.bot.loop.run_in_executor(
            None, lambda: self.spotify.search(query, limit=5)
        )
        if not results:
            await interaction.followup.send("No results found.")
            return

        lines = [
            f"**{i + 1}.** {t.title} [{format_duration(t.duration)}]"
            for i, t in enumerate(results)
        ]
        embed = discord.Embed(
            title=f"Spotify results for: {query}",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="search", description="Search and pick from results (uses server default)")
    @app_commands.describe(query="Search keywords")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.search_mode == "spotify":
            await self._do_spotify_search(interaction, query)
        else:
            await self._do_youtube_search(interaction, query)

    @app_commands.command(name="youtube-search", description="Search YouTube and pick from results")
    @app_commands.describe(query="Search keywords")
    async def youtube_search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        await self._do_youtube_search(interaction, query)

    @app_commands.command(name="spotify-search", description="Search Spotify and pick from results")
    @app_commands.describe(query="Search keywords")
    async def spotify_search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        await self._do_spotify_search(interaction, query)

    @app_commands.command(name="searchmode", description="Toggle default search between YouTube and Spotify")
    async def searchmode(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.search_mode == "youtube":
            gq.search_mode = "spotify"
        else:
            gq.search_mode = "youtube"
        self.queues.save_settings()
        await interaction.response.send_message(
            f"Default search mode set to **{gq.search_mode}**."
        )

    @app_commands.command(name="maxqueue", description="Set the maximum queue size")
    @app_commands.describe(size="Maximum number of tracks in the queue (1-500)")
    async def maxqueue(self, interaction: discord.Interaction, size: int) -> None:
        if not 1 <= size <= 500:
            await interaction.response.send_message(
                "Max queue size must be between 1 and 500.", ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.max_queue = size
        self.queues.save_settings()
        await interaction.response.send_message(f"Max queue size set to **{size}**.")

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Position in the queue (1-indexed)")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        removed = gq.remove_at(position - 1)
        if removed is None:
            await interaction.response.send_message(
                f"Invalid position. Queue has {len(gq.queue)} tracks.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"Removed **{removed.title}** from the queue.")

    @app_commands.command(name="skipto", description="Skip to a specific position in the queue")
    @app_commands.describe(position="Position in the queue to skip to (1-indexed)")
    async def skipto(self, interaction: discord.Interaction, position: int) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        target = gq.skip_to(position - 1)
        if target is None:
            await interaction.response.send_message(
                f"Invalid position. Queue has {len(gq.queue)} tracks.", ephemeral=True
            )
            return

        gq.current = None
        vc.stop()  # triggers _play_next → pops target from front
        await interaction.response.send_message(f"Skipping to **{target.title}**.")

    @app_commands.command(name="clear", description="Clear the queue (keeps current track playing)")
    async def clear(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        count = len(gq.queue)
        if count == 0:
            await interaction.response.send_message("Queue is already empty.", ephemeral=True)
            return
        gq.queue.clear()
        await interaction.response.send_message(f"Cleared **{count}** tracks from the queue.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if len(gq.queue) < 2:
            await interaction.response.send_message(
                "Not enough tracks to shuffle.", ephemeral=True
            )
            return
        gq.shuffle()
        await interaction.response.send_message(f"Shuffled **{len(gq.queue)}** tracks.")

    @app_commands.command(name="loop", description="Cycle loop mode: off → single → queue → off")
    async def loop(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.loop_mode = gq.loop_mode.next()
        await interaction.response.send_message(f"Loop mode: **{gq.loop_mode.label()}**.")

    @app_commands.command(name="autoplay", description="Toggle autoplay — auto-queue similar tracks when the queue runs out")
    async def autoplay(self, interaction: discord.Interaction) -> None:
        if not self.spotify.available:
            await interaction.response.send_message(
                "Autoplay requires Spotify credentials.", ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.autoplay = not gq.autoplay
        self.queues.save_settings()
        state = "on" if gq.autoplay else "off"
        await interaction.response.send_message(f"Autoplay is now **{state}**.")

    # ── filter / seek ────────────────────────────────────────────────────

    @app_commands.command(name="filter", description="Apply an audio filter to playback")
    @app_commands.describe(name="Audio filter to apply")
    @app_commands.choices(name=[
        app_commands.Choice(name="Bass Boost", value="bassboost"),
        app_commands.Choice(name="Nightcore", value="nightcore"),
        app_commands.Choice(name="Vaporwave", value="vaporwave"),
        app_commands.Choice(name="8D", value="8d"),
        app_commands.Choice(name="None", value="none"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, name: str) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.filter_name = name if name != "none" else None
        self.queues.save_settings()

        await interaction.response.defer()
        elapsed = int(time.time() - gq.play_start_time) if gq.play_start_time else 0
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)

        label = name if name != "none" else "off"
        await interaction.followup.send(f"Audio filter: **{label}**.")

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Time to seek to (e.g. 90, 1:30, 1:30:00)")
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        secs = parse_time(position)
        if secs is None or secs < 0:
            await interaction.response.send_message("Invalid time format. Use `90`, `1:30`, or `1:30:00`.", ephemeral=True)
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current and gq.current.duration and secs >= gq.current.duration:
            await interaction.response.send_message("Seek position is past the end of the track.", ephemeral=True)
            return

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=secs)
        await interaction.followup.send(f"Seeked to **{format_duration(secs)}**.")

    # ── lyrics ───────────────────────────────────────────────────────────

    @app_commands.command(name="lyrics", description="Show lyrics for the current or specified track")
    @app_commands.describe(query="Search query (defaults to current track)")
    async def lyrics(self, interaction: discord.Interaction, query: str | None = None) -> None:
        if query is None:
            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            if gq.current is None:
                await interaction.response.send_message(
                    "Nothing is playing. Provide a search query.", ephemeral=True
                )
                return
            query = gq.current.title

        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://lrclib.net/api/search",
                    params={"q": query},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Could not fetch lyrics.")
                        return
                    results = await resp.json()
        except Exception:
            await interaction.followup.send("Could not fetch lyrics.")
            return

        if not results:
            await interaction.followup.send(f"No lyrics found for **{query}**.")
            return

        hit = results[0]
        text = hit.get("plainLyrics") or hit.get("syncedLyrics") or ""
        if not text:
            await interaction.followup.send(f"No lyrics found for **{query}**.")
            return

        title = hit.get("trackName", query)
        artist = hit.get("artistName", "")
        header = f"**{title}**" + (f" — {artist}" if artist else "")

        if len(text) <= 4096 - len(header) - 4:
            embed = discord.Embed(
                title="Lyrics",
                description=f"{header}\n\n{text}",
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed)
        else:
            # Paginate into multiple embeds
            chunks: list[str] = []
            while text:
                cut = text[:4000]
                # Try to break at a newline
                nl = cut.rfind("\n")
                if nl > 2000:
                    cut = text[:nl]
                chunks.append(cut)
                text = text[len(cut):].lstrip("\n")

            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"Lyrics ({i + 1}/{len(chunks)})" if len(chunks) > 1 else "Lyrics",
                    description=f"{header}\n\n{chunk}" if i == 0 else chunk,
                    color=discord.Color.blurple(),
                )
                await interaction.followup.send(embed=embed)

    # ── vote skip ────────────────────────────────────────────────────────

    @app_commands.command(name="voteskip", description="Start a vote to skip the current track")
    async def voteskip(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        # Count listeners (non-bot members in the voice channel)
        listeners = [m for m in vc.channel.members if not m.bot]
        if len(listeners) <= 1:
            # Solo — just skip
            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            title = gq.current.title if gq.current else "current track"
            vc.stop()
            await interaction.response.send_message(f"Skipped **{title}**.")
            return

        required = math.ceil(len(listeners) / 2)
        view = VoteSkipView(self, interaction.guild, required)  # type: ignore[arg-type]
        view.voters.add(interaction.user.id)
        view.children[0].label = f"Skip (1/{required})"  # type: ignore[union-attr]

        if 1 >= required:
            vc.stop()
            await interaction.response.send_message("Vote skip passed! Skipping...")
            return

        await interaction.response.send_message(
            f"Vote to skip — **1/{required}** votes. Click below to vote!",
            view=view,
        )

    # ── history / top ────────────────────────────────────────────────────

    @app_commands.command(name="top", description="Show the most played tracks in this server")
    async def top(self, interaction: discord.Interaction) -> None:
        top_tracks = self.history.top(interaction.guild.id)  # type: ignore[union-attr]
        if not top_tracks:
            await interaction.response.send_message("No play history yet.", ephemeral=True)
            return

        lines = [
            f"`{i + 1}.` **{title}** — {count} play{'s' if count != 1 else ''}"
            for i, (title, _url, count) in enumerate(top_tracks)
        ]
        embed = discord.Embed(
            title="Most Played",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ── favorites ────────────────────────────────────────────────────────

    @app_commands.command(name="fav", description="Save the current track to your favorites")
    async def fav(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        ok = self.favorites.add(interaction.user.id, gq.current)
        if ok:
            await interaction.response.send_message(
                f"Saved **{gq.current.title}** to your favorites."
            )
        else:
            await interaction.response.send_message(
                "Already in your favorites or favorites list is full (50 max).",
                ephemeral=True,
            )

    @app_commands.command(name="favs", description="List your favorite tracks")
    async def favs(self, interaction: discord.Interaction) -> None:
        favs = self.favorites.list(interaction.user.id)
        if not favs:
            await interaction.response.send_message(
                "You have no favorites yet. Use `/fav` to save the current track.",
                ephemeral=True,
            )
            return

        lines = [
            f"`{i + 1}.` {f['title']}"
            for i, f in enumerate(favs)
        ]
        embed = discord.Embed(
            title=f"Favorites — {interaction.user.display_name}",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="unfav", description="Remove a track from your favorites")
    @app_commands.describe(position="Position in your favorites list (1-indexed)")
    async def unfav(self, interaction: discord.Interaction, position: int) -> None:
        removed = self.favorites.remove(interaction.user.id, position - 1)
        if removed is None:
            await interaction.response.send_message(
                "Invalid position.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Removed **{removed['title']}** from your favorites."
        )

    @app_commands.command(name="playfavs", description="Queue all your favorite tracks")
    async def playfavs(self, interaction: discord.Interaction) -> None:
        tracks = self.favorites.as_tracks(
            interaction.user.id, requester=interaction.user.display_name
        )
        if not tracks:
            await interaction.response.send_message(
                "You have no favorites. Use `/fav` to save tracks.", ephemeral=True
            )
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        count = 0
        for track in tracks:
            if gq.add(track) is None:
                break
            count += 1

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Queued **{count}** favorite{'s' if count != 1 else ''}."
        if count < len(tracks):
            msg += f" ({len(tracks) - count} skipped — queue full)"
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-disconnect when the bot is left alone in a voice channel."""
        if member.bot:
            return

        # Only care about someone leaving the bot's channel
        if before.channel is None:
            return

        vc: Optional[discord.VoiceClient] = member.guild.voice_client  # type: ignore[assignment]
        if vc is None or vc.channel != before.channel:
            return

        # Check if bot is the only one left
        non_bot_members = [m for m in before.channel.members if not m.bot]
        if len(non_bot_members) == 0:
            gq = self.queues.get(member.guild.id)
            gq.clear()
            vc.stop()
            await vc.disconnect()
            self.queues.remove(member.guild.id)
            await self._update_presence(None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
