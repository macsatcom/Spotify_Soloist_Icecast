"""Minimal asyncio process supervisor with restart backoff and log ring buffers."""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import time
from typing import Awaitable, Callable

log = logging.getLogger("supervisor")

LOG_LINES = 500


class Proc:
    """A supervised child process.

    `build` returns (argv, env) each start so config changes apply on restart.
    `on_exit(code)` may return False to suppress the automatic restart.
    """

    def __init__(self, name: str, build: Callable[[], tuple[list[str], dict[str, str] | None]],
                 on_exit: Callable[[int], Awaitable[bool]] | None = None,
                 redact: Callable[[str], str] = lambda s: s):
        self.name = name
        self.build = build
        self.on_exit = on_exit
        self.redact = redact
        self.logs: collections.deque[str] = collections.deque(maxlen=LOG_LINES)
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.wanted = False
        self.started_at: float | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self._listeners: list[Callable[[str], None]] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def add_listener(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def status(self) -> dict:
        return {"name": self.name, "running": self.running, "wanted": self.wanted,
                "pid": self.proc.pid if self.running else None, "started_at": self.started_at,
                "restarts": self.restarts, "last_exit": self.last_exit}

    def _log(self, line: str) -> None:
        line = self.redact(line.rstrip())
        self.logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        log.info("[%s] %s", self.name, line)
        for fn in self._listeners:
            try:
                fn(line)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def start(self) -> None:
        self.wanted = True
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"proc-{self.name}")

    async def stop(self, timeout: float = 8) -> None:
        self.wanted = False
        await self._kill(timeout)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def _kill(self, timeout: float) -> None:
        p = self.proc
        if not p or p.returncode is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(p.wait(), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await p.wait()

    async def _run(self) -> None:
        backoff = 1.0
        while self.wanted:
            try:
                argv, env = self.build()
            except Exception as e:  # noqa: BLE001
                self._log(f"cannot start: {e}")
                await asyncio.sleep(10)
                continue
            self._log("starting")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL, env={**os.environ, **(env or {})},
                    start_new_session=True)
            except OSError as e:
                self._log(f"spawn failed: {e}")
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            self.started_at = time.time()
            assert self.proc.stdout
            async for raw in self.proc.stdout:
                self._log(raw.decode(errors="replace"))
            code = await self.proc.wait()
            self.last_exit = code
            self._log(f"exited with code {code}")
            ran = time.time() - (self.started_at or time.time())
            if ran > 60:
                backoff = 1.0
            restart = True
            if self.on_exit:
                try:
                    restart = await self.on_exit(code)
                except Exception:  # noqa: BLE001
                    log.exception("on_exit failed")
            if not self.wanted or not restart:
                break
            self.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        self.proc = None


async def run_once(argv: list[str], env: dict[str, str] | None = None, timeout: float | None = None,
                   on_line: Callable[[str], None] | None = None) -> int:
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, env={**os.environ, **(env or {})}, start_new_session=True)

    async def pump():
        assert p.stdout
        async for raw in p.stdout:
            if on_line:
                on_line(raw.decode(errors="replace").rstrip())
        return await p.wait()

    try:
        return await asyncio.wait_for(pump(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await p.wait()
        raise
