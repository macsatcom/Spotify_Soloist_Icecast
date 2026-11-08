# Spotify Soloist → Icecast

Runs [Spotify Soloist](https://developer.spotify.com/documentation/soloist) (a headless
Spotify Connect device) in a Docker container and streams its audio to Icecast, with a
web UI and an HTTP control API. Built as the backend for a future Lyrion Music Server plugin
(separate project), in the spirit of [Lyrion_FM_Radio](https://github.com/macsatcom/Lyrion_FM_Radio).

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

## Quick start

```bash
cd docker
cp .env.example .env      # paths, web port, first-start Icecast defaults
docker compose up -d --build
```

1. Get an API key at <https://developer.spotify.com/dashboard/soloist> (Premium required).
2. Open `http://<host>:8380` → **Settings**, paste the key, set the device name, save.
3. In the Spotify app, open the device picker and select the device. The session is stored
   in `$DATA_DIR/soloist` and survives restarts.
4. Listen at `http://<host>:8100/spotify` (internal Icecast), or in the **Now playing** tab.
5. Optional: connect the Spotify Web API in the **Browse** tab for search and library
   (see [Search & browse](#search--browse-spotify-web-api)).

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
One-time setup in the **Browse** tab: create an app at developer.spotify.com/dashboard, add the
redirect URI `http://127.0.0.1:8888/callback`, paste the Client ID, click *Connect*, approve, then
paste the URL of the (non-loading) callback page back. Uses PKCE (no client secret needed); the
refresh token is stored in `secrets.json`. All results use the same entity shape as Soloist's events.

The redirect URI in the Browse tab must match one registered in the Spotify app **exactly**
(including any trailing `/`). Spotify only allows `http://` redirect URIs for loopback
addresses, so a LAN IP like `http://192.168.1.x:8380/callback` is rejected; the loopback
redirect plus paste-back works regardless of where the container runs.

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
