from __future__ import annotations

import logging
import os

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from .audio_source import TrackInfo

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

    def _track_to_info(self, track: dict) -> TrackInfo:
        title = self._format_track(track)
        return TrackInfo(
            title=title,
            url=f"ytsearch:{title}",
            duration=track.get("duration_ms", 0) // 1000,
            artist=", ".join(a["name"] for a in track.get("artists", [])),
        )

    def search(self, query: str, limit: int = 5) -> list[TrackInfo]:
        """Search Spotify for tracks and return TrackInfo results."""
        if not self._sp:
            return []
        results = self._sp.search(q=query, type="track", limit=limit)
        tracks: list[TrackInfo] = []
        for item in results.get("tracks", {}).get("items", []):
            artist_names = ", ".join(a["name"] for a in item["artists"])
            title = f"{artist_names} - {item['name']}"
            duration_ms = item.get("duration_ms", 0)
            tracks.append(
                TrackInfo(
                    title=title,
                    url=f"ytsearch:{title}",
                    duration=duration_ms // 1000,
                )
            )
        return tracks

    def recommend(self, query: str) -> TrackInfo | None:
        """Search Spotify for a track matching query, then return a recommendation."""
        if not self._sp:
            return None
        # Find a seed track
        results = self._sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return None
        seed_id = items[0]["id"]
        # Get recommendations
        recs = self._sp.recommendations(seed_tracks=[seed_id], limit=5)
        rec_tracks = recs.get("tracks", [])
        if not rec_tracks:
            return None
        # Pick first that isn't the seed
        for track in rec_tracks:
            if track["id"] != seed_id:
                title = self._format_track(track)
                return TrackInfo(
                    title=title,
                    url=f"ytsearch:{title}",
                    duration=track.get("duration_ms", 0) // 1000,
                )
        return None

    def recommend_multiple(self, query: str, limit: int = 5) -> list[TrackInfo]:
        """Search Spotify for a seed track, return multiple recommendations."""
        if not self._sp:
            return []
        results = self._sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return []
        seed_id = items[0]["id"]
        recs = self._sp.recommendations(seed_tracks=[seed_id], limit=limit)
        out: list[TrackInfo] = []
        for track in recs.get("tracks", []):
            if track["id"] != seed_id:
                out.append(self._track_to_info(track))
        return out

    def recommend_by_seed(
        self, seed: str, exclude_ids: set[str] | None = None, limit: int = 5
    ) -> list[tuple[str, TrackInfo]]:
        """Get recommendations seeded by artist/genre/track name.

        Returns list of (spotify_track_id, TrackInfo) for de-duplication.
        Tries artist seed first, then genre, then track.
        """
        if not self._sp:
            return []
        exclude_ids = exclude_ids or set()

        # Try to find an artist
        artist_results = self._sp.search(q=seed, type="artist", limit=1)
        artist_items = artist_results.get("artists", {}).get("items", [])

        # Try to find a track as fallback
        track_results = self._sp.search(q=seed, type="track", limit=1)
        track_items = track_results.get("tracks", {}).get("items", [])

        kwargs: dict = {"limit": limit + len(exclude_ids)}
        if artist_items:
            kwargs["seed_artists"] = [artist_items[0]["id"]]
        elif track_items:
            kwargs["seed_tracks"] = [track_items[0]["id"]]
        else:
            # Try as genre seed
            kwargs["seed_genres"] = [seed.lower()]

        try:
            recs = self._sp.recommendations(**kwargs)
        except Exception as exc:
            log.warning("Spotify recommendations failed: %s", exc)
            return []

        out: list[tuple[str, TrackInfo]] = []
        for track in recs.get("tracks", []):
            tid = track["id"]
            if tid in exclude_ids:
                continue
            out.append((tid, self._track_to_info(track)))
            if len(out) >= limit:
                break
        return out

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
