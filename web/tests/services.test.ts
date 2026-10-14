import { describe, expect, it } from "vitest";
import { totp, totpKey, unescapeJs } from "../server/spotifyToken";
import { parsePlaylistId, pathfinderTrack } from "../src/spotify";
import * as yt from "../src/youtube";

describe("Spotify token (same results as spotify_public.py)", () => {
  it("derives the key and codes like the CLI", async () => {
    const key = totpKey(unescapeJs(String.raw`=n:b#OuEf\x41\\H"S`));
    expect(new TextDecoder().decode(key)).toBe("52100491104665122851198379925569");
    expect(await totp(key, 1760000000)).toBe("847239");
    expect(await totp(key, 59)).toBe("837903");
  });

  it("matches RFC 6238's SHA-1 test vector", async () => {
    const key = new TextEncoder().encode("12345678901234567890");
    expect(await totp(key, 59)).toBe("287082");
    expect(await totp(key, 20000000000)).toBe("353130");
  });
});

describe("Spotify links", () => {
  const id = "37i9dQZF1DXcBWIGoYBM5M";
  it.each([
    `https://open.spotify.com/playlist/${id}`,
    `https://open.spotify.com/playlist/${id}?si=4f8a2c9e1b7d4e3a`,
    `https://open.spotify.com/playlist/${id}?si=abc&pi=u-xyz&_branch_match_id=123&utm_source=copy-link`,
    `https://open.spotify.com/intl-he/playlist/${id}?si=x`,
    `https://open.spotify.com/embed/playlist/${id}`,
    `spotify:playlist:${id}`,
    `  ${id}  `,
    `https://example.app.link/?$fallback_url=https%3A%2F%2Fopen.spotify.com%2Fplaylist%2F${id}%3Fsi%3Dx`,
  ])("finds the id in %s", (ref) => expect(parsePlaylistId(ref)).toBe(id));

  it.each(["https://open.spotify.com/album/1A2GTWGtFfWp7KSQTwWOyo", "hello", ""])("rejects %s", (ref) =>
    expect(parsePlaylistId(ref)).toBeNull());

  it("reads a playlist item and skips episodes", () => {
    expect(pathfinderTrack({ itemV2: { data: {
      __typename: "Track", uri: "spotify:track:abc", name: "Song",
      artists: { items: [{ profile: { name: "A" } }, { profile: { name: "B" } }] },
      albumOfTrack: { name: "Album" }, trackDuration: { totalMilliseconds: 201000 },
    } } })).toEqual({ id: "abc", title: "Song", artists: ["A", "B"], album: "Album", durationMs: 201000, service: "spotify" });
    expect(pathfinderTrack({ itemV2: { data: { __typename: "Episode", uri: "spotify:episode:x" } } })).toBeNull();
    expect(pathfinderTrack(null)).toBeNull();
  });
});

describe("YouTube helpers", () => {
  it("parses durations, links and HTML entities", () => {
    expect(yt.isoDuration("PT3M35S")).toBe(215000);
    expect(yt.isoDuration("PT1H2M")).toBe(3720000);
    expect(yt.isoDuration("P0D")).toBeNull();
    expect(yt.parseVideoId("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x")).toBe("dQw4w9WgXcQ");
    expect(yt.parseVideoId("https://youtu.be/dQw4w9WgXcQ?si=1")).toBe("dQw4w9WgXcQ");
    expect(yt.parseVideoId("nope")).toBeNull();
    expect(yt.decodeEntities("Rock &amp; Roll &#39;n&#x27; &quot;Soul&quot;")).toBe(`Rock & Roll 'n' "Soul"`);
  });

  it("reads 'Artist - Title' uploads both ways", () => {
    const v: yt.Video = { id: "x", title: "Queen - Bohemian Rhapsody (Official Video)", artists: ["Queen Official"],
      channel: "Queen Official", service: "youtube", thumbnail: "" };
    expect(yt.readings(v).map((r) => [r.title, r.artists])).toEqual([
      ["Queen - Bohemian Rhapsody (Official Video)", ["Queen Official"]],
      ["Bohemian Rhapsody (Official Video)", ["Queen"]],
    ]);
  });
});

describe("findMatch against a fake YouTube API", () => {
  type Video = { id: string; title: string; channel: string; secs: number };
  type Fake = { music?: Record<string, Video[]> | "down"; api?: Record<string, Video[]> };
  /** `music`: what /api/youtube-search answers (or "down"); `api`: what the official search.list answers. */
  function fake({ music = {}, api = {} }: Fake) {
    const calls: string[] = [];
    yt.setAccessToken("test-token");
    yt.setHttp(async (input) => {
      const url = new URL(String(input), "https://site.test");
      if (url.pathname === yt.SEARCH_PROXY) {
        calls.push("music");
        if (music === "down") return Response.json({ error: "YouTube Music answered HTTP 403" }, { status: 502 });
        const results = (music[url.searchParams.get("q")!] ?? []).map((v) => ({
          videoId: v.id, title: v.title, artists: [v.channel], durationMs: v.secs * 1000, thumbnail: "" }));
        return Response.json({ results });
      }
      calls.push(url.pathname.split("/").pop()!);
      const all = Object.values(api).flat();
      if (url.pathname.endsWith("/search")) {
        const items = (api[url.searchParams.get("q")!] ?? []).map((v) => ({
          id: { videoId: v.id }, snippet: { title: v.title, channelTitle: v.channel } }));
        return Response.json({ items });
      }
      if (url.pathname.endsWith("/videos")) {
        const ids = url.searchParams.get("id")!.split(",");
        return Response.json({ items: all.filter((v) => ids.includes(v.id)).map((v) => ({
          id: v.id, contentDetails: { duration: `PT${Math.floor(v.secs / 60)}M${v.secs % 60}S` } })) });
      }
      return new Response("{}", { status: 404 });
    });
    return calls;
  }
  const source = { id: "s1", title: "Bohemian Rhapsody - Remastered 2011", artists: ["Queen"], durationMs: 354000, service: "spotify" as const };
  const results = { "Bohemian Rhapsody Queen": [
    { id: "live000000a", title: "Bohemian Rhapsody (Live Aid 1985)", channel: "Queen Official", secs: 360 },
    { id: "studio0000a", title: "Bohemian Rhapsody (Remastered 2011)", channel: "Queen - Topic", secs: 355 },
    { id: "cover00000a", title: "Bohemian Rhapsody", channel: "Pentatonix", secs: 300 },
  ] };
  const coverOnly = { "Bohemian Rhapsody Queen": [{ id: "cover00000a", title: "Bohemian Rhapsody", channel: "Pentatonix", secs: 300 }] };

  it("searches YouTube Music through the site's proxy, without touching the official API", async () => {
    const calls = fake({ music: results });
    const m = await yt.findMatch(source);
    expect(m.video?.id).toBe("studio0000a");
    expect(m.score).toBeGreaterThan(0.9);
    expect([m.searches, m.apiSearches]).toEqual([1, 0]);
    expect(calls).toEqual(["music"]);
  });

  it("falls back to the official search when the proxy fails", async () => {
    const calls = fake({ music: "down", api: results });
    const m = await yt.findMatch(source);
    expect(m.video?.id).toBe("studio0000a");
    expect([m.searches, m.apiSearches]).toEqual([1, 1]);
    expect(calls).toEqual(["music", "search", "videos"]);
  });

  it("falls back to the official search when the proxy finds nothing", async () => {
    const calls = fake({ music: {}, api: results });
    const m = await yt.findMatch(source);
    expect(m.video?.id).toBe("studio0000a");
    expect(calls).toEqual(["music", "search", "videos"]);
  });

  it("falls back when the proxy answers something unexpected", async () => {
    fake({ api: results });
    const real = yt.http;
    yt.setHttp(async (input, init) => String(input).startsWith(yt.SEARCH_PROXY) ? new Response("<html>", { status: 200 }) : real(input, init));
    expect((await yt.findMatch(source)).video?.id).toBe("studio0000a");
  });

  it("reports a below-threshold suggestion instead of adding it", async () => {
    fake({ music: coverOnly });
    const m = await yt.findMatch(source);
    expect(m.video).toBeNull();
    expect(m.suggestion?.id).toBe("cover00000a");
    expect(m.searches).toBe(4); // every query tried before giving up
  });

  it("treats an empty proxy answer as 'not found' once the official quota is gone", async () => {
    yt.setAccessToken("test-token");
    yt.setHttp(async (input) => String(input).startsWith(yt.SEARCH_PROXY)
      ? Response.json({ results: [] })
      : Response.json({ error: { code: 403, errors: [{ reason: "quotaExceeded" }] } }, { status: 403 }));
    const m = await yt.findMatch(source);
    expect([m.video, m.searches, m.apiSearches]).toEqual([null, 4, 0]);
  });

  it("turns an exhausted quota into a QuotaError when the proxy is down too", async () => {
    yt.setAccessToken("test-token");
    yt.setHttp(async (input) => String(input).startsWith(yt.SEARCH_PROXY)
      ? new Response("{}", { status: 502 })
      : Response.json({ error: { code: 403, errors: [{ reason: "quotaExceeded" }] } }, { status: 403 }));
    await expect(yt.findMatch(source)).rejects.toBeInstanceOf(yt.QuotaError);
  });
});
