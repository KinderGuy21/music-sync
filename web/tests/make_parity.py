"""Regenerate parity.json: what the CLI's matching.py says about a set of titles and pairs,
so the web app's TypeScript port (src/matching.ts) can be checked against it.

    pip install rapidfuzz && python web/tests/make_parity.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rapidfuzz import fuzz  # noqa: E402

from music_sync import matching  # noqa: E402
from music_sync.models import Track  # noqa: E402

TITLES = [
    "Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody (Remastered 2011)", "Under Pressure (feat. David Bowie)",
    "Hotel California (Live at the Forum, 1976)", "Layla - Acoustic; Live at MTV Unplugged", "Levels (Skrillex Remix)",
    "Beyoncé - Halo", "Don't Stop Me Now", "Queen - Bohemian Rhapsody (Official Video Remastered)",
    "Despacito (feat. Daddy Yankee) - Remix", "Strobe - Radio Edit", "מה לעשות", "Ma La'asot", "מלכת הדור (Prod. by Stav)",
    "Blinding Lights [Official Audio]", "Sped Up Song - sped up", "Kizomba Love (with Anna) - Extended Mix",
    "Señorita", "Ünïcödé Tëst — Live", "liveשלום", "Song (From \"The Movie\")", "Track 2009 - Remaster 2009",
]
ARTISTS = [["Simon & Garfunkel"], ["Queen - Topic"], ["The Beatles"], ["Beyoncé, JAY-Z"], ["QueenVEVO"],
           ["Calvin Harris x Dua Lipa"], ["A feat. B", "C vs. D"], ["עומר אדם"], ["Marshmello; Bastille"]]
PAIRS = [
    (("Bohemian Rhapsody - Remastered 2011", ["Queen"], 354), ("Bohemian Rhapsody (Remastered 2011)", ["Queen"], 355)),
    (("Under Pressure", ["Queen", "David Bowie"], 248), ("Under Pressure (feat. David Bowie)", ["Queen"], 247)),
    (("Hotel California", ["Eagles"], 391), ("Hotel California - Live", ["Eagles"], 430)),
    (("Hallelujah", ["Leonard Cohen"], 279), ("Hallelujah", ["Pentatonix"], 268)),
    (("Ma La'asot", ["Omer Adam"], 253), ("מה לעשות", ["עומר אדם"], 253)),
    (("Ma La'asot", ["Omer Adam"], 253), ("מלכת הדור", ["Omer Adam"], 184)),
    (("מה לעשות", ["עומר אדם"], 253), ("מה לעשות", ["Omer Adam"], 254)),
    (("Bohemian Rhapsody", ["Queen"], 354), ("Queen - Bohemian Rhapsody (Official Video Remastered)", ["Queen Official"], 360)),
    (("Bohemian Rhapsody", ["Queen"], 354), ("Bohemian Rhapsody", ["Queen Official"], None)),
    (("Titanium (feat. Sia)", ["David Guetta", "Sia"], 245), ("David Guetta - Titanium ft. Sia (Official Video)", ["David Guetta"], 250)),
    (("Shape of You", ["Ed Sheeran"], 233), ("Shape of You [Official Video]", ["Ed Sheeran"], 263)),
    (("Shape of You", ["Ed Sheeran"], 233), ("Shape of You (Acoustic)", ["Ed Sheeran"], 223)),
    (("Kizomba Love", ["DJ X", "Anna"], 300), ("Kizomba Love (with Anna) - Extended Mix", ["DJ X"], 420)),
]
STRINGS = [("bohemian rhapsody", "bohemian rhapsody remastered"), ("queen", "queen official"),
           ("simon garfunkel", "garfunkel and simon"), ("abc def", "xyz"), ("a b c", "a b d e"),
           ("hotel california", "california hotel live"), ("sbt", "sbbt"), ("", "")]


def track(title, artists, secs):
    return Track(id=title, title=title, artists=artists, service="x", duration_ms=secs * 1000 if secs else None)


out = {
    "titles": [{"raw": t, "core": (p := matching.parse_title(t)).core, "search": p.search, "tags": sorted(p.tags)}
               for t in TITLES],
    "artists": [{"raw": a, "split": matching.split_artists(a)} for a in ARTISTS],
    "skeletons": [{"raw": t, "skeleton": matching.skeleton(t)} for t in TITLES],
    "queries": [{"title": a[0], "artists": a[1], "queries": matching.search_queries(track(*a))} for a, _ in PAIRS],
    "scores": [{"a": list(a), "b": list(b), "score": matching.score(track(*a), track(*b))} for a, b in PAIRS],
    "fuzz": [{"a": a, "b": b, "ratio": fuzz.ratio(a, b), "token_set_ratio": fuzz.token_set_ratio(a, b)}
             for a, b in STRINGS],
}
Path(__file__).with_name("parity.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
