/**
 * YouTube Music through the official YouTube Data API v3, called from the browser with the
 * visitor's own Google sign-in (Google Identity Services' popup token flow). No backend: the
 * access token lives only in this page's memory.
 *
 * Quota: every call is charged to the site's Google Cloud project, shared by all visitors
 * (10,000 units a day by default). search.list costs 100 units, playlistItems.insert 50,
 * playlists.insert 50, videos.list and channels.list 1. So matching tries as few searches as
 * it can, and every match is remembered in localStorage so nothing is searched twice.
 */

import { bestMatch, searchQueries, THRESHOLD } from "./matching";
import type { Track } from "./models";

const API = "https://www.googleapis.com/youtube/v3";
const SCOPE = "https://www.googleapis.com/auth/youtube";

export const COST = { search: 100, insert: 50, createPlaylist: 50 } as const;

export class YouTubeError extends Error {}
/** The site's daily API quota is used up (it resets at midnight Pacific time). */
export class QuotaError extends YouTubeError {}
export class AuthError extends YouTubeError {}

export interface Video extends Track {
  channel: string;
  thumbnail: string;
}

// --- Google sign-in ------------------------------------------------------------

interface TokenResponse { access_token?: string; expires_in?: number; error?: string; error_description?: string }
interface TokenClient { requestAccessToken(options?: { prompt?: string }): void }
declare const google: {
  accounts: { oauth2: {
    initTokenClient(config: {
      client_id: string; scope: string; include_granted_scopes?: boolean; callback: (r: TokenResponse) => void;
      error_callback?: (e: { type: string; message?: string }) => void;
    }): TokenClient;
    revoke(token: string, done?: () => void): void;
  } };
};

let tokenClient: TokenClient | null = null;
let pending: { resolve: (t: string) => void; reject: (e: Error) => void } | null = null;
let accessToken: string | null = null;
let expiresAt = 0;

export const clientId: string = import.meta.env.VITE_GOOGLE_CLIENT_ID ?? "";

function loadGis(): Promise<void> {
  if (typeof google !== "undefined") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.onload = () => resolve();
    script.onerror = () => reject(new AuthError("Couldn't load Google sign-in"));
    document.head.append(script);
  });
}

/** Opens Google's sign-in popup (must be called from a click). Resolves with an access token. */
export async function signIn(): Promise<string> {
  if (!clientId) throw new AuthError("This site has no Google client ID configured (VITE_GOOGLE_CLIENT_ID)");
  await loadGis();
  tokenClient ??= google.accounts.oauth2.initTokenClient({
    client_id: clientId,
    scope: SCOPE,
    // GIS defaults this to true, which adds every scope the account already granted this client
    // ID (e.g. drive.file from another app on the same client). Google then rejects the request:
    // "scopes that cannot be requested together". Ask for the YouTube scope alone.
    include_granted_scopes: false,
    callback: (r) => {
      if (r.access_token) {
        accessToken = r.access_token;
        expiresAt = Date.now() + (r.expires_in ?? 3600) * 1000;
        pending?.resolve(r.access_token);
      } else {
        pending?.reject(new AuthError(r.error_description || r.error || "Google sign-in failed"));
      }
      pending = null;
    },
    error_callback: (e) => {
      pending?.reject(new AuthError(e.type === "popup_closed" ? "The Google sign-in window was closed" : e.message || e.type));
      pending = null;
    },
  });
  return new Promise((resolve, reject) => {
    pending = { resolve, reject };
    tokenClient!.requestAccessToken();
  });
}

export function signOut(): void {
  if (accessToken && typeof google !== "undefined") google.accounts.oauth2.revoke(accessToken);
  accessToken = null;
  expiresAt = 0;
}

export const signedIn = (): boolean => !!accessToken && Date.now() < expiresAt - 30_000;

// --- the API ---------------------------------------------------------------------

// For tests and the mock mode: replace how requests are made.
export let http: typeof fetch = (...args) => fetch(...args);
export function setHttp(f: typeof fetch): void { http = f; }
export function setAccessToken(token: string, ttlSeconds = 3600): void {
  accessToken = token;
  expiresAt = Date.now() + ttlSeconds * 1000;
}

async function call<T>(method: string, path: string, params: Record<string, string>, body?: unknown, attempt = 0): Promise<T> {
  if (!signedIn()) throw new AuthError("Your Google sign-in has expired: sign in again");
  const res = await http(`${API}/${path}?${new URLSearchParams(params)}`, {
    method,
    headers: { Authorization: `Bearer ${accessToken}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  }).catch(() => { throw new YouTubeError("Couldn't reach YouTube"); });
  if (res.ok) return res.json() as Promise<T>;

  const error = (await res.json().catch(() => ({})))?.error ?? {};
  const reason: string = error.errors?.[0]?.reason ?? error.status ?? "";
  if (res.status === 401) {
    accessToken = null;
    throw new AuthError("Your Google sign-in has expired: sign in again");
  }
  if (/quotaExceeded|dailyLimitExceeded|rateLimitExceeded/.test(reason)) {
    throw new QuotaError("This site's YouTube API quota for today is used up; it resets at midnight Pacific time (09:00/10:00 in Israel)");
  }
  if (reason === "youtubeSignupRequired" || reason === "channelNotFound") {
    throw new YouTubeError("This Google account has no YouTube channel yet: open youtube.com, create one, then try again");
  }
  // playlistItems.insert often answers 409/5xx when called quickly; it works on a retry.
  if ((res.status === 409 || res.status >= 500) && attempt < 3) {
    await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
    return call(method, path, params, body, attempt + 1);
  }
  throw new YouTubeError(`YouTube answered HTTP ${res.status}${error.message ? `: ${stripTags(error.message)}` : ""}`);
}

/** The signed-in account's channel name. */
export async function me(): Promise<string> {
  const r = await call<{ items?: { snippet: { title: string } }[] }>("GET", "channels", { part: "snippet", mine: "true" });
  if (!r.items?.length) throw new YouTubeError("This Google account has no YouTube channel yet: open youtube.com, create one, then try again");
  return r.items[0].snippet.title;
}

interface SearchItem { id: { videoId: string }; snippet: { title: string; channelTitle: string; thumbnails?: { default?: { url: string } } } }
interface VideoItem { id: string; contentDetails: { duration: string }; snippet?: SearchItem["snippet"] }

export async function search(query: string): Promise<Video[]> {
  const r = await call<{ items?: SearchItem[] }>("GET", "search", {
    part: "snippet", type: "video", videoCategoryId: "10", maxResults: "10", q: query,
  });
  const items = (r.items ?? []).filter((i) => i.id?.videoId);
  return withDurations(items.map((i) => ({ id: i.id.videoId, snippet: i.snippet })));
}

/** One video by id (for a link the user pasted), or null. */
export async function video(id: string): Promise<Video | null> {
  const r = await call<{ items?: VideoItem[] }>("GET", "videos", { part: "snippet,contentDetails", id });
  const item = r.items?.[0];
  return item?.snippet ? toVideo(item.id, item.snippet, isoDuration(item.contentDetails.duration)) : null;
}

async function withDurations(items: { id: string; snippet: SearchItem["snippet"] }[]): Promise<Video[]> {
  if (!items.length) return [];
  // Search results carry no length; one cheap call gets all ten.
  const r = await call<{ items?: VideoItem[] }>("GET", "videos", { part: "contentDetails", id: items.map((i) => i.id).join(",") });
  const durations = new Map((r.items ?? []).map((v) => [v.id, isoDuration(v.contentDetails.duration)]));
  return items.map((i) => toVideo(i.id, i.snippet, durations.get(i.id) ?? null));
}

function toVideo(id: string, snippet: SearchItem["snippet"], durationMs: number | null): Video {
  const channel = decodeEntities(snippet.channelTitle);
  return {
    id, title: decodeEntities(snippet.title), artists: [channel], channel, durationMs, service: "youtube",
    thumbnail: snippet.thumbnails?.default?.url ?? "",
  };
}

/**
 * The ways a video can be read as a song. "Artist - Title (Official Video)" is the usual
 * YouTube title for an upload, while Topic-channel "art tracks" have the bare song title.
 */
export function readings(v: Video): Video[] {
  const out = [v];
  const m = /^(.+?)\s+[-–—]\s+(.+)$/u.exec(v.title);
  if (m) out.push({ ...v, title: m[2], artists: [m[1]] });
  return out;
}

export interface Match {
  video: Video | null; // what will be added (null: nothing found)
  score: number;
  suggestion?: Video; // the best below-threshold candidate, shown but not added
  searches: number;
}

/** Finds `track` on YouTube with as few searches (100 quota units each) as it takes. */
export async function findMatch(track: Track): Promise<Match> {
  let best: Video | null = null, bestScore = 0, searches = 0;
  for (const query of searchQueries(track)) {
    const videos = await search(query);
    searches++;
    for (const v of videos) {
      const [, s] = bestMatch(track, readings(v));
      if (s > bestScore) [best, bestScore] = [v, s];
    }
    // The CLI keeps searching up to GOOD_ENOUGH; here any acceptable match stops it, to save quota.
    if (bestScore >= THRESHOLD) break;
  }
  if (best && bestScore >= THRESHOLD) return { video: best, score: bestScore, searches };
  return { video: null, score: bestScore, suggestion: best ?? undefined, searches };
}

export type Privacy = "private" | "unlisted" | "public";

export async function createPlaylist(title: string, description: string, privacy: Privacy): Promise<string> {
  const r = await call<{ id: string }>("POST", "playlists", { part: "snippet,status" }, {
    snippet: { title: title.slice(0, 150), description: description.slice(0, 5000) },
    status: { privacyStatus: privacy },
  });
  return r.id;
}

export async function addToPlaylist(playlistId: string, videoId: string): Promise<void> {
  await call("POST", "playlistItems", { part: "snippet" }, {
    snippet: { playlistId, resourceId: { kind: "youtube#video", videoId } },
  });
}

export const playlistUrl = (id: string): string => `https://music.youtube.com/playlist?list=${id}`;
export const videoUrl = (id: string): string => `https://music.youtube.com/watch?v=${id}`;

/** The video id in a YouTube / YouTube Music link, or a bare id. */
export function parseVideoId(ref: string): string | null {
  ref = ref.trim();
  const m = /(?:[?&]v=|youtu\.be\/|\/shorts\/|\/embed\/)([\w-]{11})/.exec(ref);
  if (m) return m[1];
  return /^[\w-]{11}$/.test(ref) ? ref : null;
}

/** ISO 8601 duration ("PT3M35S") in ms. */
export function isoDuration(text: string): number | null {
  const m = /^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/.exec(text || "");
  if (!m) return null;
  const [d, h, min, s] = m.slice(1).map((x) => Number(x ?? 0));
  return (((d * 24 + h) * 60 + min) * 60 + s) * 1000 || null;
}

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

/** The API HTML-escapes titles ("Rock &amp; Roll", "Don&#39;t"). */
export function decodeEntities(text: string): string {
  return (text || "").replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, e: string) => {
    if (e[0] === "#") return String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : Number(e.slice(1)));
    return ENTITIES[e.toLowerCase()] ?? whole;
  });
}

function stripTags(text: string): string {
  return text.replace(/<[^>]+>/g, "");
}
