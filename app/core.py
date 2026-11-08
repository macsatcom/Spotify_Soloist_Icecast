"""Orchestrates all processes: PulseAudio, Icecast, encoder, Soloist, updater, metadata."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import encoder
from .config import Env, Store
from .soloist_client import SoloistClient
from .supervisor import Proc, run_once
from .updater import Updater

log = logging.getLogger("core")

PULSE_DIR = Path("/run/pulse")
PULSE_SOCKET = PULSE_DIR / "native"
PULSE_ENV = {"PULSE_SERVER": f"unix:{PULSE_SOCKET}"}
EXIT_EXPIRED = 10
# Harmless in a container (no D-Bus, no per-user home, system mode is intentional)
PULSE_NOISE = ("system bus", "D-Bus", "running PA in system mode", "WhatIsWrongWithSystemWide",
               "cookie", "authentication key", "Home directory of user")


class Core:
    def __init__(self, env: Env):
        self.env = env
        self.store = Store(env.data_dir, env.seed_api_key, env)
        self.soloist_dir = env.data_dir / "soloist"
        self.soloist_dir.mkdir(parents=True, exist_ok=True)
        env.cache_dir.mkdir(parents=True, exist_ok=True)
        self.updater = Updater(env.data_dir)
        self.client = SoloistClient(self.soloist_dir)
        self.client.on_track_change.append(self._on_track_change)
        self.pairing = False
        self.pair_logs: list[str] = []
        self._bg: list[asyncio.Task] = []
        self._apply_lock = asyncio.Lock()
        redact = lambda s: encoder.redact(self._redact_key(s), self.ice)  # noqa: E731

        self.pulse = Proc("pulseaudio", self._pulse_cmd, redact=redact, ignore=PULSE_NOISE)
        self.icecast = Proc("icecast", self._icecast_cmd, redact=redact)
        self.encoder = Proc("encoder", self._encoder_cmd, redact=redact)
        self.soloist = Proc("soloist", self._soloist_cmd, on_exit=self._soloist_exit, redact=redact)
        self.procs = [self.pulse, self.icecast, self.encoder, self.soloist]

    @property
    def ice(self) -> Env:
        """Effective Icecast settings (web UI store), in the Env shape the encoder helpers use."""
        i = self.store.settings.icecast
        return self.env.model_copy(update={
            "icecast_mode": i.mode, "icecast_host": "127.0.0.1" if i.mode == "internal" else i.host,
            "icecast_port": i.port, "icecast_mount": i.mount, "icecast_admin_user": i.admin_user,
            "icecast_source": self.store.get_secret("icecast_source") or "changeme",
            "icecast_admin_pass": self.store.get_secret("icecast_admin_pass") or "changeme_admin"})

    def _redact_key(self, s: str) -> str:
        k = self.store.get_secret("soloist_api_key")
        return s.replace(k, "***") if k else s

    # ---------- command builders ----------
    def _pulse_cmd(self):
        PULSE_DIR.mkdir(parents=True, exist_ok=True)
        enc = self.store.settings.encoder
        fmt = encoder.capture_format(enc)
        pa = Path("/tmp/soloist-pulse.pa")
        pa.write_text(
            f"load-module module-native-protocol-unix socket={PULSE_SOCKET} auth-anonymous=1\n"
            f"load-module module-null-sink sink_name={encoder.PULSE_SINK} format={fmt} "
            f"rate={enc.sample_rate} channels=2 sink_properties=device.description=Soloist\n"
            f"set-default-sink {encoder.PULSE_SINK}\n"
            "load-module module-always-sink\n"
        )
        daemon_conf = Path("/tmp/soloist-daemon.conf")
        daemon_conf.write_text(
            f"default-sample-format = {fmt}\ndefault-sample-rate = {enc.sample_rate}\n"
            f"alternate-sample-rate = {enc.sample_rate}\navoid-resampling = yes\n"
            "resample-method = soxr-vhq\nflat-volumes = no\nexit-idle-time = -1\n"
            "default-fragments = 4\ndefault-fragment-size-msec = 10\n"
            "realtime-scheduling = no\nhigh-priority = no\n"
        )
        return (["pulseaudio", "--system", "--daemonize=no", "--disallow-exit",
                 "--disallow-module-loading", "-n", "-F", str(pa), f"--log-target=stderr",
                 "--use-pid-file=no"], {"PULSE_CONFIG": str(daemon_conf)})

    def _icecast_cmd(self):
        cfg = Path("/tmp/icecast.xml")
        cfg.write_text(encoder.icecast_xml(self.ice, self.store.settings.encoder))
        logdir = Path("/tmp/icecast-log")
        logdir.mkdir(exist_ok=True)
        shutil.chown(logdir, "icecast2", "icecast")
        return ["icecast2", "-c", str(cfg)], None

    def _encoder_cmd(self):
        return ["bash", "-c", encoder.pipeline_shell(self.store.settings.encoder, self.ice)], PULSE_ENV

    def soloist_args(self, extra: list[str] | None = None) -> list[str]:
        s = self.store.settings.soloist
        key = self.store.get_secret("soloist_api_key")
        if not key:
            raise RuntimeError("no Soloist API key configured (set it in the web UI)")
        if not self.updater.binary.exists():
            raise RuntimeError("soloist binary not installed yet")
        args = [str(self.updater.binary), "--device-name", s.device_name, "--api-key", key,
                "--data-dir", str(self.soloist_dir), "--cache-dir", str(self.env.cache_dir)]
        if s.cache_size_mb:
            args += ["--cache-size", str(max(100, s.cache_size_mb))]
        if s.initial_volume is not None:
            args += ["--initial-volume", str(s.initial_volume)]
        if s.verbose:
            args += ["--verbose"]
        args += list(s.extra_args)
        return args + (extra or [])

    def _soloist_cmd(self):
        if self.pairing:
            raise RuntimeError("pairing in progress")
        return self.soloist_args(["--ws", "127.0.0.1:0"]), PULSE_ENV

    async def _soloist_exit(self, code: int) -> bool:
        if code == EXIT_EXPIRED:
            log.warning("soloist build expired; fetching a new one")
            self.client.emit({"type": "updater", "event": "expired"})
            await self.updater.check(force=True)
            self.updater.activate_pending()
        return True

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.pulse.start()
        await self._wait_for(PULSE_SOCKET, 15)
        if self.ice.icecast_mode == "internal":
            self.icecast.start()
            await asyncio.sleep(1)
        self.encoder.start()
        if not self.updater.current():
            await self.updater.check()
            self.updater.activate_pending()
        if self.store.get_secret("soloist_api_key") and self.updater.current():
            self.soloist.start()
        self.client.start()
        self._bg.append(asyncio.create_task(self._update_loop(), name="update-loop"))

    async def shutdown(self) -> None:
        for t in self._bg:
            t.cancel()
        await self.client.stop()
        for p in reversed(self.procs):
            await p.stop()

    @staticmethod
    async def _wait_for(path: Path, timeout: float) -> None:
        for _ in range(int(timeout * 10)):
            if path.exists():
                return
            await asyncio.sleep(0.1)
        log.warning("timeout waiting for %s", path)

    async def restart_component(self, name: str) -> None:
        if name == "pulseaudio":
            await self.encoder.stop()
            await self.soloist.stop()
            await self.pulse.restart()
            await self._wait_for(PULSE_SOCKET, 15)
            self.encoder.start()
            if self.store.get_secret("soloist_api_key"):
                self.soloist.start()
            return
        proc = {p.name: p for p in self.procs}.get(name)
        if not proc:
            raise KeyError(name)
        if name == "icecast" and self.ice.icecast_mode != "internal":
            raise ValueError("icecast is external")
        await proc.restart()

    async def apply_settings(self, changed_sections: set[str], old: dict | None = None) -> None:
        async with self._apply_lock:
            await self._apply_settings(changed_sections, old)

    async def _apply_settings(self, changed_sections: set[str], old: dict | None = None) -> None:
        """Restart only what the changed settings affect."""
        if "encoder" in changed_sections:
            enc = self.store.settings.encoder
            o = (old or {}).get("encoder", {})
            fmt_changed = (o.get("sample_rate") != enc.sample_rate or
                           encoder.capture_format(enc) != encoder.capture_format(
                               type(enc).model_validate({**enc.model_dump(), **o})))
            if fmt_changed:  # PCM format/rate change needs a new null sink (interrupts Soloist briefly)
                await self.restart_component("pulseaudio")
            else:  # codec/bitrate/name: only the encoder restarts, playback continues
                await self.encoder.restart()
        if "icecast" in changed_sections or "icecast_secret" in changed_sections:
            if self.ice.icecast_mode == "internal":
                await self.icecast.restart()  # regenerates icecast.xml
                await asyncio.sleep(1)
            else:
                await self.icecast.stop()
            await self.encoder.restart()
        if "soloist" in changed_sections or "api_key" in changed_sections:
            if self.store.get_secret("soloist_api_key") and self.updater.current():
                await self.soloist.restart()
            else:
                await self.soloist.stop()

    # ---------- pairing ----------
    async def pair(self, timeout: float = 300) -> int:
        if self.pairing:
            raise RuntimeError("already pairing")
        self.pairing = True
        self.pair_logs = []
        await self.soloist.stop()

        def on_line(line: str) -> None:
            line = self._redact_key(line)
            self.pair_logs.append(line)
            self.client.emit({"type": "system", "event": "pair_log", "line": line})

        self.client.emit({"type": "system", "event": "pair_started"})
        code = -1
        try:
            code = await run_once(self.soloist_args(["--pair"]), PULSE_ENV, timeout, on_line)
        except asyncio.TimeoutError:
            on_line("pairing timed out")
        finally:
            self.pairing = False
            self.client.emit({"type": "system", "event": "pair_finished", "exit_code": code})
            self.soloist.start()
        return code

    # ---------- updater ----------
    async def update_now(self, force: bool = False) -> dict:
        staged = await self.updater.check(force=force)
        if staged:
            await self._activate()
        return self.updater.status()

    async def _activate(self) -> None:
        if self.updater.activate_pending():
            self.client.emit({"type": "updater", "event": "activated", "status": self.updater.status()})
            if self.store.get_secret("soloist_api_key"):
                await self.soloist.restart()

    async def _update_loop(self) -> None:
        await asyncio.sleep(30)
        last_check = 0.0
        loop = asyncio.get_running_loop()
        while True:
            us = self.store.settings.updater
            now = loop.time()
            # check on schedule; more frequently when close to expiry
            interval = us.check_interval_hours * 3600
            if self.updater.needs_update(refresh_days_left=10):
                interval = min(interval, 6 * 3600)
            if us.auto_update and now - last_check >= interval:
                last_check = now
                staged = await self.updater.check()
                if staged:
                    self.client.emit({"type": "updater", "event": "staged", "status": self.updater.status()})
            if self.updater.pending:
                cur = self.updater.current()
                idle = self.client.status != "playing"
                urgent = cur is None or cur.days_left() <= us.force_update_days_left
                if idle or urgent:
                    await self._activate()
            await asyncio.sleep(60)

    # ---------- metadata ----------
    def _on_track_change(self, playback: dict) -> None:
        asyncio.get_event_loop().create_task(self._push_metadata(playback))

    async def _push_metadata(self, playback: dict) -> None:
        enc = self.store.settings.encoder
        if not encoder.supports_admin_metadata(enc.codec):
            return
        title = format_title(playback.get("item") or {})
        if not title:
            return
        ice = self.ice
        url = f"http://{ice.icecast_host}:{ice.icecast_port}/admin/metadata"
        params = {"mount": ice.icecast_mount, "mode": "updinfo", "song": title, "charset": "UTF-8"}
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url, params=params, auth=(ice.icecast_admin_user, ice.icecast_admin_pass))
                if r.status_code >= 400:
                    # some servers accept source credentials for metadata
                    r = await c.get(url, params=params, auth=("source", ice.icecast_source))
                if r.status_code >= 400:
                    log.warning("metadata push failed: HTTP %s", r.status_code)
        except httpx.HTTPError as e:
            log.warning("metadata push failed: %s", e)

    # ---------- status ----------
    def stream_url(self, host: str | None = None) -> str:
        ice = self.ice
        h = ice.icecast_host if ice.icecast_mode == "external" else (host or "localhost")
        return f"http://{h}:{ice.icecast_port}{ice.icecast_mount}"

    def status(self, host: str | None = None) -> dict:
        enc = self.store.settings.encoder
        return {
            "time": datetime.now(timezone.utc).isoformat(),
            "soloist": {**self.client.auth, "ws_connected": self.client.connected,
                        "status": self.client.status, "pairing": self.pairing,
                        "api_key_set": bool(self.store.get_secret("soloist_api_key"))},
            "stream": {"url": self.stream_url(host), "codec": enc.codec,
                       "content_type": encoder.CODECS[enc.codec][1], "mode": self.ice.icecast_mode},
            "updater": self.updater.status(),
            "processes": {p.name: p.status() for p in self.procs},
        }


def format_title(item: dict) -> str:
    dec = item.get("decorations") or {}
    name = (dec.get("identity") or {}).get("name") or ""
    artists = ", ".join(((c.get("entity") or {}).get("decorations") or {}).get("identity", {}).get("name", "")
                        for c in dec.get("creators") or [])
    artists = artists.strip(", ")
    return f"{artists} - {name}" if artists and name else name
