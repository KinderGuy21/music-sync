/**
 * Song search through YouTube Music's own web client API, so finding a song doesn't spend the
 * YouTube Data API's search.list allowance (100 calls a day for the whole site).
 *
 * UNOFFICIAL AND UNDOCUMENTED: this is the request music.youtube.com itself makes (and what
 * ytmusicapi uses), POST music.youtube.com/youtubei/v1/search with the WEB_REMIX client context
 * (checked October 2026). It needs no login or cookie for a plain search. YouTube answers it
 * with CORS headers only for Google's own sites, so it has to be made server-side. Google can
 * change or block it without notice; the browser then falls back to the official search.list.
 *
 * Plain fetch, so it runs unchanged on Cloudflare and in Node (the Vite dev server).
 */

const SEARCH_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false";
const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
  "Content-Type": "application/json",
};

// ytmusicapi's search filters ("EgWKAQ" + type + "AWoMEA4QChADEAQQCRAF"): songs are the
// "art tracks" a YouTube Music playlist should hold; videos catch uploads that have no art track.
const FILTERS = { songs: "EgWKAQIIAWoMEA4QChADEAQQCRAF", videos: "EgWKAQIQAWoMEA4QChADEAQQCRAF" } as const;

export const MAX_QUERY_LENGTH = 200;

export interface MusicResult {
  videoId: string;
  title: string;
  artists: string[];
  /** Album for songs; absent for videos. */
  album?: string;
  durationMs: number | null;
  thumbnail: string;
  kind: "song" | "video";
}

export class SearchUnavailable extends Error {}

/** GET /api/youtube-search?q=…, as functions/api/youtube-search.ts serves it. Logs no queries. */
export async function handleSearchRequest(request: Request, http: typeof fetch = fetch): Promise<Response> {
  const headers = { "Cache-Control": "no-store" };
  const query = new URL(request.url).searchParams.get("q")?.trim() ?? "";
  if (!query || query.length > MAX_QUERY_LENGTH) {
    return Response.json({ error: `Give a search query of 1 to ${MAX_QUERY_LENGTH} characters as ?q=` }, { status: 400, headers });
  }
  try {
    return Response.json({ results: await searchYouTubeMusic(query, http) }, { headers });
  } catch (err) {
    const message = err instanceof SearchUnavailable ? err.message : "Couldn't search YouTube Music";
    return Response.json({ error: message }, { status: 502, headers });
  }
}

/** The client version the web app sends: ytmusicapi uses today's date the same way. */
export function clientVersion(now = new Date()): string {
  return `1.${now.toISOString().slice(0, 10).replaceAll("-", "")}.01.00`;
}

/** Songs, then videos, for `query`: both searches run at once. */
export async function searchYouTubeMusic(query: string, http: typeof fetch = fetch): Promise<MusicResult[]> {
  query = query.trim().slice(0, MAX_QUERY_LENGTH);
  if (!query) return [];
  const [songs, videos] = await Promise.allSettled([
    searchOnce(query, "songs", http),
    searchOnce(query, "videos", http),
  ]);
  if (songs.status === "rejected" && videos.status === "rejected") throw songs.reason;
  const out: MusicResult[] = [];
  for (const r of [songs, videos]) {
    if (r.status !== "fulfilled") continue;
    for (const item of r.value) if (!out.some((o) => o.videoId === item.videoId)) out.push(item);
  }
  return out;
}

async function searchOnce(query: string, filter: keyof typeof FILTERS, http: typeof fetch): Promise<MusicResult[]> {
  const res = await http(SEARCH_URL, {
    method: "POST",
    headers: BROWSER_HEADERS,
    body: JSON.stringify({
      context: { client: { clientName: "WEB_REMIX", clientVersion: clientVersion(), hl: "en", gl: "US" } },
      query,
      params: FILTERS[filter],
    }),
  }).catch(() => { throw new SearchUnavailable("Couldn't reach YouTube Music"); });
  if (!res.ok) throw new SearchUnavailable(`YouTube Music answered HTTP ${res.status}`);
  const body = await res.json().catch(() => { throw new SearchUnavailable("YouTube Music sent something that isn't JSON"); });
  return parseSearchResponse(body, filter === "songs" ? "song" : "video");
}

// --- reading the answer ------------------------------------------------------------------

interface Run { text: string; navigationEndpoint?: { watchEndpoint?: { videoId?: string }; browseEndpoint?: unknown } }
interface ListItem {
  flexColumns?: { musicResponsiveListItemFlexColumnRenderer?: { text?: { runs?: Run[] } } }[];
  playlistItemData?: { videoId?: string };
  thumbnail?: { musicThumbnailRenderer?: { thumbnail?: { thumbnails?: { url: string }[] } } };
}

const SEPARATOR = " • ";
const DURATION_RE = /^(?:(\d+):)?(\d{1,2}):(\d{2})$/;
// Joiners between artist names in the subtitle ("A, B & C").
const JOINER_RE = /^\s*(?:,|&|and|x|×)\s*$/i;
const TYPE_WORDS = new Set(["song", "video", "episode"]);

/**
 * Every result row in a search answer, wherever it sits (YouTube moves shelves around, so
 * rows are found by their renderer name rather than by a fixed path).
 */
export function parseSearchResponse(body: unknown, kind: MusicResult["kind"]): MusicResult[] {
  const out: MusicResult[] = [];
  walk(body, (item) => {
    const result = parseItem(item, kind);
    if (result && !out.some((o) => o.videoId === result.videoId)) out.push(result);
  });
  return out;
}

function walk(node: unknown, visit: (item: ListItem) => void): void {
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit);
  } else if (node && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) {
      if (key === "musicResponsiveListItemRenderer") visit(value as ListItem);
      else walk(value, visit);
    }
  }
}

function parseItem(item: ListItem, kind: MusicResult["kind"]): MusicResult | null {
  const columns = (item.flexColumns ?? []).map((c) => c.musicResponsiveListItemFlexColumnRenderer?.text?.runs ?? []);
  const titleRuns = columns[0] ?? [];
  const title = titleRuns.map((r) => r.text).join("").trim();
  const videoId = item.playlistItemData?.videoId ?? titleRuns.find((r) => r.navigationEndpoint?.watchEndpoint)?.navigationEndpoint?.watchEndpoint?.videoId;
  if (!title || !videoId) return null; // an artist, album or playlist row

  // The subtitle is "Artist, Artist • Album • 3:35" (songs) or "Artist • 1.2M views • 3:35" (videos),
  // sometimes with a leading "Song" / "Video" when the search isn't filtered.
  const segments: Run[][] = [[]];
  for (const run of columns[1] ?? []) {
    if (run.text === SEPARATOR) segments.push([]);
    else segments[segments.length - 1].push(run);
  }
  const text = (runs: Run[]) => runs.map((r) => r.text).join("").trim();
  if (segments.length > 1 && TYPE_WORDS.has(text(segments[0]).toLowerCase())) segments.shift();

  const last = text(segments[segments.length - 1] ?? []);
  const duration = DURATION_RE.exec(last);
  const artists = (segments[0] ?? []).map((r) => r.text.trim()).filter((t) => t && !JOINER_RE.test(t));
  const album = kind === "song" && segments.length >= 3 ? text(segments[1]) : undefined;
  const thumbnails = item.thumbnail?.musicThumbnailRenderer?.thumbnail?.thumbnails ?? [];

  return {
    videoId, title, artists,
    ...(album ? { album } : {}),
    durationMs: duration ? ((Number(duration[1] ?? 0) * 60 + Number(duration[2])) * 60 + Number(duration[3])) * 1000 : null,
    thumbnail: thumbnails[0]?.url ?? "",
    kind,
  };
}
