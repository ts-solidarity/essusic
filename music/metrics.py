"""Prometheus metric definitions for Essusic.

Requires ``prometheus-client`` (optional dependency).
"""
from __future__ import annotations

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    tracks_played_total = Counter(
        "essusic_tracks_played_total",
        "Total tracks played across all guilds",
    )
    playback_errors_total = Counter(
        "essusic_playback_errors_total",
        "Total playback errors",
    )
    queue_size = Gauge(
        "essusic_queue_size",
        "Current queue size",
        ["guild_id"],
    )
    active_players = Gauge(
        "essusic_active_players",
        "Number of guilds currently playing audio",
    )
    command_latency_seconds = Histogram(
        "essusic_command_latency_seconds",
        "Slash command processing latency",
        buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    ytdl_fetch_seconds = Histogram(
        "essusic_ytdl_fetch_seconds",
        "Time to fetch audio info from yt-dlp",
        buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    )
    voice_connections = Gauge(
        "essusic_voice_connections",
        "Number of active voice connections",
    )

    def start_metrics_server(port: int = 9090) -> None:
        start_http_server(port)

except ImportError:
    # Stub implementations when prometheus_client is not installed

    class _Noop:
        def inc(self, *a, **kw): pass
        def dec(self, *a, **kw): pass
        def set(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def labels(self, *a, **kw): return self
        def time(self): return _NoopContext()

    class _NoopContext:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    _noop = _Noop()
    tracks_played_total = _noop  # type: ignore[assignment]
    playback_errors_total = _noop  # type: ignore[assignment]
    queue_size = _noop  # type: ignore[assignment]
    active_players = _noop  # type: ignore[assignment]
    command_latency_seconds = _noop  # type: ignore[assignment]
    ytdl_fetch_seconds = _noop  # type: ignore[assignment]
    voice_connections = _noop  # type: ignore[assignment]

    def start_metrics_server(port: int = 9090) -> None:
        raise ImportError("prometheus_client is not installed")
