import json

import pytest
from click.testing import CliRunner

from music_sync import cli
from music_sync.config import load_settings
from music_sync.providers.base import ProviderError
from music_sync.providers.spotify import SpotifyProvider
from music_sync.providers.spotify_public import PublicSpotifyReader, parse_playlist_id, totp
from music_sync.providers.youtube_music import YouTubeMusicProvider

from .fakes import FakeProvider, T

PID = "37i9dQZF1DXcBWIGoYBM5M"
API = f"https://api.spotify.com/v1/playlists/{PID}"
EMBED = f"https://open.spotify.com/embed/playlist/{PID}"
PAGE = f"https://open.spotify.com/playlist/{PID}"


class Response:
    def __init__(self, status: int = 200, body=None, text: str = ""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


class FakeHttp:
    """Answers requests from a {url: Response} table and records them."""

    def __init__(self, routes: dict[str, Response]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        route = self.routes.get(url, Response(404, {"error": "not found"}, "not found"))
        return route(**kw) if callable(route) else route

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def urls(self, method="GET"):
        return [u for m, u, _ in self.calls if m == method]


def api_item(n: int) -> dict:
    return {"item": {"type": "track", "id": f"sp{n}", "name": f"Song {n}", "duration_ms": 200000 + n,
                     "artists": [{"name": f"Artist {n}"}], "album": {"name": "Album"},
                     "external_ids": {"isrc": f"ISRC{n}"}}}


def embed_page(entries: list[dict], name: str = "Shared Mix") -> str:
    data = {"props": {"pageProps": {"state": {"data": {"entity": {
        "type": "playlist", "name": name, "id": PID, "trackList": entries}}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def embed_entry(n: int, artists: str = "") -> dict:
    return {"uri": f"spotify:track:sp{n}", "title": f"Song {n}", "subtitle": artists or f"Artist {n}",
            "duration": 200000 + n, "entityType": "track"}


TOKEN = {"https://accounts.spotify.com/api/token": Response(200, {"access_token": "app-token"})}

BUNDLE = "https://open.spotifycdn.com/cdn/build/web-player/web-player.abc123.js"
PATHFINDER = "https://api-partner.spotify.com/pathfinder/v2/query"
HASH = "8964e8eafb21aa992a7d951d256d83285c04be2105d209262901de70cb97584a"
APP_CONFIG = "eyJjbGllbnRWZXJzaW9uIjogIjEuMy42In0="  # base64 of {"clientVersion": "1.3.6"}


def pathfinder_item(n: int) -> dict:
    return {"itemV2": {"__typename": "TrackResponseWrapper", "data": {
        "__typename": "Track", "uri": f"spotify:track:sp{n}", "name": f"Song {n}",
        "artists": {"items": [{"profile": {"name": f"Artist {n}"}}]},
        "albumOfTrack": {"name": "Album"}, "trackDuration": {"totalMilliseconds": 200000 + n}}}}


def web_player(total: int, name: str = "Shared Mix") -> dict:
    """The routes of open.spotify.com's anonymous web-player access, for a playlist of ``total`` songs."""
    entries = [pathfinder_item(n) for n in range(total)]
    entries.insert(5, {"itemV2": {"__typename": "EpisodeResponseWrapper", "data": {"__typename": "Episode"}}})

    def playlist_query(json, headers, **kw):
        assert headers["Authorization"] == "Bearer anon-token" and headers["client-token"] == "client-token"
        assert json["extensions"]["persistedQuery"]["sha256Hash"] == HASH
        offset, limit = json["variables"]["offset"], json["variables"]["limit"]
        return Response(200, {"data": {"playlistV2": {"__typename": "Playlist", "name": name, "description": "",
                                                      "content": {"totalCount": len(entries),
                                                                  "items": entries[offset:offset + limit]}}}})

    return {
        PAGE: Response(200, text=f'<script src="{BUNDLE}"></script>'
                                 f'<script id="appServerConfig" type="text/plain">{APP_CONFIG}</script>'),
        BUNDLE: Response(200, text="let ej=[{secret:'abc\\'d',version:61},{secret:'old',version:60}];"
                                   f'x("fetchPlaylist","query","{HASH}",null)'),
        "https://open.spotify.com/api/server-time": Response(200, {"serverTime": 1791987630}),
        "https://open.spotify.com/api/token": Response(200, {"accessToken": "anon-token", "clientId": "web",
                                                             "isAnonymous": True}),
        "https://clienttoken.spotify.com/v1/clienttoken": Response(200, {"granted_token": {"token": "client-token"}}),
        PATHFINDER: playlist_query,
    }


# --- link parsing -------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    f"https://open.spotify.com/playlist/{PID}?si=4f8a2c9e1b7d4e3a&pt=abc",
    f"https://open.spotify.com/intl-he/playlist/{PID}?si=x",
    f"  open.spotify.com/embed/playlist/{PID}  ",
    f"spotify:playlist:{PID}",
    PID,
])
def test_spotify_links_reduce_to_the_playlist_id(ref):
    assert parse_playlist_id(ref) == PID


@pytest.mark.parametrize("ref", ["Road Trip", f"https://open.spotify.com/album/{PID}", "short"])
def test_names_and_other_links_are_not_playlist_ids(ref):
    assert parse_playlist_id(ref) is None


def test_youtube_music_link_reduces_to_list_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_SYNC_HOME", str(tmp_path))
    yt = YouTubeMusicProvider(load_settings())
    assert yt.parse_playlist_ref("https://music.youtube.com/playlist?list=PLabc-12_x&si=zz") == "PLabc-12_x"
    assert yt.parse_playlist_ref("Road Trip") is None


# --- app-only (client credentials) reading -------------------------------------

def test_client_credentials_reads_a_public_playlist_with_paging():
    page2 = f"{API}/items?offset=2"
    http = FakeHttp({
        **TOKEN,
        API: Response(200, {"name": "Shared Mix", "description": "for Ofek",
                            "items": {"items": [api_item(0), api_item(1)], "next": page2, "total": 3}}),
        page2: Response(200, {"items": [api_item(2), {"item": {"type": "episode", "id": "ep"}}], "next": None}),
    })

    result = PublicSpotifyReader("cid", "secret", http).read(PID)

    assert result.playlist.name == "Shared Mix"
    assert [t.id for t in result.tracks] == ["sp0", "sp1", "sp2"]
    assert result.tracks[0].isrc == "ISRC0" and result.tracks[0].artists == ["Artist 0"]
    assert not result.notices
    _, _, token_call = http.calls[0]
    assert token_call["data"] == {"grant_type": "client_credentials"}
    assert token_call["auth"] == ("cid", "secret")
    assert all(kw["headers"]["Authorization"] == "Bearer app-token" for m, _, kw in http.calls if m == "GET")
    assert EMBED not in http.urls()  # no need for the web page


def test_legacy_tracks_field_is_accepted():
    http = FakeHttp({**TOKEN, API: Response(200, {"name": "Old", "tracks": {
        "items": [{"track": api_item(0)["item"]}], "next": None, "total": 1}})})
    assert [t.id for t in PublicSpotifyReader("cid", "secret", http).read(PID).tracks] == ["sp0"]


def test_dev_mode_metadata_only_falls_back_to_the_public_page():
    # Feb 2026: development-mode apps get the name but no items for playlists they don't own.
    http = FakeHttp({**TOKEN,
                     API: Response(200, {"name": "Shared Mix", "description": "hi"}),
                     EMBED: Response(200, text=embed_page([embed_entry(0, "Queen,\xa0David Bowie"), embed_entry(1)]))})

    result = PublicSpotifyReader("cid", "secret", http).read(PID)

    assert result.playlist.name == "Shared Mix" and result.playlist.description == "hi"
    assert [t.id for t in result.tracks] == ["sp0", "sp1"]
    assert result.tracks[0].artists == ["Queen", "David Bowie"]


def test_without_any_keys_the_public_page_is_read():
    http = FakeHttp({EMBED: Response(200, text=embed_page([embed_entry(0), embed_entry(1, "Tyler, The Creator")]))})

    result = PublicSpotifyReader("", "", http).read(PID)

    assert http.urls("POST") == []  # no token request at all
    assert result.playlist.name == "Shared Mix"
    assert result.tracks[1].artists == ["Tyler, The Creator"]


def test_totp_matches_the_rfc_6238_test_vector():
    assert totp(b"12345678901234567890", 59) == "287082"


def test_anonymous_web_player_access_reads_every_page():
    http = FakeHttp(web_player(250))

    result = PublicSpotifyReader("", "", http).read(PID)

    assert [t.id for t in result.tracks] == [f"sp{n}" for n in range(250)]  # the episode is skipped
    assert result.tracks[120].artists == ["Artist 120"] and result.tracks[120].album == "Album"
    assert result.playlist.name == "Shared Mix" and result.playlist.track_count == 251
    assert not result.notices  # everything was read: no 100-song warning
    queries = [kw["json"]["variables"] for m, u, kw in http.calls if u == PATHFINDER]
    assert [(q["offset"], q["limit"]) for q in queries] == [(0, 100), (100, 100), (200, 100)]
    _, _, token_call = next(c for c in http.calls if c[1] == "https://open.spotify.com/api/token")
    assert token_call["params"]["totpVer"] == "61" and len(token_call["params"]["totp"]) == 6
    assert EMBED not in http.urls()


def test_anonymous_token_on_the_web_api_is_used_when_spotify_allows_it():
    http = FakeHttp({**web_player(1), API: Response(200, {"name": "Shared Mix", "items": {
        "items": [api_item(0), api_item(1)], "next": None, "total": 2}})})

    result = PublicSpotifyReader("", "", http).read(PID)

    assert [t.isrc for t in result.tracks] == ["ISRC0", "ISRC1"]
    assert PATHFINDER not in http.urls("POST")
    assert http.calls[[u for _, u, _ in http.calls].index(API)][2]["headers"]["Authorization"] == "Bearer anon-token"


def test_web_api_rate_limit_falls_through_to_the_web_players_query():
    http = FakeHttp({**web_player(3), API: Response(429, {"error": {"status": 429}})})

    result = PublicSpotifyReader("", "", http).read(PID)

    assert [t.id for t in result.tracks] == ["sp0", "sp1", "sp2"] and not result.notices


@pytest.mark.parametrize("broken", [
    {"https://open.spotify.com/api/token": Response(400, {"error": "Unauthorized request"})},
    {BUNDLE: Response(200, text="nothing to see")},
    {PATHFINDER: Response(403, {"error": "Client/request not allowed"})},
])
def test_when_the_anonymous_route_breaks_the_embed_page_is_the_fallback(broken):
    routes = {**web_player(150), **broken,
              EMBED: Response(200, text=embed_page([embed_entry(n) for n in range(100)]))}
    page = routes[PAGE].text + '<meta name="music:song_count" content="150"/>'
    routes[PAGE] = Response(200, text=page)
    http = FakeHttp(routes)

    result = PublicSpotifyReader("", "", http).read(PID)

    assert len(result.tracks) == 100 and result.playlist.track_count == 150
    assert "first 100" in result.notices[0] and "web-player route failed" in result.notices[0]


def test_short_playlist_read_from_the_embed_page_gets_no_warning():
    http = FakeHttp({EMBED: Response(200, text=embed_page([embed_entry(n) for n in range(40)]))})

    result = PublicSpotifyReader("", "", http).read(PID)

    assert len(result.tracks) == 40 and not result.notices


def test_playlist_the_web_player_cannot_find_gives_a_clear_error():
    http = FakeHttp({**web_player(1), PATHFINDER: Response(200, {"data": {"playlistV2": {"__typename": "NotFound"}}})})

    with pytest.raises(ProviderError) as exc:
        PublicSpotifyReader("", "", http).read(PID)

    assert "public" in str(exc.value)


def test_long_playlist_without_login_warns_it_was_cut_short():
    http = FakeHttp({EMBED: Response(200, text=embed_page([embed_entry(n) for n in range(100)])),
                     PAGE: Response(200, text='<meta name="music:song_count" content="150"/>')})

    result = PublicSpotifyReader("", "", http).read(PID)

    assert len(result.tracks) == 100
    assert result.playlist.track_count == 150
    assert "first 100" in result.notices[0] and "150" in result.notices[0]


def test_private_playlist_gives_a_clear_error():
    http = FakeHttp({**TOKEN})  # API and page both 404

    with pytest.raises(ProviderError) as exc:
        PublicSpotifyReader("cid", "secret", http).read(PID)

    assert "public" in str(exc.value) and "music-sync auth spotify" in str(exc.value)


# --- the CLI with only the target side signed in --------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_SYNC_HOME", str(tmp_path))  # no spotify_token.json: never signed in
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    http = FakeHttp({**TOKEN, API: Response(200, {"name": "Shared Mix", "items": {
        "items": [api_item(0), api_item(1)], "next": None, "total": 2}})})
    youtube = FakeProvider("youtube", [T("youtube", "yt0", "Song 0", "Artist 0", 200),
                                       T("youtube", "yt1", "Song 1 (Official Audio)", "Artist 1 - Topic", 201)])
    apple = FakeProvider("apple", [])

    def get_provider(name, settings):
        if name == "spotify":
            return SpotifyProvider(settings, http=http)
        return {"youtube": youtube, "apple": apple}[name]

    monkeypatch.setattr(cli, "get_provider", get_provider)
    return http, youtube


def test_sync_public_spotify_link_to_youtube_without_spotify_login(cli_env):
    http, youtube = cli_env
    link = f"https://open.spotify.com/playlist/{PID}?si=4f8a2c9e1b7d4e3a"

    result = CliRunner().invoke(cli.main, ["sync", "--from", "spotify", "--to", "youtube", link])

    assert result.exit_code == 0, result.output
    [(pid, playlist)] = youtube.playlists.items()
    assert playlist.name == "Shared Mix"
    assert youtube.contents[pid] == ["yt0", "yt1"]
    assert "2 added" in result.output
    assert http.urls().count(API) == 1  # read once, reused for name and tracks


def test_sync_private_spotify_playlist_without_login_explains(cli_env, monkeypatch):
    http, _ = cli_env
    del http.routes[API]

    result = CliRunner().invoke(cli.main, ["sync", "--from", "spotify", "--to", "youtube", PID])

    assert result.exit_code == 1
    assert "public" in result.output and "music-sync auth spotify" in result.output


def test_spotify_playlist_name_without_login_asks_for_a_link(cli_env):
    result = CliRunner().invoke(cli.main, ["sync", "--from", "spotify", "--to", "youtube", "Road Trip"])

    assert result.exit_code == 1
    assert "public playlist's link" in result.output
