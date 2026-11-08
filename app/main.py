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
