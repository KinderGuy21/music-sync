/** A song as one service sees it (music_sync/models.py's Track). */
export interface Track {
  /** What that service needs to add the song: Spotify track id, YouTube videoId. */
  id: string;
  title: string;
  artists: string[];
  service: "spotify" | "youtube";
  album?: string | null;
  durationMs?: number | null;
  isrc?: string | null;
}

export interface Playlist {
  id: string;
  name: string;
  description: string;
  trackCount: number;
  tracks: Track[];
}

export const primaryArtist = (t: Track): string => t.artists[0] ?? "";

export const label = (t: Track): string => `${t.artists.join(", ") || "Unknown artist"} - ${t.title}`;
