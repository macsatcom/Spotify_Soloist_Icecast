"""Configuration handling.

Two layers:
  * Env  (bootstrap, read-only at runtime): paths, web port, Icecast connection.
  * Store (/data/config.json + /data/secrets.json): everything editable in the web UI.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Env(BaseModel):
    data_dir: Path = Path("/data")
    cache_dir: Path = Path("/cache")
    web_port: int = 8380
    icecast_mode: Literal["internal", "external"] = "internal"
    icecast_host: str = "127.0.0.1"
    icecast_port: int = 8100
    icecast_mount: str = "/spotify"
    icecast_source: str = "changeme"
    icecast_admin_user: str = "admin"
    icecast_admin_pass: str = "changeme_admin"
    seed_api_key: str = ""

    @classmethod
    def from_environ(cls, env: dict[str, str] | None = None) -> "Env":
        e = os.environ if env is None else env
        g = lambda k, d: e.get(k) or d  # noqa: E731
        mode = g("ICECAST_MODE", "internal").lower()
        mount = g("ICECAST_MOUNT", "/spotify")
        if not mount.startswith("/"):
            mount = "/" + mount
        return cls(
            data_dir=Path(g("DATA_DIR_IN_CONTAINER", "/data")),
            cache_dir=Path(g("CACHE_DIR_IN_CONTAINER", "/cache")),
            web_port=int(g("WEB_PORT", "8380")),
            icecast_mode=mode if mode in ("internal", "external") else "internal",
            icecast_host="127.0.0.1" if mode == "internal" else g("ICECAST_HOST", "127.0.0.1"),
            icecast_port=int(g("ICECAST_PORT", "8100")),
            icecast_mount=mount,
            icecast_source=g("ICECAST_SOURCE", "changeme"),
            icecast_admin_user=g("ICECAST_ADMIN_USER", "admin"),
            icecast_admin_pass=g("ICECAST_ADMIN_PASS", "changeme_admin"),
            seed_api_key=g("SOLOIST_API_KEY", ""),
        )

    def public(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude={"icecast_source", "icecast_admin_pass", "seed_api_key"})
        return d


class SoloistSettings(BaseModel):
    device_name: str = "Soloist"
    initial_volume: int | None = Field(default=100, ge=0, le=100)
    cache_size_mb: int = Field(default=0, ge=0)  # 0 = unlimited; else >= 100
    verbose: bool = False
    extra_args: list[str] = []


class EncoderSettings(BaseModel):
    codec: Literal["mp3", "aac", "opus", "vorbis", "flac"] = "mp3"
    bitrate_kbps: int = Field(default=320, ge=32, le=512)  # mp3/aac/opus/vorbis
    sample_rate: Literal[44100, 48000] = 44100
    bit_depth: Literal[16, 24] = 16  # flac only
    flac_compression: int = Field(default=5, ge=0, le=12)
    stream_name: str = "Spotify Soloist"
    stream_description: str = "Spotify via Soloist"


class UpdaterSettings(BaseModel):
    check_interval_hours: int = Field(default=24, ge=1)
    force_update_days_left: int = Field(default=7, ge=1)  # restart even if playing
    auto_update: bool = True


class IcecastSettings(BaseModel):
    mode: Literal["internal", "external"] = "internal"
    host: str = "127.0.0.1"  # external only; internal always uses localhost
    port: int = Field(default=8100, ge=1, le=65535)
    mount: str = "/spotify"
    admin_user: str = "admin"


class SecuritySettings(BaseModel):
    api_token_enabled: bool = False


class WebApiSettings(BaseModel):
    client_id: str = ""
    redirect_uri: str = "http://127.0.0.1:8888/callback"


class Settings(BaseModel):
    soloist: SoloistSettings = SoloistSettings()
    encoder: EncoderSettings = EncoderSettings()
    icecast: IcecastSettings = IcecastSettings()
    updater: UpdaterSettings = UpdaterSettings()
    security: SecuritySettings = SecuritySettings()
    webapi: WebApiSettings = WebApiSettings()


SECRET_KEYS = ("soloist_api_key", "api_token", "webapi_client_secret", "webapi_refresh_token",
               "icecast_source", "icecast_admin_pass")


class Store:
    """Persisted settings and secrets. Thread-safe, atomic writes."""

    def __init__(self, data_dir: Path, seed_api_key: str = "", env: "Env | None" = None):
        self.data_dir = data_dir
        self.cfg_path = data_dir / "config.json"
        self.sec_path = data_dir / "secrets.json"
        self._lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()
        self.secrets: dict[str, str] = self._load_json(self.sec_path)
        if seed_api_key and not self.secrets.get("soloist_api_key"):
            self.set_secret("soloist_api_key", seed_api_key)
        if env is not None:
            self._seed_icecast(env)

    def _seed_icecast(self, env: "Env") -> None:
        """First run only: take Icecast settings from .env; afterwards the web UI owns them."""
        if "icecast" not in self._load_json(self.cfg_path):
            self.settings.icecast = IcecastSettings(
                mode=env.icecast_mode, host=env.icecast_host, port=env.icecast_port,
                mount=env.icecast_mount, admin_user=env.icecast_admin_user)
            self.save()
        for k, v in (("icecast_source", env.icecast_source), ("icecast_admin_pass", env.icecast_admin_pass)):
            if v and not self.secrets.get(k):
                self.set_secret(k, v)

    @staticmethod
    def _load_json(p: Path) -> dict:
        try:
            return json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _load_settings(self) -> Settings:
        raw = self._load_json(self.cfg_path)
        try:
            return Settings.model_validate(raw)
        except Exception:
            return Settings()

    @staticmethod
    def _atomic_write(p: Path, data: dict, mode: int = 0o644) -> None:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, mode)
        os.replace(tmp, p)

    def save(self) -> None:
        with self._lock:
            self._atomic_write(self.cfg_path, self.settings.model_dump(mode="json"))

    def update(self, patch: dict[str, Any]) -> Settings:
        """Deep-merge a patch into settings, validate, persist."""
        with self._lock:
            merged = _deep_merge(self.settings.model_dump(mode="json"), patch)
            m = (merged.get("icecast") or {}).get("mount")
            if m and not m.startswith("/"):
                merged["icecast"]["mount"] = "/" + m
            new = Settings.model_validate(merged)
            self.settings = new
            self._atomic_write(self.cfg_path, new.model_dump(mode="json"))
            return new

    def get_secret(self, key: str) -> str:
        return self.secrets.get(key, "")

    def set_secret(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(key)
        with self._lock:
            if value:
                self.secrets[key] = value
            else:
                self.secrets.pop(key, None)
            self._atomic_write(self.sec_path, self.secrets, 0o600)

    def secrets_status(self) -> dict[str, bool]:
        return {k: bool(self.secrets.get(k)) for k in SECRET_KEYS}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
