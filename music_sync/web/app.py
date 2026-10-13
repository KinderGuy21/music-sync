"""A small local web UI: connect services, pick source/target, run syncs and watch the log."""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template, request, url_for

from ..config import Settings, load_settings
from ..providers import SERVICES, NotAuthenticated, Provider, ProviderError, get_provider, resolve_service
from ..state import State
from ..sync import SyncEngine

# One sync at a time: the engine and the state file aren't built for concurrent writers.
_sync_lock = threading.Lock()


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__)
    providers: dict[str, Provider] = {n: get_provider(n, settings) for n in SERVICES}
    state = State(settings.state_file)
    jobs: dict[str, dict] = {}

    def service_status() -> list[dict]:
        out = []
        for name, p in providers.items():
            entry = {"name": name, "display_name": p.display_name, "connected": p.is_authenticated(),
                     "supports_removal": p.supports_removal}
            if name == "apple":
                entry["configured"] = p.has_developer_credentials()
            elif name == "spotify":
                entry["configured"] = bool(settings.spotify_client_id and settings.spotify_client_secret)
            else:
                entry["configured"] = bool(settings.ytmusic_client_id and settings.ytmusic_client_secret)
            out.append(entry)
        return out

    @app.errorhandler(NotAuthenticated)
    def not_authenticated(exc):
        return jsonify(error=str(exc), auth=True), 401

    @app.errorhandler(ProviderError)
    def provider_error(exc):
        return jsonify(error=str(exc)), 502

    # --- pages -------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html", services=service_status())

    # --- auth: Spotify -----------------------------------------------------

    @app.get("/auth/spotify")
    def auth_spotify():
        return redirect(providers["spotify"].authorize_url())

    @app.get("/auth/spotify/callback")
    def auth_spotify_callback():
        if request.args.get("error"):
            return render_template("message.html", title="Spotify", message=f"Spotify said: {request.args['error']}")
        providers["spotify"].complete_login(request.args["code"])
        return redirect(url_for("index"))

    # --- auth: YouTube Music -----------------------------------------------

    @app.get("/auth/youtube")
    def auth_youtube():
        code, error = None, None
        if settings.ytmusic_client_id and settings.ytmusic_client_secret:
            try:
                code = providers["youtube"].start_device_login()
            except Exception as exc:  # show the reason on the page rather than a 500
                error = str(exc)
        return render_template("youtube_auth.html", code=code, error=error)

    @app.post("/auth/youtube/finish")
    def auth_youtube_finish():
        try:
            providers["youtube"].finish_device_login(request.form["device_code"])
        except Exception as exc:
            return render_template("message.html", title="YouTube Music",
                                   message=f"Login didn't complete: {exc}", back="/auth/youtube")
        return redirect(url_for("index"))

    @app.post("/auth/youtube/headers")
    def auth_youtube_headers():
        try:
            providers["youtube"].login_with_headers(request.form["headers"])
        except Exception as exc:
            return render_template("message.html", title="YouTube Music",
                                   message=f"Those headers didn't work: {exc}", back="/auth/youtube")
        return redirect(url_for("index"))

    # --- auth: Apple Music -------------------------------------------------

    @app.get("/auth/apple")
    def auth_apple():
        apple = providers["apple"]
        try:
            token = apple.developer_token()
        except Exception as exc:
            return render_template("message.html", title="Apple Music", message=str(exc))
        return render_template("apple_auth.html", developer_token=token)

    @app.post("/auth/apple/token")
    def auth_apple_token():
        token = (request.get_json(silent=True) or {}).get("token", "")
        if not token:
            return jsonify(error="no token"), 400
        providers["apple"].save_user_token(token)
        return jsonify(ok=True)

    # --- API -----------------------------------------------------------------

    @app.get("/api/services")
    def api_services():
        return jsonify(service_status())

    @app.get("/api/playlists/<service>")
    def api_playlists(service: str):
        provider = providers[resolve_service(service)]
        return jsonify([p.to_dict() for p in provider.list_playlists()])

    @app.get("/api/links")
    def api_links():
        return jsonify(state.links())

    @app.post("/api/sync")
    def api_sync():
        body = request.get_json(force=True)
        source = resolve_service(body["source"])
        target = resolve_service(body["target"])
        if source == target:
            return jsonify(error="Source and target must be different services"), 400
        # Ticked playlist ids and/or pasted share links (e.g. a public Spotify playlist).
        playlist_ids = [providers[source].parse_playlist_ref(p) or p.strip()
                        for p in body.get("playlists") or [] if p.strip()]
        if not playlist_ids:
            return jsonify(error="Pick at least one playlist"), 400
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "status": "queued", "log": [], "reports": [], "error": None,
               "started": datetime.now().isoformat(timespec="seconds")}
        jobs[job_id] = job

        def log(msg: str) -> None:
            job["log"].append(msg)

        def run() -> None:
            with _sync_lock:
                job["status"] = "running"
                engine = SyncEngine(providers, state, threshold=settings.match_threshold,
                                    dry_run=bool(body.get("dry_run")), log=log)
                try:
                    for pid in playlist_ids:
                        if body.get("two_way"):
                            reports = engine.sync_two_way(source, pid, target, delete=bool(body.get("delete")),
                                                          b_playlist_name=body.get("target_name") or None)
                        else:
                            reports = [engine.sync_one_way(source, pid, target, delete=bool(body.get("delete")),
                                                           target_name=body.get("target_name") or None)]
                        job["reports"] += [r.to_dict() for r in reports]
                    job["status"] = "done"
                except Exception as exc:
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    log(traceback.format_exc(limit=3))

        threading.Thread(target=run, daemon=True).start()
        return jsonify(id=job_id)

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="unknown job"), 404
        return jsonify(job)

    return app
