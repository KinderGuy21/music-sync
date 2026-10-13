"""Service-agnostic data types shared by every provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Track:
    """A song as seen by one service.

    ``id`` is whatever that service needs to add the song to a playlist
    (Spotify track id, YouTube videoId, Apple Music catalog song id).
    """

    id: str
    title: str
    artists: list[str]
    service: str
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    # Provider-specific data needed for later operations (e.g. YouTube's setVideoId).
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def label(self) -> str:
        artists = ", ".join(self.artists) or "Unknown artist"
        return f"{artists} - {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "service": self.service,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "isrc": self.isrc,
        }


@dataclass
class Playlist:
    id: str
    name: str
    service: str
    description: str = ""
    track_count: int | None = None
    # False when the service won't return the contents (e.g. Spotify playlists you don't own).
    readable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "service": self.service,
            "description": self.description,
            "track_count": self.track_count,
            "readable": self.readable,
        }
