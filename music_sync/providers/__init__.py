from __future__ import annotations

from ..config import Settings
from .base import NotAuthenticated, Provider, ProviderError

SERVICES = {
    "spotify": "Spotify",
    "youtube": "YouTube Music",
    "apple": "Apple Music",
}

ALIASES = {
    "spotify": "spotify",
    "sp": "spotify",
    "youtube": "youtube",
    "youtube-music": "youtube",
    "youtubemusic": "youtube",
    "ytmusic": "youtube",
    "yt": "youtube",
    "apple": "apple",
    "apple-music": "apple",
    "applemusic": "apple",
    "am": "apple",
}


def resolve_service(name: str) -> str:
    key = name.strip().lower().replace(" ", "-").replace("_", "-")
    if key not in ALIASES:
        raise ValueError(f"Unknown service '{name}'. Choose one of: {', '.join(SERVICES)}")
    return ALIASES[key]


def get_provider(name: str, settings: Settings) -> Provider:
    service = resolve_service(name)
    if service == "spotify":
        from .spotify import SpotifyProvider
        return SpotifyProvider(settings)
    if service == "youtube":
        from .youtube_music import YouTubeMusicProvider
        return YouTubeMusicProvider(settings)
    from .apple_music import AppleMusicProvider
    return AppleMusicProvider(settings)


__all__ = ["SERVICES", "NotAuthenticated", "Provider", "ProviderError", "get_provider", "resolve_service"]
