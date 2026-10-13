"""The interface every music service implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Playlist, Track


class ProviderError(RuntimeError):
    pass


class NotAuthenticated(ProviderError):
    pass


class Provider(ABC):
    #: Short id used on the command line and in state ("spotify", "youtube", "apple").
    name: str = ""
    display_name: str = ""
    #: Whether tracks can be removed from a playlist through the API.
    supports_removal: bool = True
    #: Whether search results carry ISRCs, so matching can be exact.
    supports_isrc_lookup: bool = False
    #: Whether a share link opens a playlist without signing in (public Spotify playlists).
    reads_public_links: bool = False

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True if stored credentials exist (they may still need a refresh)."""

    @abstractmethod
    def list_playlists(self) -> list[Playlist]: ...

    @abstractmethod
    def get_tracks(self, playlist_id: str) -> list[Track]: ...

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[Track]: ...

    def lookup_isrc(self, isrc: str) -> Track | None:
        return None

    @abstractmethod
    def create_playlist(self, name: str, description: str = "") -> str:
        """Create a playlist and return its id."""

    @abstractmethod
    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None: ...

    def remove_tracks(self, playlist_id: str, tracks: list[Track]) -> None:
        raise ProviderError(f"{self.display_name} does not allow removing tracks from playlists via its API")

    def parse_playlist_ref(self, ref: str) -> str | None:
        """The playlist id in a share link or URI for this service, or None if ``ref`` isn't one."""
        return None

    def take_notices(self) -> list[str]:
        """Warnings gathered while reading (e.g. a playlist only partly readable); clears them."""
        return []

    def get_playlist(self, playlist_id: str) -> Playlist | None:
        for playlist in self.list_playlists():
            if playlist.id == playlist_id:
                return playlist
        return None

    def find_playlist_by_name(self, name: str) -> Playlist | None:
        wanted = name.strip().lower()
        for playlist in self.list_playlists():
            if playlist.name.strip().lower() == wanted:
                return playlist
        return None
