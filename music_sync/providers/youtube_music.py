"""YouTube Music via ytmusicapi.

Two ways to log in:
  * OAuth (Google device flow) - needs a Google Cloud OAuth client of type
    "TVs and Limited Input devices" (YTMUSIC_CLIENT_ID / YTMUSIC_CLIENT_SECRET).
  * Browser headers - paste the request headers of a logged-in
    music.youtube.com request. ytmusicapi's fallback when OAuth misbehaves.
"""

from __future__ import annotations

import json

import ytmusicapi
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.auth.oauth.token import RefreshingToken

from ..config import Settings
from ..models import Playlist, Track
from .base import NotAuthenticated, Provider, ProviderError


class YouTubeMusicProvider(Provider):
    name = "youtube"
    display_name = "YouTube Music"
    supports_removal = True
    supports_isrc_lookup = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: YTMusic | None = None

    # --- auth --------------------------------------------------------------

    def _credentials(self) -> OAuthCredentials:
        s = self.settings
        if not (s.ytmusic_client_id and s.ytmusic_client_secret):
            raise NotAuthenticated(
                "Set YTMUSIC_CLIENT_ID and YTMUSIC_CLIENT_SECRET for OAuth, "
                "or log in with browser headers: music-sync auth youtube --browser"
            )
        return OAuthCredentials(s.ytmusic_client_id, s.ytmusic_client_secret)

    def is_authenticated(self) -> bool:
        return self.settings.ytmusic_oauth_file.exists() or self.settings.ytmusic_browser_file.exists()

    def start_device_login(self) -> dict:
        """Begin the OAuth device flow. Returns verification_url, user_code, device_code."""
        code = self._credentials().get_code()
        code["url"] = f"{code['verification_url']}?user_code={code['user_code']}"
        return code

    def finish_device_login(self, device_code: str) -> None:
        """Exchange an approved device code for tokens and store them."""
        credentials = self._credentials()
        raw = credentials.token_from_code(device_code)
        if "access_token" not in raw:
            raise ProviderError(f"Google has not approved the login yet: {raw.get('error', raw)}")
        token = RefreshingToken(
            credentials=credentials,
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            scope=raw["scope"],
            token_type=raw["token_type"],
            expires_in=raw.get("refresh_token_expires_in", raw["expires_in"]),
        )
        token.update(raw)
        token.store_token(str(self.settings.ytmusic_oauth_file))
        self._client = None

    def login_with_headers(self, headers_raw: str) -> None:
        ytmusicapi.setup(filepath=str(self.settings.ytmusic_browser_file), headers_raw=headers_raw)
        self._client = None

    @property
    def client(self) -> YTMusic:
        if self._client is None:
            s = self.settings
            if s.ytmusic_oauth_file.exists():
                self._client = YTMusic(str(s.ytmusic_oauth_file), oauth_credentials=self._credentials())
            elif s.ytmusic_browser_file.exists():
                self._client = YTMusic(str(s.ytmusic_browser_file))
            else:
                raise NotAuthenticated("YouTube Music is not connected. Run: music-sync auth youtube")
        return self._client

    # --- reading -----------------------------------------------------------

    @staticmethod
    def _to_track(item: dict) -> Track | None:
        if not item or not item.get("videoId"):
            return None
        duration = item.get("duration_seconds")
        extra = {}
        if item.get("setVideoId"):
            extra["setVideoId"] = item["setVideoId"]
        return Track(
            id=item["videoId"],
            title=item.get("title") or "",
            artists=[a["name"] for a in (item.get("artists") or []) if a.get("name")],
            album=(item.get("album") or {}).get("name"),
            duration_ms=int(duration * 1000) if duration else None,
            service="youtube",
            extra=extra,
        )

    def list_playlists(self) -> list[Playlist]:
        out = []
        for p in self.client.get_library_playlists(limit=None):
            count = p.get("count")
            try:
                count = int(str(count).replace(",", "").split()[0]) if count is not None else None
            except ValueError:
                count = None
            out.append(Playlist(
                id=p["playlistId"],
                name=p.get("title") or "",
                description=p.get("description") or "",
                track_count=count,
                service=self.name,
            ))
        return out

    def get_tracks(self, playlist_id: str) -> list[Track]:
        playlist = self.client.get_playlist(playlist_id, limit=None)
        return [t for t in (self._to_track(i) for i in playlist.get("tracks", [])) if t]

    def search(self, query: str, limit: int = 10) -> list[Track]:
        results = self.client.search(query, filter="songs", limit=limit)
        tracks = [t for t in (self._to_track(r) for r in results) if t]
        if not tracks:
            # Some songs only exist as uploaded videos.
            results = self.client.search(query, filter="videos", limit=limit)
            tracks = [t for t in (self._to_track(r) for r in results) if t]
        return tracks[:limit]

    # --- writing -----------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        result = self.client.create_playlist(name, description or "", privacy_status="PRIVATE")
        if not isinstance(result, str):
            raise ProviderError(f"YouTube Music could not create the playlist: {json.dumps(result)[:300]}")
        return result

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        for i in range(0, len(track_ids), 50):
            result = self.client.add_playlist_items(playlist_id, track_ids[i:i + 50], duplicates=True)
            if isinstance(result, dict) and result.get("status") not in (None, "STATUS_SUCCEEDED"):
                raise ProviderError(f"YouTube Music refused to add tracks: {json.dumps(result)[:300]}")

    def remove_tracks(self, playlist_id: str, tracks: list[Track]) -> None:
        videos = [{"videoId": t.id, "setVideoId": t.extra["setVideoId"]}
                  for t in tracks if t.extra.get("setVideoId")]
        if videos:
            self.client.remove_playlist_items(playlist_id, videos)
