"""Localization manager for Essusic.

Usage:
    from music.i18n import t
    msg = t("nothing_playing", locale)
    msg = t("queued_track", locale, title="Never Gonna Give You Up", pos=3)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_locales: dict[str, dict[str, str]] = {}
_LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"


def load_locales() -> None:
    """Load all locale JSON files from the locales/ directory."""
    _locales.clear()
    if not _LOCALE_DIR.is_dir():
        log.warning("Locales directory not found: %s", _LOCALE_DIR)
        return
    for path in _LOCALE_DIR.glob("*.json"):
        lang = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _locales[lang] = data
            log.info("Loaded locale: %s (%d keys)", lang, len(data))
        except Exception as exc:
            log.warning("Failed to load locale %s: %s", lang, exc)


def available_locales() -> list[str]:
    return sorted(_locales.keys())


def t(key: str, locale: str = "en", **kwargs) -> str:
    """Translate a key for the given locale.

    Falls back to English, then to the raw key.
    Supports {variable} substitution via kwargs.
    """
    strings = _locales.get(locale) or _locales.get("en") or {}
    template = strings.get(key)
    if template is None:
        # Fall back to English
        en = _locales.get("en", {})
        template = en.get(key, key)
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError):
        return template
