# Integration guide (API v1)

How to control this container from another system: a Lyrion Music Server plugin, Home
Assistant, Node-RED, a script, or anything that can make HTTP requests and play an
HTTP audio stream.

The container exposes two things:

1. **A control API** (HTTP/JSON + Server-Sent Events) on `http://<host>:<WEB_PORT>/api/v1`
   (default port 8380).
2. **An audio stream** on Icecast: `http://<icecast-host>:<port><mount>`
   (default `http://<host>:8100/spotify`).

Your integration tells the API *what* to play and points its audio player at the stream.
Soloist plays into the stream continuously; when nothing is playing, the stream carries silence,
so a player can stay connected.

```
 your integration ── HTTP ──► /api/v1 (play, pause, search, events…)
        │
        └── audio player ◄── Icecast stream (mp3/aac/opus/vorbis/flac)
```

---

## 1. What your integration needs to configure

| Setting | Example | Notes |
|---|---|---|
| API base URL | `http://192.168.1.75:8380` | the container host and `WEB_PORT` |
| API token | `3f9a…` | only if *Require API token* is enabled in Settings |
| Stream URL | `http://192.168.1.75:8100/spotify` | optional; can be discovered (below) |

Only the API base URL is required: ask the API for the stream URL instead of having the user
type it:

```bash
curl http://192.168.1.75:8380/api/v1/status
```

```json
{
  "soloist": {"logged_in": true, "is_active": false, "device_name": "Living room",
              "ws_connected": true, "status": "paused", "pairing": false, "api_key_set": true},
  "stream":  {"url": "http://192.168.1.75:8100/spotify", "codec": "flac",
              "content_type": "audio/ogg", "mode": "internal"},
  "updater": {"days_left": 87.9, "expires_at": "2027-02-04T18:01:48+00:00", "...": "..."},
  "processes": {"soloist": {"running": true}, "encoder": {"running": true}, "...": {}}
}
```

`stream.url`:

- **internal Icecast**: built from the host name *you* used to reach the API. Call the API with
  the address your player can reach (an IP, not `localhost`), and the stream URL will use it.
- **external Icecast**: the configured external host, port and mount.

Also `/api/v1/stream` simply answers `302` to that URL, so a player can be given
`http://<host>:8380/api/v1/stream` as a stable stream address.

`stream.content_type` tells the player what it gets:

| codec | content_type | container | track titles in stream |
|---|---|---|---|
| mp3 | `audio/mpeg` | MP3 | yes (ICY `StreamTitle`) |
| aac | `audio/aac` | ADTS | yes (ICY) |
| opus, vorbis, flac | `audio/ogg` | Ogg | no; use the API |

Get metadata (title, artist, cover, position) from the API, not from the stream: it is richer
and works for every codec.

### Authentication

Off by default. When enabled, send either:

```
Authorization: Bearer <token>
```

or append `?token=<token>` (for players or URL-only contexts). Wrong/missing token: `401`.
Not protected: `/api/v1/health`, `/api/v1/stream`, and the Icecast stream itself.

### Errors

Errors are JSON `{"detail": "message"}` with:

| Status | Meaning |
|---|---|
| 401 | API token missing/wrong |
| 409 | Soloist not running, not logged in, or command rejected by Soloist |
| 422 | invalid input (e.g. `volume must be 0-100`, `only spotify:track: URIs can be queued`) |
| 4xx/502 from Web API endpoints | Spotify Web API error, or Web API not connected (Browse tab) |

---

## 2. Core concepts

### Spotify URIs

Everything is addressed by Spotify URI: `spotify:track:…`, `spotify:album:…`,
`spotify:playlist:…`, `spotify:artist:…`, `spotify:show:…`, `spotify:episode:…`.
To convert a share link: `https://open.spotify.com/album/4aaw…` → `spotify:album:4aaw…`.

### Active device

Soloist is a Spotify Connect device. Spotify plays on one device per account at a time.
`soloist.is_active` says whether this container currently holds playback. Starting playback
through the API (`/play` with a URI, or `/listen`) makes it active; selecting another device
in a Spotify app takes playback away (you'll get a `device_changed` event with
`is_active: false`).

### Entity

Every track, album, playlist, artist, show and episode, both from playback events and from
search/browse, uses the same shape. Parse it once:

```json
{
  "uri": "spotify:track:6TYQRlRci6AkVrN9a5C7ne",
  "entity_type": "track",
  "decorations": {
    "identity": {"name": "War Pigs - 2009 Remaster"},
    "visual_identity": {"cover": [
      {"url": "https://i.scdn.co/image/…", "size": "small"},
      {"url": "https://i.scdn.co/image/…", "size": "large"}]},
    "parent":   {"entity": {"uri": "spotify:album:…", "entity_type": "album",
                            "decorations": {"identity": {"name": "Paranoid"}}}},
    "creators": [{"entity": {"uri": "spotify:artist:…", "entity_type": "artist",
                             "decorations": {"identity": {"name": "Black Sabbath"}}}}],
    "playback": {"duration_ms": 477000, "content_ratings": []}
  },
  "details": {"total_tracks": 20}
}
```

| Field | |
|---|---|
| `uri`, `entity_type` | `track`, `episode`, `album`, `playlist`, `artist`, `show`, `ad`, `unknown` |
| `decorations.identity.name` | title |
| `decorations.creators[].entity…name` | artists (for playlists: the owner) |
| `decorations.parent.entity…name` | album (track) or show (episode) |
| `decorations.visual_identity.cover[]` | sizes `small`, `default`, `large`, `xlarge` (not all present); pick by size, fall back to the last |
| `decorations.playback.duration_ms` | length |
| `details` | browse results only: `description`, `publisher`, `release_date`, `total_tracks` |

Any field may be missing. Treat everything except `uri` and `entity_type` as optional.

---

## 3. Playback

| Method | Path | Body | |
|---|---|---|---|
| GET | `/api/v1/now` | | full playback state (below) |
| POST | `/api/v1/play` | `{}` or `{"uri": "spotify:…"}` | resume, or start a track/album/playlist/artist/show/episode |
| POST | `/api/v1/pause` | | |
| POST | `/api/v1/next` / `/prev` | | |
| POST | `/api/v1/seek` | `{"position_ms": 30000}` | |
| POST | `/api/v1/volume` | `{"volume": 0-100}` | Soloist volume (scales the stream) |
| POST | `/api/v1/shuffle` | `{"enabled": true}` | |
| POST | `/api/v1/repeat` | `{"mode": "off"\|"context"\|"track"}` | |
| GET | `/api/v1/queue?limit=10` | | `{"previous": [QueueEntry], "upcoming": [QueueEntry]}` |
| POST | `/api/v1/queue` | `{"uri": "spotify:track:…"}` | add to queue (tracks only) |
| POST | `/api/v1/activate` / `/deactivate` | | take / release active-device status |
| GET | `/api/v1/listen[?uri=…]` | | activate, optionally start `uri`, then `302` to the stream |
| GET | `/api/v1/stream` | | `302` to the stream |

Control commands return `{"type": "command_result", "command": "pause"}` as soon as Soloist
accepts them. The resulting state change arrives shortly after as an event (section 5); don't
assume it has happened when the call returns.

`GET /api/v1/now`:

```json
{
  "status": "playing",
  "item": Entity,
  "context": Entity,
  "position": {"position_ms": 45000, "timestamp_ms": 1747654321000, "speed": 1.0},
  "position_ms_now": 46210,
  "volume": 100,
  "is_active": true,
  "options": {"shuffle": false, "repeat": "off", "playback_speed": 1.0, "modes": {}},
  "available_actions": {"pause": {}, "seek": {}, "skip_next": {}, "skip_prev": {}, "...": {}}
}
```

- `status`: `idle`, `playing`, `paused`, `buffering`.
- `item`: what is playing; `context`: the album/playlist/artist it plays from.
- Position: `position_ms_now` is interpolated at request time. To show a running clock, compute
  `position_ms + (now_ms - timestamp_ms) * speed`.
- `available_actions`: show/enable buttons accordingly (e.g. no `skip_prev` → disable).
- Empty object `{}` when not logged in.

QueueEntry: `{"uid": "…", "source": "context"|"queue"|"autoplay", "item": Entity}`.

### Volume

The stream level follows Soloist's volume. With an external player that has its own volume
(Lyrion, Home Assistant media players), keep Soloist at 100 and use the player's volume, so
there is one volume control and no double attenuation. Initial volume 100 is the default.

---

## 4. Search and browse

Requires the Spotify Web API to be connected once in the web UI (**Browse** tab; see README,
first-time setup step 6). Check with `GET /api/v1/webapi` → `{"connected": true, …}`.
Your integration does **not** need any Spotify credentials of its own.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/search?q=…&type=track,album,artist,playlist,show,episode&limit=10&offset=0` | `{"track": Page, "album": Page, …}` (only requested types) |
| GET | `/api/v1/library/playlists` | Page of playlists |
| GET | `/api/v1/library/albums` | saved albums |
| GET | `/api/v1/library/tracks` | liked songs |
| GET | `/api/v1/library/artists` | followed artists |
| GET | `/api/v1/library/shows` | saved podcasts |
| GET | `/api/v1/library/recent` | recently played tracks |
| GET | `/api/v1/library/top/tracks`, `/top/artists` | `?time_range=short_term\|medium_term\|long_term` |
| GET | `/api/v1/item/{uri}` | an item and its contents (below) |

Page:

```json
{"items": [Entity, …], "total": 123, "offset": 0, "limit": 50, "next": true}
```

Paging: `?limit=&offset=`; request the next page while `next` is true. Library entries may
carry `"added": {"added_at": "…"}` or `{"played_at": "…"}`.

`GET /api/v1/item/{uri}` (URL-encode the URI or pass it as is; colons are allowed):

| uri type | response |
|---|---|
| playlist | `{"item": Entity, "children": Page of tracks/episodes}` |
| album | `{"item": Entity, "children": Page of tracks}` |
| artist | `{"item": Entity, "top_tracks": [Entity], "children": Page of albums}` |
| show | `{"item": Entity, "children": Page of episodes}` |
| track, episode | `{"item": Entity, "children": null}` |

If Spotify refuses a playlist's contents, `children` is
`{"items": [], "total": 0, "error": "…"}`. Show the message; the playlist can still be played.

Limits of Spotify development-mode apps (apply to every user of this container):

- Track lists only for playlists the user owns or collaborates on.
- At most 10 results per type per search request; artist albums 10 per page.
- Artist top tracks are approximated via search.
- New releases and categories return 403.

---

## 5. Live events (Server-Sent Events)

```
GET /api/v1/events            (Accept: text/event-stream)
```

Keep one connection open instead of polling. On connect you get one `data:` message with the
full status (`{"type": "status", …same as /status}`), then named events:

```
event: track_changed
data: {"type": "track_changed", "item": Entity}
```

| event | data | use |
|---|---|---|
| `status` | full `/status` (first message only) | initial state |
| `auth_state` | `logged_in`, `is_active`, `device_name` | login/logout; re-fetch `/now` |
| `playback_state` | full playback snapshot (as `/now`) | replace your state |
| `track_changed` | `item` | new track: update title/cover, re-fetch queue |
| `playback_changed` | `status` | playing/paused/idle/buffering |
| `position_sync` | `position` | re-anchor position clock (seek, pause, resume) |
| `volume_changed` | `volume` | |
| `options_changed` | `options` | shuffle/repeat |
| `context_changed` | `context` | |
| `device_changed` | `is_active`, `device_name` | another device took over playback |
| `queue_changed` | `previous`, `upcoming` | max 10 entries; use `/queue?limit=` for more |
| `system` | `event`: `soloist_connected`, `soloist_disconnected`, `pair_started`, `pair_log`, `pair_finished` | Soloist restarted, pairing |
| `updater` | `event`: `staged`, `activated`, `expired` | Soloist binary updates (Soloist restarts) |

A comment line `: keepalive` is sent every 15 s. Reconnect on drop (EventSource does this
automatically). After `soloist_connected` or `auth_state`, fetch `/now` to resync.

For clients without SSE support, poll `/api/v1/now` every 1–2 s instead.

---

## 6. Recipes

### Play something and hand the stream to a player

```bash
API=http://192.168.1.75:8380
curl -X POST $API/api/v1/play -H 'content-type: application/json' \
     -d '{"uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"}'
STREAM=$(curl -s $API/api/v1/status | jq -r .stream.url)
mpv "$STREAM"
```

Or in one URL, for systems where "play a URL" is all you can do:

```
http://192.168.1.75:8380/api/v1/listen?uri=spotify:album:4aawyAB9vmqN3uQ7FjRGTy
```

This starts the album and redirects the player to the stream.

### Search and play the first track

```bash
URI=$(curl -s "$API/api/v1/search?type=track&limit=1&q=paranoid%20black%20sabbath" | jq -r '.track.items[0].uri')
curl -X POST $API/api/v1/play -H 'content-type: application/json' -d "{\"uri\":\"$URI\"}"
```

### Is everything healthy?

`/status`: `soloist.logged_in` (paired), `soloist.ws_connected` (Soloist running),
`processes.encoder.running`, `updater.days_left` (should never get near 0; the container
updates automatically). Useful for a health indicator in your integration's settings page.

---

## 7. Writing a Lyrion Music Server plugin

Lyrion plays HTTP streams natively, so the plugin only has to drive the API; LMS handles the
audio. Suggested design, mirroring the FM Radio plugin:

**Plugin settings**

- *Soloist API URL* (`http://192.168.1.75:8380`), *API token* (optional).
- Read the stream URL from `/api/v1/status` (`stream.url`) instead of asking for it; offer an
  override field only for unusual network setups. Show `soloist.logged_in`, device name and
  `updater.days_left` on the settings page as a connection test.

**Menus (OPML / `Slim::Plugin::OPMLBased`)**

- Top level: *Search*, *Playlists*, *Albums*, *Liked songs*, *Artists*, *Podcasts*,
  *Recently played*, *Now playing*.
- Each menu maps 1:1 to an endpoint in section 4. Each Entity → menu item:
  title = `identity.name`, line 2 = creator names, icon = cover (`default`/`large`),
  `type => 'playlist'` (container) or `'audio'` (track/episode).
- Opening a container calls `/api/v1/item/{uri}`; use `next`/`offset` for paging.
  Show `children.error` as a text item if present.

**Playing**

- Give each playable item the URL `"$api/api/v1/listen?uri=$uri"` (plus `&token=` if needed).
  When LMS opens it, the container starts that item and redirects LMS to the stream. Nothing
  else is needed for a first version.
- Better: a protocol handler (`spotifysoloist://<uri>`), whose `getNextTrack`/`canDirectStream`
  calls `POST /play {uri}` and then returns the stream URL. This lets the plugin also handle
  *Play next* / *Add to queue* (`POST /queue`), and treats the whole Spotify context as one
  endless "radio" stream in LMS.
- Map LMS transport to the API: pause → `/pause`, play → `/play`, next/prev → `/next`/`/prev`.
  The Icecast stream is live, so LMS seeking cannot work on the stream itself; send
  `/seek` to the API instead, if offered at all.
- Keep volume in LMS; leave Soloist at 100.

**Metadata**

- Subscribe to `/api/v1/events` (or poll `/now`) and push `item` title/artist/album/cover/duration
  into the LMS song info (`getMetadataFor`), so the LMS now-playing screen shows the Spotify
  track, independent of codec.

**Latency**

- Expect a few seconds between an API command and hearing it (encoder + Icecast + LMS buffer).
  This is inherent to streaming via Icecast.

**One container = one Spotify device = one stream.** Several LMS players listening to the same
stream hear the same thing in sync-ish. For independent zones, run one container per zone
(different device name, `WEB_PORT`, Icecast port/mount and data directory).

---

## 8. Writing an integration for other systems

The same pattern applies everywhere:

| System | Control | Audio |
|---|---|---|
| Home Assistant | `rest_command` / RESTful sensors on `/now`; or a custom integration consuming `/events` | `media_player.play_media` with the stream URL (any speaker that plays HTTP) |
| Node-RED | http request nodes; SSE via an EventSource node | send stream URL to your player |
| MPD / Volumio / moOde | script calling the API | add stream URL as a web radio |
| Snapcast | API for control | ffmpeg/`stream` source from the Icecast URL |
| Browsers | `fetch` + `EventSource` (the built-in web UI is a full example: `app/static/index.html`) | `<audio src=stream.url>` |

Checklist for any integration:

1. Configure the API base URL (+ token).
2. Read `stream.url` and `stream.content_type` from `/api/v1/status`.
3. Check `soloist.logged_in`; if false, tell the user to select the device in the Spotify app.
4. Browse/search via section 4, play via `POST /play {uri}` (or `/listen`).
5. Point the player at the stream once; keep it connected (it carries silence when idle).
6. Follow `/api/v1/events` for now-playing metadata and state.
7. Handle `409` (Soloist restarting or not logged in) by retrying after a `soloist_connected` /
   `auth_state` event.

---

## 9. Stability

All endpoints in this document are `v1`. Fields may be **added** in v1; existing fields and
endpoints will not be removed or renamed without a new version prefix (`/api/v2`). Ignore
unknown fields. The admin endpoints (`/config`, `/secrets`, `/system/*`, `/webapi` setup) are
for the web UI and may change; integrations should not depend on them.
