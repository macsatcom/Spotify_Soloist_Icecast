"""Client for Soloist's local WebSocket API. Keeps a merged state cache and fans out events."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import websockets

log = logging.getLogger("soloist-ws")


class CommandError(Exception):
    pass


class SoloistClient:
    def __init__(self, soloist_data_dir: Path):
        self.data_dir = soloist_data_dir
        self.ws: Any = None
        self.connected = False
        self.auth: dict = {"logged_in": False, "is_active": False, "device_name": None}
        self.playback: dict = {}
        self.queue: dict = {"previous": [], "upcoming": []}
        self._subscribers: set[asyncio.Queue] = set()
        self._pending: asyncio.Future | None = None
        self._pending_query: dict[str, asyncio.Future] = {}
        self._cmd_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.on_track_change: list = []

    # ---------- lifecycle ----------
    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="soloist-ws")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _endpoint(self) -> str | None:
        try:
            addr = (self.data_dir / "ws.addr").read_text().strip()
            port = (self.data_dir / "ws.port").read_text().strip()
            return f"ws://{addr}:{port}"
        except FileNotFoundError:
            return None

    async def _loop(self) -> None:
        while True:
            url = self._endpoint()
            if not url:
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(url, max_size=8 * 1024 * 1024, ping_interval=20) as ws:
                    self.ws, self.connected = ws, True
                    log.info("connected to %s", url)
                    self._emit({"type": "system", "event": "soloist_connected"})
                    async for raw in ws:
                        try:
                            self._handle(json.loads(raw))
                        except json.JSONDecodeError:
                            log.warning("bad json from soloist: %r", raw[:200])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("ws error: %s", e)
            if self.connected:
                self._emit({"type": "system", "event": "soloist_disconnected"})
            self.ws, self.connected = None, False
            self.auth["logged_in"] = False
            self._fail_pending("soloist disconnected")
            await asyncio.sleep(1)

    def _fail_pending(self, msg: str) -> None:
        if self._pending and not self._pending.done():
            self._pending.set_exception(CommandError(msg))
        for f in self._pending_query.values():
            if not f.done():
                f.set_exception(CommandError(msg))
        self._pending_query.clear()

    # ---------- events ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _emit(self, ev: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # slow consumer; drop

    def emit(self, ev: dict) -> None:
        """Public: inject system/updater events into the stream."""
        self._emit(ev)

    def _handle(self, ev: dict) -> None:
        t = ev.get("type")
        pb = self.playback
        prev_uri = (pb.get("item") or {}).get("uri")
        if t == "auth_state":
            self.auth = {k: ev.get(k) for k in ("logged_in", "is_active", "device_name")}
            pb["is_active"] = ev.get("is_active")
            if not ev.get("logged_in"):
                self.playback = {}
        elif t == "playback_state":
            self.playback = {k: v for k, v in ev.items() if k != "type"}
        elif t == "track_changed":
            pb["item"] = ev.get("item")
        elif t == "playback_changed":
            pb["status"] = ev.get("status")
        elif t == "volume_changed":
            pb["volume"] = ev.get("volume")
        elif t == "device_changed":
            pb["is_active"] = ev.get("is_active")
            self.auth["is_active"] = ev.get("is_active")
            self.auth["device_name"] = ev.get("device_name")
        elif t == "context_changed":
            pb["context"] = ev.get("context")
        elif t == "options_changed":
            pb["options"] = ev.get("options")
        elif t == "position_sync":
            pb["position"] = ev.get("position")
        elif t == "queue_changed":
            self.queue = {"previous": ev.get("previous", []), "upcoming": ev.get("upcoming", [])}
        elif t in ("command_result", "error"):
            if self._pending and not self._pending.done():
                if t == "error":
                    self._pending.set_exception(CommandError(ev.get("message", "error")))
                else:
                    self._pending.set_result(ev)
            elif t == "error":
                # error could belong to a query
                for f in self._pending_query.values():
                    if not f.done():
                        f.set_exception(CommandError(ev.get("message", "error")))
                self._pending_query.clear()
        # resolve queries
        f = self._pending_query.pop(t, None) if t else None
        if f and not f.done():
            f.set_result(ev)
        new_uri = (self.playback.get("item") or {}).get("uri")
        if new_uri != prev_uri:
            for cb in self.on_track_change:
                try:
                    cb(self.playback)
                except Exception:  # noqa: BLE001
                    log.exception("track change callback failed")
        self._emit(ev)

    # ---------- commands ----------
    async def command(self, command: str, timeout: float = 5, **fields: Any) -> dict:
        if not self.ws:
            raise CommandError("soloist is not running")
        async with self._cmd_lock:
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            await self.ws.send(json.dumps({"type": "command", "command": command, **fields}))
            try:
                return await asyncio.wait_for(self._pending, timeout)
            except asyncio.TimeoutError as e:
                raise CommandError(f"timeout waiting for {command}") from e
            finally:
                self._pending = None

    async def query(self, command: str, reply_type: str, timeout: float = 5, **fields: Any) -> dict:
        if not self.ws:
            raise CommandError("soloist is not running")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_query[reply_type] = fut
        await self.ws.send(json.dumps({"type": "command", "command": command, **fields}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise CommandError(f"timeout waiting for {reply_type}") from e
        finally:
            self._pending_query.pop(reply_type, None)

    async def set_repeat(self, mode: str) -> None:
        if mode == "off":
            await self.command("set_repeat_track", enabled=False)
            await self.command("set_repeat_context", enabled=False)
        elif mode == "context":
            await self.command("set_repeat_track", enabled=False)
            await self.command("set_repeat_context", enabled=True)
        elif mode == "track":
            await self.command("set_repeat_context", enabled=False)
            await self.command("set_repeat_track", enabled=True)
        else:
            raise CommandError(f"invalid repeat mode {mode}")

    # ---------- derived ----------
    @property
    def status(self) -> str:
        return self.playback.get("status") or "idle"

    def position_now(self) -> int | None:
        pos = self.playback.get("position")
        if not pos:
            return None
        elapsed = time.time() * 1000 - pos.get("timestamp_ms", 0)
        return int(pos.get("position_ms", 0) + max(0, elapsed) * pos.get("speed", 0))
