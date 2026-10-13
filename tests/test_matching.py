import pytest

from music_sync.matching import parse_title, score, search_queries, split_artists
from music_sync.models import Track


def track(title, artists, secs=200, service="x", isrc=None):
    return Track(id=title, title=title, artists=artists, service=service, duration_ms=secs * 1000, isrc=isrc)


@pytest.mark.parametrize("raw,core,tags", [
    ("Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody", set()),
    ("Bohemian Rhapsody (Remastered 2011)", "bohemian rhapsody", set()),
    ("Under Pressure (feat. David Bowie)", "under pressure", set()),
    ("Under Pressure ft. David Bowie", "under pressure", set()),
    ("Hotel California - Live", "hotel california", {"live"}),
    ("Hotel California (Live at the Forum, 1976)", "hotel california", {"live"}),
    ("Layla - Acoustic; Live at MTV Unplugged", "layla", {"live", "acoustic"}),
    ("Blinding Lights (Official Audio)", "blinding lights", set()),
    ("Strobe - Radio Edit", "strobe", set()),
    ("Levels (Skrillex Remix)", "levels", {"remix"}),
    ("Beyoncé - Halo", "beyonce halo", set()),  # a dash suffix that isn't noise stays
    ("Don't Stop Me Now", "dont stop me now", set()),
])
def test_parse_title(raw, core, tags):
    parsed = parse_title(raw)
    assert parsed.core == core
    assert set(parsed.tags) == tags


def test_split_artists():
    assert split_artists(["Simon & Garfunkel"]) == ["simon", "garfunkel"]
    assert split_artists(["Queen - Topic"]) == ["queen"]
    assert split_artists(["The Beatles"]) == ["beatles"]
    assert split_artists(["Beyoncé, JAY-Z"]) == ["beyonce", "jay z"]


def test_same_song_different_naming_scores_high():
    spotify = track("Bohemian Rhapsody - Remastered 2011", ["Queen"], 354)
    youtube = track("Bohemian Rhapsody (Remastered 2011)", ["Queen"], 355)
    apple = track("Bohemian Rhapsody", ["Queen"], 355)
    assert score(spotify, youtube) > 0.95
    assert score(spotify, apple) > 0.95


def test_featured_artist_in_title_vs_artist_list():
    a = track("Under Pressure", ["Queen", "David Bowie"], 248)
    b = track("Under Pressure (feat. David Bowie)", ["Queen"], 247)
    assert score(a, b) > 0.9


def test_live_version_is_not_the_studio_version():
    studio = track("Hotel California", ["Eagles"], 391)
    live = track("Hotel California - Live", ["Eagles"], 430)
    assert score(studio, live) < 0.72


def test_different_artist_same_title_rejected():
    a = track("Hallelujah", ["Leonard Cohen"], 279)
    b = track("Hallelujah", ["Pentatonix"], 268)
    assert score(a, b) < 0.72


def test_isrc_match_is_certain():
    a = track("Something", ["X"], isrc="GBUM71029604")
    b = track("Totally different title", ["Y"], isrc="gbum71029604")
    assert score(a, b) == 1.0


def test_search_queries_strip_noise_but_keep_version():
    queries = search_queries(track("Hotel California - Live (feat. Nobody)", ["Eagles"]))
    assert queries[0] == "Hotel California live Eagles"
    # The cleaned query comes first; the raw title is only a fallback.
    assert search_queries(track("Song - Remastered 2009", ["A"]))[0] == "Song A"


def test_hebrew_title_matches_transliteration():
    latin = track("Ma La'asot", ["Omer Adam"], 253)
    hebrew = track("מה לעשות", ["עומר אדם"], 253)
    other = track("מלכת הדור", ["Omer Adam"], 184)
    assert score(latin, hebrew) > 0.85
    assert score(latin, other) < 0.72


def test_hebrew_on_both_sides():
    assert score(track("מה לעשות", ["עומר אדם"], 253), track("מה לעשות", ["Omer Adam"], 254)) > 0.9
