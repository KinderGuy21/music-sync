// GET /api/spotify-token: a fresh anonymous Spotify web-player token for the browser.
// The site's only server code. It takes no input, sees no playlist and logs nothing.
// No CORS headers on purpose: only pages on this site's own origin can read the answer.

import { mintSpotifyToken, TokenUnavailable } from "../../server/spotifyToken";

export const onRequestGet: PagesFunction = async () => {
  try {
    return Response.json(await mintSpotifyToken(), { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const message = err instanceof TokenUnavailable ? err.message : "Couldn't reach Spotify";
    return Response.json({ error: message }, { status: 502, headers: { "Cache-Control": "no-store" } });
  }
};
