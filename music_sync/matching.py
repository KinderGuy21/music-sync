"""Cross-service song matching.

Every service names the same recording a little differently:

    Spotify:        "Bohemian Rhapsody - Remastered 2011"   by Queen
    YouTube Music:  "Bohemian Rhapsody (Remastered 2011)"   by Queen
    Apple Music:    "Bohemian Rhapsody"                      by Queen

So titles are reduced to a *core* title (no featured artists, no
remaster/edition noise) plus a set of *version tags* (live, remix,
acoustic...), which must agree: a live recording is not the studio one.
Candidates are scored on title, artists and duration.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz

from .models import Track

# Words in a bracket or dash suffix that make it a different recording.
VERSION_WORDS = {
    "live": "live",
    "remix": "remix",
    "rmx": "remix",
    "acoustic": "acoustic",
    "unplugged": "acoustic",
    "instrumental": "instrumental",
    "karaoke": "karaoke",
    "demo": "demo",
    "cover": "cover",
    "extended": "extended",
    "sped": "sped up",
    "slowed": "slowed",
    "reverb": "slowed",
    "orchestral": "orchestral",
    "piano": "piano",
    "a cappella": "a cappella",
    "acapella": "a cappella",
}

# Brackets / dash suffixes made only of these are noise and are dropped.
NOISE_PATTERNS = [
    r"\bremaster(ed)?\b",
    r"\b(19|20)\d{2}\b",
    r"\bversion\b",
    r"\bofficial\b",
    r"\b(music |lyric )?video\b",
    r"\baudio\b",
    r"\blyrics?\b",
    r"\bexplicit\b",
    r"\bclean\b",
    r"\bmono\b",
    r"\bstereo\b",
    r"\bdeluxe\b",
    r"\bedition\b",
    r"\bsingle\b",
    r"\balbum\b",
    r"\bradio\b",
    r"\bedit\b",
    r"\boriginal\b",
    r"\bmix\b",
    r"\bhd\b",
    r"\bhq\b",
    r"\b4k\b",
    r"\bvisuali[sz]er\b",
    r"\bfrom\b.*",  # "From the Motion Picture ..."
    r"\bwith\b.*",
    r"\bfeat\b.*",
    r"\bft\b.*",
    r"\bfeaturing\b.*",
    r"\bprod\b.*",
]

FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(feat|ft|featuring|with)\b\.?\s+[^\)\]]*[\)\]]?", re.I)
BRACKET_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
DASH_SUFFIX_RE = re.compile(r"\s+[-–—]\s+(.*)$")
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|;|\band\b|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*", re.I)


def fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _version_tags(fragment: str) -> set[str]:
    folded = fold(fragment)
    tags = set()
    for word, tag in VERSION_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", folded):
            tags.add(tag)
    return tags


def _is_noise(fragment: str) -> bool:
    rest = fold(fragment)
    for pattern in NOISE_PATTERNS:
        rest = re.sub(pattern, " ", rest)
    return not rest.strip()


# --- Hebrew vs. transliterated Latin --------------------------------------
# One service may list "מה לעשות" by "עומר אדם" while another has "Ma La'asot"
# by "Omer Adam". Vowels don't survive transliteration reliably, so both sides
# are reduced to a consonant skeleton and compared.

HEBREW_RE = re.compile(r"[\u0590-\u05ff]")
HEBREW_SKELETON = str.maketrans({
    "ב": "b", "ג": "g", "ד": "d", "ז": "z", "ח": "k", "ט": "t", "כ": "k", "ך": "k",
    "ל": "l", "מ": "m", "ם": "m", "נ": "n", "ן": "n", "ס": "s", "פ": "p", "ף": "p",
    "צ": "z", "ץ": "z", "ק": "k", "ר": "r", "ש": "s", "ת": "t",
    # Letters that are mostly vowels or silent in transliteration.
    "א": None, "ה": None, "ו": None, "י": None, "ע": None,
})
LATIN_DIGRAPHS = [("sh", "s"), ("ch", "k"), ("kh", "k"), ("tz", "z"), ("ts", "z"), ("ph", "p")]
LATIN_SKELETON = str.maketrans({"f": "p", "v": "b", "w": "b", "c": "k", "q": "k", "x": "k", "j": "g"})


def skeleton(text: str) -> str:
    words = []
    for word in fold(text).split():
        if HEBREW_RE.search(word):
            word = re.sub(r"[\u0591-\u05c7]", "", word)  # niqqud / cantillation
            word = re.sub(r"[^a-z]", "", word.translate(HEBREW_SKELETON))
        else:
            for digraph, sound in LATIN_DIGRAPHS:
                word = word.replace(digraph, sound)
            word = re.sub(r"[aeiouyh]", "", word.translate(LATIN_SKELETON))
        words.append(word)
    return re.sub(r"(.)\1+", r"\1", "".join(words))  # "Shabbat" -> "sbt"


def cross_script_ratio(a: str, b: str) -> float:
    """Similarity of a Hebrew string and a Latin one; 0 unless exactly one side is Hebrew."""
    if bool(HEBREW_RE.search(a)) == bool(HEBREW_RE.search(b)):
        return 0.0
    sa, sb = skeleton(a), skeleton(b)
    if len(sa) < 2 or len(sb) < 2:
        return 0.0
    # Skeletons are lossy, so cap below a real exact match.
    return 0.9 * fuzz.ratio(sa, sb) / 100


@dataclass(frozen=True)
class ParsedTitle:
    core: str  # folded, comparable title
    search: str  # readable title to put in a search query
    tags: frozenset[str]


@lru_cache(maxsize=20000)
def parse_title(title: str) -> ParsedTitle:
    """Split a title into its core, a searchable form and its version tags."""
    tags: set[str] = set()
    text = title or ""

    # Featured artists never change the recording; drop them first.
    text = FEAT_RE.sub(" ", text)

    for fragment in BRACKET_RE.findall(text):
        tags |= _version_tags(fragment)
    text = BRACKET_RE.sub(" ", text)

    # "Song - Live at Wembley", "Song - 2011 Remaster"
    match = DASH_SUFFIX_RE.search(text)
    if match:
        suffix = match.group(1)
        suffix_tags = _version_tags(suffix)
        if suffix_tags or _is_noise(suffix):
            tags |= suffix_tags
            text = text[: match.start()]

    search = re.sub(r"\s+", " ", text).strip(" -–—")
    core = fold(search) or fold(title)
    return ParsedTitle(core=core, search=search or title, tags=frozenset(tags))


def split_artists(artists: list[str]) -> list[str]:
    """Normalise artist names: split joined credits, drop YouTube's ' - Topic'."""
    out: list[str] = []
    for artist in artists:
        artist = re.sub(r"\s+-\s+topic$", "", artist or "", flags=re.I)
        artist = re.sub(r"vevo$", "", artist, flags=re.I)
        for part in ARTIST_SPLIT_RE.split(artist):
            name = fold(part)
            name = re.sub(r"^the ", "", name)
            if name and name not in out:
                out.append(name)
    return out


def artist_score(a: list[str], b: list[str]) -> float:
    """0..1: how well the best artist on one side matches any on the other."""
    left, right = split_artists(a), split_artists(b)
    if not left or not right:
        return 0.5  # unknown: neither reward nor punish
    best = 0.0
    for x in left:
        for y in right:
            best = max(best, fuzz.ratio(x, y) / 100, cross_script_ratio(x, y))
    # Joined strings catch "Simon & Garfunkel" vs ["Simon", "Garfunkel"].
    joined = fuzz.token_set_ratio(" ".join(left), " ".join(right)) / 100
    return max(best, joined)


def title_score(a: ParsedTitle, b: ParsedTitle) -> float:
    if not a.core or not b.core:
        return 0.0
    exact = fuzz.ratio(a.core, b.core) / 100
    loose = fuzz.token_set_ratio(a.core, b.core) / 100
    return max(exact, loose * 0.95, cross_script_ratio(a.core, b.core))


def duration_score(a: int | None, b: int | None) -> float | None:
    if not a or not b:
        return None
    diff = abs(a - b) / 1000
    if diff <= 3:
        return 1.0
    if diff >= 30:
        return 0.0
    return 1 - (diff - 3) / 27


def score(source: Track, candidate: Track) -> float:
    """Similarity in 0..1 between a source track and a search result."""
    if source.isrc and candidate.isrc and source.isrc.upper() == candidate.isrc.upper():
        return 1.0

    st, ct = parse_title(source.title), parse_title(candidate.title)
    t = title_score(st, ct)
    a = artist_score(source.artists, candidate.artists)
    d = duration_score(source.duration_ms, candidate.duration_ms)

    if d is None:
        total = 0.6 * t + 0.4 * a
    else:
        total = 0.5 * t + 0.35 * a + 0.15 * d

    # Same title by someone else (a cover) is a different song.
    if a < 0.5:
        total -= 0.25
    # A different version (live vs studio, remix vs original) is a different song.
    if st.tags != ct.tags:
        total -= 0.15 * len(st.tags ^ ct.tags)
    return max(0.0, min(1.0, total))


def search_queries(track: Track) -> list[str]:
    """Queries to try, most specific first."""
    parsed = parse_title(track.title)
    tags = " ".join(sorted(parsed.tags))
    artist = re.sub(r"\s+-\s+topic$", "", track.primary_artist, flags=re.I)
    queries = [
        f"{parsed.search} {tags} {artist}".strip(),
        f"{track.title} {' '.join(track.artists)}".strip(),
        f"{parsed.search} {tags}".strip(),
        # Last resort for titles written differently (e.g. Hebrew vs transliterated):
        # the artist's songs, scored by the cross-script comparison.
        artist,
    ]
    seen: list[str] = []
    for q in queries:
        q = re.sub(r"\s+", " ", q)
        if q and q not in seen:
            seen.append(q)
    return seen


def best_match(source: Track, candidates: list[Track]) -> tuple[Track | None, float]:
    best, best_score = None, 0.0
    for candidate in candidates:
        s = score(source, candidate)
        if s > best_score:
            best, best_score = candidate, s
    return best, best_score
