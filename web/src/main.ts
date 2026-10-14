import { THRESHOLD } from "./matching";
import { label, type Playlist, type Track } from "./models";
import { isShortLink, parsePlaylistId, readPlaylist, SpotifyError } from "./spotify";
import * as store from "./store";
import * as yt from "./youtube";

interface Row {
  track: Track;
  match?: store.SavedMatch;
  include: boolean;
  el: HTMLTableRowElement;
}

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

let playlist: Playlist | null = null;
let rows: Row[] = [];
let matching = false;
let stopRequested = false;
let creating = false;
let forceNewPlaylist = false;

// --- 1. Spotify ---------------------------------------------------------------------

$("spotify-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (matching || creating) return;
  const ref = $<HTMLInputElement>("spotify-link").value;
  const id = parsePlaylistId(ref);
  const status = $("spotify-status");
  if (!id) {
    setStatus(status, isShortLink(ref)
      ? "That's a short share link: open it in your browser and paste the full open.spotify.com/playlist/… address it goes to."
      : "That doesn't look like a Spotify playlist link (open.spotify.com/playlist/…).", "error");
    return;
  }
  const button = $<HTMLButtonElement>("spotify-read");
  button.disabled = true;
  setStatus(status, "Reading the playlist…");
  try {
    playlist = await readPlaylist(id, (read, total) => setStatus(status, `Reading the playlist… ${read} of ${total} songs`));
    const skipped = playlist.trackCount - playlist.tracks.length;
    setStatus(status, [h("bdi", {}, playlist.name), `: ${count(playlist.tracks.length, "song")}` +
      (skipped > 0 ? ` (${count(skipped, "item")} skipped: podcast episodes, local files or removed songs)` : "")], "ok");
    showPlaylist(playlist);
  } catch (err) {
    setStatus(status, err instanceof SpotifyError ? err.message : `Couldn't read the playlist: ${String(err)}`, "error");
  } finally {
    button.disabled = false;
  }
});

function showPlaylist(p: Playlist): void {
  forceNewPlaylist = false;
  const tbody = $("tracks").querySelector("tbody")!;
  tbody.replaceChildren();
  rows = p.tracks.map((track, i) => {
    const match = store.getMatch(track.id);
    const row: Row = { track, match, include: !!match?.video, el: document.createElement("tr") };
    tbody.append(row.el);
    renderRow(row, i);
    return row;
  });
  $<HTMLInputElement>("yt-title").value = p.name;
  $("tracks").hidden = false;
  $("match-filter").hidden = false;
  $("step-match").hidden = false;
  $("summary").replaceChildren();
  $("step-create").hidden = !rows.some((r) => r.match);
  updateMatchUi();
}

// --- 2. YouTube sign-in -----------------------------------------------------------------

$("yt-signin").addEventListener("click", async () => {
  const status = $("yt-status");
  setStatus(status, "Waiting for Google…");
  try {
    await yt.signIn();
    setStatus(status, `Signed in as ${await yt.me()}`, "ok");
    $("yt-signin").hidden = true;
    $("yt-signout").hidden = false;
  } catch (err) {
    setStatus(status, (err as Error).message, "error");
  }
  updateMatchUi();
});

$("yt-signout").addEventListener("click", () => {
  yt.signOut();
  setStatus($("yt-status"), "");
  $("yt-signin").hidden = false;
  $("yt-signout").hidden = true;
  updateMatchUi();
});

if (!yt.clientId) {
  $<HTMLButtonElement>("yt-signin").disabled = true;
  setStatus($("yt-status"), "This copy of the site has no Google client ID set up yet (VITE_GOOGLE_CLIENT_ID).", "error");
}

// --- 3. Matching ---------------------------------------------------------------------

$("match-start").addEventListener("click", () => void matchAll());
$("match-stop").addEventListener("click", () => { stopRequested = true; });
for (const radio of document.querySelectorAll<HTMLInputElement>('input[name="filter"]')) {
  radio.addEventListener("change", applyFilter);
}

async function matchAll(): Promise<void> {
  if (!yt.signedIn()) return needSignIn();
  matching = true;
  stopRequested = false;
  updateMatchUi();
  const todo = rows.filter((r) => !r.match);
  const progress = $<HTMLProgressElement>("match-progress");
  progress.hidden = false;
  progress.max = todo.length;
  let done = 0, searches = 0, problem = "";
  for (const row of todo) {
    if (stopRequested) break;
    setStatus($("match-status"), `Looking for ${done + 1} of ${todo.length}: ${label(row.track)}`);
    try {
      const m = await yt.findMatch(row.track);
      searches += m.searches;
      row.match = { video: m.video, score: m.score, suggestion: m.suggestion };
      row.include = !!m.video;
      store.setMatch(row.track.id, row.match);
      renderRow(row);
    } catch (err) {
      problem = (err as Error).message;
      if (err instanceof yt.AuthError) setStatus($("yt-status"), problem, "error");
      break;
    }
    progress.value = ++done;
  }
  matching = false;
  progress.hidden = true;
  const left = rows.filter((r) => !r.match).length;
  setStatus($("match-status"), problem
    ? `Stopped: ${problem}.${left ? ` ${count(left, "song")} still to look up; what was found is kept.` : ""}`
    : left ? `Stopped with ${count(left, "song")} still to look up.` : `Done: ${count(searches, "search", "searches")} used.`,
    problem ? "error" : "ok");
  updateMatchUi();
}

function renderRow(row: Row, index = rows.indexOf(row)): void {
  const { track, match } = row;
  const shown = match?.video ?? match?.suggestion;
  row.el.className = !match ? "pending" : match.video ? "matched" : "missing";
  row.el.replaceChildren(
    h("td", { class: "n" }, String(index + 1)),
    h("td", {}, songCell(track.title, track.artists.join(", "), track.durationMs)),
    h("td", {}, ...(!match ? [h("span", { class: "muted" }, "Not looked up yet")]
      : shown ? [videoCell(shown, !match.video)] : [h("span", { class: "muted" }, "No match found")]),
      changeControl(row)),
    h("td", { class: "score" }, match ? scoreBadge(match) : ""),
    h("td", { class: "add" }, includeBox(row)),
  );
}

function songCell(title: string, artists: string, durationMs?: number | null): HTMLElement {
  return h("div", { class: "song" }, h("div", { class: "title", dir: "auto" }, title),
    h("div", { class: "muted", dir: "auto" }, artists + (durationMs ? ` · ${clock(durationMs)}` : "")));
}

function videoCell(v: yt.Video, suggestion: boolean): HTMLElement {
  const link = h("a", { href: yt.videoUrl(v.id), target: "_blank", rel: "noopener", dir: "auto" }, v.title);
  return h("div", { class: `video${suggestion ? " suggestion" : ""}` },
    v.thumbnail ? h("img", { src: v.thumbnail, alt: "", loading: "lazy", width: "48", height: "36" }) : "",
    h("div", { class: "song" }, h("div", { class: "title" }, link),
      h("div", { class: "muted" }, (suggestion ? "Possible match · " : "") + v.channel + (v.durationMs ? ` · ${clock(v.durationMs)}` : ""))));
}

function scoreBadge(m: store.SavedMatch): HTMLElement {
  const pct = Math.round(m.score * 100);
  const cls = m.video ? (m.score >= 0.9 ? "good" : "ok") : "bad";
  return h("span", { class: `badge ${cls}`, title: m.video ? "Will be added if ticked" : `Below the ${Math.round(THRESHOLD * 100)}% needed` }, `${pct}%`);
}

function includeBox(row: Row): HTMLElement {
  const usable = row.match?.video ?? row.match?.suggestion;
  const box = h("input", { type: "checkbox", "aria-label": `Add ${row.track.title}` }) as HTMLInputElement;
  box.checked = row.include;
  box.disabled = !usable || creating;
  box.addEventListener("change", () => {
    if (box.checked && row.match && !row.match.video && row.match.suggestion) {
      // Ticking a possible match accepts it.
      row.match = { video: row.match.suggestion, score: row.match.score };
      store.setMatch(row.track.id, row.match);
    }
    row.include = box.checked;
    renderRow(row);
    updateMatchUi();
  });
  return box;
}

function changeControl(row: Row): HTMLElement {
  const wrap = h("div", { class: "change" });
  const open = h("button", { type: "button", class: "link" }, row.match?.video ? "Change" : "Pick a video") as HTMLButtonElement;
  open.disabled = creating;
  open.addEventListener("click", () => {
    const input = h("input", { type: "text", placeholder: "Paste a YouTube or YouTube Music link", "aria-label": "YouTube link" }) as HTMLInputElement;
    const ok = h("button", { type: "button", class: "secondary" }, "Use it") as HTMLButtonElement;
    const note = h("span", { class: "status error" });
    const submit = async () => {
      const id = yt.parseVideoId(input.value);
      if (!id) return void (note.textContent = "Not a YouTube video link");
      if (!yt.signedIn()) return void (note.textContent = "Sign in with Google first");
      ok.disabled = true;
      try {
        const v = await yt.video(id);
        if (!v) return void (note.textContent = "No such video");
        row.match = { video: v, score: 1 };
        row.include = true;
        store.setMatch(row.track.id, row.match);
        renderRow(row);
        updateMatchUi();
      } catch (err) {
        note.textContent = (err as Error).message;
      } finally {
        ok.disabled = false;
      }
    };
    ok.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") void submit(); });
    wrap.replaceChildren(h("div", { class: "row tight" }, input, ok), note);
    input.focus();
  });
  wrap.append(open);
  return wrap;
}

function applyFilter(): void {
  const missingOnly = document.querySelector<HTMLInputElement>('input[name="filter"][value="missing"]')!.checked;
  for (const row of rows) row.el.hidden = missingOnly && row.include;
}

function updateMatchUi(): void {
  const lookups = rows.filter((r) => !r.match).length;
  const chosen = rows.filter((r) => r.include).length;
  const signed = yt.signedIn();
  const start = $<HTMLButtonElement>("match-start");
  start.hidden = matching || !lookups;
  start.disabled = !signed || creating;
  start.textContent = lookups === rows.length ? "Find the songs on YouTube" : `Find the other ${count(lookups, "song")}`;
  $("match-stop").hidden = !matching;
  if (!matching) {
    if (!lookups && rows.length) setStatusIfEmpty($("match-status"), "Every song has been looked up.");
    else if (!signed) setStatus($("match-status"), "Sign in with Google (step 2) to look the songs up.");
  }
  $("match-counts").textContent = rows.length
    ? `${chosen} of ${rows.length} to add · ${rows.filter((r) => r.match && !r.include).length} not matched or left out` : "";

  const job = playlist && !forceNewPlaylist ? store.getJob(playlist.id) : undefined;
  const toAdd = rows.filter((r) => r.include && !job?.added.includes(r.track.id)).length;
  // Each search is 100 units, each added song 50, of the site's shared 10,000 a day.
  const units = lookups * yt.COST.search + toAdd * yt.COST.insert + (job ? 0 : yt.COST.createPlaylist);
  $("quota-note").textContent = rows.length && (lookups || toAdd)
    ? "YouTube's API gives this site a shared daily budget (10,000 units by default). " +
      `${lookups ? `Looking up ${count(lookups, "song")} and adding` : "Adding"} ${count(toAdd, "song")} costs at least ${units.toLocaleString("en-GB")} units. ` +
      "Matches are remembered in this browser, so if the budget runs out, come back later and carry on where it stopped."
    : "";

  $("step-create").hidden = !rows.some((r) => r.match);
  const create = $<HTMLButtonElement>("create-start");
  create.disabled = creating || matching || !toAdd || !signed;
  create.replaceChildren(...(job
    ? toAdd ? [`Add ${count(toAdd, "song")} to `, h("bdi", {}, job.title), " (made earlier)"] : ["Every chosen song is in the playlist"]
    : [`Create playlist with ${count(toAdd, "song")}`]));
  $("create-new").hidden = !job || creating;
  const title = $<HTMLInputElement>("yt-title");
  if (job) title.value = job.title;
  title.disabled = !!job;
  applyFilter();
}

// --- 4. Creating ------------------------------------------------------------------------

$("create-new").addEventListener("click", () => {
  forceNewPlaylist = true;
  updateMatchUi();
});

$("create-start").addEventListener("click", () => void createPlaylist());

async function createPlaylist(): Promise<void> {
  if (!playlist) return;
  if (!yt.signedIn()) return needSignIn();
  creating = true;
  rows.forEach((r) => renderRow(r));
  updateMatchUi();
  const summary = $("summary");
  summary.replaceChildren(h("p", { class: "status" }, "Creating the playlist…"));
  const progress = $<HTMLProgressElement>("create-progress");

  let job = forceNewPlaylist ? undefined : store.getJob(playlist.id);
  const todo = rows.filter((r) => r.include && r.match?.video && !job?.added.includes(r.track.id));
  let added = 0, problem = "";
  try {
    if (!job) {
      const title = $<HTMLInputElement>("yt-title").value.trim() || playlist.name;
      const privacy = $<HTMLSelectElement>("yt-privacy").value as yt.Privacy;
      const id = await yt.createPlaylist(title, `Copied from Spotify with music-sync: https://open.spotify.com/playlist/${playlist.id}`, privacy);
      job = { playlistId: id, title, added: [] };
      store.setJob(playlist.id, job);
      forceNewPlaylist = false;
    }
    progress.hidden = false;
    progress.max = todo.length;
    for (const row of todo) {
      summary.replaceChildren(h("p", { class: "status" }, `Adding ${added + 1} of ${todo.length}: ${label(row.track)}`));
      await yt.addToPlaylist(job.playlistId, row.match!.video!.id);
      job.added.push(row.track.id);
      store.setJob(playlist.id, job);
      progress.value = ++added;
    }
  } catch (err) {
    problem = (err as Error).message;
    if (err instanceof yt.AuthError) setStatus($("yt-status"), problem, "error");
  }
  creating = false;
  progress.hidden = true;
  showSummary(job, added, todo.length, problem);
  rows.forEach((r) => renderRow(r));
  updateMatchUi();
}

function showSummary(job: store.Job | undefined, added: number, planned: number, problem: string): void {
  const summary = $("summary");
  if (!job) {
    summary.replaceChildren(h("p", { class: "status error" }, `Couldn't create the playlist: ${problem}`));
    return;
  }
  const notAdded = rows.filter((r) => !job.added.includes(r.track.id));
  const unmatched = notAdded.filter((r) => !r.include);
  const left = planned - added;
  summary.replaceChildren(
    h("div", { class: `result ${problem ? "partial" : "done"}` },
      h("h3", {}, problem ? "Partly done" : "Done"),
      h("p", {}, h("a", { href: yt.playlistUrl(job.playlistId), target: "_blank", rel: "noopener" }, "Open ", h("bdi", {}, job.title), " in YouTube Music")),
      h("ul", {},
        h("li", {}, `${count(job.added.length, "song")} in the playlist${added !== job.added.length ? ` (${added} added just now)` : ""}`),
        h("li", {}, `${count(unmatched.length, "song")} skipped (not found or left out)`),
        ...(left ? [h("li", { class: "error" }, `${count(left, "song")} not added yet: ${problem}. Press the button again later to carry on.`)] : [])),
      ...(unmatched.length ? [h("details", {}, h("summary", {}, "Songs that were skipped"),
        h("ol", {}, ...unmatched.map((r) => h("li", {}, h("bdi", {}, label(r.track))))))] : [])));
}

// --- helpers ---------------------------------------------------------------------------

function needSignIn(): void {
  setStatus($("yt-status"), "Sign in with Google first", "error");
  $("step-youtube").scrollIntoView({ behavior: "smooth" });
}

function h(tag: string, attrs: Record<string, string> = {}, ...children: (Node | string)[]): HTMLElement {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...children);
  return el;
}

function setStatus(el: HTMLElement, text: string | (Node | string)[], kind: "" | "ok" | "error" = ""): void {
  el.replaceChildren(...(Array.isArray(text) ? text : [text]));
  el.className = `status ${kind}`.trim();
}

function setStatusIfEmpty(el: HTMLElement, text: string): void {
  if (!el.textContent) setStatus(el, text, "ok");
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-GB")} ${n === 1 ? one : many}`;
}

function clock(ms: number): string {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
