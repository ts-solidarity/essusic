from __future__ import annotations

import asyncio
import base64
import json
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

from music.audio_source import (
    AUDIO_FILTERS,
    EQ_PRESETS,
    CrossfadeSource,
    TrackInfo,
    YTDLSource,
)
from music.metrics import (
    active_players as metric_active_players,
    playback_errors_total,
    queue_size as metric_queue_size,
    tracks_played_total,
    voice_connections as metric_voice_connections,
)
from music.queue_manager import (
    FavoritesManager,
    GuildQueue,
    HistoryManager,
    PlaylistManager,
    QueueManager,
    RatingsManager,
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


def _check_dj(interaction: discord.Interaction, gq: GuildQueue) -> str | None:
    """Return None if the user is authorized, or an error message string."""
    if gq.dj_role_id is None:
        return None
    member = interaction.user
    if member.guild_permissions.administrator:  # type: ignore[union-attr]
        return None
    if any(r.id == gq.dj_role_id for r in member.roles):  # type: ignore[union-attr]
        return None
    # Allow if user is alone with bot in VC
    if member.voice and member.voice.channel:  # type: ignore[union-attr]
        non_bot = [m for m in member.voice.channel.members if not m.bot]  # type: ignore[union-attr]
        if len(non_bot) <= 1:
            return None
    role = interaction.guild.get_role(gq.dj_role_id)  # type: ignore[union-attr]
    role_name = role.name if role else "DJ"
    return f"You need the **{role_name}** role to use this command."


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


class RateView(discord.ui.View):
    """Thumbs up/down rating buttons for the current track."""

    def __init__(self, cog: MusicCog, guild_id: int, track_url: str, track_title: str) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.track_url = track_url
        self.track_title = track_title
        up, down = self.cog.ratings.get_rating(guild_id, track_url)
        self.up_btn.label = f"\U0001f44d {up}"
        self.down_btn.label = f"\U0001f44e {down}"

    @discord.ui.button(label="\U0001f44d 0", style=discord.ButtonStyle.success)
    async def up_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        up, down = self.cog.ratings.vote(
            self.guild_id, self.track_url, self.track_title,
            interaction.user.id, "up",
        )
        self.up_btn.label = f"\U0001f44d {up}"
        self.down_btn.label = f"\U0001f44e {down}"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="\U0001f44e 0", style=discord.ButtonStyle.danger)
    async def down_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        up, down = self.cog.ratings.vote(
            self.guild_id, self.track_url, self.track_title,
            interaction.user.id, "down",
        )
        self.up_btn.label = f"\U0001f44d {up}"
        self.down_btn.label = f"\U0001f44e {down}"
        await interaction.response.edit_message(view=self)


class DJApprovalView(discord.ui.View):
    """Approve/reject a track request in DJ queue mode."""

    def __init__(self, cog: MusicCog, guild: discord.Guild, track: TrackInfo) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.track = track

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        pos = gq.add(self.track)
        if pos is None:
            await interaction.response.send_message("Queue is full.", ephemeral=True)
            return
        # Remove from pending
        try:
            gq.pending_requests.remove(self.track)
        except ValueError:
            pass
        self._disable_all()
        await interaction.response.edit_message(
            content=f"Approved **{self.track.title}** (position #{pos}).",
            view=self,
        )
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and not vc.is_playing() and not vc.is_paused():
            await self.cog._play_next(self.guild)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        try:
            gq.pending_requests.remove(self.track)
        except ValueError:
            pass
        self._disable_all()
        await interaction.response.edit_message(
            content=f"Rejected **{self.track.title}**.",
            view=self,
        )

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    async def on_timeout(self) -> None:
        self._disable_all()


class PlayerView(discord.ui.View):
    """Interactive music player with controls, progress bar, and seek."""

    def __init__(self, cog: MusicCog, guild: discord.Guild) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.message: discord.Message | None = None
        self._update_task: asyncio.Task | None = None
        self._build_seek_menu()

    def _build_seek_menu_safe(self) -> None:
        """Rebuild the seek menu, removing the old one first."""
        # Remove old select menu
        to_remove = [c for c in self.children if isinstance(c, discord.ui.Select)]
        for c in to_remove:
            self.remove_item(c)
        self._build_seek_menu()

    def _build_seek_menu(self) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if not gq.current or gq.current.duration <= 0:
            return
        dur = gq.current.duration

        # Pick an interval that gives 15-25 options (Discord max is 25)
        if dur <= 120:        # <=2m  → every 10s
            step = 10
        elif dur <= 300:     # <=5m  → every 15s
            step = 15
        elif dur <= 600:     # <=10m → every 30s
            step = 30
        elif dur <= 1800:    # <=30m → every 60s
            step = 60
        elif dur <= 3600:    # <=1h  → every 2m
            step = 120
        elif dur <= 7200:    # <=2h  → every 5m
            step = 300
        else:                # >2h   → every 10m
            step = 600

        options: list[discord.SelectOption] = []
        t = 0
        while t < dur and len(options) < 25:
            pct = int(t / dur * 100)
            bar_len = 10
            filled = round(bar_len * t / dur)
            bar = "\u25ac" * filled + "\U0001f518" + "\u25ac" * (bar_len - filled)
            options.append(discord.SelectOption(
                label=f"{format_duration(t)}  /  {format_duration(dur)}",
                value=str(t),
                description=f"{bar}  ({pct}%)",
            ))
            t += step

        if not options:
            return

        select = discord.ui.Select(
            placeholder=f"\u23e9  Seek  \u2014  {format_duration(dur)}",
            options=options,
            row=1,
        )
        select.callback = self._on_seek
        self.add_item(select)

    async def _on_seek(self, interaction: discord.Interaction) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        secs = int(interaction.data["values"][0])
        if gq.current.duration and secs >= gq.current.duration:
            secs = max(0, gq.current.duration - 1)
        await interaction.response.defer()
        await self.cog._restart_playback(self.guild, seek_seconds=secs)
        await asyncio.sleep(0.5)
        await self._refresh()

    def _build_embed(self) -> discord.Embed:
        gq = self.cog.queues.get(self.guild.id)
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]

        if gq.current is None:
            return discord.Embed(
                title="Not playing",
                description="Use `/play` to start a track.",
                color=discord.Color.dark_grey(),
            )

        track = gq.current
        elapsed = self.cog._get_elapsed(gq)

        url = track.url if track.url and not track.url.startswith("ytsearch:") else None
        embed = discord.Embed(title=track.title, url=url, color=discord.Color.blurple())
        bar = progress_bar(elapsed, track.duration)
        embed.description = f"\n{bar}\n\u2193 *Use the dropdown to seek* \u2193" if track.duration > 0 else f"\n{bar}\n"

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        embed.add_field(name="Requested by", value=track.requester or "Unknown", inline=True)
        embed.add_field(name="Queue", value=f"{len(gq.queue)} tracks", inline=True)
        embed.add_field(name="Volume", value=f"{int(gq.volume * 100)}%", inline=True)

        parts: list[str] = []
        if vc and vc.is_paused():
            parts.append("Paused")
        else:
            parts.append("Playing")
        if gq.loop_mode.label() != "off":
            parts.append(f"Loop: {gq.loop_mode.label()}")
        if gq.autoplay:
            parts.append("Autoplay")
        if gq.filter_name:
            parts.append(f"Filter: {gq.filter_name}")
        if gq.speed != 1.0:
            parts.append(f"Speed: {gq.speed}x")
        if gq.normalize:
            parts.append("Normalize")
        embed.set_footer(text=" · ".join(parts))

        return embed

    def _sync_pause_button(self) -> None:
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        paused = vc is not None and vc.is_paused()
        self.pause_resume_btn.emoji = "\u25b6" if paused else "\u23f8"
        self.pause_resume_btn.style = (
            discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        )

    async def _auto_update(self) -> None:
        try:
            while not self.is_finished():
                await asyncio.sleep(10)
                if self.message is None:
                    break
                await self._refresh()
        except asyncio.CancelledError:
            pass

    async def _refresh(self) -> None:
        embed = self._build_embed()
        self._sync_pause_button()
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.cog._active_players.pop(self.guild.id, None)

    # Row 0: transport controls

    @discord.ui.button(emoji="\u23ee", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if gq.previous is None:
            await interaction.response.send_message("No previous track.", ephemeral=True)
            return
        track = gq.previous
        gq.previous = None
        gq.queue.appendleft(track)
        gq.current = None
        if vc:
            vc.stop()
        await interaction.response.defer()
        await asyncio.sleep(1.5)
        await self._refresh()

    @discord.ui.button(emoji="\u23ea", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        elapsed = self.cog._get_elapsed(gq)
        seek_to = max(0, elapsed - 10)
        await interaction.response.defer()
        await self.cog._restart_playback(self.guild, seek_seconds=seek_to)
        await asyncio.sleep(0.5)
        await self._refresh()

    @discord.ui.button(emoji="\u23f8", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._refresh()

    @discord.ui.button(emoji="\u23e9", style=discord.ButtonStyle.secondary, row=0)
    async def forward_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        elapsed = self.cog._get_elapsed(gq)
        seek_to = elapsed + 10
        if gq.current.duration and seek_to >= gq.current.duration:
            await interaction.response.send_message("Already near the end.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._restart_playback(self.guild, seek_seconds=seek_to)
        await asyncio.sleep(0.5)
        await self._refresh()

    @discord.ui.button(emoji="\u23ed", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.defer()
        await asyncio.sleep(1.5)
        await self._refresh()

    # Row 2: volume controls

    @discord.ui.button(emoji="\U0001f509", style=discord.ButtonStyle.secondary, row=2)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        gq.volume = max(0.0, round(gq.volume - 0.1, 2))
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume
        self.cog.queues.save_settings()
        await interaction.response.defer()
        await self._refresh()

    @discord.ui.button(emoji="\U0001f50a", style=discord.ButtonStyle.secondary, row=2)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        gq.volume = min(1.0, round(gq.volume + 0.1, 2))
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume
        self.cog.queues.save_settings()
        await interaction.response.defer()
        await self._refresh()


class QueueView(discord.ui.View):
    """Paginated queue display with navigation buttons."""

    PER_PAGE = 10

    def __init__(self, gq: GuildQueue, page: int = 0) -> None:
        super().__init__(timeout=120)
        self.gq = gq
        self.page = page
        self.total_pages = max(1, math.ceil(len(gq.queue) / self.PER_PAGE))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        gq = self.gq
        lines: list[str] = []
        if gq.current:
            lines.append(f"**Now playing:** {gq.current.title} [{format_duration(gq.current.duration)}]")

        start = self.page * self.PER_PAGE
        end = start + self.PER_PAGE
        queue_list = list(gq.queue)
        for i, track in enumerate(queue_list[start:end], start=start):
            lines.append(f"`{i + 1}.` {track.title} [{format_duration(track.duration)}]")

        total_duration = sum(t.duration for t in queue_list) + (gq.current.duration if gq.current else 0)
        footer_parts = [
            f"{len(gq.queue)} tracks",
            format_duration(total_duration),
            f"Loop: {gq.loop_mode.label()}",
            f"Vol: {int(gq.volume * 100)}%",
            f"Page {self.page + 1}/{self.total_pages}",
        ]

        embed = discord.Embed(
            title="Queue",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=" · ".join(footer_parts))
        return embed

    @discord.ui.button(emoji="\u25c0", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="\u25b6", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

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
        self.playlists = PlaylistManager()
        self.ratings = RatingsManager()
        self._active_players: dict[int, PlayerView] = {}
        self._crossfade_timers: dict[int, asyncio.TimerHandle] = {}

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_elapsed(self, gq: GuildQueue) -> int:
        """Get elapsed playback time in seconds, accounting for speed."""
        if not gq.play_start_time:
            return 0
        return int((time.time() - gq.play_start_time) * gq.speed)

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
            metric_voice_connections.inc()
        elif vc.channel != channel:
            await vc.move_to(channel)

        return vc

    def _check_idle(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        if gq.stay_connected:
            return
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

    async def _notify_text_channel(self, guild: discord.Guild, msg: str) -> None:
        """Send a message to the guild's tracked text channel, if set."""
        gq = self.queues.get(guild.id)
        if gq.text_channel_id:
            channel = guild.get_channel(gq.text_channel_id)
            if channel and hasattr(channel, "send"):
                try:
                    await channel.send(msg)  # type: ignore[union-attr]
                except discord.HTTPException:
                    pass

    async def _play_next(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None:
            gq.clear()
            return

        # Guard against stale callbacks from _restart_playback race
        if vc.is_playing() or vc.is_paused():
            return

        track = gq.next_track()
        if track is None:
            # Radio mode: continuously queue similar tracks
            if gq.radio_mode and self.spotify.available and gq.radio_seed:
                try:
                    results = await self.bot.loop.run_in_executor(
                        None,
                        lambda: self.spotify.recommend_by_seed(
                            gq.radio_seed, gq.radio_history, 1  # type: ignore[arg-type]
                        ),
                    )
                    if results:
                        tid, rec = results[0]
                        gq.radio_history.add(tid)
                        if len(gq.radio_history) > 200:
                            gq.radio_history = set(list(gq.radio_history)[-200:])
                        gq.add(rec)
                        track = gq.next_track()
                        await self._notify_text_channel(
                            guild, f"Radio: queued **{rec.title}**"
                        )
                except Exception as exc:
                    log.warning("Radio recommendation failed: %s", exc)

            # Autoplay: recommend a track based on what just played
            if track is None and gq.autoplay and self.spotify.available and gq.current is not None:
                try:
                    rec = await self.bot.loop.run_in_executor(
                        None, self.spotify.recommend, gq.current.title
                    )
                    if rec:
                        gq.add(rec)
                        track = gq.next_track()
                        await self._notify_text_channel(
                            guild, f"Autoplay: queued **{rec.title}**"
                        )
                except Exception as exc:
                    log.warning("Autoplay recommendation failed: %s", exc)

            if track is None:
                metric_active_players.dec()
                self.queues.save_queue_state(guild.id)
                await self._update_presence(None)
                if not gq.stay_connected:
                    self.bot.loop.call_later(300, self._check_idle, guild)
                return

        try:
            source = await YTDLSource.from_query(
                track.url, loop=self.bot.loop, volume=gq.volume,
                filter_name=gq.filter_name,
                speed=gq.speed, normalize=gq.normalize,
                eq_bands=gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None,
                is_live=track.is_live,
            )
        except Exception as exc:
            log.error("Failed to create source for %s: %s", track.title, exc)
            playback_errors_total.inc()
            await self._notify_text_channel(
                guild, f"Failed to play **{track.title}**, skipping..."
            )
            await self._play_next(guild)
            return

        tracks_played_total.inc()
        metric_active_players.inc()
        metric_queue_size.labels(guild_id=str(guild.id)).set(len(gq.queue))
        gq.play_start_time = time.time()
        self.history.record(
            guild.id, track,
            requester_id=track.requester_id,
            duration=track.duration,
        )
        self.queues.save_queue_state(guild.id)
        vc.play(source, after=lambda e: self._after_play(guild, e))
        await self._update_presence(track)

        # Schedule crossfade if enabled and track has known duration
        self._cancel_crossfade_timer(guild.id)
        if (
            gq.crossfade_seconds > 0
            and track.duration > 0
            and not track.is_live
            and gq.queue
        ):
            delay = max(0, (track.duration / gq.speed) - gq.crossfade_seconds)
            handle = self.bot.loop.call_later(
                delay, lambda: asyncio.ensure_future(self._start_crossfade(guild))
            )
            self._crossfade_timers[guild.id] = handle

        # Auto-send/refresh the player view in the text channel
        await self._send_player(guild, gq)

    async def _send_player(self, guild: discord.Guild, gq: GuildQueue) -> None:
        """Send or refresh the interactive PlayerView in the text channel."""
        # Clean up the old player
        old = self._active_players.pop(guild.id, None)
        if old:
            if old._update_task and not old._update_task.done():
                old._update_task.cancel()
            old.stop()
            # Delete old message to avoid clutter
            if old.message:
                try:
                    await old.message.delete()
                except discord.HTTPException:
                    pass

        if gq.current is None or gq.text_channel_id is None:
            return

        channel = guild.get_channel(gq.text_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return

        view = PlayerView(self, guild)
        self._active_players[guild.id] = view
        embed = view._build_embed()
        view._sync_pause_button()
        try:
            msg = await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
            view.message = msg
            view._update_task = asyncio.create_task(view._auto_update())
        except discord.HTTPException:
            self._active_players.pop(guild.id, None)

    async def _update_presence(self, track: TrackInfo | None) -> None:
        if track:
            activity = discord.Activity(
                type=discord.ActivityType.listening, name=track.title
            )
        else:
            activity = None
        await self.bot.change_presence(activity=activity)

    def _cancel_crossfade_timer(self, guild_id: int) -> None:
        handle = self._crossfade_timers.pop(guild_id, None)
        if handle:
            handle.cancel()

    async def _start_crossfade(self, guild: discord.Guild) -> None:
        """Begin crossfade from current track to next."""
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None or not vc.is_playing() or not gq.queue:
            return

        next_track = gq.queue[0]
        try:
            incoming = await YTDLSource.from_query(
                next_track.url, loop=self.bot.loop, volume=gq.volume,
                filter_name=gq.filter_name,
                speed=gq.speed, normalize=gq.normalize,
                eq_bands=gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None,
                is_live=next_track.is_live,
            )
        except Exception as exc:
            log.warning("Crossfade pre-fetch failed: %s", exc)
            return

        outgoing = vc.source
        xfade = CrossfadeSource(outgoing, incoming, gq.crossfade_seconds)
        xfade_vol = discord.PCMVolumeTransformer(xfade, volume=gq.volume)

        gq._restarting = True
        vc.stop()

        # Advance queue
        gq.previous = gq.current
        gq.current = gq.queue.popleft()
        gq.skip_votes.clear()
        gq.play_start_time = time.time()
        self.history.record(
            guild.id, gq.current,
            requester_id=gq.current.requester_id,
            duration=gq.current.duration,
        )
        self.queues.save_queue_state(guild.id)

        vc.play(xfade_vol, after=lambda e: self._after_play(guild, e))
        gq._restarting = False
        await self._update_presence(gq.current)

        # Schedule next crossfade
        if gq.crossfade_seconds > 0 and gq.current.duration > 0 and gq.queue:
            delay = max(0, (gq.current.duration / gq.speed) - gq.crossfade_seconds)
            handle = self.bot.loop.call_later(
                delay, lambda: asyncio.ensure_future(self._start_crossfade(guild))
            )
            self._crossfade_timers[guild.id] = handle

    async def _restart_playback(
        self, guild: discord.Guild, seek_seconds: int = 0
    ) -> None:
        """Restart current track with the active filter and/or seek position."""
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            return

        eq = gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None
        is_live = gq.current.is_live

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
                speed=gq.speed,
                normalize=gq.normalize,
                eq_bands=eq,
                is_live=is_live,
            )
        else:
            source = await YTDLSource.from_query(
                gq.current.url,
                loop=self.bot.loop,
                volume=gq.volume,
                filter_name=gq.filter_name,
                seek_seconds=seek_seconds,
                speed=gq.speed,
                normalize=gq.normalize,
                eq_bands=eq,
                is_live=is_live,
            )

        gq.play_start_time = time.time() - (seek_seconds / gq.speed)
        vc.play(source, after=lambda e: self._after_play(guild, e))
        gq._restarting = False

    async def _enqueue_and_play(
        self, interaction: discord.Interaction, track: TrackInfo
    ) -> None:
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id

        # DJ queue mode: non-DJs submit requests for approval
        if gq.dj_queue_mode and _check_dj(interaction, gq) is not None:
            gq.pending_requests.append(track)
            msg = f"**{track.title}** submitted for DJ approval."
            if interaction.response.is_done():
                await interaction.followup.send(msg)
            else:
                await interaction.response.send_message(msg)
            # Send approval view to text channel
            if gq.text_channel_id:
                channel = interaction.guild.get_channel(gq.text_channel_id)  # type: ignore[union-attr]
                if channel and hasattr(channel, "send"):
                    view = DJApprovalView(self, interaction.guild, track)  # type: ignore[arg-type]
                    await channel.send(  # type: ignore[union-attr]
                        f"**DJ Approval Required:** {track.title} (requested by {track.requester})",
                        view=view,
                    )
            return

        # Duplicate detection
        is_dup = gq.has_duplicate(track)
        pos = gq.add(track)
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

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
            if is_dup:
                msg += "\n**{title}** is already in the queue. Adding anyway.".format(
                    title=track.title
                )

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
        gq.text_channel_id = interaction.channel_id
        total_entries = sum(1 for e in entries if e is not None)
        progress_msg = None
        if total_entries > 5:
            progress_msg = await interaction.followup.send(
                f"Loading... (0/{total_entries} queued)", wait=True
            )

        count = 0
        skipped = 0
        for entry in entries:
            if entry is None:
                continue
            entry_url = entry.get("webpage_url") or entry.get("url", "")
            if not entry_url:
                video_id = entry.get("id", "")
                if video_id:
                    entry_url = f"https://www.youtube.com/watch?v={video_id}"
            track = TrackInfo(
                title=entry.get("title", "Unknown"),
                url=entry_url,
                duration=int(entry.get("duration", 0) or 0),
                thumbnail=entry.get("thumbnail", ""),
                requester=interaction.user.display_name,
            )
            if gq.add(track) is None:
                skipped = total_entries - count
                break
            count += 1
            if progress_msg and count % 5 == 0:
                try:
                    await progress_msg.edit(content=f"Loading... ({count}/{total_entries} queued)")
                except discord.HTTPException:
                    pass

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        playlist_title = data.get("title", "YouTube playlist")
        msg = f"Queued **{count} tracks** from **{playlist_title}**."
        if skipped:
            msg += f" ({skipped} skipped — queue full)"
        if progress_msg:
            try:
                await progress_msg.edit(content=msg)
            except discord.HTTPException:
                await interaction.followup.send(msg)
        else:
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
                artist=data.get("artist", "") or data.get("uploader", "") or "",
                requester_id=interaction.user.id,
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
            gq.text_channel_id = interaction.channel_id
            total = len(search_strings)
            progress_msg = None
            if total > 5:
                progress_msg = await interaction.followup.send(
                    f"Loading... (0/{total} queued)", wait=True
                )

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
                if progress_msg and count % 5 == 0:
                    try:
                        await progress_msg.edit(content=f"Loading... ({count}/{total} queued)")
                    except discord.HTTPException:
                        pass

            self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

            if not vc.is_playing() and not vc.is_paused():
                await self._play_next(interaction.guild)  # type: ignore[arg-type]

            msg = f"Queued **{count} track{'s' if count != 1 else ''}** from Spotify."
            if count < len(search_strings):
                msg += f" ({len(search_strings) - count} skipped — queue full)"
            if progress_msg:
                try:
                    await progress_msg.edit(content=msg)
                except discord.HTTPException:
                    await interaction.followup.send(msg)
            else:
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

        # Radio/live stream
        if input_type == InputType.RADIO_STREAM:
            await interaction.response.defer()
            track = TrackInfo(
                title=value.split("/")[-1] or "Live Stream",
                url=value,
                duration=0,
                requester=interaction.user.display_name,
                requester_id=interaction.user.id,
                is_live=True,
            )
            await self._enqueue_and_play(interaction, track)
            return

        # SoundCloud
        if input_type == InputType.SOUNDCLOUD_URL:
            await interaction.response.defer()
            await self._play_single_url(interaction, value)
            return

        if input_type == InputType.SOUNDCLOUD_PLAYLIST:
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
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        gq.clear()
        self.queues.clear_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        self._cancel_crossfade_timer(interaction.guild.id)  # type: ignore[union-attr]
        vc.stop()
        await vc.disconnect()
        metric_voice_connections.dec()
        metric_active_players.dec()
        self.queues.remove(interaction.guild.id)  # type: ignore[union-attr]
        await self._update_presence(None)
        await interaction.response.send_message("Stopped and disconnected.")

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        title = gq.current.title if gq.current else "current track"
        vc.stop()  # triggers _after_play → _play_next
        await interaction.response.send_message(f"Skipped **{title}**.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]

        if not gq.current and not gq.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return

        if len(gq.queue) > QueueView.PER_PAGE:
            view = QueueView(gq)
            await interaction.response.send_message(embed=view.build_embed(), view=view)
        else:
            lines: list[str] = []
            if gq.current:
                lines.append(f"**Now playing:** {gq.current.title} [{format_duration(gq.current.duration)}]")
            for i, track in enumerate(gq.queue):
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
        elapsed = self._get_elapsed(gq)

        embed = discord.Embed(
            title="Now Playing",
            description=f"**{track.title}**\n{progress_bar(elapsed, track.duration)}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Requested by", value=track.requester or "Unknown")
        embed.add_field(name="Loop", value=gq.loop_mode.label())
        if gq.autoplay:
            embed.add_field(name="Autoplay", value="on")
        if gq.queue:
            next_track = gq.queue[0]
            embed.add_field(name="Up next", value=next_track.title, inline=False)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        if track.url:
            embed.url = track.url

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="player", description="Show an interactive music player with controls")
    async def player(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        gq.text_channel_id = interaction.channel_id
        await interaction.response.defer()
        await self._send_player(interaction.guild, gq)  # type: ignore[arg-type]
        await interaction.followup.send("Player opened.", ephemeral=True)

    @app_commands.command(name="volume", description="Adjust volume (1-100)")
    @app_commands.describe(level="Volume level from 1 to 100")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not 1 <= level <= 100:
            await interaction.response.send_message(
                "Volume must be between 1 and 100.", ephemeral=True
            )
            return

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
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not 1 <= size <= 500:
            await interaction.response.send_message(
                "Max queue size must be between 1 and 500.", ephemeral=True
            )
            return
        gq.max_queue = size
        self.queues.save_settings()
        await interaction.response.send_message(f"Max queue size set to **{size}**.")

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Position in the queue (1-indexed)")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        gq.snapshot(f"Removed #{position}")
        removed = gq.remove_at(position - 1)
        if removed is None:
            await interaction.response.send_message(
                f"Invalid position. Queue has {len(gq.queue)} tracks.", ephemeral=True
            )
            return
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Removed **{removed.title}** from the queue.")

    @app_commands.command(name="move", description="Move a track to a different position in the queue")
    @app_commands.describe(
        from_pos="Current position of the track (1-indexed)",
        to_pos="New position for the track (1-indexed)",
    )
    async def move(self, interaction: discord.Interaction, from_pos: int, to_pos: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        moved = gq.move(from_pos - 1, to_pos - 1)
        if moved is None:
            await interaction.response.send_message(
                f"Invalid position. Queue has {len(gq.queue)} tracks.", ephemeral=True
            )
            return
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(
            f"Moved **{moved.title}** to position #{to_pos}."
        )

    @app_commands.command(name="skipto", description="Skip to a specific position in the queue")
    @app_commands.describe(position="Position in the queue to skip to (1-indexed)")
    async def skipto(self, interaction: discord.Interaction, position: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        gq.snapshot(f"Skip to #{position}")
        target = gq.skip_to(position - 1)
        if target is None:
            await interaction.response.send_message(
                f"Invalid position. Queue has {len(gq.queue)} tracks.", ephemeral=True
            )
            return

        gq.current = None
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        vc.stop()  # triggers _play_next → pops target from front
        await interaction.response.send_message(f"Skipping to **{target.title}**.")

    @app_commands.command(name="clear", description="Clear the queue (keeps current track playing)")
    async def clear(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        count = len(gq.queue)
        if count == 0:
            await interaction.response.send_message("Queue is already empty.", ephemeral=True)
            return
        gq.snapshot("Clear queue")
        gq.queue.clear()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Cleared **{count}** tracks from the queue.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if len(gq.queue) < 2:
            await interaction.response.send_message(
                "Not enough tracks to shuffle.", ephemeral=True
            )
            return
        gq.snapshot("Shuffle")
        gq.smart_shuffle()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Shuffled **{len(gq.queue)}** tracks.")

    @app_commands.command(name="loop", description="Cycle loop mode: off → single → queue → off")
    async def loop(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        gq.loop_mode = gq.loop_mode.next()
        self.queues.save_settings()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
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

    # ── dj permissions ────────────────────────────────────────────────────

    @app_commands.command(name="dj", description="Set the DJ role (admin only)")
    @app_commands.describe(role="Role to set as the DJ role")
    async def dj(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "Only admins can set the DJ role.", ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if role is None:
            if gq.dj_role_id:
                r = interaction.guild.get_role(gq.dj_role_id)  # type: ignore[union-attr]
                name = r.name if r else str(gq.dj_role_id)
                await interaction.response.send_message(f"Current DJ role: **{name}**.")
            else:
                await interaction.response.send_message("No DJ role is set.")
            return
        gq.dj_role_id = role.id
        self.queues.save_settings()
        await interaction.response.send_message(
            f"DJ role set to **{role.name}**. Only users with this role (or admins) can use destructive commands."
        )

    @app_commands.command(name="djclear", description="Clear the DJ role restriction (admin only)")
    async def djclear(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "Only admins can clear the DJ role.", ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.dj_role_id = None
        self.queues.save_settings()
        await interaction.response.send_message("DJ role restriction cleared.")

    # ── replay / back ───────────────────────────────────────────────────

    @app_commands.command(name="replay", description="Restart the current track from the beginning")
    async def replay(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=0)  # type: ignore[arg-type]
        title = gq.current.title if gq.current else "current track"
        await interaction.followup.send(f"Replaying **{title}**.")

    @app_commands.command(name="back", description="Play the previous track")
    async def back(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.previous is None:
            await interaction.response.send_message("No previous track.", ephemeral=True)
            return
        track = gq.previous
        gq.previous = None
        gq.queue.appendleft(track)
        gq.current = None
        vc.stop()  # triggers _play_next → pops track from front
        await interaction.response.send_message(f"Playing previous: **{track.title}**.")

    # ── 24/7 mode ───────────────────────────────────────────────────────

    @app_commands.command(name="24-7", description="Toggle 24/7 mode — bot stays connected even when idle or alone")
    async def stay(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        gq.stay_connected = not gq.stay_connected
        self.queues.save_settings()
        state = "on" if gq.stay_connected else "off"
        await interaction.response.send_message(f"24/7 mode is now **{state}**.")

    # ── filter / seek ────────────────────────────────────────────────────

    @app_commands.command(name="filter", description="Apply an audio filter to playback")
    @app_commands.describe(name="Audio filter to apply")
    @app_commands.choices(name=[
        app_commands.Choice(name="Bass Boost", value="bassboost"),
        app_commands.Choice(name="Nightcore", value="nightcore"),
        app_commands.Choice(name="Vaporwave", value="vaporwave"),
        app_commands.Choice(name="8D", value="8d"),
        app_commands.Choice(name="Karaoke", value="karaoke"),
        app_commands.Choice(name="None", value="none"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, name: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        gq.filter_name = name if name != "none" else None
        self.queues.save_settings()

        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)

        label = name if name != "none" else "off"
        await interaction.followup.send(f"Audio filter: **{label}**.")

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Time to seek to (e.g. 90, 1:30, 1:30:00)")
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        if gq.current and gq.current.is_live:
            await interaction.response.send_message("Cannot seek in a live stream.", ephemeral=True)
            return

        secs = parse_time(position)
        if secs is None or secs < 0:
            await interaction.response.send_message("Invalid time format. Use `90`, `1:30`, or `1:30:00`.", ephemeral=True)
            return

        if gq.current and gq.current.duration and secs >= gq.current.duration:
            await interaction.response.send_message("Seek position is past the end of the track.", ephemeral=True)
            return

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=secs)
        await interaction.followup.send(f"Seeked to **{format_duration(secs)}**.")

    @app_commands.command(name="speed", description="Set playback speed (0.5x - 2.0x)")
    @app_commands.describe(rate="Speed multiplier (0.5 to 2.0)")
    async def speed(self, interaction: discord.Interaction, rate: float) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not 0.5 <= rate <= 2.0:
            await interaction.response.send_message(
                "Speed must be between 0.5 and 2.0.", ephemeral=True
            )
            return

        elapsed = self._get_elapsed(gq)
        gq.speed = rate
        self.queues.save_settings()

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)
        await interaction.followup.send(f"Playback speed set to **{rate}x**.")

    @app_commands.command(name="normalize", description="Toggle loudness normalization")
    async def normalize(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        elapsed = self._get_elapsed(gq)
        gq.normalize = not gq.normalize
        self.queues.save_settings()

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)
        state = "on" if gq.normalize else "off"
        await interaction.followup.send(f"Loudness normalization: **{state}**.")

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
            f"`{i + 1}.` {f['title']} [{format_duration(f.get('duration', 0))}]"
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

    # ── grab ────────────────────────────────────────────────────────────

    @app_commands.command(name="grab", description="Save the current track info to your DMs")
    async def grab(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        track = gq.current
        embed = discord.Embed(
            title="Saved Track",
            description=f"**{track.title}**",
            color=discord.Color.green(),
        )
        if track.url:
            embed.add_field(name="URL", value=track.url, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration))
        embed.add_field(name="Requested by", value=track.requester or "Unknown")
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("Track info sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't send you a DM. Please enable DMs from server members.", ephemeral=True
            )

    # ── saved playlists ──────────────────────────────────────────────────

    playlist_group = app_commands.Group(name="playlist", description="Save and load playlists")

    async def _playlist_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = self.playlists.names(interaction.guild.id)  # type: ignore[union-attr]
        filtered = [n for n in names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]

    @playlist_group.command(name="save", description="Save the current queue as a named playlist")
    @app_commands.describe(name="Playlist name (max 64 characters)")
    async def playlist_save(self, interaction: discord.Interaction, name: str) -> None:
        if len(name) > 64:
            await interaction.response.send_message("Name must be 64 characters or less.", ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        tracks: list[TrackInfo] = []
        if gq.current:
            tracks.append(gq.current)
        tracks.extend(gq.queue)
        if not tracks:
            await interaction.response.send_message("Nothing to save — queue is empty.", ephemeral=True)
            return
        err = self.playlists.save(
            interaction.guild.id, name, tracks,  # type: ignore[union-attr]
            created_by=interaction.user.display_name,
        )
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Saved playlist **{name}** with **{len(tracks)}** track{'s' if len(tracks) != 1 else ''}."
        )

    @playlist_group.command(name="load", description="Queue tracks from a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_load(self, interaction: discord.Interaction, name: str) -> None:
        tracks = self.playlists.load(interaction.guild.id, name)  # type: ignore[union-attr]
        if tracks is None:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        count = 0
        for track in tracks:
            track.requester = interaction.user.display_name
            if gq.add(track) is None:
                break
            count += 1

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Queued **{count}** track{'s' if count != 1 else ''} from **{name}**."
        if count < len(tracks):
            msg += f" ({len(tracks) - count} skipped — queue full)"
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)

    @playlist_group.command(name="list", description="List all saved playlists")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        playlists = self.playlists.list_all(interaction.guild.id)  # type: ignore[union-attr]
        if not playlists:
            await interaction.response.send_message("No saved playlists.", ephemeral=True)
            return
        lines: list[str] = []
        for pl in playlists:
            count = len(pl.get("tracks", []))
            lines.append(f"**{pl['name']}** — {count} track{'s' if count != 1 else ''} (by {pl.get('created_by', '?')})")
        embed = discord.Embed(
            title="Saved Playlists",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if self.playlists.delete(interaction.guild.id, name):  # type: ignore[union-attr]
            await interaction.response.send_message(f"Deleted playlist **{name}**.")
        else:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)

    # ── collaborative playlists ──────────────────────────────────────────

    @playlist_group.command(name="adduser", description="Add a collaborator to a playlist")
    @app_commands.describe(name="Playlist name", user="User to add as collaborator")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_adduser(
        self, interaction: discord.Interaction, name: str, user: discord.Member
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)
            return
        # Only creator or DJ can add collaborators
        gq = self.queues.get(guild_id)
        if creator != interaction.user.display_name and _check_dj(interaction, gq) is not None:
            await interaction.response.send_message("Only the playlist creator or a DJ can add collaborators.", ephemeral=True)
            return
        if self.playlists.add_collaborator(guild_id, name, user.id):
            await interaction.response.send_message(f"Added **{user.display_name}** as collaborator on **{name}**.")
        else:
            await interaction.response.send_message("User is already a collaborator.", ephemeral=True)

    @playlist_group.command(name="removeuser", description="Remove a collaborator from a playlist")
    @app_commands.describe(name="Playlist name", user="User to remove")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_removeuser(
        self, interaction: discord.Interaction, name: str, user: discord.Member
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)
            return
        gq = self.queues.get(guild_id)
        if creator != interaction.user.display_name and _check_dj(interaction, gq) is not None:
            await interaction.response.send_message("Only the playlist creator or a DJ can remove collaborators.", ephemeral=True)
            return
        if self.playlists.remove_collaborator(guild_id, name, user.id):
            await interaction.response.send_message(f"Removed **{user.display_name}** from **{name}**.")
        else:
            await interaction.response.send_message("User is not a collaborator.", ephemeral=True)

    @playlist_group.command(name="addtrack", description="Add the current track to a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_addtrack(self, interaction: discord.Interaction, name: str) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        gq = self.queues.get(guild_id)
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)
            return
        is_creator = creator == interaction.user.display_name
        is_collab = self.playlists.is_collaborator(guild_id, name, interaction.user.id)
        if not is_creator and not is_collab:
            await interaction.response.send_message("You must be the creator or a collaborator.", ephemeral=True)
            return
        err = self.playlists.add_track_to_playlist(guild_id, name, gq.current)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.send_message(f"Added **{gq.current.title}** to **{name}**.")

    @playlist_group.command(name="removetrack", description="Remove a track from a playlist by position")
    @app_commands.describe(name="Playlist name", position="Track position (1-indexed)")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_removetrack(
        self, interaction: discord.Interaction, name: str, position: int
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(f"Playlist **{name}** not found.", ephemeral=True)
            return
        is_creator = creator == interaction.user.display_name
        is_collab = self.playlists.is_collaborator(guild_id, name, interaction.user.id)
        if not is_creator and not is_collab:
            await interaction.response.send_message("You must be the creator or a collaborator.", ephemeral=True)
            return
        removed = self.playlists.remove_track_from_playlist(guild_id, name, position - 1)
        if removed is None:
            await interaction.response.send_message("Invalid position.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Removed **{removed['title']}** from **{name}**.")

    # ── equalizer ────────────────────────────────────────────────────────

    @app_commands.command(name="eq", description="Apply an EQ preset")
    @app_commands.describe(preset="Equalizer preset to apply")
    @app_commands.choices(preset=[
        app_commands.Choice(name="Flat", value="flat"),
        app_commands.Choice(name="Bass Heavy", value="bass_heavy"),
        app_commands.Choice(name="Treble Heavy", value="treble_heavy"),
        app_commands.Choice(name="Vocal", value="vocal"),
        app_commands.Choice(name="Electronic", value="electronic"),
    ])
    async def eq(self, interaction: discord.Interaction, preset: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        gq.eq_bands = list(EQ_PRESETS[preset])
        self.queues.save_settings()
        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)  # type: ignore[arg-type]
        await interaction.followup.send(f"EQ preset: **{preset.replace('_', ' ').title()}**.")

    @app_commands.command(name="eqcustom", description="Set an individual EQ band gain")
    @app_commands.describe(band="Band number (1-10)", gain="Gain in dB (-12 to +12)")
    async def eqcustom(self, interaction: discord.Interaction, band: int, gain: float) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not 1 <= band <= 10:
            await interaction.response.send_message("Band must be 1-10.", ephemeral=True)
            return
        if not -12.0 <= gain <= 12.0:
            await interaction.response.send_message("Gain must be -12 to +12 dB.", ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        gq.eq_bands[band - 1] = gain
        self.queues.save_settings()
        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)  # type: ignore[arg-type]
        from music.audio_source import EQ_BANDS
        band_name = EQ_BANDS[band - 1][0]
        await interaction.followup.send(f"EQ band {band} ({band_name}): **{gain:+.1f} dB**.")

    # ── similar / radio ──────────────────────────────────────────────────

    @app_commands.command(name="similar", description="Show tracks similar to the current one")
    async def similar(self, interaction: discord.Interaction) -> None:
        if not self.spotify.available:
            await interaction.response.send_message("Requires Spotify credentials.", ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        results = await self.bot.loop.run_in_executor(
            None, lambda: self.spotify.recommend_multiple(gq.current.title, 5)  # type: ignore[union-attr]
        )
        if not results:
            await interaction.followup.send("No similar tracks found.")
            return
        lines = [
            f"**{i + 1}.** {t.title} [{format_duration(t.duration)}]"
            for i, t in enumerate(results)
        ]
        embed = discord.Embed(
            title=f"Similar to: {gq.current.title}",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="radio", description="Start continuous radio seeded by artist or genre")
    @app_commands.describe(seed="Artist name or genre to seed the radio")
    async def radio(self, interaction: discord.Interaction, seed: str) -> None:
        if not self.spotify.available:
            await interaction.response.send_message("Requires Spotify credentials.", ephemeral=True)
            return
        await interaction.response.defer()
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id

        results = await self.bot.loop.run_in_executor(
            None, lambda: self.spotify.recommend_by_seed(seed, set(), 5)
        )
        if not results:
            await interaction.followup.send(f"No tracks found for **{seed}**.")
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq.radio_mode = True
        gq.radio_seed = seed
        gq.radio_history = {tid for tid, _ in results}

        count = 0
        for _, track in results:
            track.requester = "Radio"
            track.requester_id = interaction.user.id
            if gq.add(track) is None:
                break
            count += 1

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        await interaction.followup.send(
            f"Radio started with **{seed}** — queued {count} tracks. "
            f"More will be added automatically."
        )

    @app_commands.command(name="radio-off", description="Stop radio mode")
    async def radio_off(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if not gq.radio_mode:
            await interaction.response.send_message("Radio mode is not active.", ephemeral=True)
            return
        gq.radio_mode = False
        gq.radio_seed = None
        gq.radio_history.clear()
        await interaction.response.send_message("Radio mode stopped.")

    # ── queue import/export ──────────────────────────────────────────────

    @app_commands.command(name="queue-export", description="Export the queue as a shareable code")
    async def queue_export(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        tracks: list[dict] = []
        if gq.current:
            tracks.append({"t": gq.current.title, "u": gq.current.url, "d": gq.current.duration})
        for t in gq.queue:
            tracks.append({"t": t.title, "u": t.url, "d": t.duration})
        if not tracks:
            await interaction.response.send_message("Nothing to export.", ephemeral=True)
            return
        data = base64.b64encode(json.dumps(tracks, separators=(",", ":")).encode()).decode()
        if len(data) <= 1900:
            await interaction.response.send_message(f"```\n{data}\n```")
        else:
            import io
            file = discord.File(io.BytesIO(data.encode()), filename="queue.txt")
            await interaction.response.send_message("Queue exported:", file=file)

    @app_commands.command(name="queue-import", description="Import a queue from an exported code")
    @app_commands.describe(code="The exported queue code")
    async def queue_import(self, interaction: discord.Interaction, code: str) -> None:
        try:
            raw = base64.b64decode(code.strip().strip("`")).decode()
            items = json.loads(raw)
        except Exception:
            await interaction.response.send_message("Invalid queue code.", ephemeral=True)
            return
        if not isinstance(items, list) or not items:
            await interaction.response.send_message("No tracks in the code.", ephemeral=True)
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id
        count = 0
        for item in items:
            track = TrackInfo(
                title=item.get("t", "Unknown"),
                url=item.get("u", ""),
                duration=item.get("d", 0),
                requester=interaction.user.display_name,
                requester_id=interaction.user.id,
            )
            if gq.add(track) is None:
                break
            count += 1

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Imported **{count}** track{'s' if count != 1 else ''}."
        if count < len(items):
            msg += f" ({len(items) - count} skipped — queue full)"
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)

    # ── ratings ──────────────────────────────────────────────────────────

    @app_commands.command(name="rate", description="Rate the current track (thumbs up/down)")
    async def rate(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        view = RateView(self, interaction.guild.id, gq.current.url, gq.current.title)  # type: ignore[union-attr]
        embed = discord.Embed(
            title="Rate this track",
            description=f"**{gq.current.title}**",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="toprated", description="Show the top-rated tracks in this server")
    async def toprated(self, interaction: discord.Interaction) -> None:
        items = self.ratings.top_rated(interaction.guild.id)  # type: ignore[union-attr]
        if not items:
            await interaction.response.send_message("No ratings yet.", ephemeral=True)
            return
        lines = [
            f"`{i + 1}.` **{title}** — \U0001f44d {up} \U0001f44e {down} (net: {up - down:+d})"
            for i, (title, _url, up, down) in enumerate(items)
        ]
        embed = discord.Embed(
            title="Top Rated Tracks",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ── listening stats ──────────────────────────────────────────────────

    @app_commands.command(name="stats", description="Show server listening stats")
    async def stats(self, interaction: discord.Interaction) -> None:
        data = self.history.server_stats(interaction.guild.id)  # type: ignore[union-attr]
        if data["total_plays"] == 0:
            await interaction.response.send_message("No play history yet.", ephemeral=True)
            return
        hours = data["total_time_seconds"] / 3600
        lines = [
            f"**Total plays:** {data['total_plays']}",
            f"**Unique tracks:** {data['unique_tracks']}",
            f"**Total listening time:** {hours:.1f} hours",
        ]
        if data["top_tracks"]:
            lines.append("\n**Top Tracks:**")
            for i, (title, count) in enumerate(data["top_tracks"][:5]):
                lines.append(f"`{i + 1}.` {title} — {count} plays")
        if data["top_users"]:
            lines.append("\n**Top Listeners:**")
            for i, (uid, count) in enumerate(data["top_users"][:5]):
                member = interaction.guild.get_member(uid)  # type: ignore[union-attr]
                name = member.display_name if member else f"User {uid}"
                lines.append(f"`{i + 1}.` {name} — {count} plays")
        embed = discord.Embed(
            title=f"Server Stats — {interaction.guild.name}",  # type: ignore[union-attr]
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="mystats", description="Show your personal listening stats")
    async def mystats(self, interaction: discord.Interaction) -> None:
        data = self.history.user_stats(interaction.guild.id, interaction.user.id)  # type: ignore[union-attr]
        if data["total_plays"] == 0:
            await interaction.response.send_message("No listening history for you yet.", ephemeral=True)
            return
        hours = data["total_time_seconds"] / 3600
        lines = [
            f"**Total plays:** {data['total_plays']}",
            f"**Total listening time:** {hours:.1f} hours",
        ]
        if data["top_tracks"]:
            lines.append("\n**Your Top Tracks:**")
            for i, (title, count) in enumerate(data["top_tracks"][:5]):
                lines.append(f"`{i + 1}.` {title} — {count} plays")
        embed = discord.Embed(
            title=f"Your Stats — {interaction.user.display_name}",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        await interaction.response.send_message(embed=embed)

    # ── DJ queue mode ────────────────────────────────────────────────────

    @app_commands.command(name="djmode", description="Toggle DJ approval queue mode (admin only)")
    async def djmode(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message("Only admins can toggle DJ mode.", ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.dj_queue_mode = not gq.dj_queue_mode
        state = "on" if gq.dj_queue_mode else "off"
        await interaction.response.send_message(
            f"DJ queue mode is now **{state}**. "
            + ("Non-DJs must get approval to add tracks." if gq.dj_queue_mode else "")
        )

    # ── undo ─────────────────────────────────────────────────────────────

    @app_commands.command(name="undo", description="Undo the last queue mutation")
    async def undo(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        desc = gq.undo()
        if desc is None:
            await interaction.response.send_message("Nothing to undo.", ephemeral=True)
            return
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Undone: **{desc}**. Queue restored.")

    # ── language ──────────────────────────────────────────────────────────

    @app_commands.command(name="language", description="Set the bot language for this server")
    @app_commands.describe(lang="Language code (e.g. en, tr, de)")
    async def language(self, interaction: discord.Interaction, lang: str) -> None:
        from music.i18n import available_locales
        available = available_locales()
        if lang not in available:
            await interaction.response.send_message(
                f"Language **{lang}** is not available. Available: {', '.join(available) or 'en'}",
                ephemeral=True,
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.locale = lang
        self.queues.save_settings()
        await interaction.response.send_message(f"Language set to **{lang}**.")

    # ── crossfade ────────────────────────────────────────────────────────

    @app_commands.command(name="crossfade", description="Set crossfade duration between tracks (0-10 seconds)")
    @app_commands.describe(seconds="Crossfade duration in seconds (0 to disable)")
    async def crossfade(self, interaction: discord.Interaction, seconds: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not 0 <= seconds <= 10:
            await interaction.response.send_message("Must be 0-10 seconds.", ephemeral=True)
            return
        gq.crossfade_seconds = seconds
        self.queues.save_settings()
        if seconds == 0:
            self._cancel_crossfade_timer(interaction.guild.id)  # type: ignore[union-attr]
            await interaction.response.send_message("Crossfade disabled.")
        else:
            await interaction.response.send_message(f"Crossfade set to **{seconds}s**.")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-disconnect when alone + join notification."""
        if member.bot:
            return

        vc: Optional[discord.VoiceClient] = member.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            return

        # Join notification: user joined the bot's VC
        joined_bot_vc = (
            after.channel is not None
            and after.channel == vc.channel
            and (before.channel is None or before.channel != after.channel)
        )
        if joined_bot_vc:
            gq = self.queues.get(member.guild.id)
            if gq.current and gq.text_channel_id and vc.is_playing():
                channel = member.guild.get_channel(gq.text_channel_id)
                if channel and hasattr(channel, "send"):
                    track = gq.current
                    elapsed = self._get_elapsed(gq)
                    embed = discord.Embed(
                        title="Now Playing",
                        description=f"**{track.title}**\n{progress_bar(elapsed, track.duration)}",
                        color=discord.Color.green(),
                    )
                    if track.thumbnail:
                        embed.set_thumbnail(url=track.thumbnail)
                    try:
                        await channel.send(embed=embed, delete_after=30)  # type: ignore[union-attr]
                    except discord.HTTPException:
                        pass

        # Auto-disconnect when bot is left alone
        if before.channel is None:
            return

        if vc.channel != before.channel:
            return

        non_bot_members = [m for m in before.channel.members if not m.bot]
        if len(non_bot_members) == 0:
            gq = self.queues.get(member.guild.id)
            if gq.stay_connected:
                return
            gq.clear()
            self.queues.clear_queue_state(member.guild.id)
            self._cancel_crossfade_timer(member.guild.id)
            vc.stop()
            await vc.disconnect()
            metric_voice_connections.dec()
            metric_active_players.dec()
            self.queues.remove(member.guild.id)
            await self._update_presence(None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
