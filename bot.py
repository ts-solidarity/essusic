import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("essusic")


class Essusic(commands.AutoShardedBot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Load i18n locales
        try:
            from music.i18n import load_locales
            load_locales()
        except Exception as exc:
            log.warning("Failed to load locales: %s", exc)

        await self.load_extension("cogs.music_cog")
        await self.tree.sync()
        log.info("Command tree synced.")

        # Start metrics server if prometheus_client is available
        try:
            from music.metrics import start_metrics_server
            start_metrics_server()
            log.info("Prometheus metrics server started on :9090")
        except ImportError:
            log.info("prometheus_client not installed — metrics disabled")
        except Exception as exc:
            log.warning("Failed to start metrics server: %s", exc)

        # Start web dashboard if configured
        web_port = os.getenv("WEB_PORT")
        if web_port:
            try:
                from web.app import start_web_server
                await start_web_server(self, int(web_port))
                log.info("Web dashboard started on :%s", web_port)
            except ImportError:
                log.info("aiohttp not available — web dashboard disabled")
            except Exception as exc:
                log.warning("Failed to start web dashboard: %s", exc)

    async def on_ready(self) -> None:
        guild_count = len(self.guilds)
        log.info("Logged in as %s (ID: %s) — %d guilds, %s shard(s)",
                 self.user, self.user.id, guild_count,
                 self.shard_count or 1)
        # Aggregate presence for sharded bot
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"music in {guild_count} servers",
        )
        await self.change_presence(activity=activity)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set in .env")

    bot = Essusic()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
