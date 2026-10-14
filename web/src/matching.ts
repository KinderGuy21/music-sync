/**
 * Cross-service song matching: a port of the CLI's music_sync/matching.py (keep the two in step).
 *
 * Every service names the same recording a little differently:
 *
 *     Spotify:        "Bohemian Rhapsody - Remastered 2011"   by Queen
 *     YouTube Music:  "Bohemian Rhapsody (Remastered 2011)"   by Queen
 *
 * So titles are reduced to a *core* title (no featured artists, no remaster/edition noise)
 * plus a set of *version tags* (live, remix, acoustic...), which must agree: a live recording
 * is not the studio one. Candidates are scored on title, artists and duration.
 */

import { ratio, tokenSetRatio } from "./fuzz";
import { primaryArtist, type Track } from "./models";

export const THRESHOLD = 0.72;
export const GOOD_ENOUGH = 0.9;

// Python's \b is Unicode-aware, JavaScript's only knows ASCII: "\b" in these patterns is
// rewritten to a boundary between [\p{L}\p{N}_] and anything else, so Hebrew behaves too.
const W = "[\\p{L}\\p{N}_]";
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;
const re = (source: string, flags = "") => new RegExp(source.replaceAll("\\b", B), `u${flags}`);

// Words in a bracket or dash suffix that make it a different recording.
const VERSION_WORDS: Record<string, string> = {
  live: "live", remix: "remix", rmx: "remix", acoustic: "acoustic", unplugged: "acoustic",
  instrumental: "instrumental", karaoke: "karaoke", demo: "demo", cover: "cover",
  extended: "extended", sped: "sped up", slowed: "slowed", reverb: "slowed",
  orchestral: "orchestral", piano: "piano", "a cappella": "a cappella", acapella: "a cappella",
};
const VERSION_RES = Object.entries(VERSION_WORDS).map(
  ([word, tag]) => [re(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`), tag] as const);

// Brackets / dash suffixes made only of these are noise and are dropped.
const NOISE_RES = [
  "\\bremaster(ed)?\\b", "\\b(19|20)\\d{2}\\b", "\\bversion\\b", "\\bofficial\\b",
  "\\b(music |lyric )?video\\b", "\\baudio\\b", "\\blyrics?\\b", "\\bexplicit\\b", "\\bclean\\b",
  "\\bmono\\b", "\\bstereo\\b", "\\bdeluxe\\b", "\\bedition\\b", "\\bsingle\\b", "\\balbum\\b",
  "\\bradio\\b", "\\bedit\\b", "\\boriginal\\b", "\\bmix\\b", "\\bhd\\b", "\\bhq\\b", "\\b4k\\b",
  "\\bvisuali[sz]er\\b",
  "\\bfrom\\b.*", // "From the Motion Picture ..."
  "\\bwith\\b.*", "\\bfeat\\b.*", "\\bft\\b.*", "\\bfeaturing\\b.*", "\\bprod\\b.*",
].map((p) => re(p, "g"));

const FEAT_RE = re("\\s*[\\(\\[]?\\s*\\b(feat|ft|featuring|with)\\b\\.?\\s+[^\\)\\]]*[\\)\\]]?", "gi");
const BRACKET_RE = /[([{]([^)\]}]*)[)\]}]/gu;
const DASH_SUFFIX_RE = /\s+[-–—]\s+(.*)$/u;
const ARTIST_SPLIT_RE = re("\\s*(?:,|&|\\+|/|;|\\band\\b|\\bx\\b|\\bvs\\.?|\\bfeat\\.?|\\bft\\.?|\\bfeaturing\\b|\\bwith\\b)\\s*", "i");

/** Lowercase, strip accents and punctuation, collapse spaces. */
export function fold(text: string): string {
  let out = (text || "").normalize("NFKD").replace(/\p{M}/gu, "");
  out = out.toLowerCase().replaceAll("&", " and ").replaceAll("'", "").replaceAll("’", "");
  out = out.replace(/[^\p{L}\p{N}_\s]/gu, " ");
  return out.replace(/\s+/gu, " ").trim();
}

function versionTags(fragment: string): Set<string> {
  const folded = fold(fragment);
  const tags = new Set<string>();
  for (const [pattern, tag] of VERSION_RES) if (pattern.test(folded)) tags.add(tag);
  return tags;
}

function isNoise(fragment: string): boolean {
  let rest = fold(fragment);
  for (const pattern of NOISE_RES) rest = rest.replace(pattern, " ");
  return !rest.trim();
}

// --- Hebrew vs. transliterated Latin --------------------------------------
// One service may list "מה לעשות" by "עומר אדם" while another has "Ma La'asot" by
// "Omer Adam". Vowels don't survive transliteration reliably, so both sides are reduced
// to a consonant skeleton and compared.

const HEBREW_RE = /[֐-׿]/u;
const HEBREW_SKELETON: Record<string, string> = {
  ב: "b", ג: "g", ד: "d", ז: "z", ח: "k", ט: "t", כ: "k", ך: "k",
  ל: "l", מ: "m", ם: "m", נ: "n", ן: "n", ס: "s", פ: "p", ף: "p",
  צ: "z", ץ: "z", ק: "k", ר: "r", ש: "s", ת: "t",
  // Letters that are mostly vowels or silent in transliteration.
  א: "", ה: "", ו: "", י: "", ע: "",
};
const LATIN_DIGRAPHS: [string, string][] = [["sh", "s"], ["ch", "k"], ["kh", "k"], ["tz", "z"], ["ts", "z"], ["ph", "p"]];
const LATIN_SKELETON: Record<string, string> = { f: "p", v: "b", w: "b", c: "k", q: "k", x: "k", j: "g" };

const translate = (text: string, table: Record<string, string>) =>
  Array.from(text, (c) => (c in table ? table[c] : c)).join("");

export function skeleton(text: string): string {
  const words = fold(text).split(" ").filter(Boolean).map((word) => {
    if (HEBREW_RE.test(word)) {
      word = word.replace(/[֑-ׇ]/gu, ""); // niqqud / cantillation
      return translate(word, HEBREW_SKELETON).replace(/[^a-z]/g, "");
    }
    for (const [digraph, sound] of LATIN_DIGRAPHS) word = word.replaceAll(digraph, sound);
    return translate(word, LATIN_SKELETON).replace(/[aeiouyh]/g, "");
  });
  return words.join("").replace(/(.)\1+/gu, "$1"); // "Shabbat" -> "sbt"
}

/** Similarity of a Hebrew string and a Latin one; 0 unless exactly one side is Hebrew. */
export function crossScriptRatio(a: string, b: string): number {
  if (HEBREW_RE.test(a) === HEBREW_RE.test(b)) return 0;
  const sa = skeleton(a), sb = skeleton(b);
  if (sa.length < 2 || sb.length < 2) return 0;
  // Skeletons are lossy, so cap below a real exact match.
  return (0.9 * ratio(sa, sb)) / 100;
}

export interface ParsedTitle {
  core: string; // folded, comparable title
  search: string; // readable title to put in a search query
  tags: Set<string>;
}

const titleCache = new Map<string, ParsedTitle>();

/** Split a title into its core, a searchable form and its version tags. */
export function parseTitle(title: string): ParsedTitle {
  const hit = titleCache.get(title);
  if (hit) return hit;
  const tags = new Set<string>();
  let text = title || "";

  // Featured artists never change the recording; drop them first.
  text = text.replace(FEAT_RE, " ");

  for (const m of text.matchAll(BRACKET_RE)) for (const t of versionTags(m[1])) tags.add(t);
  text = text.replace(BRACKET_RE, " ");

  // "Song - Live at Wembley", "Song - 2011 Remaster"
  const match = DASH_SUFFIX_RE.exec(text);
  if (match) {
    const suffixTags = versionTags(match[1]);
    if (suffixTags.size || isNoise(match[1])) {
      for (const t of suffixTags) tags.add(t);
      text = text.slice(0, match.index);
    }
  }

  const search = text.replace(/\s+/gu, " ").replace(/^[ \-–—]+|[ \-–—]+$/gu, "");
  const parsed = { core: fold(search) || fold(title), search: search || title, tags };
  titleCache.set(title, parsed);
  return parsed;
}

/** Normalise artist names: split joined credits, drop YouTube's ' - Topic'. */
export function splitArtists(artists: string[]): string[] {
  const out: string[] = [];
  for (let artist of artists) {
    artist = (artist || "").replace(/\s+-\s+topic$/iu, "").replace(/vevo$/iu, "");
    for (const part of artist.split(ARTIST_SPLIT_RE)) {
      const name = fold(part).replace(/^the /u, "");
      if (name && !out.includes(name)) out.push(name);
    }
  }
  return out;
}

/** 0..1: how well the best artist on one side matches any on the other. */
export function artistScore(a: string[], b: string[]): number {
  const left = splitArtists(a), right = splitArtists(b);
  if (!left.length || !right.length) return 0.5; // unknown: neither reward nor punish
  let best = 0;
  for (const x of left) for (const y of right) best = Math.max(best, ratio(x, y) / 100, crossScriptRatio(x, y));
  // Joined strings catch "Simon & Garfunkel" vs ["Simon", "Garfunkel"].
  return Math.max(best, tokenSetRatio(left.join(" "), right.join(" ")) / 100);
}

export function titleScore(a: ParsedTitle, b: ParsedTitle): number {
  if (!a.core || !b.core) return 0;
  const exact = ratio(a.core, b.core) / 100;
  const loose = tokenSetRatio(a.core, b.core) / 100;
  return Math.max(exact, loose * 0.95, crossScriptRatio(a.core, b.core));
}

export function durationScore(a?: number | null, b?: number | null): number | null {
  if (!a || !b) return null;
  const diff = Math.abs(a - b) / 1000;
  if (diff <= 3) return 1;
  if (diff >= 30) return 0;
  return 1 - (diff - 3) / 27;
}

/** Similarity in 0..1 between a source track and a search result. */
export function score(source: Track, candidate: Track): number {
  if (source.isrc && candidate.isrc && source.isrc.toUpperCase() === candidate.isrc.toUpperCase()) return 1;

  const st = parseTitle(source.title), ct = parseTitle(candidate.title);
  const t = titleScore(st, ct);
  const a = artistScore(source.artists, candidate.artists);
  const d = durationScore(source.durationMs, candidate.durationMs);

  let total = d === null ? 0.6 * t + 0.4 * a : 0.5 * t + 0.35 * a + 0.15 * d;

  // Same title by someone else (a cover) is a different song.
  if (a < 0.5) total -= 0.25;
  // A different version (live vs studio, remix vs original) is a different song.
  const differing = [...st.tags].filter((x) => !ct.tags.has(x)).length + [...ct.tags].filter((x) => !st.tags.has(x)).length;
  total -= 0.15 * differing;
  return Math.max(0, Math.min(1, total));
}

/** Queries to try, most specific first. */
export function searchQueries(track: Track): string[] {
  const parsed = parseTitle(track.title);
  const tags = [...parsed.tags].sort().join(" ");
  const artist = primaryArtist(track).replace(/\s+-\s+topic$/iu, "");
  const queries = [
    `${parsed.search} ${tags} ${artist}`.trim(),
    `${track.title} ${track.artists.join(" ")}`.trim(),
    `${parsed.search} ${tags}`.trim(),
    // Last resort for titles written differently (e.g. Hebrew vs transliterated):
    // the artist's songs, scored by the cross-script comparison.
    artist,
  ];
  const seen: string[] = [];
  for (let q of queries) {
    q = q.replace(/\s+/gu, " ");
    if (q && !seen.includes(q)) seen.push(q);
  }
  return seen;
}

export function bestMatch<T extends Track>(source: Track, candidates: T[]): [T | null, number] {
  let best: T | null = null, bestScore = 0;
  for (const candidate of candidates) {
    const s = score(source, candidate);
    if (s > bestScore) [best, bestScore] = [candidate, s];
  }
  return [best, bestScore];
}
