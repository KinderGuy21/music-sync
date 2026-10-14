import { afterEach, describe, expect, it, vi } from "vitest";
import { clientVersion, handleSearchRequest, parseSearchResponse, searchYouTubeMusic } from "../server/youtubeMusicSearch";
import songs from "./fixtures/ytmusic-search-songs.json";

// A video row the way YouTube Music lays it out: "Artist • views • length".
const videoRow = (videoId: string, title: string, subtitle: string[]) => ({
  musicResponsiveListItemRenderer: {
    playlistItemData: { videoId },
    flexColumns: [
      { musicResponsiveListItemFlexColumnRenderer: { text: { runs: [{ text: title, navigationEndpoint: { watchEndpoint: { videoId } } }] } } },
      { musicResponsiveListItemFlexColumnRenderer: { text: { runs: subtitle.map((text) => ({ text })) } } },
    ],
  },
});
const videos = { contents: { sectionListRenderer: { contents: [{ musicShelfRenderer: { contents: [
  videoRow("fJ9rUzIMcZQ", "Queen – Bohemian Rhapsody (Official Video Remastered)", ["Video", " • ", "Queen Official", " • ", "1.9B views", " • ", "6:00"]),
  { musicResponsiveListItemRenderer: { flexColumns: [{ musicResponsiveListItemFlexColumnRenderer: { text: { runs: [{ text: "Queen" }] } } }] } }, // an artist row
  videoRow("long0000000", "A long mix", ["Some DJ", " • ", "12K views", " • ", "1:02:03"]),
] } }] } } };

describe("reading YouTube Music's search answer", () => {
  it("reads songs from a real (trimmed) answer", () => {
    expect(parseSearchResponse(songs, "song")).toEqual([
      { videoId: "BSTsnWoslP4", title: "Bohemian Rhapsody", artists: ["Queen"], album: "A Night At The Opera", durationMs: 355000,
        thumbnail: expect.stringContaining("googleusercontent.com"), kind: "song" },
      { videoId: "_xwmsnDWfxQ", title: "Bohemian Rhapsody", artists: ["Alex Goot", "Sam Tsui", "Tyler Ward", "Kurt Hugo Schneider"],
        album: "Bohemian Rhapsody", durationMs: 357000, thumbnail: expect.any(String), kind: "song" },
      expect.objectContaining({ videoId: "Eyi-1APybjE", artists: ["Brooklyn Duo"], durationMs: 384000 }),
    ]);
  });

  it("reads videos, skips rows that aren't playable and drops a leading type word", () => {
    expect(parseSearchResponse(videos, "video")).toEqual([
      { videoId: "fJ9rUzIMcZQ", title: "Queen – Bohemian Rhapsody (Official Video Remastered)", artists: ["Queen Official"],
        durationMs: 360000, thumbnail: "", kind: "video" },
      { videoId: "long0000000", title: "A long mix", artists: ["Some DJ"], durationMs: 3723000, thumbnail: "", kind: "video" },
    ]);
  });

  it("finds nothing in an answer it doesn't recognise", () => {
    expect(parseSearchResponse({ contents: { somethingNew: [] } }, "song")).toEqual([]);
    expect(parseSearchResponse(null, "song")).toEqual([]);
  });

  it("sends the web client's context and puts songs before videos", async () => {
    const bodies: { context: { client: { clientName: string; clientVersion: string } }; query: string; params: string }[] = [];
    const http = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init!.body));
      bodies.push(body);
      expect(new Headers(init!.headers).has("Origin")).toBe(false);
      return Response.json(body.params.startsWith("EgWKAQII") ? songs : videos);
    });
    const results = await searchYouTubeMusic("  Bohemian Rhapsody Queen ", http as typeof fetch);
    expect(http).toHaveBeenCalledTimes(2);
    expect(String(http.mock.calls[0][0])).toMatch(/^https:\/\/music\.youtube\.com\/youtubei\/v1\/search/);
    expect(bodies.map((b) => [b.context.client.clientName, b.query])).toEqual([
      ["WEB_REMIX", "Bohemian Rhapsody Queen"], ["WEB_REMIX", "Bohemian Rhapsody Queen"]]);
    expect(results.map((r) => r.kind)).toEqual(["song", "song", "song", "video", "video"]);
  });

  it("keeps the songs when only the video search fails, and fails when both do", async () => {
    const half = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) =>
      JSON.parse(String(init!.body)).params.startsWith("EgWKAQII") ? Response.json(songs) : new Response("", { status: 429 }));
    expect(await searchYouTubeMusic("q", half as typeof fetch)).toHaveLength(3);
    await expect(searchYouTubeMusic("q", (async () => new Response("", { status: 403 })) as typeof fetch))
      .rejects.toThrow("YouTube Music answered HTTP 403");
  });

  it("dates its client version like ytmusicapi", () => {
    expect(clientVersion(new Date("2026-10-14T12:00:00Z"))).toBe("1.20261014.01.00");
  });
});

describe("GET /api/youtube-search", () => {
  afterEach(() => vi.unstubAllGlobals());
  const get = (q?: string) => handleSearchRequest(
    new Request(`https://site.test/api/youtube-search${q === undefined ? "" : `?q=${encodeURIComponent(q)}`}`));

  it("answers the results as JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(songs)));
    const res = await get("Bohemian Rhapsody Queen");
    expect(res.status).toBe(200);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBeNull();
    const body = await res.json() as { results: { videoId: string }[] };
    expect(body.results.map((r) => r.videoId)).toEqual(["BSTsnWoslP4", "_xwmsnDWfxQ", "Eyi-1APybjE"]);
  });

  it("refuses a missing or oversized query without calling YouTube", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    expect((await get()).status).toBe(400);
    expect((await get("x".repeat(201))).status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("answers 502 when YouTube Music can't be searched", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network down"); }));
    const res = await get("anything");
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "Couldn't reach YouTube Music" });
  });
});
