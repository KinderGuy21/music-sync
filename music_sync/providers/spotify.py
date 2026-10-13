"""Spotify via the Web API (spotipy), OAuth authorization-code flow.

Uses the post-February-2026 endpoints (/me/playlists, /playlists/{id}/items),
which spotipy >= 2.26 calls. Spotify only returns the contents of playlists
you own or collaborate on; other public playlists, and every playlist when
nobody has signed in, are read through spotify_public (no login needed).
"""

from __future__ import annotations

import requests
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth

from ..config import Settings
from ..models import Playlist, Track
from .base import NotAuthenticated, Provider, ProviderError
from .spotify_public import PublicPlaylist, PublicSpotifyReader, api_track, parse_playlist_id

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
])


class SpotifyProvider(Provider):
    name = "spotify"
    display_name = "Spotify"
    supports_removal = True
    supports_isrc_lookup = True
    reads_public_links = True

    def __init__(self, settings: Settings, http: requests.Session | None = None):
        self.settings = settings
        self._client: spotipy.Spotify | None = None
        self._me: dict | None = None
        self._reader = PublicSpotifyReader(settings.spotify_client_id, settings.spotify_client_secret, http)
        self._public: dict[str, PublicPlaylist] = {}
        self._notices: list[str] = []

    # --- auth --------------------------------------------------------------

    def oauth(self, open_browser: bool = True) -> SpotifyOAuth:
        s = self.settings
        if not (s.spotify_client_id and s.spotify_client_secret):
            raise NotAuthenticated("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET (see README)")
        return SpotifyOAuth(
            client_id=s.spotify_client_id,
            client_secret=s.spotify_client_secret,
            redirect_uri=s.spotify_redirect_uri,
            scope=SCOPES,
            cache_handler=CacheFileHandler(cache_path=str(s.spotify_cache)),
            open_browser=open_browser,
        )

    def is_authenticated(self) -> bool:
        return self.settings.spotify_cache.exists()

    def login_interactive(self) -> None:
        """CLI login: opens the browser and catches the redirect on a local port."""
        auth = self.oauth(open_browser=True)
        auth.get_access_token(as_dict=False)
        self._client = None

    def authorize_url(self) -> str:
        return self.oauth(open_browser=False).get_authorize_url()

    def complete_login(self, code: str) -> None:
        self.oauth(open_browser=False).get_access_token(code, as_dict=False, check_cache=False)
        self._client = None

    @property
    def client(self) -> spotipy.Spotify:
        if self._client is None:
            if not self.is_authenticated():
                raise NotAuthenticated("Spotify is not connected. Run `music-sync auth spotify`, "
                                       "or give a public playlist's link or id instead")
            self._client = spotipy.Spotify(auth_manager=self.oauth(open_browser=False), retries=3)
        return self._client

    # --- reading -----------------------------------------------------------

    _to_track = staticmethod(api_track)

    def parse_playlist_ref(self, ref: str) -> str | None:
        return parse_playlist_id(ref)

    def take_notices(self) -> list[str]:
        notices, self._notices = self._notices, []
        return notices

    def public_playlist(self, playlist_id: str) -> PublicPlaylist:
        """A public playlist read without a user login (cached for this run)."""
        if playlist_id not in self._public:
            self._public[playlist_id] = self._reader.read(playlist_id)
        return self._public[playlist_id]

    def get_playlist(self, playlist_id: str) -> Playlist | None:
        if not self.is_authenticated():
            return self.public_playlist(playlist_id).playlist
        found = super().get_playlist(playlist_id)
        if found or not parse_playlist_id(playlist_id):
            return found
        try:  # someone else's public playlist
            return self.public_playlist(playlist_id).playlist
        except ProviderError:
            return None

    def _pages(self, page: dict):
        while page:
            yield from page.get("items", [])
            page = self.client.next(page) if page.get("next") else None

    def list_playlists(self) -> list[Playlist]:
        me = self._me or self.client.me()
        self._me = me
        out = []
        for p in self._pages(self.client.current_user_playlists(limit=50)):
            if not p:
                continue
            owned = (p.get("owner") or {}).get("id") == me["id"] or p.get("collaborative")
            counts = p.get("items") or p.get("tracks") or {}
            out.append(Playlist(
                id=p["id"],
                name=p.get("name") or "",
                description=p.get("description") or "",
                track_count=counts.get("total"),
                service=self.name,
                # Public playlists you don't own are read the no-login way (first 100 songs).
                readable=bool(owned or p.get("public")),
            ))
        return out

    def get_tracks(self, playlist_id: str) -> list[Track]:
        if not self.is_authenticated():
            return self._public_tracks(playlist_id)
        try:
            return self._own_tracks(playlist_id)
        except spotipy.SpotifyException as exc:
            if exc.http_status not in (403, 404):
                raise
            return self._public_tracks(playlist_id)  # not yours: read it like anyone else would

    def _public_tracks(self, playlist_id: str) -> list[Track]:
        public = self.public_playlist(playlist_id)
        self._notices += public.notices
        return list(public.tracks)

    def _own_tracks(self, playlist_id: str) -> list[Track]:
        tracks = []
        for entry in self._pages(self.client.playlist_items(playlist_id, limit=50, additional_types=("track",))):
            # Feb 2026 renamed entry["track"] to entry["item"]; accept both.
            track = self._to_track(entry.get("item") or entry.get("track"))
            if track:
                tracks.append(track)
        return tracks

    def search(self, query: str, limit: int = 10) -> list[Track]:
        # Spotify caps search at 10 results per request since February 2026.
        result = self.client.search(q=query, type="track", limit=min(limit, 10))
        return [t for t in (self._to_track(i) for i in result["tracks"]["items"]) if t]

    def lookup_isrc(self, isrc: str) -> Track | None:
        results = self.search(f"isrc:{isrc}", limit=1)
        return results[0] if results else None

    # --- writing -----------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        playlist = self.client.current_user_playlist_create(name, public=False, description=description)
        return playlist["id"]

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        for i in range(0, len(track_ids), 100):
            self.client.playlist_add_items(playlist_id, [f"spotify:track:{t}" for t in track_ids[i:i + 100]])

    def remove_tracks(self, playlist_id: str, tracks: list[Track]) -> None:
        ids = [f"spotify:track:{t.id}" for t in tracks]
        try:
            for i in range(0, len(ids), 100):
                self.client.playlist_remove_all_occurrences_of_items(playlist_id, ids[i:i + 100])
        except spotipy.SpotifyException as exc:
            raise ProviderError(f"Spotify refused the removal: {exc}") from exc
