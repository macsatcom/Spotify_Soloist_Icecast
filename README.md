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
cp .env.example .env      # edit Icecast settings
docker compose up -d --build
```

1. Get an API key at <https://developer.spotify.com/dashboard/soloist> (Premium required).
2. Open `http://<host>:8380`, paste the key, set the device name.
3. In the Spotify app, open the device picker and select the device. The session is stored
   in `$DATA_DIR/soloist` and survives restarts.
4. Listen at `http://<host>:8100/spotify` (internal Icecast).

`network_mode: host` is required for Spotify Connect discovery. Because of host networking,
ports cannot be remapped: if `WEB_PORT` (default 8380) or `ICECAST_PORT` (default 8100) is
already used on the host, change it in `.env`. The container exits with an error if the port is taken.

## Configuration

`.env` holds only bootstrap settings: volume paths, web port and the Icecast connection
(`ICECAST_MODE=internal|external`, host, port, mount, passwords). Everything else
(device name, initial volume, cache size, codec, bitrate, sample rate, FLAC bit depth,
updater policy, API token) is edited in the web UI and saved to `$DATA_DIR/config.json`.
Secrets are stored in `$DATA_DIR/secrets.json` (mode 0600) and never returned by the API.

### Encoders

| Codec  | Container | Notes |
|--------|-----------|-------|
| mp3    | MP3       | up to 320 kbps, ICY "Artist - Title" metadata |
| aac    | ADTS      | ICY metadata |
| opus   | Ogg       | always 48 kHz |
| vorbis | Ogg       | |
| flac   | Ogg       | lossless, 16 or 24 bit, 44.1/48 kHz |

Soloist itself has no quality option; it outputs PCM to PulseAudio, and the container
keeps that lossless end-to-end when FLAC is selected.

## API (v1, work in progress)

| Method | Path | |
|---|---|---|
| GET | `/api/v1/status` | soloist auth/playback, stream URL, updater, processes |
| GET | `/api/v1/events` | Server-Sent Events (Soloist events + system/updater) |
| GET/PUT | `/api/v1/config` | settings (deep-merge patch) |
| PUT | `/api/v1/secrets/{soloist_api_key\|api_token\|...}` | `{ "value": "..." }` |
| POST | `/api/v1/system/pair` | re-pair (select device in Spotify app) |
| GET/POST | `/api/v1/system/update[?force=true]` | binary status / check now |
| POST | `/api/v1/system/restart/{pulseaudio\|icecast\|encoder\|soloist}` | |
| GET | `/api/v1/system/logs[?component=]` | |

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

Search/browse (Spotify Web API) follows in a later milestone.

## License

GPL-3.0. Spotify Soloist is proprietary software by Spotify AB and is not part of this project.
