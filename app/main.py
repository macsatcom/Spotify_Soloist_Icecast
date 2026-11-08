"""FastAPI entry point."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import Env
from .core import Core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

env = Env.from_environ()
core = Core(env)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await core.start()
    yield
    await core.shutdown()


app = FastAPI(title="Soloist Icecast", version="0.1.0", lifespan=lifespan)


def auth(request: Request) -> None:
    sec = core.store.settings.security
    if not sec.api_token_enabled:
        return
    token = core.store.get_secret("api_token")
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
        or request.query_params.get("token", "")
    if not token or given != token:
        raise HTTPException(401, "invalid or missing token")


def host_of(request: Request) -> str:
    return (request.headers.get("host") or "localhost").split(":")[0]


api = Depends(auth)


@app.get("/api/v1/status", dependencies=[api])
async def status(request: Request):
    return core.status(host_of(request))


@app.get("/api/v1/health")
async def health():
    return {"ok": core.pulse.running and core.encoder.running}


# ---------- config ----------
@app.get("/api/v1/config", dependencies=[api])
async def get_config():
    return {"settings": core.store.settings.model_dump(mode="json"),
            "secrets": core.store.secrets_status(), "env": env.public()}


@app.put("/api/v1/config", dependencies=[api])
async def put_config(patch: dict):
    try:
        old = core.store.settings.model_dump(mode="json")
        new = core.store.update(patch).model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, str(e)) from e
    changed = {k for k in new if new[k] != old.get(k)}
    asyncio.create_task(core.apply_settings(changed))
    return {"settings": new, "changed": sorted(changed)}


class SecretIn(BaseModel):
    value: str


@app.put("/api/v1/secrets/{key}", dependencies=[api])
async def put_secret(key: str, body: SecretIn):
    try:
        core.store.set_secret(key, body.value.strip())
    except KeyError as e:
        raise HTTPException(404, f"unknown secret {key}") from e
    if key == "soloist_api_key":
        asyncio.create_task(core.apply_settings({"api_key"}))
    return core.store.secrets_status()


# ---------- system ----------
@app.post("/api/v1/system/pair", dependencies=[api])
async def pair():
    if core.pairing:
        raise HTTPException(409, "already pairing")
    if not core.store.get_secret("soloist_api_key"):
        raise HTTPException(400, "set the Soloist API key first")
    asyncio.create_task(core.pair())
    return {"pairing": True, "message": f"Open Spotify and select '{core.store.settings.soloist.device_name}'"}


@app.get("/api/v1/system/pair", dependencies=[api])
async def pair_status():
    return {"pairing": core.pairing, "logs": core.pair_logs[-100:]}


@app.post("/api/v1/system/update", dependencies=[api])
async def update(force: bool = False):
    return await core.update_now(force=force)


@app.get("/api/v1/system/update", dependencies=[api])
async def update_status():
    return core.updater.status()


@app.post("/api/v1/system/restart/{component}", dependencies=[api])
async def restart(component: str):
    try:
        await core.restart_component(component)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return {"restarted": component}


@app.get("/api/v1/system/logs", dependencies=[api])
async def logs(component: str | None = None, lines: int = 200):
    procs = [p for p in core.procs if component in (None, p.name)]
    if not procs:
        raise HTTPException(404, "unknown component")
    return {p.name: list(p.logs)[-lines:] for p in procs}


# ---------- events ----------
@app.get("/api/v1/events", dependencies=[api])
async def events(request: Request):
    q = core.client.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'status', **core.status(host_of(request))})}\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            core.client.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---------- playback control (milestone 3) ----------
from fastapi.responses import RedirectResponse  # noqa: E402

from .soloist_client import CommandError  # noqa: E402


async def _cmd(command: str, **fields):
    try:
        return await core.client.command(command, **fields)
    except CommandError as e:
        raise HTTPException(409, str(e)) from e


class PlayIn(BaseModel):
    uri: str | None = None


class SeekIn(BaseModel):
    position_ms: int


class VolumeIn(BaseModel):
    volume: int


class EnabledIn(BaseModel):
    enabled: bool


class RepeatIn(BaseModel):
    mode: str


class UriIn(BaseModel):
    uri: str


@app.get("/api/v1/now", dependencies=[api])
async def now():
    pb = dict(core.client.playback)
    pb["position_ms_now"] = core.client.position_now()
    return pb


@app.get("/api/v1/queue", dependencies=[api])
async def get_queue(limit: int = 10):
    try:
        ev = await core.client.query("get_queue", "queue_changed", limit=limit)
    except CommandError as e:
        raise HTTPException(409, str(e)) from e
    return {"previous": ev.get("previous", []), "upcoming": ev.get("upcoming", [])}


@app.post("/api/v1/play", dependencies=[api])
async def play(body: PlayIn | None = None):
    return await _cmd("play", **({"uri": body.uri} if body and body.uri else {}))


@app.post("/api/v1/pause", dependencies=[api])
async def pause():
    return await _cmd("pause")


@app.post("/api/v1/next", dependencies=[api])
async def next_():
    return await _cmd("skip_next")


@app.post("/api/v1/prev", dependencies=[api])
async def prev():
    return await _cmd("skip_prev")


@app.post("/api/v1/seek", dependencies=[api])
async def seek(body: SeekIn):
    return await _cmd("seek", position_ms=body.position_ms)


@app.post("/api/v1/volume", dependencies=[api])
async def volume(body: VolumeIn):
    if not 0 <= body.volume <= 100:
        raise HTTPException(422, "volume must be 0-100")
    return await _cmd("set_volume", volume=body.volume)


@app.post("/api/v1/shuffle", dependencies=[api])
async def shuffle(body: EnabledIn):
    return await _cmd("set_shuffle", enabled=body.enabled)


@app.post("/api/v1/repeat", dependencies=[api])
async def repeat(body: RepeatIn):
    try:
        await core.client.set_repeat(body.mode)
    except CommandError as e:
        raise HTTPException(409, str(e)) from e
    return {"repeat": body.mode}


@app.post("/api/v1/queue", dependencies=[api])
async def add_queue(body: UriIn):
    if not body.uri.startswith("spotify:track:"):
        raise HTTPException(422, "only spotify:track: URIs can be queued")
    return await _cmd("add_to_queue", uri=body.uri)


@app.post("/api/v1/activate", dependencies=[api])
async def activate():
    return await _cmd("activate")


@app.post("/api/v1/deactivate", dependencies=[api])
async def deactivate():
    return await _cmd("deactivate")


@app.get("/api/v1/stream")
async def stream(request: Request):
    return RedirectResponse(core.stream_url(host_of(request)), 302)


@app.get("/api/v1/listen", dependencies=[api])
async def listen(request: Request, uri: str | None = None):
    """Lyrion helper: make Soloist active, optionally start a URI, redirect to the stream."""
    if not core.client.auth.get("is_active"):
        try:
            await core.client.command("activate")
        except CommandError:
            pass
    if uri:
        await _cmd("play", uri=uri)
    return RedirectResponse(core.stream_url(host_of(request)), 302)


# ---------- Spotify Web API: auth + browse (milestone 5) ----------
from .webapi import WebApi, WebApiError, entity, page  # noqa: E402

webapi = WebApi(core.store)
SEARCH_TYPES = {"track", "album", "artist", "playlist", "show", "episode"}


async def _w(coro):
    try:
        return await coro
    except WebApiError as e:
        raise HTTPException(e.status if 400 <= e.status < 600 else 502, str(e)) from e


class PasteIn(BaseModel):
    url: str


class WebApiCfgIn(BaseModel):
    client_id: str
    redirect_uri: str | None = None
    client_secret: str | None = None


@app.get("/api/v1/webapi", dependencies=[api])
async def webapi_status():
    if webapi.connected and not webapi.user:
        try:
            await webapi.me()
        except WebApiError:
            pass
    return webapi.status()


@app.put("/api/v1/webapi", dependencies=[api])
async def webapi_cfg(body: WebApiCfgIn):
    patch = {"client_id": body.client_id.strip()}
    if body.redirect_uri:
        patch["redirect_uri"] = body.redirect_uri.strip()
    core.store.update({"webapi": patch})
    if body.client_secret is not None:
        core.store.set_secret("webapi_client_secret", body.client_secret.strip())
    return webapi.status()


@app.get("/api/v1/webapi/login", dependencies=[api])
async def webapi_login():
    return {"url": await _w(_async(webapi.login_url))}


async def _async(fn):
    return fn()


@app.post("/api/v1/webapi/callback", dependencies=[api])
async def webapi_callback(body: PasteIn):
    return await _w(webapi.complete(body.url))


@app.delete("/api/v1/webapi", dependencies=[api])
async def webapi_disconnect():
    webapi.disconnect()
    return webapi.status()


@app.get("/api/v1/search", dependencies=[api])
async def search(q: str, type: str = "track,album,artist,playlist", limit: int = 10, offset: int = 0):
    types = [t for t in type.split(",") if t in SEARCH_TYPES]
    if not types:
        raise HTTPException(422, f"type must be among {sorted(SEARCH_TYPES)}")
    d = await _w(webapi.get("/search", {"q": q, "type": ",".join(types), "limit": max(1, min(limit, 10)),
                                        "offset": offset}, ttl=300))
    return {t: page(d, t + "s") for t in types if d.get(t + "s")}


def _lim(limit: int) -> int:
    return max(1, min(limit, 50))


@app.get("/api/v1/library/playlists", dependencies=[api])
async def my_playlists(limit: int = 50, offset: int = 0):
    return page(await _w(webapi.get("/me/playlists", {"limit": _lim(limit), "offset": offset})))


@app.get("/api/v1/library/tracks", dependencies=[api])
async def my_tracks(limit: int = 50, offset: int = 0):
    return page(await _w(webapi.get("/me/tracks", {"limit": _lim(limit), "offset": offset})), unwrap="track")


@app.get("/api/v1/library/albums", dependencies=[api])
async def my_albums(limit: int = 50, offset: int = 0):
    return page(await _w(webapi.get("/me/albums", {"limit": _lim(limit), "offset": offset})), unwrap="album")


@app.get("/api/v1/library/shows", dependencies=[api])
async def my_shows(limit: int = 50, offset: int = 0):
    return page(await _w(webapi.get("/me/shows", {"limit": _lim(limit), "offset": offset})), unwrap="show")


@app.get("/api/v1/library/artists", dependencies=[api])
async def my_artists(limit: int = 50):
    d = await _w(webapi.get("/me/following", {"type": "artist", "limit": _lim(limit)}))
    return page(d, "artists")


@app.get("/api/v1/library/recent", dependencies=[api])
async def recent(limit: int = 50):
    return page(await _w(webapi.get("/me/player/recently-played", {"limit": _lim(limit)}, ttl=30)), unwrap="track")


@app.get("/api/v1/library/top/{kind}", dependencies=[api])
async def top(kind: str, limit: int = 50, time_range: str = "medium_term"):
    if kind not in ("tracks", "artists"):
        raise HTTPException(404, "kind must be tracks or artists")
    return page(await _w(webapi.get(f"/me/top/{kind}", {"limit": _lim(limit), "time_range": time_range}, ttl=3600)))


@app.get("/api/v1/browse/categories", dependencies=[api])
async def categories(limit: int = 50, offset: int = 0):
    d = await _w(webapi.get("/browse/categories", {"limit": _lim(limit), "offset": offset}, ttl=3600))
    c = d["categories"]
    return {"items": [{"id": i["id"], "name": i["name"], "icon": (i.get("icons") or [{}])[0].get("url")}
                      for i in c.get("items", [])], "total": c.get("total"), "next": bool(c.get("next"))}


@app.get("/api/v1/browse/new-releases", dependencies=[api])
async def new_releases(limit: int = 50, offset: int = 0):
    return page(await _w(webapi.get("/browse/new-releases", {"limit": _lim(limit), "offset": offset}, ttl=3600)), "albums")


@app.get("/api/v1/item/{uri}", dependencies=[api])
async def item(uri: str, limit: int = 50, offset: int = 0):
    """Details + children for any spotify:<type>:<id> URI (playlist/album/artist/show/track/episode)."""
    parts = uri.split(":")
    if len(parts) < 3 or parts[0] != "spotify":
        raise HTTPException(422, "expected spotify:<type>:<id>")
    t, i = parts[-2], parts[-1]  # also handles legacy spotify:user:<u>:playlist:<id>
    lim = {"limit": _lim(limit), "offset": offset}
    if t == "playlist":
        meta = await _w(webapi.get(f"/playlists/{i}"))
        try:  # Spotify replaced /tracks (item key "track") with /items (key "item") in 2026
            kids = page(await _w(webapi.get(f"/playlists/{i}/items", lim)), unwrap="item")
        except HTTPException:
            try:
                kids = page(await _w(webapi.get(f"/playlists/{i}/tracks", lim)), unwrap="track")
            except HTTPException:
                kids = {"items": [], "total": 0, "error": "Spotify only lets development-mode apps list tracks of "
                         "playlists you own or collaborate on. You can still play it; the queue on "
                         "Now playing then shows its tracks."}
    elif t == "album":
        meta = await _w(webapi.get(f"/albums/{i}", ttl=3600))
        kids = page(await _w(webapi.get(f"/albums/{i}/tracks", lim, ttl=3600)))
        for k in kids["items"]:  # album tracks lack images; inherit from album
            k["decorations"].setdefault("visual_identity", {"cover": entity(meta)["decorations"].get("visual_identity", {}).get("cover", [])})
    elif t == "artist":
        meta = await _w(webapi.get(f"/artists/{i}", ttl=3600))
        # dev-mode apps: artist albums max limit 10
        albums = page(await _w(webapi.get(f"/artists/{i}/albums", {"limit": min(lim["limit"], 10), "offset": offset,
                                                                    "include_groups": "album,single,compilation"}, ttl=3600)))
        try:
            tops = await _w(webapi.get(f"/artists/{i}/top-tracks", {"market": (webapi.user or {}).get("country") or "from_token"}, ttl=3600))
            top_tracks = [entity(x) for x in tops.get("tracks", [])]
        except HTTPException:  # top-tracks is forbidden for dev-mode apps; approximate via search
            try:
                d = await _w(webapi.get("/search", {"q": f'artist:"{meta.get("name", "")}"', "type": "track", "limit": 10}, ttl=3600))
                top_tracks = [e for e in page(d, "tracks")["items"]
                              if any(c["entity"]["uri"] == meta.get("uri") for c in e["decorations"].get("creators", []))]
            except HTTPException:
                top_tracks = []
        return {"item": entity(meta), "top_tracks": top_tracks, "children": albums}
    elif t == "show":
        meta = await _w(webapi.get(f"/shows/{i}", ttl=3600))
        kids = page(await _w(webapi.get(f"/shows/{i}/episodes", lim, ttl=600)))
    elif t in ("track", "episode"):
        return {"item": entity(await _w(webapi.get(f"/{t}s/{i}", ttl=3600))), "children": None}
    else:
        raise HTTPException(422, f"unsupported type {t}")
    return {"item": entity(meta), "children": kids}
