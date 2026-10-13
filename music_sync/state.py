"""Persistent sync state: cached track matches and linked playlists.

Stored as one JSON file so it's easy to inspect or delete:

    {
      "matches": {"spotify:4uLU6hMC->youtube": {"id": "fJ9rUzIMcZQ", "score": 0.97}, ...},
      "links": [
        {"a": {"service": "spotify", "id": "..."},
         "b": {"service": "youtube", "id": "..."},
         "pairs": [["<a track id>", "<b track id>"], ...],   # snapshot after the last two-way sync
         "last_synced": "2026-10-13T18:00:00"}
      ]
    }
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"matches": {}, "links": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except json.JSONDecodeError:
                pass

    def save(self) -> None:
        with _lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)

    # --- track match cache -------------------------------------------------

    @staticmethod
    def _match_key(source_service: str, source_id: str, target_service: str) -> str:
        return f"{source_service}:{source_id}->{target_service}"

    def get_match(self, source_service: str, source_id: str, target_service: str) -> dict | None:
        """Returns {"id": target_id or None, "score": float} if this was looked up before."""
        return self.data["matches"].get(self._match_key(source_service, source_id, target_service))

    def set_match(self, source_service: str, source_id: str, target_service: str,
                  target_id: str | None, score: float) -> None:
        self.data["matches"][self._match_key(source_service, source_id, target_service)] = {
            "id": target_id,
            "score": round(score, 3),
        }
        # A match works both ways.
        if target_id:
            self.data["matches"][self._match_key(target_service, target_id, source_service)] = {
                "id": source_id,
                "score": round(score, 3),
            }

    def forget_misses(self) -> int:
        misses = [k for k, v in self.data["matches"].items() if not v.get("id")]
        for k in misses:
            del self.data["matches"][k]
        return len(misses)

    # --- playlist links ----------------------------------------------------

    def find_link(self, service: str, playlist_id: str, other_service: str) -> dict | None:
        for link in self.data["links"]:
            ends = {link["a"]["service"]: link["a"]["id"], link["b"]["service"]: link["b"]["id"]}
            if ends.get(service) == playlist_id and other_service in ends and other_service != service:
                return link
        return None

    def linked_id(self, service: str, playlist_id: str, other_service: str) -> str | None:
        link = self.find_link(service, playlist_id, other_service)
        if not link:
            return None
        return link["a"]["id"] if link["a"]["service"] == other_service else link["b"]["id"]

    def upsert_link(self, a_service: str, a_id: str, b_service: str, b_id: str,
                    pairs: list[tuple[str, str]] | None = None) -> dict:
        link = self.find_link(a_service, a_id, b_service)
        if link is None:
            link = {"a": {"service": a_service, "id": a_id}, "b": {"service": b_service, "id": b_id}, "pairs": []}
            self.data["links"].append(link)
        # Normalise orientation so pairs are always stored [a_id, b_id] for the link's own a/b.
        if link["a"]["service"] != a_service:
            pairs = [(b, a) for a, b in pairs] if pairs is not None else None
            link["a"]["id"] = b_id
            link["b"]["id"] = a_id
        else:
            link["a"]["id"] = a_id
            link["b"]["id"] = b_id
        if pairs is not None:
            link["pairs"] = [list(p) for p in pairs]
        link["last_synced"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return link

    def link_pairs(self, link: dict, a_service: str) -> list[tuple[str, str]]:
        """The link's last snapshot, oriented so the first id belongs to ``a_service``."""
        pairs = [tuple(p) for p in link.get("pairs", [])]
        if link["a"]["service"] == a_service:
            return pairs
        return [(b, a) for a, b in pairs]

    def links(self) -> list[dict]:
        return list(self.data["links"])
