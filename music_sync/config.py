"""Settings come from environment variables (or a .env file in the working directory).

Tokens and sync state live in MUSIC_SYNC_HOME (default ~/.music-sync).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    home: Path
    web_host: str
    web_port: int

    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str

    ytmusic_client_id: str
    ytmusic_client_secret: str

    apple_team_id: str
    apple_key_id: str
    apple_private_key_path: str
    apple_developer_token: str
    apple_storefront: str

    match_threshold: float

    @property
    def spotify_cache(self) -> Path:
        return self.home / "spotify_token.json"

    @property
    def ytmusic_oauth_file(self) -> Path:
        return self.home / "ytmusic_oauth.json"

    @property
    def ytmusic_browser_file(self) -> Path:
        return self.home / "ytmusic_browser.json"

    @property
    def apple_user_token_file(self) -> Path:
        return self.home / "apple_music_user_token.txt"

    @property
    def state_file(self) -> Path:
        return self.home / "state.json"


def load_settings() -> Settings:
    home = Path(_env("MUSIC_SYNC_HOME") or Path.home() / ".music-sync").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    port = int(_env("MUSIC_SYNC_PORT", "8765"))
    host = _env("MUSIC_SYNC_HOST", "127.0.0.1")
    return Settings(
        home=home,
        web_host=host,
        web_port=port,
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
        # Spotify only accepts loopback IPs (not "localhost") for http redirect URIs.
        spotify_redirect_uri=_env("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{port}/auth/spotify/callback"),
        ytmusic_client_id=_env("YTMUSIC_CLIENT_ID"),
        ytmusic_client_secret=_env("YTMUSIC_CLIENT_SECRET"),
        apple_team_id=_env("APPLE_TEAM_ID"),
        apple_key_id=_env("APPLE_KEY_ID"),
        apple_private_key_path=_env("APPLE_PRIVATE_KEY_PATH"),
        apple_developer_token=_env("APPLE_DEVELOPER_TOKEN"),
        apple_storefront=_env("APPLE_STOREFRONT"),
        match_threshold=float(_env("MUSIC_SYNC_MATCH_THRESHOLD", "0.72")),
    )
