"""The sync engine: one-way (source -> target) and two-way (merge both sides)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import matching
from .models import Playlist, Track
from .providers.base import Provider, ProviderError
from .state import State

Log = Callable[[str], None]

# Stop trying further search queries once a candidate is this good.
GOOD_ENOUGH = 0.9


@dataclass
class DirectionReport:
    source: str
    target: str
    source_playlist: str
    target_playlist: str
    target_playlist_id: str | None = None
    created: bool = False
    already_present: int = 0
    added: list[Track] = field(default_factory=list)
    removed: list[Track] = field(default_factory=list)
    unmatched: list[Track] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.source} '{self.source_playlist}' -> {self.target} '{self.target_playlist}'"]
        if self.created:
            parts.append("created")
        parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        parts.append(f"{self.already_present} already there")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} not found")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "source_playlist": self.source_playlist,
            "target_playlist": self.target_playlist,
            "target_playlist_id": self.target_playlist_id,
            "created": self.created,
            "already_present": self.already_present,
            "added": [t.label() for t in self.added],
            "removed": [t.label() for t in self.removed],
            "unmatched": [t.label() for t in self.unmatched],
            "warnings": self.warnings,
            "summary": self.summary(),
        }


class SyncEngine:
    def __init__(self, providers: dict[str, Provider], state: State, *,
                 threshold: float = 0.72, dry_run: bool = False, log: Log | None = None):
        self.providers = providers
        self.state = state
        self.threshold = threshold
        self.dry_run = dry_run
        self.log = log or (lambda msg: None)

    # --- matching ----------------------------------------------------------

    def find_on(self, track: Track, target: Provider) -> tuple[Track | str | None, float]:
        """Find ``track`` in ``target``'s catalogue. Returns (Track or cached id, score)."""
        if track.service == target.name:
            return track, 1.0

        cached = self.state.get_match(track.service, track.id, target.name)
        if cached is not None:
            return cached.get("id"), cached.get("score", 0.0)

        best: Track | None = None
        best_score = 0.0
        if track.isrc and target.supports_isrc_lookup:
            try:
                hit = target.lookup_isrc(track.isrc)
            except ProviderError:
                hit = None
            if hit:
                best, best_score = hit, 1.0

        if best_score < GOOD_ENOUGH:
            for query in matching.search_queries(track):
                candidates = target.search(query, limit=10)
                cand, s = matching.best_match(track, candidates)
                if cand and s > best_score:
                    best, best_score = cand, s
                if best_score >= GOOD_ENOUGH:
                    break

        if best and best_score >= self.threshold:
            self.state.set_match(track.service, track.id, target.name, best.id, best_score)
            return best, best_score
        self.state.set_match(track.service, track.id, target.name, None, best_score)
        return None, best_score

    def pair_existing(self, source_tracks: list[Track], target_tracks: list[Track]) -> dict[str, Track]:
        """Map source track id -> target track for songs the target playlist already has.

        Uses the match cache, ISRCs, then fuzzy matching against the target's own
        tracks - no search calls needed for songs that are already there.
        """
        by_id = {t.id: t for t in target_tracks}
        by_isrc = {t.isrc.upper(): t for t in target_tracks if t.isrc}
        used: set[str] = set()
        pairs: dict[str, Track] = {}
        pending: list[Track] = []

        for s in source_tracks:
            hit = None
            if s.service == (target_tracks[0].service if target_tracks else None) and s.id in by_id:
                hit = by_id[s.id]
            cached = self.state.get_match(s.service, s.id, target_tracks[0].service) if target_tracks else None
            if not hit and cached and cached.get("id") in by_id:
                hit = by_id[cached["id"]]
            if not hit and s.isrc and s.isrc.upper() in by_isrc:
                hit = by_isrc[s.isrc.upper()]
            if hit and hit.id not in used:
                pairs[s.id] = hit
                used.add(hit.id)
            else:
                pending.append(s)

        remaining = [t for t in target_tracks if t.id not in used]
        for s in pending:
            cand, score = matching.best_match(s, remaining)
            if cand and score >= self.threshold:
                pairs[s.id] = cand
                remaining.remove(cand)
                self.state.set_match(s.service, s.id, cand.service, cand.id, score)
        return pairs

    # --- playlist resolution ----------------------------------------------

    def resolve_target(self, source: Provider, source_playlist: Playlist, target: Provider,
                       target_playlist_id: str | None, target_name: str | None,
                       report: DirectionReport) -> str | None:
        if target_playlist_id:
            return target_playlist_id
        linked = self.state.linked_id(source.name, source_playlist.id, target.name)
        if linked and not target_name:
            if target.get_playlist(linked):
                return linked
            self.log(f"Linked {target.display_name} playlist {linked} is gone; looking again")
        name = target_name or source_playlist.name
        existing = target.find_playlist_by_name(name)
        if existing:
            self.log(f"Using existing {target.display_name} playlist '{existing.name}'")
            return existing.id
        if self.dry_run:
            self.log(f"[dry run] would create {target.display_name} playlist '{name}'")
            return None
        self.log(f"Creating {target.display_name} playlist '{name}'")
        description = source_playlist.description or f"Synced from {source.display_name} by music-sync"
        report.created = True
        return target.create_playlist(name, description[:300])

    # --- one-way -----------------------------------------------------------

    def sync_one_way(self, source_name: str, source_playlist_id: str, target_name_: str, *,
                     target_playlist_id: str | None = None, target_name: str | None = None,
                     delete: bool = False) -> DirectionReport:
        source, target = self.providers[source_name], self.providers[target_name_]
        source_playlist = source.get_playlist(source_playlist_id) or Playlist(
            id=source_playlist_id, name=target_name or source_playlist_id, service=source.name)
        report = DirectionReport(source.display_name, target.display_name, source_playlist.name,
                                 target_name or source_playlist.name)

        self.log(f"Reading {source.display_name} playlist '{source_playlist.name}'")
        source_tracks = source.get_tracks(source_playlist.id)
        self.log(f"  {len(source_tracks)} tracks")
        report.warnings += source.take_notices()

        tgt_id = self.resolve_target(source, source_playlist, target, target_playlist_id, target_name, report)
        report.target_playlist_id = tgt_id
        if tgt_id:
            tgt = target.get_playlist(tgt_id)
            if tgt:
                report.target_playlist = tgt.name
        target_tracks = target.get_tracks(tgt_id) if tgt_id and not report.created else []

        present = self.pair_existing(source_tracks, target_tracks)
        report.already_present = len(present)
        pairs = [(sid, t.id) for sid, t in present.items()]
        target_ids = {t.id for t in target_tracks}

        to_add: list[Track] = []
        missing = [t for t in source_tracks if t.id not in present]
        for i, track in enumerate(missing, 1):
            self.log(f"  [{i}/{len(missing)}] {track.label()}")
            found, score = self.find_on(track, target)
            found_id = found.id if isinstance(found, Track) else found
            if not found_id:
                report.unmatched.append(track)
                self.log(f"      not found (best score {score:.2f})")
                continue
            pairs.append((track.id, found_id))
            if found_id in target_ids:
                report.already_present += 1
                continue
            target_ids.add(found_id)
            to_add.append(found if isinstance(found, Track) else
                          Track(id=found_id, title=track.title, artists=track.artists, service=target.name))

        if to_add:
            if self.dry_run:
                self.log(f"[dry run] would add {len(to_add)} tracks")
            else:
                target.add_tracks(tgt_id, [t.id for t in to_add])
        report.added = to_add

        if delete and target_tracks:
            matched_ids = {tid for _, tid in pairs}
            extra = [t for t in target_tracks if t.id not in matched_ids]
            if extra:
                if not target.supports_removal:
                    report.warnings.append(
                        f"{len(extra)} tracks are only on {target.display_name}; "
                        f"its API can't remove them, so they were left in place")
                elif self.dry_run:
                    self.log(f"[dry run] would remove {len(extra)} tracks")
                    report.removed = extra
                else:
                    target.remove_tracks(tgt_id, extra)
                    report.removed = extra

        if tgt_id and not self.dry_run:
            self.state.upsert_link(source.name, source_playlist.id, target.name, tgt_id, pairs)
        self.state.save()
        self.log(report.summary())
        return report

    # --- two-way -----------------------------------------------------------

    def sync_two_way(self, a_name: str, a_playlist_id: str, b_name: str, *,
                     b_playlist_id: str | None = None, b_playlist_name: str | None = None,
                     delete: bool = False) -> list[DirectionReport]:
        """Merge two playlists so both end up with the union of their songs.

        With ``delete``, a song removed on one side since the last two-way sync
        is removed on the other side too (where that service allows removal).
        """
        a, b = self.providers[a_name], self.providers[b_name]
        a_playlist = a.get_playlist(a_playlist_id) or Playlist(id=a_playlist_id, name=a_playlist_id, service=a.name)

        b_id = b_playlist_id or self.state.linked_id(a.name, a_playlist.id, b.name)
        if b_id and not b.get_playlist(b_id):
            b_id = None
        if not b_id:
            existing = b.find_playlist_by_name(b_playlist_name or a_playlist.name)
            b_id = existing.id if existing else None
        if not b_id:
            # Nothing to merge yet: a plain copy, which also records the snapshot.
            return [self.sync_one_way(a.name, a_playlist.id, b.name, target_name=b_playlist_name)]

        b_playlist = b.get_playlist(b_id) or Playlist(id=b_id, name=b_id, service=b.name)
        self.log(f"Two-way: {a.display_name} '{a_playlist.name}' <-> {b.display_name} '{b_playlist.name}'")
        a_tracks, b_tracks = a.get_tracks(a_playlist.id), b.get_tracks(b_id)
        self.log(f"  {len(a_tracks)} on {a.display_name}, {len(b_tracks)} on {b.display_name}")
        a_by_id, b_by_id = {t.id: t for t in a_tracks}, {t.id: t for t in b_tracks}

        link = self.state.find_link(a.name, a_playlist.id, b.name)
        previous = self.state.link_pairs(link, a.name) if link else []

        ab = DirectionReport(a.display_name, b.display_name, a_playlist.name, b_playlist.name, b_id)
        ba = DirectionReport(b.display_name, a.display_name, b_playlist.name, a_playlist.name, a_playlist.id)
        ab.warnings += a.take_notices()
        ba.warnings += b.take_notices()

        # 1. Deletions since the last sync.
        remove_from_a: list[Track] = []
        remove_from_b: list[Track] = []
        kept_previous: list[tuple[str, str]] = []
        for a_id, b_tid in previous:
            in_a, in_b = a_id in a_by_id, b_tid in b_by_id
            if in_a and in_b:
                kept_previous.append((a_id, b_tid))
            elif delete and in_b and not in_a:
                remove_from_b.append(b_by_id[b_tid])
            elif delete and in_a and not in_b:
                remove_from_a.append(a_by_id[a_id])
            elif in_a or in_b:
                # Not propagating deletes: the surviving copy will be re-added below.
                pass

        removed_a_ids = {t.id for t in remove_from_a}
        removed_b_ids = {t.id for t in remove_from_b}
        live_a = [t for t in a_tracks if t.id not in removed_a_ids]
        live_b = [t for t in b_tracks if t.id not in removed_b_ids]

        # 2. Pair up songs both sides already have.
        paired_a = {x for x, _ in kept_previous}
        paired_b = {y for _, y in kept_previous}
        fresh = self.pair_existing([t for t in live_a if t.id not in paired_a],
                                   [t for t in live_b if t.id not in paired_b])
        pairs = kept_previous + [(x, t.id) for x, t in fresh.items()]
        paired_a |= set(fresh)
        paired_b |= {t.id for t in fresh.values()}
        ab.already_present = ba.already_present = len(pairs)

        # 3. Copy what's only on one side to the other.
        for src_tracks, paired, src, dst, dst_id, report, flip in (
            (live_a, paired_a, a, b, b_id, ab, False),
            (live_b, paired_b, b, a, a_playlist.id, ba, True),
        ):
            dst_ids = set((b_by_id if not flip else a_by_id).keys())
            only_here = [t for t in src_tracks if t.id not in paired]
            to_add: list[str] = []
            for i, track in enumerate(only_here, 1):
                self.log(f"  [{src.display_name} -> {dst.display_name} {i}/{len(only_here)}] {track.label()}")
                found, score = self.find_on(track, dst)
                found_id = found.id if isinstance(found, Track) else found
                if not found_id:
                    report.unmatched.append(track)
                    continue
                pairs.append((found_id, track.id) if flip else (track.id, found_id))
                if found_id in dst_ids:
                    continue
                dst_ids.add(found_id)
                to_add.append(found_id)
                report.added.append(found if isinstance(found, Track) else track)
            if to_add and not self.dry_run:
                dst.add_tracks(dst_id, to_add)

        # 4. Apply deletions.
        for provider, playlist_id, tracks, report in (
            (b, b_id, remove_from_b, ab), (a, a_playlist.id, remove_from_a, ba)):
            if not tracks:
                continue
            if not provider.supports_removal:
                report.warnings.append(
                    f"{len(tracks)} songs were removed on the other side, but {provider.display_name}'s "
                    "API can't remove tracks; remove them by hand")
                continue
            if not self.dry_run:
                provider.remove_tracks(playlist_id, tracks)
            report.removed = tracks

        if not self.dry_run:
            self.state.upsert_link(a.name, a_playlist.id, b.name, b_id, pairs)
        self.state.save()
        for report in (ab, ba):
            self.log(report.summary())
        return [ab, ba]
