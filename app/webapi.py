"""Spotify Web API: OAuth (PKCE, paste-back redirect) and a normalised search/browse proxy.

Spotify only accepts HTTPS or loopback redirect URIs, so the default redirect is
http://127.0.0.1:8888/callback. That page won't load in the user's browser; the user
copies the URL from the address bar back into the web UI, which completes the exchange.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Store

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-private user-library-read playlist-read-private playlist-read-collaborative "
          "user-read-recently-played user-top-read user-follow-read")


class WebApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class WebApi:
    def __init__(self, store: Store):
        self.store = store
        self._verifier: str | None = None
        self._state: str | None = None
        self._access: str | None = None
        self._expires = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self.user: dict | None = None

    # ---------- auth ----------
    @property
    def configured(self) -> bool:
        return bool(self.store.settings.webapi.client_id)

    @property
    def connected(self) -> bool:
        return bool(self.store.get_secret("webapi_refresh_token"))

    def status(self) -> dict:
        w = self.store.settings.webapi
        return {"configured": self.configured, "connected": self.connected, "client_id": w.client_id,
                "redirect_uri": w.redirect_uri, "user": self.user}

    def login_url(self) -> str:
        w = self.store.settings.webapi
        if not w.client_id:
            raise WebApiError(400, "set the Client ID first")
        self._verifier = secrets.token_urlsafe(64)[:96]
        self._state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URL + "?" + urlencode({
            "client_id": w.client_id, "response_type": "code", "redirect_uri": w.redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": self._state, "scope": SCOPES})

    async def complete(self, pasted: str) -> dict:
        q = parse_qs(urlparse(pasted.strip()).query)
        if "error" in q:
            raise WebApiError(400, f"Spotify returned: {q['error'][0]}")
        code = (q.get("code") or [None])[0]
        if not code:
            raise WebApiError(400, "no 'code' in the pasted URL")
        if not self._verifier or (q.get("state") or [None])[0] != self._state:
            raise WebApiError(400, "login session expired or state mismatch; click Connect again")
        w = self.store.settings.webapi
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": w.redirect_uri,
                "client_id": w.client_id, "code_verifier": self._verifier}
        await self._token(data)
        self._verifier = self._state = None
        await self.me()
        return self.status()

    def disconnect(self) -> None:
        self.store.set_secret("webapi_refresh_token", "")
        self._access, self._expires, self.user = None, 0, None
        self._cache.clear()

    async def _token(self, data: dict) -> None:
        secret = self.store.get_secret("webapi_client_secret")
        auth = (self.store.settings.webapi.client_id, secret) if secret else None
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(TOKEN_URL, data=data, auth=auth)
        if r.status_code != 200:
            raise WebApiError(400 if data.get("grant_type") == "authorization_code" else 502,
                              f"Spotify token request failed: {r.text[:300]}")
        t = r.json()
        self._access = t["access_token"]
        self._expires = time.time() + t.get("expires_in", 3600) - 60
        if t.get("refresh_token"):
            self.store.set_secret("webapi_refresh_token", t["refresh_token"])

    async def _access_token(self) -> str:
        if self._access and time.time() < self._expires:
            return self._access
        rt = self.store.get_secret("webapi_refresh_token")
        if not rt:
            raise WebApiError(401, "Spotify Web API not connected (Browse tab)")
        await self._token({"grant_type": "refresh_token", "refresh_token": rt,
                           "client_id": self.store.settings.webapi.client_id})
        return self._access  # type: ignore[return-value]

    async def get(self, path: str, params: dict | None = None, ttl: float = 60) -> Any:
        key = path + "?" + urlencode(sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        tok = await self._access_token()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(API + path, params=params, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 401:
                self._access = None
                tok = await self._access_token()
                r = await c.get(API + path, params=params, headers={"Authorization": f"Bearer {tok}"})
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise WebApiError(r.status_code, f"Spotify API: {msg}")
        d = r.json()
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[key] = (time.time() + ttl, d)
        return d

    async def me(self) -> dict:
        d = await self.get("/me", ttl=3600)
        self.user = {"id": d.get("id"), "name": d.get("display_name"), "product": d.get("product"),
                     "country": d.get("country")}
        return self.user


# ---------- normalisation to Soloist's entity shape ----------
def _covers(images: list | None) -> list[dict]:
    imgs = sorted(images or [], key=lambda i: i.get("width") or 0)
    sizes = ["small", "default", "large", "xlarge"]
    out = []
    for i, img in enumerate(imgs):
        idx = 0 if len(imgs) == 1 else round(i * 3 / (len(imgs) - 1)) if len(imgs) > 1 else 0
        out.append({"url": img["url"], "size": sizes[min(idx, 3)]})
    if len(imgs) == 1:
        out = [{"url": imgs[0]["url"], "size": s} for s in ("small", "large")]
    return out


def entity(o: dict | None) -> dict | None:
    if not o or not o.get("uri"):
        return None
    t = o.get("type", "unknown")
    dec: dict = {"identity": {"name": o.get("name", "")}}
    images = o.get("images") or (o.get("album") or {}).get("images") or (o.get("show") or {}).get("images")
    if images:
        dec["visual_identity"] = {"cover": _covers(images)}
    parent = o.get("album") or o.get("show")
    if parent and parent.get("uri"):
        dec["parent"] = {"entity": {"uri": parent["uri"], "entity_type": parent.get("type", "album"),
                                    "decorations": {"identity": {"name": parent.get("name", "")}}}}
    creators = o.get("artists") or ([o["owner"]] if o.get("owner") else [])
    if creators:
        dec["creators"] = [{"entity": {"uri": a.get("uri", ""), "entity_type": a.get("type", "artist"),
                                       "decorations": {"identity": {"name": a.get("name") or a.get("display_name", "")}}}}
                           for a in creators]
    if o.get("duration_ms"):
        dec["playback"] = {"duration_ms": o["duration_ms"],
                           "content_ratings": ["explicit"] if o.get("explicit") else []}
    e = {"uri": o["uri"], "entity_type": t, "decorations": dec}
    extra = {k: o[k] for k in ("description", "publisher", "release_date", "total_tracks") if o.get(k)}
    if t == "playlist":
        tr = o.get("items") if isinstance(o.get("items"), dict) else o.get("tracks")
        extra["total_tracks"] = (tr or {}).get("total")
    if extra:
        e["details"] = extra
    return e


def page(d: dict, key: str | None = None, unwrap: str | None = None) -> dict:
    """Normalise a Spotify paging object."""
    p = d[key] if key else d
    items = []
    for it in p.get("items") or []:
        if unwrap:
            meta = {k: it.get(k) for k in ("added_at", "played_at") if it.get(k)}
            it = it.get(unwrap)
        else:
            meta = {}
        e = entity(it)
        if e:
            if meta:
                e["added"] = meta
            items.append(e)
    return {"items": items, "total": p.get("total", len(items)), "offset": p.get("offset", 0),
            "limit": p.get("limit", len(items)), "next": bool(p.get("next"))}
