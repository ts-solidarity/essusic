"""Embedded web dashboard API for Essusic.

Requires ``aiohttp`` (already a dependency for lyrics).
Shares the same bot process — direct access to MusicCog state.

Start by setting WEB_PORT env var. Discord OAuth2 requires
DISCORD_CLIENT_SECRET and WEB_BASE_URL env vars.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import aiohttp.web as web

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _get_cog(request: web.Request):
    bot: commands.Bot = request.app["bot"]
    cog = bot.get_cog("MusicCog")
    if cog is None:
        raise web.HTTPServiceUnavailable(text="MusicCog not loaded")
    return cog


# ── Health ───────────────────────────────────────────────────────────────

@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    return web.json_response({
        "status": "ok",
        "guilds": len(bot.guilds),
        "shards": bot.shard_count or 1,
    })


# ── Queue ────────────────────────────────────────────────────────────────

@routes.get("/api/guilds/{guild_id}/queue")
async def get_queue(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = int(request.match_info["guild_id"])
    gq = cog.queues.get(guild_id)

    def _track(t):
        return {"title": t.title, "url": t.url, "duration": t.duration,
                "requester": t.requester}

    data = {
        "current": _track(gq.current) if gq.current else None,
        "queue": [_track(t) for t in gq.queue],
        "volume": gq.volume,
        "loop_mode": gq.loop_mode.name,
        "filter": gq.filter_name,
        "autoplay": gq.autoplay,
        "radio_mode": gq.radio_mode,
    }
    return web.json_response(data)


@routes.post("/api/guilds/{guild_id}/skip")
async def skip(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = int(request.match_info["guild_id"])
    bot = request.app["bot"]
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise web.HTTPNotFound(text="Guild not found")
    vc = guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        return web.json_response({"status": "skipped"})
    raise web.HTTPBadRequest(text="Nothing is playing")


@routes.post("/api/guilds/{guild_id}/volume")
async def set_volume(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = int(request.match_info["guild_id"])
    body = await request.json()
    level = body.get("level", 50)
    if not 1 <= level <= 100:
        raise web.HTTPBadRequest(text="Volume must be 1-100")

    gq = cog.queues.get(guild_id)
    gq.volume = level / 100

    bot = request.app["bot"]
    guild = bot.get_guild(guild_id)
    if guild:
        vc = guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            vc.source.volume = gq.volume

    cog.queues.save_settings()
    return web.json_response({"volume": level})


@routes.get("/api/guilds/{guild_id}/playlists")
async def get_playlists(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = int(request.match_info["guild_id"])
    playlists = cog.playlists.list_all(guild_id)
    result = []
    for pl in playlists:
        result.append({
            "name": pl["name"],
            "track_count": len(pl.get("tracks", [])),
            "created_by": pl.get("created_by", ""),
        })
    return web.json_response(result)


@routes.get("/api/guilds/{guild_id}/stats")
async def get_stats(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = int(request.match_info["guild_id"])
    data = cog.history.server_stats(guild_id)
    # Convert Counter tuples to serializable format
    data["top_tracks"] = [{"title": t, "count": c} for t, c in data["top_tracks"]]
    data["top_users"] = [{"user_id": u, "count": c} for u, c in data["top_users"]]
    return web.json_response(data)


# ── OAuth2 (stubs for future frontend) ──────────────────────────────────

@routes.get("/auth/login")
async def auth_login(request: web.Request) -> web.Response:
    client_id = os.getenv("DISCORD_CLIENT_ID")
    base_url = os.getenv("WEB_BASE_URL", "http://localhost:8080")
    if not client_id:
        raise web.HTTPServiceUnavailable(text="DISCORD_CLIENT_ID not set")
    redirect_uri = f"{base_url}/auth/callback"
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}&redirect_uri={redirect_uri}"
        f"&response_type=code&scope=identify+guilds"
    )
    raise web.HTTPFound(url)


@routes.get("/auth/callback")
async def auth_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    if not code:
        raise web.HTTPBadRequest(text="Missing code parameter")
    # TODO: Exchange code for token, create session
    return web.json_response({"status": "not_implemented", "code": code})


# ── Server lifecycle ─────────────────────────────────────────────────────

async def start_web_server(bot: commands.Bot, port: int = 8080) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app.router.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
