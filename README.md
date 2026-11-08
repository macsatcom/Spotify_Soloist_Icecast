# Spotify Soloist → Icecast

Runs [Spotify Soloist](https://developer.spotify.com/documentation/soloist) (a headless
Spotify Connect device) in a Docker container and streams its audio to Icecast, with a
web UI and an HTTP control API. Built as the backend for a future Lyrion Music Server plugin
(separate project; see [docs/API.md](docs/API.md)), in the spirit of [Lyrion_FM_Radio](https://github.com/macsatcom/Lyrion_FM_Radio).

> **Personal use only.** The Soloist API key belongs to your Spotify account. Do not expose
> this container to the internet or share it with others.

## Architecture

```
Spotify app ─(Connect/mDNS, host network)─► soloist ─► PulseAudio null-sink "soloist_out"
                                              │ ws://127.0.0.1                │ .monitor
                                              ▼                               ▼
                         soloist-icecast daemon (FastAPI)          parec │ ffmpeg ─► Icecast
                          • web UI + REST/SSE API                    (mp3/aac/opus/vorbis/flac)
                          • updater (Soloist binary, 90-day expiry)      internal | external
                          • process supervisor, Icecast metadata
```

## Soloist binary & 90-day expiry

Spotify does not allow redistributing Soloist, so **the image does not contain it**. On first
start the container downloads the official build for your architecture into
`$DATA_DIR/bin/`. Builds expire 90 days after their build date, so the updater:

- checks the download URL daily (cheap `HEAD`/ETag; every 6 h when <10 days remain),
- stages a newer build, verifies it with `soloist --version`,
- activates it when playback is idle (or immediately when ≤7 days remain),
- fetches immediately if Soloist exits with code `10` (expired),
- keeps the previous build for rollback.

## First-time setup

You need three things, all from your own Spotify **Premium** account:

| What | Used for | Where |
|---|---|---|
| Soloist API key | running Soloist at all | developer.spotify.com/dashboard/soloist |
| Spotify Connect pairing | logging Soloist into your account | the Spotify app (phone/desktop) |
| Web API app (Client ID) | search and library in the Browse tab (optional) | developer.spotify.com/dashboard |

Plus, optionally, an **API token** of your own choosing to protect this container's API.

### 1. Start the container

```bash
git clone <this repo> && cd Spotify_Soloist_Icecast/docker
cp .env.example .env
```

Edit `.env`:

- `DATA_DIR` / `CACHE_DIR`: where settings, the Spotify session and the Soloist binary are kept.
- `WEB_PORT`: web UI port (default 8380). Must be free on the host (host networking).
- `ICECAST_*`: first-start defaults only. Change at least `ICECAST_SOURCE` and
  `ICECAST_ADMIN_PASS`. Everything can be changed later in the web UI.
- `SOLOIST_API_KEY`: leave empty; you'll paste it in the UI (step 3).

```bash
docker compose up -d --build
docker compose logs -f      # optional: watch it start
```

On first start the container downloads the Soloist binary from Spotify (about 20 MB) into
`$DATA_DIR/bin`. Open `http://<host>:8380`; the **Build** light in the header turns green with
the number of days left once that is done.

### 2. Get a Soloist API key

1. Log in at <https://developer.spotify.com/dashboard> with your Premium account.
2. Open <https://developer.spotify.com/dashboard/soloist> (**Spotify Soloist API Key**).
3. Accept the terms if asked and click **Generate**.
4. Copy the key. Treat it like a password: it is tied to your account and must not be shared.

### 3. Configure Soloist

1. In the web UI open **Settings → Soloist**.
2. Paste the key into **API key**, choose a **Device name** (this is what you'll see in Spotify,
   e.g. `Living room LMS`).
3. Leave **Initial volume** at 100 (control volume in Lyrion/your player instead).
4. Click **Save settings** at the bottom. Soloist starts; the **Session** light turns amber
   (running, not logged in).

The key is stored in `$DATA_DIR/secrets.json` and is never shown again; the field shows
`· set`. Paste a new key to replace it.

### 4. Pair with your Spotify account

Soloist has no username/password login; it logs in through Spotify Connect:

1. Open the Spotify app on a phone or computer **on the same network**.
2. Start playing anything, open the device picker (speaker icon) and select your device name.
3. Soloist logs in, stores the session in `$DATA_DIR/soloist` and starts playing.
   The **Session** light turns green; **Now playing** shows the track.

The session survives restarts and updates. To switch account or fix a broken session, use
**Pairing → Re-pair device** and select the device in the Spotify app again within 5 minutes.

If the device does not appear: check that phone and host are on the same LAN/VLAN, that the
Wi-Fi has no client isolation, that no VPN is active on the phone, and look at **Logs → soloist**.

### 5. Listen

- Internal Icecast: `http://<host>:8100/spotify` (or the player in **Now playing**).
- Choose codec/quality in **Settings → Encoder** (e.g. FLAC, 24 bit, 48 kHz for lossless).
- To use an existing Icecast server: **Settings → Icecast → Server: External**, enter host,
  port, mount and the server's **source password**. Admin user/password are only needed for
  "Artist – Title" metadata with MP3/AAC.

### 6. Optional: connect the Spotify Web API (search & library)

Soloist itself cannot search or list playlists. For the **Browse** tab (and the `/search`,
`/library`, `/item` API endpoints) create your own Web API app:

1. Go to <https://developer.spotify.com/dashboard> → **Create app**.
   - App name/description: anything, e.g. `Soloist Icecast`.
   - Redirect URI: `http://127.0.0.1:8888/callback` → click **Add**.
   - APIs used: tick **Web API**.
   - Accept the terms and **Save**.
2. Open the app → **Settings**, copy the **Client ID**. (The client secret is not needed.)
   Check the Redirect URIs list shows exactly what you will enter in the next step.
3. In the web UI open **Browse**. Enter the Client ID and the Redirect URI, **identical** to
   the dashboard, including any trailing `/`. (`http://127.0.0.1:8888/` and
   `http://127.0.0.1:8888/callback` are different URIs to Spotify.)
4. Click **Connect**. A Spotify tab opens; log in and click **Agree**.
5. That tab ends on a page that **cannot be reached**. This is expected: nothing runs on
   127.0.0.1 on your computer. Copy the **whole URL** from the address bar; it looks like
   `http://127.0.0.1:8888/callback?code=AQB...&state=...`.
6. Back in the web UI, paste it into the second field and click **Finish**. Search and the
   library buttons appear, with your account name on the right.

Do steps 4–6 in one go: clicking Connect again, or restarting the container, invalidates the
pasted URL ("state mismatch"); just click Connect again.

The refresh token is stored in `secrets.json`; you won't need to do this again. **Disconnect**
in the Browse tab removes it.

Why not `http://<host-ip>:8380/callback`? Spotify only accepts `http://` redirect URIs for
loopback addresses (`127.0.0.1`, `[::1]`); other addresses must be `https://`. The redirect
happens in your browser, not in the container, so the loopback address works wherever the
container runs.

Common errors:

| Message | Cause |
|---|---|
| `INVALID_CLIENT: Invalid redirect URI` / "redirect_uri: Not matching configuration" | redirect URI in Browse differs from the dashboard (trailing `/`, `localhost` vs `127.0.0.1`, port, forgot to Save in dashboard) |
| state mismatch / login session expired | Connect was clicked again or the container restarted; repeat steps 4–6 |
| `invalid_grant` | the code was already used or is older than ~10 minutes; repeat steps 4–6 |
| Forbidden on some items | Spotify's development-mode limits, see below |

### 7. Optional: protect the API with a token

By default the API and web UI are open to anyone on your LAN, like the FM Radio daemon. To
require a token:

1. **Settings → API security**, click **Generate** (or paste your own string).
2. Tick **Require API token** and **Save settings**.
3. Your browser stores the token automatically. Other browsers will prompt for it once.
4. Clients (e.g. the Lyrion plugin, curl) must send it:

```bash
curl -H "Authorization: Bearer <token>" http://<host>:8380/api/v1/status
curl "http://<host>:8380/api/v1/listen?token=<token>"     # for URLs that cannot set headers
```

`/api/v1/health` and the Icecast stream itself are not protected. To turn it off, untick
**Require API token** and save. If you lose the token, stop the container and set
`"api_token_enabled": false` in `$DATA_DIR/config.json`.

### Summary of credentials

| Credential | Set in | Stored in |
|---|---|---|
| Soloist API key | Settings → Soloist | `secrets.json` |
| Spotify session | Spotify app device picker | `$DATA_DIR/soloist/` |
| Web API Client ID / redirect URI | Browse | `config.json` |
| Web API refresh token | Browse → Connect/Finish | `secrets.json` |
| Icecast source / admin password | Settings → Icecast | `secrets.json` |
| API token | Settings → API security | `secrets.json` (+ browser localStorage) |

Back up `$DATA_DIR` to keep all of it. Never commit or share `secrets.json`.

### Ports and host networking

`network_mode: host` is required for Spotify Connect discovery (Soloist does its own mDNS;
no avahi/dbus needed). Because of host networking, ports cannot be remapped:

- Web UI/API port: `WEB_PORT` in `.env` (default 8380). If it is taken, the web UI does not start.
- Internal Icecast port: **Settings → Icecast** (default 8100, seeded from `ICECAST_PORT`).
  If it is taken, Icecast keeps failing; see the Logs tab.

### Updating the container

```bash
cd docker && git pull && docker compose up -d --build
```

Settings, secrets, the Spotify session and the Soloist binary live on the `/data` volume and
are kept. The Soloist binary itself is updated automatically (see above).

## Web UI

| Tab | |
|---|---|
| Now playing | cover, title, progress (click to seek), play/pause/prev/next, shuffle, repeat, volume, built-in stream player, play/queue any Spotify URI or `open.spotify.com` link, up-next queue |
| Browse | search and library (playlists, albums, liked songs, followed artists, podcasts, recent, top tracks); open items and play/queue |
| Pairing | session state, instructions, re-pair (replaces stored session) |
| Settings | Soloist, encoder, Icecast, updater, API token |
| Updates | days until build expiry, version, pending build, check now / force re-download |
| Logs | per-component logs, restart a component |

Status lights in the header: session, active device, encoder, Icecast, days left on the build.
All views update live via Server-Sent Events.

## Configuration

`.env` holds bootstrap settings only:

| Variable | |
|---|---|
| `DATA_DIR`, `CACHE_DIR` | host paths for the `/data` and `/cache` volumes |
| `WEB_PORT` | web UI/API port (default 8380) |
| `ICECAST_MODE/HOST/PORT/MOUNT/SOURCE/ADMIN_USER/ADMIN_PASS` | first-start defaults only |
| `SOLOIST_API_KEY` | optional first-start seed |
| `TZ` | time zone |

Everything else is managed in the web UI and saved to `$DATA_DIR/config.json`:

- **Soloist:** API key, device name, initial volume (default 100; control volume in Lyrion
  instead), cache size, extra arguments, verbose logging.
- **Encoder:** codec, bitrate, sample rate, FLAC bit depth and compression, stream name/description.
- **Icecast:** internal/external, host (external), port, mount, source password, admin user/password.
  In external mode the internal Icecast is not started; the source password must match your server.
- **Updater:** automatic updates, check interval, days-left threshold for restarting while playing.
- **Security:** optional API token.

Secrets (Soloist API key, API token, Icecast passwords, Web API refresh token) are stored in
`$DATA_DIR/secrets.json` (mode 0600) and never returned by the API.

What a save restarts:

| Change | Restarts | Effect |
|---|---|---|
| codec, bitrate, stream name | encoder | playback continues, listeners reconnect |
| sample rate, bit depth | audio sink, encoder, Soloist | short playback gap |
| Icecast settings/passwords | internal Icecast (if used), encoder | listeners reconnect |
| Soloist settings / API key | Soloist | short playback gap |

### Encoders

| Codec  | Container | Notes |
|--------|-----------|-------|
| mp3    | MP3       | up to 320 kbps, ICY "Artist - Title" metadata |
| aac    | ADTS      | ICY "Artist - Title" metadata |
| opus   | Ogg       | always 48 kHz |
| vorbis | Ogg       | |
| flac   | Ogg       | lossless, 16 or 24 bit, 44.1/48 kHz (24/48 ≈ 2–3 Mbit/s) |

Soloist itself has no quality option; it outputs PCM to PulseAudio, and the container
keeps that lossless end-to-end when FLAC is selected.

## API (v1)

**Writing an integration** (Lyrion plugin, Home Assistant, scripts…)? See
[docs/API.md](docs/API.md): configuration (API URL, token, stream discovery), the entity
format, playback, search/browse, live events, recipes and a Lyrion plugin design.
The tables below are a quick reference.

Authentication is off by default (LAN, personal use). When the token is enabled in Settings,
send `Authorization: Bearer <token>` or `?token=<token>`.

| Method | Path | |
|---|---|---|
| GET | `/api/v1/health` | `{ok}`; no auth, used by the Docker healthcheck |
| GET | `/api/v1/status` | soloist auth/playback, stream URL, updater, processes |
| GET | `/api/v1/events` | Server-Sent Events (Soloist events + system/updater) |
| GET/PUT | `/api/v1/config` | settings (deep-merge patch) |
| PUT | `/api/v1/secrets/{key}` | `{ "value": "..." }`; keys: `soloist_api_key`, `api_token`, `icecast_source`, `icecast_admin_pass`, `webapi_client_secret` |
| GET/POST | `/api/v1/system/update[?force=true]` | binary status / check now |
| POST | `/api/v1/system/restart/{pulseaudio\|icecast\|encoder\|soloist}` | |
| GET/POST | `/api/v1/system/pair` | pairing status and log / start re-pair |
| GET | `/api/v1/system/logs[?component=&lines=]` | |

### Playback control

| Method | Path | Body |
|---|---|---|
| GET | `/api/v1/now` | current playback (+ interpolated `position_ms_now`) |
| GET | `/api/v1/queue?limit=10` | previous/upcoming |
| POST | `/api/v1/play` | optional `{ "uri": "spotify:playlist:..." }` |
| POST | `/api/v1/pause`, `/next`, `/prev`, `/activate`, `/deactivate` | |
| POST | `/api/v1/seek` | `{ "position_ms": 30000 }` |
| POST | `/api/v1/volume` | `{ "volume": 0-100 }` |
| POST | `/api/v1/shuffle` | `{ "enabled": true }` |
| POST | `/api/v1/repeat` | `{ "mode": "off\|context\|track" }` |
| POST | `/api/v1/queue` | `{ "uri": "spotify:track:..." }` |
| GET | `/api/v1/stream` | 302 to the Icecast stream |
| GET | `/api/v1/listen[?uri=]` | activate, optionally play URI, 302 to stream (for Lyrion) |

### Search & browse (Spotify Web API)

Soloist itself cannot search or list your library, so the container proxies the Spotify Web API.
Setup: see [First-time setup, step 6](#6-optional-connect-the-spotify-web-api-search--library).
All results use the same entity shape as Soloist's events.

| Method | Path | |
|---|---|---|
| GET/PUT/DELETE | `/api/v1/webapi` | status / `{client_id, redirect_uri}` / disconnect |
| GET | `/api/v1/webapi/login` | authorize URL |
| POST | `/api/v1/webapi/callback` | `{ "url": "<pasted redirect URL>" }` |
| GET | `/api/v1/search?q=&type=track,album,artist,playlist,show,episode&limit=&offset=` | |
| GET | `/api/v1/library/{playlists,albums,tracks,artists,shows,recent}` | paged (`limit`, `offset`) |
| GET | `/api/v1/library/top/{tracks,artists}` | |
| GET | `/api/v1/browse/{categories,new-releases}` | currently 403 for development-mode Spotify apps |
| GET | `/api/v1/item/{spotify:type:id}` | item + children (playlist/album tracks, artist releases + top tracks, show episodes) |

Spotify restricts development-mode apps:

- Track lists are only available for playlists you own or collaborate on. Other playlists can
  still be **played**; their tracks then appear in the queue.
- Search returns at most 10 results per type; artist releases are capped at 10 per page.
- Artist top tracks are forbidden, so they are approximated via search.
- New releases and categories are forbidden (403).

Paged responses: `{ items: [Entity], total, offset, limit, next }`.

## Troubleshooting

- **Web UI shows another app:** the web port is used by something else on the host. Change
  `WEB_PORT` in `.env`.
- **Device not visible in Spotify:** same network, no client isolation/VPN; check the API key
  and the Soloist log.
- **Silence:** the stream sends silence while nothing plays; select the device in Spotify.
- **External Icecast not receiving:** check host/port/mount and the source password; see the
  encoder log.

## License

GPL-3.0. Spotify Soloist is proprietary software by Spotify AB and is not part of this project.
