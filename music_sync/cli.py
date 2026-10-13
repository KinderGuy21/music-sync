"""Command-line interface: `music-sync --help`."""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

import click

from .config import load_settings
from .models import Playlist
from .providers import SERVICES, NotAuthenticated, Provider, ProviderError, get_provider, resolve_service
from .state import State
from .sync import SyncEngine


class Context:
    def __init__(self):
        self.settings = load_settings()
        self.state = State(self.settings.state_file)
        self.providers: dict[str, Provider] = {n: get_provider(n, self.settings) for n in SERVICES}

    def provider(self, name: str) -> Provider:
        return self.providers[resolve_service(name)]


pass_ctx = click.make_pass_decorator(Context, ensure=True)
SERVICE = click.Choice(["spotify", "youtube", "apple"], case_sensitive=False)


def echo_err(msg: str) -> None:
    click.secho(msg, fg="red", err=True)


def find_playlist(provider: Provider, ref: str) -> Playlist:
    """Accept a playlist id, a share link, an exact name, or an unambiguous part of a name."""
    link_id = provider.parse_playlist_ref(ref)
    if link_id and provider.reads_public_links and not provider.is_authenticated():
        return provider.get_playlist(link_id)  # a public playlist: no Spotify login needed
    playlists = provider.list_playlists()
    for p in playlists:
        if p.id == (link_id or ref):
            return p
    if link_id:
        found = provider.get_playlist(link_id)  # e.g. someone else's public Spotify playlist
        if found:
            return found
        raise click.ClickException(f"Can't open {provider.display_name} playlist {link_id}")
    exact = [p for p in playlists if p.name.lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in playlists if ref.lower() in p.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise click.ClickException(f"No {provider.display_name} playlist matches '{ref}'")
    names = ", ".join(f"'{p.name}'" for p in partial[:8])
    raise click.ClickException(f"'{ref}' matches several {provider.display_name} playlists: {names}")


@click.group()
def main():
    """Sync playlists between Spotify, YouTube Music and Apple Music, in any direction."""


@main.command()
@pass_ctx
def services(ctx: Context):
    """Show each service and whether it's connected."""
    for name, provider in ctx.providers.items():
        ok = provider.is_authenticated()
        status = click.style("connected", fg="green") if ok else click.style("not connected", fg="yellow")
        if not ok and provider.reads_public_links:
            status += click.style("  (public playlist links still work as a source)", fg="yellow")
        click.echo(f"{name:8} {provider.display_name:14} {status}")


@main.command()
@click.argument("service", type=SERVICE)
@click.option("--browser", is_flag=True, help="YouTube Music: log in by pasting browser request headers.")
@click.option("--token", help="Apple Music: store this Music User Token directly.")
@pass_ctx
def auth(ctx: Context, service: str, browser: bool, token: str | None):
    """Connect a service (opens the browser where needed)."""
    provider = ctx.provider(service)
    try:
        if provider.name == "spotify":
            click.echo("Opening Spotify in your browser; approve access and come back here.")
            provider.login_interactive()
        elif provider.name == "youtube":
            if browser:
                click.echo("In a logged-in music.youtube.com tab, open DevTools > Network, click any "
                           "'browse' POST request, copy its request headers, paste them here, "
                           "then press Ctrl-D (Ctrl-Z Enter on Windows).")
                provider.login_with_headers(sys.stdin.read())
            else:
                code = provider.start_device_login()
                click.echo(f"Open {code['url']} and approve access (code {code['user_code']}).")
                webbrowser.open(code["url"])
                click.prompt("Press Enter once you've approved", default="", show_default=False)
                provider.finish_device_login(code["device_code"])
        else:
            if token:
                provider.save_user_token(token)
            else:
                _apple_browser_login(ctx)
    except (NotAuthenticated, ProviderError) as exc:
        raise click.ClickException(str(exc)) from None
    if provider.is_authenticated():
        click.secho(f"{provider.display_name} connected.", fg="green")


def _apple_browser_login(ctx: Context) -> None:
    """Apple's user token only comes from MusicKit JS, so serve the web page and wait for it."""
    from werkzeug.serving import make_server

    from .web.app import create_app

    provider = ctx.providers["apple"]
    provider.developer_token()  # fail early if the developer key isn't configured
    token_file = ctx.settings.apple_user_token_file
    before = token_file.stat().st_mtime if token_file.exists() else 0
    server = make_server(ctx.settings.web_host, ctx.settings.web_port, create_app(ctx.settings))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{ctx.settings.web_host}:{ctx.settings.web_port}/auth/apple"
    click.echo(f"Opening {url} - sign in with your Apple ID there.")
    webbrowser.open(url)
    try:
        while not (token_file.exists() and token_file.stat().st_mtime > before):
            time.sleep(1)
    finally:
        server.shutdown()


@main.command()
@click.argument("service", type=SERVICE)
@pass_ctx
def playlists(ctx: Context, service: str):
    """List your playlists on a service."""
    provider = ctx.provider(service)
    try:
        items = provider.list_playlists()
    except (NotAuthenticated, ProviderError) as exc:
        raise click.ClickException(str(exc)) from None
    for p in items:
        count = "?" if p.track_count is None else p.track_count
        note = "" if p.readable else click.style("  (not yours: contents hidden by Spotify)", fg="yellow")
        click.echo(f"{p.id:40}  {count:>5}  {p.name}{note}")


@main.command()
@click.argument("service", type=SERVICE)
@click.argument("playlist")
@pass_ctx
def tracks(ctx: Context, service: str, playlist: str):
    """List the tracks of a playlist (id, link or name)."""
    provider = ctx.provider(service)
    try:
        for t in provider.get_tracks(find_playlist(provider, playlist).id):
            isrc = f"  [{t.isrc}]" if t.isrc else ""
            click.echo(f"{t.id:24}  {t.label()}{isrc}")
        for notice in provider.take_notices():
            click.secho(f"! {notice}", fg="yellow", err=True)
    except (NotAuthenticated, ProviderError) as exc:
        raise click.ClickException(str(exc)) from None


@main.command()
@click.argument("service", type=SERVICE)
@click.argument("query")
@pass_ctx
def search(ctx: Context, service: str, query: str):
    """Search a service's catalogue for songs."""
    try:
        for t in ctx.provider(service).search(query):
            click.echo(f"{t.id:24}  {t.label()}")
    except (NotAuthenticated, ProviderError) as exc:
        raise click.ClickException(str(exc)) from None


@main.command()
@click.option("--from", "source", required=True, type=SERVICE, help="Service to read from.")
@click.option("--to", "target", required=True, type=SERVICE, help="Service to write to.")
@click.argument("playlist_refs", nargs=-1)
@click.option("--all", "sync_all", is_flag=True, help="Sync every playlist on the source service.")
@click.option("--target-name", help="Name of the playlist on the target (default: same as source).")
@click.option("--two-way", is_flag=True, help="Merge both playlists so each gets the other's songs.")
@click.option("--delete", is_flag=True,
              help="One-way: remove target songs not in the source. Two-way: carry removals across.")
@click.option("--dry-run", is_flag=True, help="Show what would change without changing anything.")
@pass_ctx
def sync(ctx: Context, source: str, target: str, playlist_refs: tuple[str, ...], sync_all: bool,
         target_name: str | None, two_way: bool, delete: bool, dry_run: bool):
    """Sync playlists from one service to another.

    \b
    Examples:
      music-sync sync --from spotify --to youtube "Road Trip"
      music-sync sync --from spotify --to youtube https://open.spotify.com/playlist/<id>?si=...
      music-sync sync --from youtube --to apple --all --dry-run
      music-sync sync --from apple --to spotify "Gym" --two-way
    """
    source, target = resolve_service(source), resolve_service(target)
    if source == target:
        raise click.ClickException("Source and target must be different services")
    if not playlist_refs and not sync_all:
        raise click.ClickException("Name at least one playlist, or pass --all")
    if target_name and (sync_all or len(playlist_refs) > 1):
        raise click.ClickException("--target-name only works with a single playlist")

    engine = SyncEngine(ctx.providers, ctx.state, threshold=ctx.settings.match_threshold,
                        dry_run=dry_run, log=click.echo)
    src = ctx.providers[source]
    try:
        if sync_all:
            selected = [p for p in src.list_playlists() if p.readable]
        else:
            selected = [find_playlist(src, ref) for ref in playlist_refs]
        reports = []
        for playlist in selected:
            click.secho(f"\n== {playlist.name} ==", bold=True)
            if two_way:
                reports += engine.sync_two_way(source, playlist.id, target,
                                               b_playlist_name=target_name, delete=delete)
            else:
                reports.append(engine.sync_one_way(source, playlist.id, target,
                                                   target_name=target_name, delete=delete))
    except (NotAuthenticated, ProviderError) as exc:
        raise click.ClickException(str(exc)) from None

    click.secho("\nDone" + (" (dry run, nothing changed)" if dry_run else ""), bold=True)
    for r in reports:
        click.echo(f"  {r.summary()}")
        for w in r.warnings:
            click.secho(f"    ! {w}", fg="yellow")
        for t in r.unmatched:
            click.secho(f"    not found: {t.label()}", fg="yellow")


@main.command("sync-linked")
@click.option("--delete", is_flag=True, help="Carry removals across.")
@click.option("--dry-run", is_flag=True)
@pass_ctx
def sync_linked(ctx: Context, delete: bool, dry_run: bool):
    """Two-way sync every pair of playlists synced before (handy for cron)."""
    engine = SyncEngine(ctx.providers, ctx.state, threshold=ctx.settings.match_threshold,
                        dry_run=dry_run, log=click.echo)
    links = ctx.state.links()
    if not links:
        click.echo("No linked playlists yet. Run `music-sync sync` first.")
        return
    failed = 0
    for link in links:
        a, b = link["a"], link["b"]
        try:
            for r in engine.sync_two_way(a["service"], a["id"], b["service"],
                                         b_playlist_id=b["id"], delete=delete):
                click.echo(f"  {r.summary()}")
        except (NotAuthenticated, ProviderError) as exc:
            failed += 1
            echo_err(f"{a['service']}:{a['id']} <-> {b['service']}:{b['id']}: {exc}")
    if failed:
        sys.exit(1)


@main.command()
@pass_ctx
def links(ctx: Context):
    """Show playlists that have been synced with each other."""
    for link in ctx.state.links():
        a, b = link["a"], link["b"]
        click.echo(f"{a['service']}:{a['id']}  <->  {b['service']}:{b['id']}  "
                   f"({len(link.get('pairs', []))} songs, last {link.get('last_synced', '?')})")


@main.command("forget-misses")
@pass_ctx
def forget_misses(ctx: Context):
    """Retry songs that weren't found last time (clears cached misses)."""
    n = ctx.state.forget_misses()
    ctx.state.save()
    click.echo(f"Cleared {n} cached misses.")


@main.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@pass_ctx
def web(ctx: Context, host: str | None, port: int | None):
    """Start the web interface."""
    from .web.app import create_app

    host = host or ctx.settings.web_host
    port = port or ctx.settings.web_port
    click.echo(f"music-sync web UI on http://{host}:{port}")
    create_app(ctx.settings).run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
