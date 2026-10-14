"""Reading public Spotify playlists without anyone signing in to Spotify.

Three ways, tried in order:

1. App-only auth: the Client Credentials flow (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET,
   no user, no redirect URI, no browser) and GET /v1/playlists/{id}. Under Spotify's
   February 2026 rules a development-mode app only gets the *contents* of playlists the
   caller owns, and an app token owns nothing, so most apps get just the name this way;
   apps with extended quota get every track (with ISRCs).
2. The web player's anonymous access: the guest token open.spotify.com gets for a visitor
   who isn't logged in, used the way the web player uses it, 100 songs a page until the
   playlist ends. Needs no keys and has no song limit, but is unofficial (see below).
3. The public embed page (open.spotify.com/embed/playlist/{id}): the list anyone sees
   when they open the link. Needs no keys at all, but only carries the first 100 songs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
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
# ...and the desktop web player is what the anonymous route needs.
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
SERVER_TIME_URL = "https://open.spotify.com/api/server-time"
WEB_TOKEN_URL = "https://open.spotify.com/api/token"
CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
PAGE_SIZE = 100

_ID = r"[0-9A-Za-z]{22}"
_LINK = re.compile(rf"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?playlist/|spotify:playlist:)({_ID})")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_SONG_COUNT = re.compile(r'<meta name="music:song_count" content="(\d+)"')
_BUNDLE = re.compile(r'https://[^"\']+/web-player/web-player\.[0-9a-f]+\.js')
_APP_CONFIG = re.compile(r'<script id="appServerConfig" type="text/plain">([^<]+)</script>')
# The newest TOTP secret comes first in the bundle's list, e.g. {secret:'...',version:61}.
_SECRET = re.compile(r"""secret:(['"])((?:\\.|(?!\1).)*)\1,version:(\d+)""")
_QUERY_HASH = re.compile(r'"fetchPlaylist","query","([0-9a-f]{64})"')


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
        self._web: dict | None = None

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

        anonymous_failure = ""
        try:
            anonymous = self._anonymous(playlist_id)
        except (_AnonymousUnavailable, requests.RequestException, ValueError, KeyError, TypeError) as exc:
            anonymous, anonymous_failure = None, str(exc) or type(exc).__name__
        if anonymous is not None:
            info, tracks, total = anonymous
            return PublicPlaylist(
                Playlist(id=playlist_id, name=(meta or {}).get("name") or info.get("name") or playlist_id,
                         service="spotify",
                         description=(meta or {}).get("description") or info.get("description") or "",
                         track_count=total if total is not None else len(tracks)),
                tracks, notices)

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
                why = f" (Spotify's web-player route failed: {anonymous_failure})" if anonymous_failure else ""
                notices.append(
                    f"Without a Spotify login only the first {EMBED_LIMIT} songs of this public playlist "
                    f"could be read{f' (it has {total})' if total else ''}; the rest were skipped{why}")
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

    def _api_get(self, url: str, token: str | None = None) -> dict:
        res = self.http.get(url, headers={"Authorization": f"Bearer {token or self._token_value()}"}, timeout=20)
        if res.status_code != 200:
            raise _ApiError(res.status_code, f"HTTP {res.status_code}")
        return res.json()

    def _api_tracks(self, page: dict, token: str | None = None) -> list[Track]:
        tracks = []
        while page:
            for entry in page.get("items") or []:
                track = api_track((entry or {}).get("item") or (entry or {}).get("track"))
                if track:
                    tracks.append(track)
            page = self._api_get(page["next"], token) if page.get("next") else None
        return tracks

    # --- the web player's anonymous access ---------------------------------------
    #
    # UNOFFICIAL AND UNDOCUMENTED. This copies what open.spotify.com's own web player does for
    # a visitor who isn't logged in (checked October 2026):
    #   1. its JavaScript bundle carries a TOTP secret; with it, GET /api/token?totp=...
    #      returns an anonymous bearer token (isAnonymous: true);
    #   2. POST clienttoken.spotify.com returns the "client-token" its API calls also send;
    #   3. the tracks come from its GraphQL endpoint (api-partner.spotify.com/pathfinder,
    #      the persisted "fetchPlaylist" query), 100 per page via offset/limit.
    # The standard Web API (GET /v1/playlists/{id}) is tried first with the same token, in case
    # Spotify lets it through again (it gives ISRCs); in October 2026 it answers 429 for these
    # tokens. Spotify can change or block any of this without notice; its token endpoint says
    # its use isn't permitted under the Developer Terms. So every failure here is caught by
    # read(), which falls back to the public embed page (first 100 songs) instead of failing.

    def _anonymous(self, playlist_id: str) -> tuple[dict, list[Track], int | None] | None:
        """(name/description, every track, song count), or None if Spotify says there's no such
        public playlist. Raises when the anonymous route itself doesn't work."""
        web = self._web_player(playlist_id)
        try:
            meta = self._api_get(f"{API_URL}/playlists/{playlist_id}", web["token"])
            contents = meta.get("items") or meta.get("tracks")
            if isinstance(contents, dict) and "items" in contents:
                return meta, self._api_tracks(contents, web["token"]), contents.get("total")
        except _ApiError:
            pass  # rate-limited or refused: the web player's own route below
        info: dict = {}
        tracks: list[Track] = []
        total: int | None = None
        offset = 0
        while total is None or offset < total:
            playlist = self._pathfinder_page(web, playlist_id, offset)
            if playlist is None:
                return None
            content = playlist.get("content") or {}
            items = content.get("items") or []
            if total is None:
                info = {"name": playlist.get("name"), "description": playlist.get("description")}
                total = int(content.get("totalCount") or 0)
            tracks += [t for t in (pathfinder_track(i) for i in items) if t]
            if not items:
                break
            offset += len(items)
        return info, tracks, total

    def _pathfinder_page(self, web: dict, playlist_id: str, offset: int) -> dict | None:
        res = self.http.post(PATHFINDER_URL, timeout=20, headers={
            **BROWSER_HEADERS, "Authorization": f"Bearer {web['token']}", "client-token": web["client_token"],
            "app-platform": "WebPlayer", "Content-Type": "application/json;charset=UTF-8"}, json={
            "operationName": "fetchPlaylist",
            "variables": {"uri": f"spotify:playlist:{playlist_id}", "offset": offset, "limit": PAGE_SIZE,
                          "enableWatchFeedEntrypoint": False},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": web["query_hash"]}}})
        if res.status_code != 200:
            raise _AnonymousUnavailable(f"playlist query answered HTTP {res.status_code}")
        playlist = ((res.json() or {}).get("data") or {}).get("playlistV2")
        if not playlist or playlist.get("__typename") != "Playlist":
            if playlist and playlist.get("__typename") == "NotFound":
                return None
            raise _AnonymousUnavailable("playlist query returned no playlist")
        return playlist

    def _web_player(self, playlist_id: str) -> dict:
        """The anonymous token, client token and query hash, fetched once per reader."""
        if self._web is not None:
            return self._web
        page = self.http.get(PAGE_URL.format(id=playlist_id), headers=BROWSER_HEADERS, timeout=20)
        bundle = _BUNDLE.search(page.text) if page.status_code == 200 else None
        if not bundle:
            raise _AnonymousUnavailable("web player page has changed")
        config = _APP_CONFIG.search(page.text)
        client_version = json.loads(base64.b64decode(config.group(1))).get("clientVersion") if config else None
        js = self.http.get(bundle.group(0), headers=BROWSER_HEADERS, timeout=30)
        secret = _SECRET.search(js.text) if js.status_code == 200 else None
        query_hash = _QUERY_HASH.search(js.text) if secret else None
        if not (secret and query_hash and client_version):
            raise _AnonymousUnavailable("web player code has changed")
        key = totp_key(secret.group(2).encode().decode("unicode_escape"))
        try:
            server_time = int(self.http.get(SERVER_TIME_URL, headers=BROWSER_HEADERS, timeout=20).json()["serverTime"])
            totp_server = totp(key, server_time)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            totp_server = "unavailable"  # what the web player itself sends when this fails
        res = self.http.get(WEB_TOKEN_URL, headers=BROWSER_HEADERS, timeout=20, params={
            "reason": "init", "productType": "web-player", "totp": totp(key, time.time()),
            "totpServer": totp_server, "totpVer": secret.group(3)})
        body = res.json() if res.status_code == 200 else {}
        if not body.get("accessToken"):
            raise _AnonymousUnavailable(f"anonymous token refused (HTTP {res.status_code})")
        res = self.http.post(CLIENT_TOKEN_URL, headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=20, json={
            "client_data": {"client_version": client_version, "client_id": body["clientId"], "js_sdk_data": {
                "device_brand": "unknown", "device_model": "unknown", "os": "macos", "os_version": "10.15.7",
                "device_id": hashlib.md5(str(time.time()).encode()).hexdigest(), "device_type": "computer"}}})
        client_token = ((res.json() if res.status_code == 200 else {}).get("granted_token") or {}).get("token")
        if not client_token:
            raise _AnonymousUnavailable(f"client token refused (HTTP {res.status_code})")
        self._web = {"token": body["accessToken"], "client_token": client_token,
                     "query_hash": query_hash.group(1)}
        return self._web

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


class _AnonymousUnavailable(Exception):
    pass


def totp_key(secret: str) -> bytes:
    """The web player's TOTP key from the obfuscated secret in its bundle (its own transform)."""
    return "".join(str(ord(c) ^ (i % 33 + 9)) for i, c in enumerate(secret)).encode()


def totp(key: bytes, timestamp: float) -> str:
    """RFC 6238: HMAC-SHA1, 30-second steps, 6 digits."""
    digest = hmac.new(key, struct.pack(">Q", int(timestamp // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**6).zfill(6)


def pathfinder_track(entry: dict | None) -> Track | None:
    """A web-player playlist item as a Track; None for episodes, local files and removed tracks."""
    data = (((entry or {}).get("itemV2") or {}).get("data")) or {}
    uri = data.get("uri") or ""
    if data.get("__typename") != "Track" or not uri.startswith("spotify:track:"):
        return None
    return Track(
        id=uri.rsplit(":", 1)[1],
        title=data.get("name") or "",
        artists=[(a.get("profile") or {}).get("name", "") for a in (data.get("artists") or {}).get("items") or []],
        album=(data.get("albumOfTrack") or {}).get("name"),
        duration_ms=(data.get("trackDuration") or {}).get("totalMilliseconds"),
        service="spotify",
    )


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
