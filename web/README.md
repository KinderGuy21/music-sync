# music-sync web

Copy a public Spotify playlist to YouTube Music from a web page: paste the link, sign in
with Google, check the matches, press **Create playlist**. Visitors need no Spotify account,
nothing to install, and nobody else's login: each one signs in with their own Google account
and the playlist is created in their YouTube Music.

It is a static site plus **one** tiny server function, built for Cloudflare Pages. It is a
separate app from the Python tool in the repo root (which stays the full-featured version)
and shares no code or state with it.

## How it works

```
browser ──GET /api/spotify-token──▶ Pages Function ──▶ open.spotify.com, clienttoken.spotify.com
   │                                (mints an anonymous web-player token; no user data)
   ├──POST api-partner.spotify.com/pathfinder/v2/query   playlist name + every song, 100 a page
   ├──Google Identity Services pop-up                     the visitor's own Google sign-in
   └──YouTube Data API v3                                 search, create playlist, add songs
```

- **Spotify, no login.** Spotify's web player gets an anonymous token for visitors who aren't
  logged in. Minting one means a TOTP code from a secret in the web player's JavaScript, then
  `open.spotify.com/api/token` and `clienttoken.spotify.com`, and those send no CORS headers,
  so that one step runs server-side in [`functions/api/spotify-token.ts`](functions/api/spotify-token.ts)
  (logic in [`server/spotifyToken.ts`](server/spotifyToken.ts), the same steps as the CLI's
  `spotify_public.py`). It takes no input, never sees a playlist and logs nothing. It also
  returns the web player's own client id, so no Spotify developer app is needed. The
  browser then reads the playlist itself from Spotify's GraphQL endpoint (the persisted
  `fetchPlaylist` query), which accepts calls from any origin.
  **This is unofficial**: Spotify can change or block it at any time. The page then says
  Spotify's anonymous access isn't working, and the fix is in `server/spotifyToken.ts`.
- **YouTube Music, official API.** Google Identity Services' token flow
  (`google.accounts.oauth2.initTokenClient`) gives the page a one-hour access token for the
  `youtube` scope. It's held in memory only, never stored or sent anywhere but Google.
  Playlists created through the YouTube Data API show up in YouTube Music.
- **Matching** is [`src/matching.ts`](src/matching.ts), a port of the CLI's `matching.py`
  (same title clean-up, version tags, artist and duration scoring, Hebrew ↔ transliteration,
  0.72 threshold). [`tests/parity.json`](tests/parity.json) holds what the Python code says
  for a set of titles and pairs, and the tests check the port agrees exactly. YouTube video
  titles are also read as "Artist - Title" when they look like one. Matches below the
  threshold are shown as *possible matches*, unticked; any row can be ticked, unticked or
  pointed at a different video by pasting a link.
- **Memory**: matches and half-finished copies are kept in the browser's localStorage, so a
  song is never searched twice and a copy cut short carries on into the same playlist.

## YouTube API quota: read this first

All visitors share one Google Cloud project's YouTube Data API quota: **10,000 units a day**
by default, reset at midnight Pacific time. A search costs 100 units, adding a song 50,
creating a playlist 50. So a song costs about 150 units, and the default quota covers
**roughly 60 songs a day across all visitors**. A 120-song playlist takes two days.

The page shows the cost before you start, stops cleanly when the quota runs out, and carries
on from where it stopped next time. For real use, ask Google for more quota: in the Google
Cloud console, *APIs & Services → YouTube Data API v3 → Quotas*, or the
[YouTube API Services audit and quota extension form](https://support.google.com/youtube/contact/yt_api_form).
It's free but reviewed by hand.

## Setting it up

### 1. A Google OAuth client ID (once)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (or reuse one).
2. *APIs & Services → Library*: enable **YouTube Data API v3**.
3. *APIs & Services → OAuth consent screen* (Google Auth Platform): app name, support email;
   under *Data access* add the scope `https://www.googleapis.com/auth/youtube`.
   While the app is in **Testing**, only the Google accounts listed under *Audience → Test users*
   (up to 100) can sign in. To let anyone sign in, *Publish app*. `youtube` is a sensitive
   scope, so until Google verifies the app, visitors see an "unverified app" warning they
   can click through.
4. *Credentials → Create credentials → OAuth client ID*, type **Web application**. Under
   *Authorized JavaScript origins* add every origin the site runs on, e.g.
   `https://music-sync.pages.dev`, your own domain, and `http://localhost:5173` for development.
   No redirect URIs are needed. No client secret is used (the token flow doesn't need one).
5. Copy the client ID (`….apps.googleusercontent.com`).

The client ID is public by design (it's in the page's JavaScript). It's read at build time
from `VITE_GOOGLE_CLIENT_ID`.

### 2. Run it locally

Node 22+.

```bash
cd web
npm install
echo 'VITE_GOOGLE_CLIENT_ID=…apps.googleusercontent.com' > .env.local
npm run dev            # http://localhost:5173 (serves /api/spotify-token too)
npm test               # matching parity, Spotify token/TOTP, YouTube matching against a fake API
npm run preview        # build, then serve dist/ + the function in Cloudflare's real runtime (wrangler)
```

### 3. Deploy to Cloudflare Pages

Either connect the GitHub repo in the Cloudflare dashboard (*Workers & Pages → Create →
Pages → Connect to Git*) with:

| Setting | Value |
|---|---|
| Root directory | `web` |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Environment variable | `VITE_GOOGLE_CLIENT_ID` = your client ID |

Cloudflare picks up `functions/` automatically, so `/api/spotify-token` deploys with the
site. Or deploy from your machine: `npx wrangler login`, then
`VITE_GOOGLE_CLIENT_ID=… npm run deploy` (project name `music-sync`, from `wrangler.toml`).
Then add the site's URL to the OAuth client's authorized JavaScript origins.

The function answers without CORS headers, so other websites' pages can't use it from their
visitors' browsers. Anyone could still call it from a script; it only mints the same
anonymous token Spotify gives every visitor to open.spotify.com.

## Files

```
index.html                 the page
src/main.ts                UI: the four steps, review table, progress, summary
src/spotify.ts             link parsing, reading the playlist from Spotify in the browser
src/youtube.ts             Google sign-in, YouTube Data API calls, finding a song's best match
src/matching.ts, fuzz.ts   port of matching.py and the two rapidfuzz scorers it uses
src/store.ts               localStorage: remembered matches and half-finished copies
src/style.css
functions/api/spotify-token.ts   the Cloudflare Pages Function (GET /api/spotify-token)
server/spotifyToken.ts     minting the anonymous Spotify token (also used by `npm run dev`)
tests/                     vitest; make_parity.py regenerates parity.json from the Python code
wrangler.toml              Cloudflare Pages config
```

## Limits

- Public Spotify playlists only (private ones need their owner's login: use the CLI).
- Short `spotify.link/…` share links can't be followed by a web page; open them and paste
  the full `open.spotify.com/playlist/…` address.
- The YouTube Data API has no "songs only" search like the CLI's ytmusicapi, so results are
  limited to the Music category and scored. Topic-channel "art tracks" usually win.
- Spotify → YouTube Music only; the CLI does every other direction and two-way sync.
