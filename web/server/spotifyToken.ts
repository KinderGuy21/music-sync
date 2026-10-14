/**
 * Minting the anonymous token Spotify's web player uses for a visitor who isn't logged in.
 *
 * This is the only thing music-sync's web app needs a server for: open.spotify.com/api/token
 * and clienttoken.spotify.com send no CORS headers, so a browser on another site can't call
 * them. Everything after this (reading the playlist through api-partner.spotify.com/pathfinder)
 * happens in the visitor's browser. No playlist id or other user data reaches this code.
 *
 * UNOFFICIAL AND UNDOCUMENTED, the same route as music_sync/providers/spotify_public.py
 * (checked October 2026):
 *   1. the web player's JavaScript bundle carries an obfuscated TOTP secret and the hash of the
 *      persisted "fetchPlaylist" GraphQL query;
 *   2. GET open.spotify.com/api/token?totp=... returns an anonymous bearer token and the web
 *      player's client id (so no Spotify developer app is needed);
 *   3. POST clienttoken.spotify.com returns the "client-token" the playlist query also sends.
 * Spotify can change or block any of this without notice.
 *
 * Plain fetch + WebCrypto, so it runs unchanged on Cloudflare and in Node (the Vite dev server).
 */

const HOME_URL = "https://open.spotify.com/";
const SERVER_TIME_URL = "https://open.spotify.com/api/server-time";
const WEB_TOKEN_URL = "https://open.spotify.com/api/token";
const CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken";
// The desktop web player is what the anonymous route needs.
const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
};

const BUNDLE_RE = /https:\/\/[^"']+\/web-player\/web-player\.[0-9a-f]+\.js/;
const APP_CONFIG_RE = /<script id="appServerConfig" type="text\/plain">([^<]+)<\/script>/;
// The newest TOTP secret comes first in the bundle's list, e.g. {secret:'...',version:61}.
const SECRET_RE = /secret:(['"])((?:\\.|(?!\1).)*)\1,version:(\d+)/;
const QUERY_HASH_RE = /"fetchPlaylist","query","([0-9a-f]{64})"/;

// The bundle changes every few days; reading it (a few MB) once an hour is plenty.
const BUNDLE_TTL_MS = 60 * 60 * 1000;

export interface SpotifyToken {
  accessToken: string;
  clientToken: string;
  /** sha256 of the persisted "fetchPlaylist" query, sent with every playlist page. */
  queryHash: string;
  /** Unix ms; the browser asks for a new token after this. */
  expiresAt: number;
}

interface WebPlayer {
  secret: string;
  version: string;
  queryHash: string;
  clientVersion: string;
  fetchedAt: number;
}

export class TokenUnavailable extends Error {}

let cached: WebPlayer | null = null;

export async function mintSpotifyToken(http: typeof fetch = fetch): Promise<SpotifyToken> {
  try {
    return await mint(http, await webPlayer(http));
  } catch (err) {
    // A stale bundle (rotated secret) is the likeliest failure: read it again once.
    if (!cached || Date.now() - cached.fetchedAt < 5000) throw err;
    cached = null;
    return mint(http, await webPlayer(http));
  }
}

async function webPlayer(http: typeof fetch): Promise<WebPlayer> {
  if (cached && Date.now() - cached.fetchedAt < BUNDLE_TTL_MS) return cached;
  const page = await http(HOME_URL, { headers: BROWSER_HEADERS });
  const html = page.ok ? await page.text() : "";
  const bundle = BUNDLE_RE.exec(html);
  if (!bundle) throw new TokenUnavailable("Spotify's web player page has changed");
  const config = APP_CONFIG_RE.exec(html);
  const clientVersion = config ? (JSON.parse(atob(config[1])).clientVersion as string | undefined) : undefined;
  const js = await http(bundle[0], { headers: BROWSER_HEADERS });
  const code = js.ok ? await js.text() : "";
  const secret = SECRET_RE.exec(code);
  const queryHash = QUERY_HASH_RE.exec(code);
  if (!secret || !queryHash || !clientVersion) throw new TokenUnavailable("Spotify's web player code has changed");
  cached = { secret: unescapeJs(secret[2]), version: secret[3], queryHash: queryHash[1], clientVersion, fetchedAt: Date.now() };
  return cached;
}

async function mint(http: typeof fetch, web: WebPlayer): Promise<SpotifyToken> {
  const key = totpKey(web.secret);
  let totpServer = "unavailable"; // what the web player itself sends when this fails
  try {
    const res = await http(SERVER_TIME_URL, { headers: BROWSER_HEADERS });
    const serverTime = Number((await res.json() as { serverTime?: number }).serverTime);
    if (Number.isFinite(serverTime) && serverTime > 0) totpServer = await totp(key, serverTime);
  } catch {
    // keep "unavailable"
  }
  const params = new URLSearchParams({
    reason: "init", productType: "web-player", totp: await totp(key, Date.now() / 1000),
    totpServer, totpVer: web.version,
  });
  const res = await http(`${WEB_TOKEN_URL}?${params}`, { headers: BROWSER_HEADERS });
  const body = (res.ok ? await res.json() : {}) as {
    accessToken?: string; clientId?: string; accessTokenExpirationTimestampMs?: number;
  };
  if (!body.accessToken || !body.clientId) throw new TokenUnavailable(`Spotify refused the anonymous token (HTTP ${res.status})`);

  const ct = await http(CLIENT_TOKEN_URL, {
    method: "POST",
    headers: { ...BROWSER_HEADERS, Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      client_data: {
        client_version: web.clientVersion, client_id: body.clientId, js_sdk_data: {
          device_brand: "unknown", device_model: "unknown", os: "macos", os_version: "10.15.7",
          device_id: randomHex(16), device_type: "computer",
        },
      },
    }),
  });
  const granted = (ct.ok ? await ct.json() : {}) as { granted_token?: { token?: string } };
  const clientToken = granted.granted_token?.token;
  if (!clientToken) throw new TokenUnavailable(`Spotify refused the client token (HTTP ${ct.status})`);

  return {
    accessToken: body.accessToken,
    clientToken,
    queryHash: web.queryHash,
    expiresAt: body.accessTokenExpirationTimestampMs ?? Date.now() + 30 * 60 * 1000,
  };
}

/** The web player's TOTP key from the obfuscated secret in its bundle (its own transform). */
export function totpKey(secret: string): Uint8Array<ArrayBuffer> {
  let digits = "";
  for (let i = 0; i < secret.length; i++) digits += String(secret.charCodeAt(i) ^ ((i % 33) + 9));
  return new TextEncoder().encode(digits) as Uint8Array<ArrayBuffer>;
}

/** RFC 6238: HMAC-SHA1, 30-second steps, 6 digits. */
export async function totp(key: Uint8Array<ArrayBuffer>, timestamp: number): Promise<string> {
  const counter = new ArrayBuffer(8);
  const view = new DataView(counter);
  const step = Math.floor(timestamp / 30);
  view.setUint32(0, Math.floor(step / 2 ** 32));
  view.setUint32(4, step >>> 0);
  const hmacKey = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", hmacKey, counter));
  const offset = digest[digest.length - 1] & 15;
  const code = new DataView(digest.buffer, offset, 4).getUint32(0) & 0x7fffffff;
  return String(code % 10 ** 6).padStart(6, "0");
}

/** The backslash escapes of a JS string literal (Python's unicode_escape in the CLI). */
export function unescapeJs(text: string): string {
  return text.replace(/\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)/g, (_, esc: string) => {
    if (esc[0] === "x" || esc[0] === "u") return String.fromCharCode(parseInt(esc.slice(1), 16));
    return ({ n: "\n", t: "\t", r: "\r", b: "\b", f: "\f", v: "\v", "0": "\0" } as Record<string, string>)[esc] ?? esc;
  });
}

function randomHex(bytes: number): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) => b.toString(16).padStart(2, "0")).join("");
}
