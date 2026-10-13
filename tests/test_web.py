import time

import pytest

from music_sync.config import load_settings
from music_sync.web import app as web_app

from .fakes import FakeProvider, T


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_SYNC_HOME", str(tmp_path))
    fakes = {
        "spotify": FakeProvider("spotify", [T("spotify", "sp0", "Blinding Lights", "The Weeknd", 200)]),
        "youtube": FakeProvider("youtube", [T("youtube", "yt0", "Blinding Lights (Official Audio)", "The Weeknd", 201)]),
        "apple": FakeProvider("apple", [], removal=False),
    }
    for f in fakes.values():
        f.has_developer_credentials = lambda: True
    fakes["spotify"].add_playlist("Night Drive", ["sp0"])
    monkeypatch.setattr(web_app, "get_provider", lambda name, settings: fakes[name])
    app = web_app.create_app(load_settings())
    return app.test_client(), fakes


def test_index_renders(client):
    c, _ = client
    page = c.get("/").get_data(as_text=True)
    assert "Spotify" in page and "YouTube Music" in page and "Apple Music" in page


def test_sync_through_api(client):
    c, fakes = client
    playlists = c.get("/api/playlists/spotify").get_json()
    assert playlists[0]["name"] == "Night Drive"

    job = c.post("/api/sync", json={"source": "spotify", "target": "youtube",
                                    "playlists": [playlists[0]["id"]]}).get_json()
    for _ in range(50):
        status = c.get(f"/api/jobs/{job['id']}").get_json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert status["status"] == "done", status
    assert status["reports"][0]["added"] == ["The Weeknd - Blinding Lights (Official Audio)"]
    yt_pid = next(iter(fakes["youtube"].playlists))
    assert fakes["youtube"].contents[yt_pid] == ["yt0"]
    assert len(c.get("/api/links").get_json()) == 1


def test_same_service_rejected(client):
    c, _ = client
    res = c.post("/api/sync", json={"source": "apple", "target": "apple", "playlists": ["x"]})
    assert res.status_code == 400


def test_sync_a_pasted_playlist_link(client):
    c, fakes = client
    pid = next(iter(fakes["spotify"].playlists))
    fakes["spotify"].parse_playlist_ref = lambda ref: pid if ref.startswith("https://") else None

    job = c.post("/api/sync", json={"source": "spotify", "target": "youtube",
                                    "playlists": ["https://open.spotify.com/playlist/x?si=y"]}).get_json()
    for _ in range(50):
        status = c.get(f"/api/jobs/{job['id']}").get_json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert status["status"] == "done", status
    assert status["reports"][0]["source_playlist"] == "Night Drive"
