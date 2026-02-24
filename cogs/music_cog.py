from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from music.audio_source import TrackInfo, YTDLSource
from music.queue_manager import QueueManager
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


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.queues = QueueManager()
        self.spotify = SpotifyResolver()

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
            vc.encoder.set_bitrate(96)
        elif vc.channel != channel:
            await vc.move_to(channel)

        return vc

    def _after_play(self, guild: discord.Guild, error: Exception | None) -> None:
        if error:
            log.error("Playback error in guild %s: %s", guild.id, error)
        asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

    async def _play_next(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None:
            gq.clear()
            return

        track = gq.next_track()
        if track is None:
            return

        try:
            source = await YTDLSource.from_query(
                track.url, loop=self.bot.loop, volume=gq.volume
            )
        except Exception as exc:
            log.error("Failed to create source for %s: %s", track.title, exc)
            # Skip broken tracks
            await self._play_next(guild)
            return

        vc.play(source, after=lambda e: self._after_play(guild, e))

    async def _enqueue_and_play(
        self, interaction: discord.Interaction, track: TrackInfo
    ) -> None:
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        pos = gq.add(track)

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]
            msg = f"Now playing: **{track.title}**"
        else:
            msg = f"Queued **{track.title}** at position #{pos}"

        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)

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
            first_track = None
            for s in search_strings:
                track = TrackInfo(
                    title=s,
                    url=f"ytsearch:{s}",
                    requester=interaction.user.display_name,
                )
                gq.add(track)
                if first_track is None:
                    first_track = track

            if not vc.is_playing() and not vc.is_paused():
                await self._play_next(interaction.guild)  # type: ignore[arg-type]

            if len(search_strings) == 1:
                await interaction.followup.send(
                    f"Queued **{search_strings[0]}** from Spotify."
                )
            else:
                await interaction.followup.send(
                    f"Queued **{len(search_strings)} tracks** from Spotify."
                )
            return

        # YouTube URL or search
        await interaction.response.defer()

        if input_type == InputType.SEARCH_QUERY:
            url = f"ytsearch:{value}"
        else:
            url = value

        try:
            # Peek at metadata to get title, then queue lightweight TrackInfo
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
        embed = discord.Embed(
            title="Now Playing",
            description=f"**{track.title}**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Duration", value=format_duration(track.duration))
        embed.add_field(name="Requested by", value=track.requester or "Unknown")
        embed.add_field(name="Loop", value=gq.loop_mode.label())
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

        await interaction.response.send_message(f"Volume set to **{level}%**.")

    @app_commands.command(name="search", description="Search YouTube and pick from results")
    @app_commands.describe(query="Search keywords")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()

        results = await YTDLSource.search(query, loop=self.bot.loop, limit=5)
        if not results:
            await interaction.followup.send("No results found.")
            return

        lines = [
            f"**{i + 1}.** {t.title} [{format_duration(t.duration)}]"
            for i, t in enumerate(results)
        ]
        embed = discord.Embed(
            title=f"Search results for: {query}",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )

        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
