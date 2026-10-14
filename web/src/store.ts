/**
 * What this browser remembers, in localStorage (nothing leaves the browser):
 *  - matches: Spotify track id -> the YouTube video chosen for it, or none, so a song is
 *    never searched twice (and a fallback to the official search, 100 quota units, isn't repeated);
 *  - jobs: Spotify playlist id -> the YouTube playlist made from it and the songs already
 *    added, so a copy cut short (quota, closed tab) carries on instead of starting over.
 */

import type { Video } from "./youtube";

const MATCHES = "music-sync:matches";
const JOBS = "music-sync:jobs";

export interface SavedMatch { video: Video | null; score: number; suggestion?: Video }
export interface Job { playlistId: string; title: string; added: string[] }

function load<T>(key: string): Record<string, T> {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "{}");
  } catch {
    return {};
  }
}

function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage full or disabled: just don't remember
  }
}

export const getMatch = (trackId: string): SavedMatch | undefined => load<SavedMatch>(MATCHES)[trackId];

export function setMatch(trackId: string, match: SavedMatch): void {
  const all = load<SavedMatch>(MATCHES);
  all[trackId] = match;
  save(MATCHES, all);
}

export const getJob = (spotifyId: string): Job | undefined => load<Job>(JOBS)[spotifyId];

export function setJob(spotifyId: string, job: Job | null): void {
  const all = load<Job>(JOBS);
  if (job) all[spotifyId] = job;
  else delete all[spotifyId];
  save(JOBS, all);
}

export function forgetMatches(trackIds: string[]): void {
  const all = load<SavedMatch>(MATCHES);
  for (const id of trackIds) delete all[id];
  save(MATCHES, all);
}
