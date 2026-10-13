"""An in-memory music service for exercising the sync engine without network calls."""

from __future__ import annotations

import itertools

from music_sync import matching
from music_sync.models import Playlist, Track
from music_sync.providers.base import Provider


class FakeProvider(Provider):
    _ids = itertools.count(1)

    def __init__(self, name: str, catalogue: list[Track], *, removal: bool = True, isrc: bool = False):
        self.name = name
        self.display_name = name.title()
        self.supports_removal = removal
        self.supports_isrc_lookup = isrc
        self.catalogue = {t.id: t for t in catalogue}
        self.playlists: dict[str, Playlist] = {}
        self.contents: dict[str, list[str]] = {}
        self.search_calls = 0

    def is_authenticated(self) -> bool:
        return True

    def add_playlist(self, name: str, track_ids: list[str]) -> str:
        pid = f"{self.name}-pl{next(self._ids)}"
        self.playlists[pid] = Playlist(id=pid, name=name, service=self.name)
        self.contents[pid] = list(track_ids)
        return pid

    def list_playlists(self) -> list[Playlist]:
        return list(self.playlists.values())

    def get_tracks(self, playlist_id: str) -> list[Track]:
        return [self.catalogue[t] for t in self.contents[playlist_id]]

    def search(self, query: str, limit: int = 10) -> list[Track]:
        self.search_calls += 1
        q = set(matching.fold(query).split())
        scored = []
        for t in self.catalogue.values():
            words = set(matching.fold(f"{t.title} {' '.join(t.artists)}").split())
            overlap = len(q & words)
            if overlap:
                scored.append((overlap, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:limit]]

    def lookup_isrc(self, isrc: str) -> Track | None:
        return next((t for t in self.catalogue.values() if t.isrc == isrc), None)

    def create_playlist(self, name: str, description: str = "") -> str:
        return self.add_playlist(name, [])

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self.contents[playlist_id].extend(track_ids)

    def remove_tracks(self, playlist_id: str, tracks: list[Track]) -> None:
        ids = {t.id for t in tracks}
        self.contents[playlist_id] = [t for t in self.contents[playlist_id] if t not in ids]

    def names(self, playlist_id: str) -> list[str]:
        return [self.catalogue[t].title for t in self.contents[playlist_id]]


def T(service: str, id: str, title: str, artist: str, secs: int, isrc: str | None = None) -> Track:
    return Track(id=id, title=title, artists=[artist], service=service, duration_ms=secs * 1000, isrc=isrc)
