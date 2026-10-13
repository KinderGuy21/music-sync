import itertools

import pytest

from music_sync.state import State
from music_sync.sync import SyncEngine

from .fakes import FakeProvider, T

SONGS = [
    # (title per service, artist, seconds, isrc)
    (("Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody (Remastered 2011)", "Bohemian Rhapsody"),
     "Queen", 354, "GBUM71029604"),
    (("Under Pressure", "Under Pressure (feat. David Bowie)", "Under Pressure"), "Queen", 248, "GBUM71029605"),
    (("Hotel California", "Hotel California", "Hotel California"), "Eagles", 391, "USEE10001992"),
    (("Hotel California - Live", "Hotel California (Live)", "Hotel California (Live)"), "Eagles", 430, "USEE10001993"),
    (("Blinding Lights", "Blinding Lights (Official Audio)", "Blinding Lights"), "The Weeknd", 200, "USUG11904206"),
    (("Levels", "Levels", "Levels"), "Avicii", 199, "SEUM71100001"),
]


@pytest.fixture
def services(tmp_path):
    cat = {"spotify": [], "youtube": [], "apple": []}
    for n, (titles, artist, secs, isrc) in enumerate(SONGS):
        cat["spotify"].append(T("spotify", f"sp{n}", titles[0], artist, secs, isrc))
        # YouTube has no ISRCs and uses " - Topic" channels for some artists.
        cat["youtube"].append(T("youtube", f"yt{n}", titles[1], f"{artist} - Topic" if n % 2 else artist, secs + 1))
        cat["apple"].append(T("apple", f"am{n}", titles[2], artist, secs, isrc))
    # Decoys that should never be picked.
    cat["youtube"].append(T("youtube", "yt-cover", "Bohemian Rhapsody", "Pentatonix", 300))
    cat["apple"].append(T("apple", "am-karaoke", "Levels (Karaoke Version)", "Party Hits", 199))
    providers = {
        "spotify": FakeProvider("spotify", cat["spotify"], isrc=True),
        "youtube": FakeProvider("youtube", cat["youtube"]),
        "apple": FakeProvider("apple", cat["apple"], removal=False, isrc=True),
    }
    state = State(tmp_path / "state.json")
    return providers, state


def engine(services, **kw):
    providers, state = services
    return SyncEngine(providers, state, **kw)


@pytest.mark.parametrize("src,dst", list(itertools.permutations(["spotify", "youtube", "apple"], 2)))
def test_one_way_every_direction_creates_matching_playlist(services, src, dst):
    providers, _ = services
    prefix = {"spotify": "sp", "youtube": "yt", "apple": "am"}
    pid = providers[src].add_playlist("Mix", [f"{prefix[src]}{i}" for i in range(6)])

    report = engine(services).sync_one_way(src, pid, dst)

    assert report.created
    assert not report.unmatched
    target_pid = report.target_playlist_id
    assert providers[dst].contents[target_pid] == [f"{prefix[dst]}{i}" for i in range(6)]


def test_resync_is_idempotent_and_uses_cache(services):
    providers, _ = services
    pid = providers["spotify"].add_playlist("Mix", ["sp0", "sp1", "sp2"])
    e = engine(services)
    first = e.sync_one_way("spotify", pid, "youtube")
    calls = providers["youtube"].search_calls

    providers["spotify"].contents[pid].append("sp4")
    second = e.sync_one_way("spotify", pid, "youtube")

    assert second.target_playlist_id == first.target_playlist_id  # linked, not re-created
    assert not second.created
    assert [t.id for t in second.added] == ["yt4"]
    assert second.already_present == 3
    assert providers["youtube"].search_calls - calls <= 3  # only the new song was searched


def test_existing_target_playlist_found_by_name(services):
    providers, _ = services
    pid = providers["apple"].add_playlist("Road Trip", ["am0", "am2"])
    existing = providers["spotify"].add_playlist("road trip", ["sp0"])

    report = engine(services).sync_one_way("apple", pid, "spotify")

    assert report.target_playlist_id == existing
    assert providers["spotify"].contents[existing] == ["sp0", "sp2"]


def test_one_way_delete_mirrors_source(services):
    providers, _ = services
    src = providers["youtube"].add_playlist("Mix", ["yt0"])
    dst = providers["spotify"].add_playlist("Mix", ["sp0", "sp5"])

    report = engine(services).sync_one_way("youtube", src, "spotify", delete=True)

    assert providers["spotify"].contents[dst] == ["sp0"]
    assert [t.id for t in report.removed] == ["sp5"]


def test_delete_on_apple_target_is_warned_not_attempted(services):
    providers, _ = services
    src = providers["spotify"].add_playlist("Mix", ["sp0"])
    dst = providers["apple"].add_playlist("Mix", ["am0", "am5"])

    report = engine(services).sync_one_way("spotify", src, "apple", delete=True)

    assert providers["apple"].contents[dst] == ["am0", "am5"]
    assert report.warnings


def test_dry_run_changes_nothing(services):
    providers, _ = services
    src = providers["spotify"].add_playlist("Mix", ["sp0", "sp1"])

    report = engine(services, dry_run=True).sync_one_way("spotify", src, "youtube")

    assert not providers["youtube"].playlists
    assert len(report.added) == 2


def test_two_way_merges_both_sides(services):
    providers, _ = services
    a = providers["spotify"].add_playlist("Mix", ["sp0", "sp1"])
    b = providers["youtube"].add_playlist("Mix", ["yt1", "yt4"])

    reports = engine(services).sync_two_way("spotify", a, "youtube")

    assert sorted(providers["spotify"].contents[a]) == ["sp0", "sp1", "sp4"]
    assert sorted(providers["youtube"].contents[b]) == ["yt0", "yt1", "yt4"]
    assert len(reports) == 2


def test_two_way_propagates_removals_with_delete(services):
    providers, _ = services
    a = providers["spotify"].add_playlist("Mix", ["sp0", "sp1", "sp2"])
    e = engine(services)
    e.sync_two_way("spotify", a, "youtube")  # creates the YouTube copy
    b = next(iter(providers["youtube"].playlists))

    providers["youtube"].contents[b].remove("yt1")  # removed on YouTube
    providers["spotify"].contents[a].append("sp4")  # added on Spotify
    e.sync_two_way("spotify", a, "youtube", delete=True)

    assert providers["spotify"].contents[a] == ["sp0", "sp2", "sp4"]
    assert providers["youtube"].contents[b] == ["yt0", "yt2", "yt4"]


def test_two_way_without_delete_restores_removed_song(services):
    providers, _ = services
    a = providers["spotify"].add_playlist("Mix", ["sp0", "sp1"])
    e = engine(services)
    e.sync_two_way("spotify", a, "youtube")
    b = next(iter(providers["youtube"].playlists))

    providers["youtube"].contents[b].remove("yt1")
    e.sync_two_way("spotify", a, "youtube")

    assert sorted(providers["youtube"].contents[b]) == ["yt0", "yt1"]


def test_two_way_from_the_other_end_reuses_link(services):
    providers, _ = services
    a = providers["apple"].add_playlist("Mix", ["am0"])
    e = engine(services)
    e.sync_one_way("apple", a, "youtube")
    b = next(iter(providers["youtube"].playlists))
    providers["youtube"].contents[b].append("yt2")

    e.sync_two_way("youtube", b, "apple")

    assert providers["apple"].contents[a] == ["am0", "am2"]
    assert len(providers["apple"].playlists) == 1


def test_unfindable_song_is_reported(services):
    providers, _ = services
    providers["spotify"].catalogue["sp-x"] = T("spotify", "sp-x", "Obscure B-Side", "Nobody Knows", 180)
    pid = providers["spotify"].add_playlist("Mix", ["sp0", "sp-x"])

    report = engine(services).sync_one_way("spotify", pid, "youtube")

    assert [t.id for t in report.unmatched] == ["sp-x"]
    assert providers["youtube"].contents[report.target_playlist_id] == ["yt0"]
