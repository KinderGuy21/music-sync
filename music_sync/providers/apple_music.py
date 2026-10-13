"""Apple Music via the Apple Music API (api.music.apple.com).

Apple has no classic OAuth. It needs two tokens:
  * a developer token: an ES256 JWT signed with a MusicKit private key (.p8)
    from an Apple Developer account (APPLE_TEAM_ID, APPLE_KEY_ID,
    APPLE_PRIVATE_KEY_PATH) - or a ready-made one in APPLE_DEVELOPER_TOKEN;
  * a Music User Token: obtained when the user signs in through MusicKit JS
    in the browser (the web UI's "Connect Apple Music" page does this).

The public API can create library playlists and append tracks, but it cannot
remove tracks or rename playlists, so Apple Music is add-only as a target.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import requests

from ..config import Settings
from ..models import Playlist, Track
from .base import NotAuthenticated, Provider, ProviderError

API = "https://api.music.apple.com"
TOKEN_LIFETIME = 60 * 60 * 24 * 30  # Apple allows up to ~180 days


class AppleMusicProvider(Provider):
    name = "apple"
    display_name = "Apple Music"
    supports_removal = False
    supports_isrc_lookup = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self._dev_token: tuple[str, float] | None = None
        self._storefront: str | None = settings.apple_storefront or None
        self.session = requests.Session()

    # --- auth --------------------------------------------------------------

    def has_developer_credentials(self) -> bool:
        s = self.settings
        return bool(s.apple_developer_token or (s.apple_team_id and s.apple_key_id and s.apple_private_key_path))

    def developer_token(self) -> str:
        s = self.settings
        if s.apple_developer_token:
            return s.apple_developer_token
        if not self.has_developer_credentials():
            raise NotAuthenticated(
                "Set APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_PRIVATE_KEY_PATH "
                "(a MusicKit key from developer.apple.com), or APPLE_DEVELOPER_TOKEN"
            )
        now = time.time()
        if self._dev_token and self._dev_token[1] - now > 3600:
            return self._dev_token[0]
        key = Path(s.apple_private_key_path).expanduser().read_text()
        expires = int(now) + TOKEN_LIFETIME
        token = jwt.encode(
            {"iss": s.apple_team_id, "iat": int(now), "exp": expires},
            key,
            algorithm="ES256",
            headers={"kid": s.apple_key_id},
        )
        self._dev_token = (token, expires)
        return token

    def user_token(self) -> str:
        path = self.settings.apple_user_token_file
        if not path.exists():
            raise NotAuthenticated("Apple Music is not connected. Run: music-sync auth apple")
        return path.read_text().strip()

    def save_user_token(self, token: str) -> None:
        path = self.settings.apple_user_token_file
        path.write_text(token.strip())
        path.chmod(0o600)

    def is_authenticated(self) -> bool:
        return self.settings.apple_user_token_file.exists() and self.has_developer_credentials()

    # --- HTTP --------------------------------------------------------------

    def _request(self, method: str, path: str, *, user: bool = True, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.developer_token()}"}
        if user:
            headers["Music-User-Token"] = self.user_token()
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(5):
            response = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
            if response.status_code == 429:
                time.sleep(float(response.headers.get("Retry-After", 2 ** attempt)))
                continue
            break
        if response.status_code == 401 or response.status_code == 403:
            raise NotAuthenticated(
                f"Apple Music rejected the tokens ({response.status_code}). "
                "Reconnect with: music-sync auth apple"
            )
        if response.status_code == 404:
            return {"data": []}
        if response.status_code >= 400:
            raise ProviderError(f"Apple Music {method} {path} failed: {response.status_code} {response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    def _paged(self, path: str, params: dict | None = None):
        page = self._request("GET", path, params=params)
        while True:
            yield from page.get("data", [])
            nxt = page.get("next")
            if not nxt:
                return
            page = self._request("GET", nxt)

    @property
    def storefront(self) -> str:
        if not self._storefront:
            data = self._request("GET", "/v1/me/storefront").get("data") or [{"id": "us"}]
            self._storefront = data[0]["id"]
        return self._storefront

    # --- reading -----------------------------------------------------------

    @staticmethod
    def _catalog_to_track(song: dict) -> Track:
        a = song.get("attributes", {})
        return Track(
            id=song["id"],
            title=a.get("name", ""),
            artists=[a["artistName"]] if a.get("artistName") else [],
            album=a.get("albumName"),
            duration_ms=a.get("durationInMillis"),
            isrc=a.get("isrc"),
            service="apple",
        )

    @staticmethod
    def _library_to_track(song: dict) -> Track:
        a = song.get("attributes", {})
        catalog = ((song.get("relationships") or {}).get("catalog") or {}).get("data") or []
        catalog_attrs = catalog[0].get("attributes", {}) if catalog else {}
        catalog_id = (catalog[0]["id"] if catalog else None) or (a.get("playParams") or {}).get("catalogId")
        return Track(
            # Use the catalog id where there is one: that's what playlists accept and search returns.
            id=catalog_id or song["id"],
            title=a.get("name", ""),
            artists=[a["artistName"]] if a.get("artistName") else [],
            album=a.get("albumName"),
            duration_ms=a.get("durationInMillis"),
            isrc=catalog_attrs.get("isrc"),
            service="apple",
            extra={"library_id": song["id"], "in_catalog": bool(catalog_id)},
        )

    def list_playlists(self) -> list[Playlist]:
        out = []
        for p in self._paged("/v1/me/library/playlists", {"limit": 100}):
            a = p.get("attributes", {})
            out.append(Playlist(
                id=p["id"],
                name=a.get("name", ""),
                description=(a.get("description") or {}).get("standard", ""),
                service=self.name,
            ))
        return out

    def get_tracks(self, playlist_id: str) -> list[Track]:
        return [
            self._library_to_track(s)
            for s in self._paged(f"/v1/me/library/playlists/{playlist_id}/tracks",
                                 {"limit": 100, "include": "catalog"})
            if s.get("type") in ("library-songs", "songs")
        ]

    def search(self, query: str, limit: int = 10) -> list[Track]:
        data = self._request(
            "GET", f"/v1/catalog/{self.storefront}/search",
            params={"term": query, "types": "songs", "limit": min(limit, 25)},
        )
        songs = ((data.get("results") or {}).get("songs") or {}).get("data") or []
        return [self._catalog_to_track(s) for s in songs]

    def lookup_isrc(self, isrc: str) -> Track | None:
        data = self._request("GET", f"/v1/catalog/{self.storefront}/songs", params={"filter[isrc]": isrc})
        songs = data.get("data") or []
        return self._catalog_to_track(songs[0]) if songs else None

    # --- writing -----------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        body = {"attributes": {"name": name, "description": description or ""}}
        data = self._request("POST", "/v1/me/library/playlists", json=body)
        return data["data"][0]["id"]

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        for i in range(0, len(track_ids), 100):
            body = {"data": [{"id": t, "type": "songs"} for t in track_ids[i:i + 100]]}
            self._request("POST", f"/v1/me/library/playlists/{playlist_id}/tracks", json=body)
