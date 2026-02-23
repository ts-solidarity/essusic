from __future__ import annotations

import logging
import os

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

log = logging.getLogger(__name__)


class SpotifyResolver:
    """Resolves Spotify URLs to 'Artist - Title' strings for YouTube search."""

    def __init__(self) -> None:
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            log.warning("Spotify credentials not set — Spotify links will not work.")
            self._sp = None
            return

        auth = SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret
        )
        self._sp = spotipy.Spotify(auth_manager=auth)

    @property
    def available(self) -> bool:
        return self._sp is not None

    def _format_track(self, track: dict) -> str:
        artists = ", ".join(a["name"] for a in track["artists"])
        return f"{artists} - {track['name']}"

    def resolve_track(self, track_id: str) -> list[str]:
        if not self._sp:
            return []
        track = self._sp.track(track_id)
        return [self._format_track(track)]

    def resolve_playlist(self, playlist_id: str) -> list[str]:
        if not self._sp:
            return []
        results: list[str] = []
        resp = self._sp.playlist_tracks(playlist_id)
        while resp:
            for item in resp["items"]:
                track = item.get("track")
                if track:
                    results.append(self._format_track(track))
            resp = self._sp.next(resp) if resp["next"] else None
        return results

    def resolve_album(self, album_id: str) -> list[str]:
        if not self._sp:
            return []
        results: list[str] = []
        resp = self._sp.album_tracks(album_id)
        while resp:
            for track in resp["items"]:
                artists = ", ".join(a["name"] for a in track["artists"])
                results.append(f"{artists} - {track['name']}")
            resp = self._sp.next(resp) if resp["next"] else None
        return results
