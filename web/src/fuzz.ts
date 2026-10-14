/**
 * The two rapidfuzz scorers the CLI's matching uses, ported so scores agree (0..100):
 * fuzz.ratio (normalised Indel similarity) and fuzz.token_set_ratio. Like rapidfuzz 3,
 * no preprocessing: callers pass already-folded text.
 */

function lcsLength(a: string[], b: string[]): number {
  if (!a.length || !b.length) return 0;
  let prev = new Array<number>(b.length + 1).fill(0);
  let row = new Array<number>(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      row[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], row[j - 1]);
    }
    [prev, row] = [row, prev];
  }
  return prev[b.length];
}

/** Insertions + deletions turning a into b. */
function indelDistance(a: string, b: string): number {
  const ca = Array.from(a), cb = Array.from(b);
  return ca.length + cb.length - 2 * lcsLength(ca, cb);
}

function normalized(distance: number, lensum: number): number {
  return lensum ? 100 - (100 * distance) / lensum : 100;
}

export function ratio(a: string, b: string): number {
  return normalized(indelDistance(a, b), Array.from(a).length + Array.from(b).length);
}

export function tokenSetRatio(a: string, b: string): number {
  const ta = new Set(a.split(/\s+/).filter(Boolean));
  const tb = new Set(b.split(/\s+/).filter(Boolean));
  if (!ta.size || !tb.size) return 0;
  const sect = [...ta].filter((t) => tb.has(t)).sort();
  const ab = [...ta].filter((t) => !tb.has(t)).sort();
  const ba = [...tb].filter((t) => !ta.has(t)).sort();
  // One side's words are all in the other's.
  if (sect.length && (!ab.length || !ba.length)) return 100;

  const len = (words: string[]) => Array.from(words.join(" ")).length;
  const sectLen = len(sect), abLen = len(ab), baLen = len(ba);
  const sep = sectLen ? 1 : 0;
  const sectAbLen = sectLen + sep + abLen;
  const sectBaLen = sectLen + sep + baLen;

  const result = normalized(indelDistance(ab.join(" "), ba.join(" ")), sectAbLen + sectBaLen);
  if (!sectLen) return result;
  // "sect" against "sect + ab" differs by exactly the ab part (and its space).
  const sectAb = normalized(sep + abLen, sectLen + sectAbLen);
  const sectBa = normalized(sep + baLen, sectLen + sectBaLen);
  return Math.max(result, sectAb, sectBa);
}
