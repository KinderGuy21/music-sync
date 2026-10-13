"""Reading public Spotify playlists without anyone signing in to Spotify.

Two ways, tried in order:

1. App-only auth: the Client Credentials flow (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET,
   no user, no redirect URI, no browser) and GET /v1/playlists/{id}. Under Spotify's
   February 2026 rules a development-mode app only gets the *contents* of playlists the
   caller owns, and an app token owns nothing, so most apps get just the name this way;
   apps with extended quota get every track (with ISRCs).
2. The public embed page (open.spotify.com/embed/playlist/{id}): the list anyone sees
   when they open the link. Needs no keys at all, but only carries the first 100 songs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

from ..models import Playlist, Track
from .base import ProviderError

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"
PAGE_URL = "https://open.spotify.com/playlist/{id}"
EMBED_LIMIT = 100
# A minimal user agent gets the server-rendered page (with the song count); a full
# desktop-browser one gets a JavaScript shell without it.
HEADERS = {"User-Agent": "Mozilla/5.0"}

_ID = r"[0-9A-Za-z]{22}"
_LINK = re.compile(rf"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?playlist/|spotify:playlist:)({_ID})")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_SONG_COUNT = re.compile(r'<meta name="music:song_count" content="(\d+)"')


def parse_playlist_id(ref: str) -> str | None:
    """The playlist id in a Spotify link (``?si=...`` and all), URI or bare id; else None."""
    ref = ref.strip()
    m = _LINK.search(ref)
    if m:
        return m.group(1)
    return ref if re.fullmatch(_ID, ref) else None


class PublicPlaylistUnavailable(ProviderError):
    pass


@dataclass
class PublicPlaylist:
    playlist: Playlist
    tracks: list[Track]
    notices: list[str] = field(default_factory=list)


class PublicSpotifyReader:
    def __init__(self, client_id: str = "", client_secret: str = "", http: requests.Session | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or requests.Session()
        self._token: str | None = None

    def read(self, playlist_id: str) -> PublicPlaylist:
        notices: list[str] = []
        meta: dict | None = None
        if self.client_id and self.client_secret:
            try:
                meta = self._api_get(f"{API_URL}/playlists/{playlist_id}")
            except _ApiError as exc:
                if exc.status != 404:
                    notices.append(f"Spotify's API refused app-only access ({exc}); used the public web page")
            except requests.RequestException as exc:
                notices.append(f"Couldn't reach Spotify's API ({exc}); used the public web page")
            if meta is not None:
                contents = meta.get("items") or meta.get("tracks")
                if isinstance(contents, dict) and "items" in contents:
                    return PublicPlaylist(self._playlist(playlist_id, meta, contents.get("total")),
                                          self._api_tracks(contents), notices)
                # Feb 2026 dev-mode rule: metadata only for playlists the caller doesn't own.

        embed = self._embed(playlist_id)
        if embed is None:
            raise PublicPlaylistUnavailable(
                f"Spotify playlist {playlist_id} can't be read without signing in: it is private or "
                "doesn't exist. Ask its owner to make it public, or run `music-sync auth spotify` "
                "with an account that owns or collaborates on it.")
        tracks = [t for t in (self._embed_track(e) for e in embed.get("trackList") or []) if t]
        total = None
        if len(embed.get("trackList") or []) >= EMBED_LIMIT:
            total = self._song_count(playlist_id)
            if total is None or total > len(tracks):
                notices.append(
                    f"Without a Spotify login only the first {EMBED_LIMIT} songs of a public playlist "
                    f"can be read{f' (it has {total})' if total else ''}; the rest were skipped")
        name = (meta or {}).get("name") or embed.get("name") or embed.get("title") or playlist_id
        return PublicPlaylist(
            Playlist(id=playlist_id, name=name, service="spotify",
                     description=(meta or {}).get("description") or "",
                     track_count=total or len(tracks)),
            tracks, notices)

    # --- app-only Web API ---------------------------------------------------

    def _token_value(self) -> str:
        if self._token is None:
            res = self.http.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                                 auth=(self.client_id, self.client_secret), timeout=20)
            if res.status_code != 200:
                raise _ApiError(res.status_code, "app keys rejected")
            self._token = res.json()["access_token"]
        return self._token

    def _api_get(self, url: str) -> dict:
        res = self.http.get(url, headers={"Authorization": f"Bearer {self._token_value()}"}, timeout=20)
        if res.status_code != 200:
            raise _ApiError(res.status_code, f"HTTP {res.status_code}")
        return res.json()

    def _api_tracks(self, page: dict) -> list[Track]:
        tracks = []
        while page:
            for entry in page.get("items") or []:
                track = api_track((entry or {}).get("item") or (entry or {}).get("track"))
                if track:
                    tracks.append(track)
            page = self._api_get(page["next"]) if page.get("next") else None
        return tracks

    @staticmethod
    def _playlist(playlist_id: str, meta: dict, total: int | None) -> Playlist:
        return Playlist(id=playlist_id, name=meta.get("name") or playlist_id, service="spotify",
                        description=meta.get("description") or "", track_count=total)

    # --- public web page ----------------------------------------------------

    def _embed(self, playlist_id: str) -> dict | None:
        try:
            res = self.http.get(EMBED_URL.format(id=playlist_id), headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            raise ProviderError(f"Couldn't reach open.spotify.com: {exc}") from exc
        if res.status_code != 200:
            return None
        m = _NEXT_DATA.search(res.text)
        if not m:
            raise ProviderError("Spotify's public playlist page has changed and can't be read; "
                                "run `music-sync auth spotify` instead")
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        entity = (((props.get("state") or {}).get("data") or {}).get("entity")) or {}
        return entity if entity.get("type") == "playlist" else None

    def _song_count(self, playlist_id: str) -> int | None:
        try:
            res = self.http.get(PAGE_URL.format(id=playlist_id), headers=HEADERS, timeout=20)
        except requests.RequestException:
            return None
        m = _SONG_COUNT.search(res.text) if res.status_code == 200 else None
        return int(m.group(1)) if m else None

    @staticmethod
    def _embed_track(entry: dict) -> Track | None:
        uri = entry.get("uri") or ""
        if entry.get("entityType", "track") != "track" or not uri.startswith("spotify:track:"):
            return None  # podcast episodes, local files
        # Artists are joined with ",<nbsp>", so names that contain commas survive the split.
        artists = [a.strip() for a in (entry.get("subtitle") or "").split(",\xa0") if a.strip()]
        return Track(id=uri.rsplit(":", 1)[1], title=entry.get("title") or "", artists=artists,
                     duration_ms=entry.get("duration"), service="spotify")


class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def api_track(item: dict | None) -> Track | None:
    """A Web API track object as a Track; None for episodes, local files and removed tracks."""
    if not item or item.get("type", "track") != "track" or not item.get("id"):
        return None
    return Track(
        id=item["id"],
        title=item.get("name", ""),
        artists=[a["name"] for a in item.get("artists", [])],
        album=(item.get("album") or {}).get("name"),
        duration_ms=item.get("duration_ms"),
        isrc=(item.get("external_ids") or {}).get("isrc"),
        service="spotify",
    )
