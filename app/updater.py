"""Soloist binary management.

Spotify forbids redistributing Soloist, so the image never contains it. At runtime we
download the official archive into /data/bin, keep each build in its own directory, and
point a `current` symlink at the active one. Builds expire 90 days after build date
(exit code 10), so we check regularly and swap in newer builds.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("updater")

BASE_URL = "https://soloist-builds.spotifycdn.com/soloist_release_{arch}.tar.gz"
BUILD_LIFETIME = timedelta(days=90)
ARCH_MAP = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


def detect_arch(machine: str | None = None) -> str:
    m = (machine or platform.machine()).lower()
    if m not in ARCH_MAP:
        raise RuntimeError(f"Unsupported architecture: {m}")
    return ARCH_MAP[m]


_TS_PATTERNS = [
    # ISO-ish: 2026-08-14T10:22:33Z / 2026-08-14 10:22:33 / 2026-08-14
    (re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})"), lambda m: f"{m[1]}T{m[2]}"),
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), lambda m: f"{m[1]}T00:00:00"),
    (re.compile(r"(\d{8})[T_-]?(\d{6})"), lambda m: f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}T{m[2][:2]}:{m[2][2:4]}:{m[2][4:]}"),
]


def parse_version_output(text: str) -> dict:
    """Extract version string and build timestamp from `soloist --version` output.

    The exact format is undocumented ("includes version, build timestamp, build
    identifiers, platform, architecture"), so parse defensively.
    """
    info: dict = {"raw": text.strip(), "version": None, "build_time": None}
    vm = re.search(r"(?i)version[:\s]+v?([0-9][\w.\-+]*)", text) or re.search(r"\bv?(\d+\.\d+(?:\.\d+)?[\w.\-+]*)", text)
    if vm:
        info["version"] = vm.group(1)
    # Prefer a line mentioning "build" + time/date
    lines = text.splitlines()
    ordered = [l for l in lines if re.search(r"(?i)build|timestamp|date", l)] + lines
    for line in ordered:
        epoch = re.search(r"(?i)(?:build|timestamp|time)[^\d]{0,20}?(\d{10,13})\b", line)
        if epoch:
            v = int(epoch.group(1))
            if v > 1e12:
                v //= 1000
            info["build_time"] = datetime.fromtimestamp(v, timezone.utc).isoformat()
            return info
        for rx, fmt in _TS_PATTERNS:
            m = rx.search(line)
            if m:
                try:
                    dt = datetime.fromisoformat(fmt(m)).replace(tzinfo=timezone.utc)
                    info["build_time"] = dt.isoformat()
                    return info
                except ValueError:
                    continue
    return info


@dataclass
class BuildInfo:
    build_id: str
    path: str
    version: str | None
    build_time: str | None  # ISO, from --version
    downloaded_at: str
    etag: str | None = None
    last_modified: str | None = None
    sha256: str | None = None

    def build_dt(self) -> datetime:
        for v in (self.build_time, self.last_modified, self.downloaded_at):
            if v:
                try:
                    return datetime.fromisoformat(v)
                except ValueError:
                    pass
        return datetime.now(timezone.utc)

    def expires_at(self) -> datetime:
        return self.build_dt() + BUILD_LIFETIME

    def days_left(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.expires_at() - now).total_seconds() / 86400


class Updater:
    def __init__(self, data_dir: Path, arch: str | None = None):
        self.bin_root = data_dir / "bin"
        self.bin_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.bin_root / "state.json"
        self.arch = arch or detect_arch()
        self.url = BASE_URL.format(arch=self.arch)
        self.last_check: str | None = None
        self.last_error: str | None = None
        self.pending: BuildInfo | None = None  # downloaded, awaiting activation
        self._lock = asyncio.Lock()

    # ---------- state ----------
    @property
    def current_link(self) -> Path:
        return self.bin_root / "current"

    @property
    def binary(self) -> Path:
        return self.current_link / "soloist"

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_state(self, st: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        os.replace(tmp, self.state_path)

    def current(self) -> BuildInfo | None:
        st = self._read_state().get("current")
        if st and Path(st["path"]).exists():
            b = BuildInfo(**st)
            if not b.build_time:  # re-parse with newer parser versions
                vt = Path(b.path) / "version.txt"
                if vt.exists():
                    b.build_time = parse_version_output(vt.read_text())["build_time"]
            return b
        return None

    def status(self) -> dict:
        cur = self.current()
        return {
            "arch": self.arch,
            "url": self.url,
            "installed": asdict(cur) if cur else None,
            "expires_at": cur.expires_at().isoformat() if cur else None,
            "days_left": round(cur.days_left(), 1) if cur else None,
            "pending": asdict(self.pending) if self.pending else None,
            "last_check": self.last_check,
            "last_error": self.last_error,
        }

    # ---------- download ----------
    async def _head(self, client: httpx.AsyncClient) -> tuple[str | None, str | None]:
        try:
            r = await client.head(self.url, follow_redirects=True)
            r.raise_for_status()
            lm = r.headers.get("last-modified")
            lm_iso = None
            if lm:
                from email.utils import parsedate_to_datetime
                lm_iso = parsedate_to_datetime(lm).astimezone(timezone.utc).isoformat()
            return r.headers.get("etag"), lm_iso
        except httpx.HTTPError as e:
            log.warning("HEAD failed: %s", e)
            return None, None

    @staticmethod
    def run_version(binary: Path) -> dict:
        try:
            p = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"cannot execute soloist: {e}") from e
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 10:
            raise RuntimeError("downloaded build is already expired (exit 10)")
        if p.returncode != 0:
            raise RuntimeError(f"soloist --version exited {p.returncode}: {out.strip()[:300]}")
        return parse_version_output(out)

    async def check(self, force: bool = False) -> BuildInfo | None:
        """Check for a newer build. Downloads and stages it; returns staged build or None."""
        async with self._lock:
            self.last_check = datetime.now(timezone.utc).isoformat()
            try:
                return await self._check(force)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.error("update check failed: %s", e)
                return None

    async def _check(self, force: bool) -> BuildInfo | None:
        cur = self.current()
        async with httpx.AsyncClient(timeout=120) as client:
            etag, lm = await self._head(client)
            if cur and not force and etag and etag == cur.etag:
                self.last_error = None
                log.info("no new build (etag unchanged)")
                return None
            log.info("downloading %s", self.url)
            r = await client.get(self.url, follow_redirects=True)
            r.raise_for_status()
            blob = r.content
            etag = r.headers.get("etag", etag)
        sha = hashlib.sha256(blob).hexdigest()
        if cur and cur.sha256 == sha and not force:
            # same content; refresh etag bookkeeping
            cur.etag = etag
            self._save_current(cur)
            self.last_error = None
            return None

        build_dir = self.bin_root / f"build-{sha[:12]}"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir()
        await asyncio.to_thread(self._extract, blob, build_dir)
        binary = build_dir / "soloist"
        if not binary.exists():
            raise RuntimeError("archive did not contain 'soloist'")
        binary.chmod(0o755)
        info = await asyncio.to_thread(self.run_version, binary)
        new = BuildInfo(
            build_id=sha[:12], path=str(build_dir), version=info["version"],
            build_time=info["build_time"], downloaded_at=datetime.now(timezone.utc).isoformat(),
            etag=etag, last_modified=lm, sha256=sha,
        )
        (build_dir / "version.txt").write_text(info["raw"])
        if cur and new.build_dt() <= cur.build_dt() and not force:
            log.info("downloaded build is not newer (%s <= %s); discarding", new.build_dt(), cur.build_dt())
            shutil.rmtree(build_dir, ignore_errors=True)
            cur.etag = etag
            self._save_current(cur)
            self.last_error = None
            return None
        self.pending = new
        self.last_error = None
        log.info("staged build %s (version %s, built %s)", new.build_id, new.version, new.build_time)
        return new

    @staticmethod
    def _extract(blob: bytes, dest: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for m in tf.getmembers():
                name = Path(m.name)
                if name.is_absolute() or ".." in name.parts:
                    continue
                if not (m.isfile() or m.isdir()):
                    continue
                # flatten: we only care about top-level files (soloist + licenses)
                target = dest / name.name if m.isfile() else None
                if target is None:
                    continue
                with tf.extractfile(m) as src, open(target, "wb") as out:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, out)

    def _save_current(self, b: BuildInfo) -> None:
        st = self._read_state()
        st["current"] = asdict(b)
        self._write_state(st)

    def activate_pending(self) -> BuildInfo | None:
        """Swap `current` symlink to the staged build. Keep previous for rollback."""
        if not self.pending:
            return None
        new, old = self.pending, self.current()
        tmp = self.bin_root / "current.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(Path(new.path).name)
        os.replace(tmp, self.current_link)
        st = self._read_state()
        st["previous"] = asdict(old) if old else None
        st["current"] = asdict(new)
        self._write_state(st)
        self.pending = None
        self._prune(keep={Path(new.path).name, Path(old.path).name if old else ""})
        log.info("activated build %s", new.build_id)
        return new

    def _prune(self, keep: set[str]) -> None:
        for d in self.bin_root.glob("build-*"):
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)

    def needs_update(self, now: datetime | None = None, refresh_days_left: float = 10) -> bool:
        """True if there is no build or the installed one is within refresh window (~80 days)."""
        cur = self.current()
        return cur is None or cur.days_left(now) <= refresh_days_left
