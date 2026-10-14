// GET /api/youtube-search?q=…: songs and videos on YouTube Music for one search query.
// Runs the search server-side because YouTube Music's own API only allows Google's sites
// in the browser. Logs no queries. No CORS headers on purpose: only this site's pages can read it.

import { handleSearchRequest } from "../../server/youtubeMusicSearch";

export const onRequestGet: PagesFunction = ({ request }) => handleSearchRequest(request);
