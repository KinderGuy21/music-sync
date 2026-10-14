/**
 * Reading a public Spotify playlist in the browser, with nobody signed in to Spotify.
 *
 * The site's one server function (/api/spotify-token) mints the web player's anonymous token;
 * this file then pages through Spotify's own GraphQL endpoint (the persisted "fetchPlaylist"
 * query, 100 songs a page) straight from the browser, which that endpoint allows from any
 * origin. Same route as the CLI's music_sync/providers/spotify_public.py; unofficial, so
 * Spotify can change or block it without notice.
 */

import type { Playlist, Track } from "./models";

const PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query";
const PAGE_SIZE = 100;

const ID = "[0-9A-Za-z]{22}";
const LINK_RE = new RegExp(`(?:open\\.spotify\\.com/(?:intl-[a-z-]+/)?(?:embed/)?playlist/|spotify:playlist:)(${ID})`);
const SHORT_LINK_RE = /(?:spotify\.link|spotify\.app\.link)\//;

export class SpotifyError extends Error {}

/** The playlist id in a Spotify link (`?si=...` and all), URI or bare id; else null. */
export function parsePlaylistId(ref: string): string | null {
  ref = ref.trim();
  const m = LINK_RE.exec(ref) ?? LINK_RE.exec(safeDecode(ref));
  if (m) return m[1];
  return new RegExp(`^${ID}$`).test(ref) ? ref : null;
}

/** Short share links redirect to the real one, which a browser page can't follow for you. */
export const isShortLink = (ref: string): boolean => SHORT_LINK_RE.test(ref);

function safeDecode(text: string): string {
  try {
    return decodeURIComponent(text); // links wrapped in tracking redirects
  } catch {
    return text;
  }
}

interface Token {
  accessToken: string;
  clientToken: string;
  queryHash: string;
  expiresAt: number;
}

let token: Token | null = null;

async function getToken(fresh = false): Promise<Token> {
  if (!fresh && token && token.expiresAt - Date.now() > 60_000) return token;
  let res: Response;
  try {
    res = await fetch("/api/spotify-token", { cache: "no-store" });
  } catch {
    throw new SpotifyError("Couldn't reach this site's Spotify token service");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok || !body.accessToken) {
    throw new SpotifyError(`Spotify's anonymous access isn't working right now${body.error ? ` (${body.error})` : ""}`);
  }
  token = body as Token;
  return token;
}

/** Name, description and every song of a public playlist. */
export async function readPlaylist(id: string, onProgress?: (read: number, total: number) => void): Promise<Playlist> {
  const tracks: Track[] = [];
  let name = "", description = "", total: number | null = null, offset = 0;
  while (total === null || offset < total) {
    const playlist = await page(id, offset);
    const content = playlist.content ?? {};
    const items: unknown[] = content.items ?? [];
    if (total === null) {
      name = playlist.name ?? id;
      description = playlist.description ?? "";
      total = Number(content.totalCount ?? 0);
    }
    for (const item of items) {
      const t = pathfinderTrack(item);
      if (t) tracks.push(t);
    }
    if (!items.length) break;
    offset += items.length;
    onProgress?.(Math.min(offset, total), total);
  }
  return { id, name, description: stripTags(description), trackCount: total ?? tracks.length, tracks };
}

async function page(id: string, offset: number, retried = false): Promise<any> {
  const t = await getToken(retried);
  let res: Response;
  try {
    res = await fetch(PATHFINDER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${t.accessToken}`, "client-token": t.clientToken,
        "app-platform": "WebPlayer", "Content-Type": "application/json;charset=UTF-8",
      },
      body: JSON.stringify({
        operationName: "fetchPlaylist",
        variables: { uri: `spotify:playlist:${id}`, offset, limit: PAGE_SIZE, enableWatchFeedEntrypoint: false },
        extensions: { persistedQuery: { version: 1, sha256Hash: t.queryHash } },
      }),
    });
  } catch {
    throw new SpotifyError("Couldn't reach Spotify");
  }
  if ((res.status === 401 || res.status === 400) && !retried) return page(id, offset, true);
  if (res.status === 429) throw new SpotifyError("Spotify is rate-limiting this site; try again in a minute");
  if (!res.ok) throw new SpotifyError(`Spotify's playlist query answered HTTP ${res.status}`);
  const playlist = (await res.json())?.data?.playlistV2;
  if (playlist?.__typename === "NotFound") {
    throw new SpotifyError("That playlist is private or doesn't exist: ask its owner to make it public");
  }
  if (playlist?.__typename !== "Playlist") throw new SpotifyError("Spotify returned no playlist");
  return playlist;
}

/** A web-player playlist item as a Track; null for episodes, local files and removed tracks. */
export function pathfinderTrack(entry: any): Track | null {
  const data = entry?.itemV2?.data ?? {};
  const uri: string = data.uri ?? "";
  if (data.__typename !== "Track" || !uri.startsWith("spotify:track:")) return null;
  return {
    id: uri.split(":").pop()!,
    title: data.name ?? "",
    artists: (data.artists?.items ?? []).map((a: any) => a?.profile?.name ?? ""),
    album: data.albumOfTrack?.name ?? null,
    durationMs: data.trackDuration?.totalMilliseconds ?? null,
    service: "spotify",
  };
}

function stripTags(html: string): string {
  return new DOMParser().parseFromString(html, "text/html").documentElement.textContent ?? "";
}
