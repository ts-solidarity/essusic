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

    def _get_artist_id(self, query: str) -> str | None:
        """Search for a track or artist and return an artist ID."""
        if not self._sp:
            return None
        # Try track search first (works best for "Artist - Title" queries)
        results = self._sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if items:
            return items[0]["artists"][0]["id"]
        # Fall back to artist search
        results = self._sp.search(q=query, type="artist", limit=1)
        items = results.get("artists", {}).get("items", [])
        if items:
            return items[0]["id"]
        return None

    def _related_top_tracks(
        self, artist_id: str, exclude_ids: set[str], limit: int
    ) -> list[tuple[str, TrackInfo]]:
        """Get top tracks from related artists, skipping exclude_ids."""
        try:
            related = self._sp.artist_related_artists(artist_id)
        except Exception as exc:
            log.warning("Spotify related artists failed: %s", exc)
            return []
        out: list[tuple[str, TrackInfo]] = []
        for artist in related.get("artists", []):
            if len(out) >= limit:
                break
            try:
                top = self._sp.artist_top_tracks(artist["id"])
            except Exception:
                continue
            for track in top.get("tracks", []):
                tid = track["id"]
                if tid in exclude_ids:
                    continue
                exclude_ids.add(tid)
                out.append((tid, self._track_to_info(track)))
                if len(out) >= limit:
                    break
        return out

    def recommend(self, query: str) -> TrackInfo | None:
        """Find a similar track via related artists."""
        if not self._sp:
            return None
        artist_id = self._get_artist_id(query)
        if not artist_id:
            return None
        results = self._related_top_tracks(artist_id, set(), 1)
        return results[0][1] if results else None

    def recommend_multiple(self, query: str, limit: int = 5) -> list[TrackInfo]:
        """Find similar tracks via related artists."""
        if not self._sp:
            return []
        artist_id = self._get_artist_id(query)
        if not artist_id:
            return []
        results = self._related_top_tracks(artist_id, set(), limit)
        return [info for _, info in results]

    def recommend_by_seed(
        self, seed: str, exclude_ids: set[str] | None = None, limit: int = 5
    ) -> list[tuple[str, TrackInfo]]:
        """Get similar tracks seeded by artist or track name.

        Returns list of (spotify_track_id, TrackInfo) for de-duplication.
        """
        if not self._sp:
            return []
        exclude_ids = set(exclude_ids) if exclude_ids else set()

        # Try artist search first (best for radio seed like "Radiohead")
        results = self._sp.search(q=seed, type="artist", limit=1)
        items = results.get("artists", {}).get("items", [])
        if items:
            artist_id = items[0]["id"]
        else:
            # Fall back to track search → get artist from track
            results = self._sp.search(q=seed, type="track", limit=1)
            items = results.get("tracks", {}).get("items", [])
            if not items:
                return []
            artist_id = items[0]["artists"][0]["id"]

        return self._related_top_tracks(artist_id, exclude_ids, limit)

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
