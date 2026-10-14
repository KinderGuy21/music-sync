import { describe, expect, it } from "vitest";
import { ratio, tokenSetRatio } from "../src/fuzz";
import { parseTitle, score, searchQueries, skeleton, splitArtists } from "../src/matching";
import type { Track } from "../src/models";
import parity from "./parity.json";

const track = (title: string, artists: string[], secs: number | null = 200, isrc?: string): Track =>
  ({ id: title, title, artists, service: "spotify", durationMs: secs ? secs * 1000 : null, isrc });

// The CLI's tests/test_matching.py, case for case.
describe("matching (CLI tests)", () => {
  it.each([
    ["Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody", []],
    ["Bohemian Rhapsody (Remastered 2011)", "bohemian rhapsody", []],
    ["Under Pressure (feat. David Bowie)", "under pressure", []],
    ["Under Pressure ft. David Bowie", "under pressure", []],
    ["Hotel California - Live", "hotel california", ["live"]],
    ["Hotel California (Live at the Forum, 1976)", "hotel california", ["live"]],
    ["Layla - Acoustic; Live at MTV Unplugged", "layla", ["acoustic", "live"]],
    ["Blinding Lights (Official Audio)", "blinding lights", []],
    ["Strobe - Radio Edit", "strobe", []],
    ["Levels (Skrillex Remix)", "levels", ["remix"]],
    ["Beyoncé - Halo", "beyonce halo", []], // a dash suffix that isn't noise stays
    ["Don't Stop Me Now", "dont stop me now", []],
  ])("parses %s", (raw, core, tags) => {
    const parsed = parseTitle(raw);
    expect(parsed.core).toBe(core);
    expect([...parsed.tags].sort()).toEqual(tags);
  });

  it("splits artists", () => {
    expect(splitArtists(["Simon & Garfunkel"])).toEqual(["simon", "garfunkel"]);
    expect(splitArtists(["Queen - Topic"])).toEqual(["queen"]);
    expect(splitArtists(["The Beatles"])).toEqual(["beatles"]);
    expect(splitArtists(["Beyoncé, JAY-Z"])).toEqual(["beyonce", "jay z"]);
  });

  it("scores the same song named differently high", () => {
    const spotify = track("Bohemian Rhapsody - Remastered 2011", ["Queen"], 354);
    expect(score(spotify, track("Bohemian Rhapsody (Remastered 2011)", ["Queen"], 355))).toBeGreaterThan(0.95);
    expect(score(spotify, track("Bohemian Rhapsody", ["Queen"], 355))).toBeGreaterThan(0.95);
  });

  it("matches a featured artist in the title against the artist list", () => {
    expect(score(track("Under Pressure", ["Queen", "David Bowie"], 248),
      track("Under Pressure (feat. David Bowie)", ["Queen"], 247))).toBeGreaterThan(0.9);
  });

  it("keeps live and studio apart, and covers", () => {
    expect(score(track("Hotel California", ["Eagles"], 391), track("Hotel California - Live", ["Eagles"], 430))).toBeLessThan(0.72);
    expect(score(track("Hallelujah", ["Leonard Cohen"], 279), track("Hallelujah", ["Pentatonix"], 268))).toBeLessThan(0.72);
  });

  it("treats an ISRC match as certain", () => {
    expect(score(track("Something", ["X"], 200, "GBUM71029604"), track("Totally different title", ["Y"], 200, "gbum71029604"))).toBe(1);
  });

  it("strips noise from queries but keeps the version", () => {
    expect(searchQueries(track("Hotel California - Live (feat. Nobody)", ["Eagles"]))[0]).toBe("Hotel California live Eagles");
    expect(searchQueries(track("Song - Remastered 2009", ["A"]))[0]).toBe("Song A");
  });

  it("matches Hebrew titles to their transliteration", () => {
    const latin = track("Ma La'asot", ["Omer Adam"], 253);
    expect(score(latin, track("מה לעשות", ["עומר אדם"], 253))).toBeGreaterThan(0.85);
    expect(score(latin, track("מלכת הדור", ["Omer Adam"], 184))).toBeLessThan(0.72);
    expect(score(track("מה לעשות", ["עומר אדם"], 253), track("מה לעשות", ["Omer Adam"], 254))).toBeGreaterThan(0.9);
  });
});

// Exact agreement with the Python code (fixture made by tests/make_parity.py).
describe("parity with matching.py", () => {
  it.each(parity.fuzz)("fuzz %#", ({ a, b, ratio: r, token_set_ratio: tsr }) => {
    expect(ratio(a, b)).toBeCloseTo(r, 6);
    expect(tokenSetRatio(a, b)).toBeCloseTo(tsr, 6);
  });
  it.each(parity.titles)("title $raw", ({ raw, core, search, tags }) => {
    const parsed = parseTitle(raw);
    expect({ core: parsed.core, search: parsed.search, tags: [...parsed.tags].sort() }).toEqual({ core, search, tags });
  });
  it.each(parity.skeletons)("skeleton $raw", ({ raw, skeleton: s }) => expect(skeleton(raw)).toBe(s));
  it.each(parity.artists)("artists %#", ({ raw, split }) => expect(splitArtists(raw)).toEqual(split));
  it.each(parity.queries)("queries $title", ({ title, artists, queries }) =>
    expect(searchQueries(track(title, artists))).toEqual(queries));
  it.each(parity.scores)("score %#", ({ a, b, score: s }) =>
    expect(score(track(a[0] as string, a[1] as string[], a[2] as number | null),
      track(b[0] as string, b[1] as string[], b[2] as number | null))).toBeCloseTo(s, 6));
});
