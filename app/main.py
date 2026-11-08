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
