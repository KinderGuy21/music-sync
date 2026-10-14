# music-sync

Sync playlists between **Spotify**, **YouTube Music** and **Apple Music**, in any direction:
Spotify → YouTube, YouTube → Apple, Apple → Spotify, and back again. Use it from the
command line or a small local web page.

There is also a **hosted web app** in [`web/`](web/README.md) for the most common case,
copying a public Spotify playlist to YouTube Music: nothing to install, each visitor signs in
with their own Google account, and it runs entirely in the browser (static site on Cloudflare
Pages plus one tiny token function). This Python tool stays the full-featured version.

- **One-way sync** copies a playlist to another service, creating it there or updating
  the existing one (only missing songs are added).
- **Two-way sync** merges two playlists so each ends up with the other's songs, and can
  carry removals across too.
- **Smart matching** finds the same recording despite naming differences:
  `Bohemian Rhapsody - Remastered 2011` = `Bohemian Rhapsody (Remastered 2011)`,
  `Under Pressure (feat. David Bowie)` = `Under Pressure` by Queen & David Bowie,
  `Queen - Topic` = `Queen`, Hebrew titles vs. their transliteration
  (`מלכת הדור` = `Malkat Hador`). Live, remix, acoustic and cover versions are *not*
  confused with the original, and ISRC codes are used for exact matches where the
  service provides them (Spotify, Apple Music).
- **No Spotify account needed for public playlists**: paste the link of any public
  Spotify playlist and copy it to YouTube Music or Apple Music without signing in to
  Spotify (see [Public Spotify playlists](#public-spotify-playlists-no-spotify-login)).
- **Remembers** what it matched and which playlists belong together, so re-syncs are fast
  and update the same playlist instead of making duplicates.

## Install

Python 3.10+.

```bash
git clone https://github.com/KinderGuy21/music-sync.git
cd music-sync
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in the keys below
```

## Connecting the services

Each service needs a developer key once, then you sign in with your own account.
The one exception is reading a *public* Spotify playlist, which needs no Spotify login and
no keys at all (see below).
Tokens are stored in `~/.music-sync/` (change with `MUSIC_SYNC_HOME`), never in the repo.

### Spotify (OAuth)

1. Create an app at <https://developer.spotify.com/dashboard> (Web API).
   Since February 2026 the app owner needs Spotify Premium, and a development-mode app
   can be used by up to 5 accounts, which you add under *User Management*.
2. Add the redirect URI `http://127.0.0.1:8765/auth/spotify/callback` (Spotify rejects
   `localhost`; it must be the loopback IP).
3. Put the client ID and secret in `.env` as `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`.
4. `music-sync auth spotify` (or *Connect* in the web UI).

Spotify only returns the songs of playlists you **own or collaborate on**; other people's
playlists are listed but can't be read through your login. If they're public, music-sync
reads them the no-login way described next, automatically.

### Public Spotify playlists (no Spotify login)

For copying **someone else's public playlist** to YouTube Music or Apple Music, e.g. a
friend who doesn't use Spotify received a Spotify link. This is a one-way, source-only mode:
nobody signs in to Spotify, so nothing can be written to Spotify, and two-way sync and
`--all` still need the full login above.

```bash
music-sync auth youtube --browser      # only the target service needs signing in
music-sync sync --from spotify --to youtube "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=4f8a2c9e1b7d4e3a"
music-sync tracks spotify "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=..."  # just list the songs
```

Give the share link as-is (`?si=...` and all), a `spotify:playlist:` URI, or the bare id;
a playlist *name* only works once you've signed in. In the web UI, paste the link into
*Or paste a playlist link* instead of loading playlists.

How it reads the playlist, in order:

1. **App-only API access**: if `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` are set,
   the Client Credentials flow (no user, no redirect URI, no browser) and
   `GET /v1/playlists/{id}`. Since February 2026 Spotify only gives development-mode apps
   the *contents* of playlists the caller owns, and an app token owns nothing, so for most
   apps this yields just the name and description; apps with extended quota get every song,
   with ISRCs.
2. **The web player's anonymous access**: the guest token that open.spotify.com gets for a
   visitor who isn't logged in, used the way the web player itself uses it, 100 songs a page
   until the playlist ends. Needs no keys and has **no song limit**. No ISRCs (unless
   Spotify's standard Web API accepts that token again, which music-sync tries first), so
   matching goes by title, artist, album and duration.
   **This is unofficial and undocumented**: it signs the token request with a code taken from
   the web player's JavaScript, and Spotify can change or block it at any time (its token
   endpoint says such use isn't permitted under its Developer Terms). When it stops working,
   music-sync falls back to the next way instead of failing.
3. **The public playlist page** (`open.spotify.com/embed/playlist/{id}`), the same list
   anyone sees when they open the link. Needs no keys at all, but carries only the **first
   100 songs**. It is only used if the way above fails; for playlists longer than 100 songs
   music-sync then warns how many were skipped and why the full read failed. No ISRCs.

The 100-song warning only appears when songs were actually skipped: a long playlist read in
full the second way gets no warning.

If the playlist is private or doesn't exist, every way fails and music-sync says so: ask the
owner to make it public, or sign in with `music-sync auth spotify` as an account that owns or
collaborates on it.

### YouTube Music (OAuth or browser login)

Either:

- **OAuth**: in Google Cloud, enable *YouTube Data API v3*, create an OAuth client of type
  *TVs and Limited Input devices*, put its ID/secret in `.env` as `YTMUSIC_CLIENT_ID` /
  `YTMUSIC_CLIENT_SECRET`, then `music-sync auth youtube` and enter the code shown.
- **Browser login** (no Google Cloud project needed): `music-sync auth youtube --browser`,
  then paste the request headers of a logged-in `music.youtube.com` request (DevTools →
  Network → a `browse` POST → copy request headers). The web UI has a form for this too.

YouTube Music is reached through [ytmusicapi](https://github.com/sigma67/ytmusicapi), an
unofficial API. If OAuth requests start failing, the browser login is the usual fix.

### Apple Music (MusicKit)

Apple has no classic OAuth: it needs a developer token plus your Music User Token.

1. With an Apple Developer account, create a **MusicKit** key at
   <https://developer.apple.com/account/resources/authkeys/list> and download the `.p8`.
2. Set `APPLE_TEAM_ID`, `APPLE_KEY_ID` and `APPLE_PRIVATE_KEY_PATH` in `.env`
   (music-sync signs the developer token itself), or paste a ready-made token into
   `APPLE_DEVELOPER_TOKEN`.
3. `music-sync auth apple` opens a local page that signs you in with MusicKit JS
   (or *Connect* in the web UI). You need an active Apple Music subscription.

Apple's public API can create playlists and add songs, but **cannot remove songs or rename
playlists**, so Apple Music is add-only as a target: removals are reported, not applied.

Check what's connected with `music-sync services`.

## Command line

```bash
music-sync playlists spotify                         # list playlists (id, size, name)
music-sync tracks youtube "Road Trip"                # songs in a playlist (id, link or name)

# One-way: copy/update a playlist on another service
music-sync sync --from spotify --to youtube "Road Trip"
music-sync sync --from spotify --to youtube "https://open.spotify.com/playlist/<id>?si=..."  # public, no Spotify login
music-sync sync --from youtube --to apple "Road Trip" --target-name "Road Trip (YT)"
music-sync sync --from apple --to spotify --all      # every playlist
music-sync sync --from spotify --to apple "Gym" --dry-run   # preview only
music-sync sync --from spotify --to youtube "Gym" --delete  # make the target mirror the source

# Two-way: merge both, optionally carrying removals across
music-sync sync --from spotify --to youtube "Gym" --two-way
music-sync sync --from spotify --to youtube "Gym" --two-way --delete

music-sync links            # which playlists are linked
music-sync sync-linked      # two-way sync all linked pairs (good for cron)
music-sync forget-misses    # retry songs that weren't found last time
music-sync search apple "bohemian rhapsody queen"
```

Playlists can be given by id, share link (Spotify `open.spotify.com/playlist/...?si=...`,
YouTube Music `...?list=...`; query strings are fine), exact name, or an unambiguous part
of the name.

To keep everything in sync automatically, run `music-sync sync-linked` from cron or launchd,
e.g. hourly: `0 * * * * cd ~/music-sync && .venv/bin/music-sync sync-linked`.

## Web interface

```bash
music-sync web            # http://127.0.0.1:8765
```

Connect the services, pick **From** and **To** (the ⇆ button swaps them), load and tick
playlists (or paste a playlist link), choose one-way or two-way, and press **Sync**. The
log streams live and each playlist gets a summary listing any songs that weren't found.

## Hosted web app (`web/`)

A separate, fully client-side app for one job: paste a public Spotify playlist link, sign in
with Google, review the matches, and get the playlist in your YouTube Music. It reads Spotify
the same anonymous way as `spotify_public.py` and matches songs with a TypeScript port of
`matching.py` (checked against it by a parity test), but uses the official YouTube Data API
instead of ytmusicapi. It doesn't share code or state with the Python tool. Setup, the
Google client ID it needs, YouTube's API quota and deploying to Cloudflare Pages are in
[web/README.md](web/README.md).

## How syncing works

**Matching.** For each song not already in the target playlist:

1. look it up in the match cache;
2. look it up by ISRC if both services expose ISRCs;
3. otherwise search the target with a few queries (cleaned title + version + artist, the
   raw title, the title alone, then the artist alone) and score every result on title,
   artists and duration. Titles are reduced to a core (featured artists, remaster/edition
   tags, "Official Audio" and so on removed) plus version tags (live, remix, acoustic,
   instrumental...) which must agree. A result counts if it scores at least
   `MUSIC_SYNC_MATCH_THRESHOLD` (default 0.72).

Songs already in the target playlist are recognised by the same scoring against the
playlist itself, without any search calls.

**One-way** (`A → B`): finds B's playlist (the linked one from a previous sync, else one
with the same name, else creates it), adds the missing songs in source order, and with
`--delete` removes songs that aren't in the source.

**Two-way** (`A ⇄ B`): adds A's extra songs to B and B's extra songs to A. Each two-way sync
saves a snapshot of the paired songs; with `--delete`, a song that was in the snapshot but has
since been removed on one side is removed from the other. Without `--delete`, removed songs
come back from the other side (a pure merge).

**State** lives in `~/.music-sync/state.json`: the match cache and the playlist links. Delete
it to start fresh.

## Project layout

```
music_sync/
  cli.py              command line (click)
  sync.py             sync engine: one-way, two-way, playlist resolution
  matching.py         title/artist normalisation and scoring
  state.py            match cache and playlist links (JSON)
  config.py           settings from env / .env
  models.py           Track and Playlist
  providers/
    base.py           the Provider interface
    spotify.py        Spotify Web API via spotipy
    spotify_public.py public Spotify playlists without a login (app-only API, web player, public page)
    youtube_music.py  YouTube Music via ytmusicapi
    apple_music.py    Apple Music API + MusicKit developer token
  web/
    app.py            Flask app and JSON API
    templates/        pages, including the MusicKit JS sign-in
tests/                pytest suite; the sync engine is tested against in-memory fake services
web/                  the hosted, browser-only web app (TypeScript, Cloudflare Pages); see web/README.md
```

Adding another service means implementing `Provider` (list playlists, read tracks, search,
create playlist, add/remove tracks) and registering it in `providers/__init__.py`.

## Development

```bash
pip install -e ".[dev]"
pytest
cd web && npm install && npm test   # the hosted web app
```

If you change `matching.py`, make the same change in `web/src/matching.ts` and regenerate the
parity fixture with `python web/tests/make_parity.py` (needs `rapidfuzz`).

## Limits worth knowing

- Spotify: search returns at most 10 results per query; through your login only your
  own/collaborative playlists' contents are readable, and other public playlists are read
  without a login through the web player's unofficial anonymous access (every song), falling
  back to the embed page's first 100 songs if Spotify blocks that; development-mode apps are
  limited to 5 users.
- YouTube Music: unofficial API; no ISRCs, so matching is by title/artist/duration only.
- Apple Music: add-only (no removals or renames through the API).
- Songs that don't exist on the target service are listed as "not found" rather than
  replaced with a near miss.
