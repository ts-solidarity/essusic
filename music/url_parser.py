import re
from enum import Enum, auto


class InputType(Enum):
    YOUTUBE_URL = auto()
    YOUTUBE_PLAYLIST = auto()
    SPOTIFY_TRACK = auto()
    SPOTIFY_PLAYLIST = auto()
    SPOTIFY_ALBUM = auto()
    SEARCH_QUERY = auto()


_YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/?\S+"
)

_SPOTIFY_RE = re.compile(
    r"(?:https?://)?open\.spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)"
)


def classify(query: str) -> tuple[InputType, str]:
    """Return (InputType, cleaned_value) for a user query.

    For YouTube URLs the cleaned value is the original URL.
    For Spotify URLs it's the Spotify ID.
    For search queries it's the original string.
    """
    query = query.strip()

    m = _SPOTIFY_RE.search(query)
    if m:
        kind, spotify_id = m.group(1), m.group(2)
        type_map = {
            "track": InputType.SPOTIFY_TRACK,
            "playlist": InputType.SPOTIFY_PLAYLIST,
            "album": InputType.SPOTIFY_ALBUM,
        }
        return type_map[kind], spotify_id

    if _YOUTUBE_RE.match(query):
        if "list=" in query:
            return InputType.YOUTUBE_PLAYLIST, query
        return InputType.YOUTUBE_URL, query

    return InputType.SEARCH_QUERY, query
