# Blink REST API — what we actually know

> Sources: [MattTW/BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) (unlicensed docs, observational),
> [fronzbot/blinkpy](https://github.com/fronzbot/blinkpy) (MIT), and
> [BitWise-0x/homebridge-blink-security](https://github.com/BitWise-0x/homebridge-blink-security) (GPL-3.0).
> Blink publishes no official API. Everything here is reverse-engineered from the mobile apps and
> will break without warning when Amazon changes it.

## Hosts and tiers

| Purpose | Host |
| --- | --- |
| Bootstrap / login | `https://rest-prod.immedia-semi.com` |
| Post-login (region pinned) | `https://rest-{tier}.immedia-semi.com` — e.g. `rest-u002`, `rest-e003` |
| Live view (IMMI) | `immis://{lv-host}:443/...` — e.g. `lv3-app-u002.immedia-semi.com` |
| Live view (RTSPS) | `rtsps://{lv-host}:443/...` |

After login the response carries an `account.tier` (or `region.tier`). **Every subsequent call must go
to that tier's host.** Using `rest-prod` post-login yields sporadic 401s. blinkpy handles this in
`blinkpy/auth.py`; we rely on it.

## Resource hierarchy

```
Account
└── Network            (one per Sync Module / site)
    ├── Camera         "camera"  — XT, XT2, wired floodlight  → RTSPS live view
    ├── Owl            "owl"     — Blink Mini / Mini 2 / 2K+  → IMMI live view
    ├── Doorbell       "lotus"   — Blink Video Doorbell       → IMMI live view
    └── Sync Module    (+ optional USB local storage)
```

The device *type* determines the live-view endpoint **and** the streaming protocol you get back.
Getting this wrong is the single most common failure in every prior-art project.

## Authentication

Two generations exist in the wild:

1. **Legacy `TOKEN-AUTH`** — `POST /api/v5/account/login` with `{email, password, unique_id, ...}`,
   returns `auth.token`. Sent as a `TOKEN-AUTH: <token>` header. 2FA via
   `POST /api/v4/account/{acct}/client/{client}/pin/verify`.
2. **OAuth 2.0 + PKCE** — what current app builds use (`client_id: ios`). blinkpy migrated to this;
   `homebridge-blink-security` implements it in `src/lib/auth.ts`.

`lostblink` delegates all of this to blinkpy and never implements auth itself. This is deliberate:
auth is the fastest-moving part of the surface and the part most likely to get an account flagged.

### The `unique_id` / hardware id matters

Blink ties 2FA trust to a client id. Regenerating it on every start forces a new PIN each time and
looks like credential stuffing. blinkpy persists it in the credentials file — **never delete
`.cred.json` as a "fix"**, and never commit it.

## The asynchronous command model

Anything that touches the camera radio (thumbnail, clip, live view, arm) is **asynchronous**:

1. `POST` the action → response contains `id` (a.k.a. `command_id`) and `network_id`.
2. Poll `GET /network/{network_id}/command/{command_id}` roughly every 1s.
3. Completion is `complete: true`. `status_code` `908` means "still running, keep waiting"; anything
   else is a terminal failure.
4. Optionally `POST /network/{network_id}/command/{command_id}/done/` to release it.

`409 Conflict` with a body containing `busy` means the Sync Module is already running a command.
Back off exponentially and retry — do not hammer it.

> **Live view is the exception.** The liveview POST returns the stream URL *immediately* in the same
> response. The command deliberately stays `complete: false` for the whole session — it *is* the
> session handle. Polling it to completion, as you would for a thumbnail, blocks until timeout and
> throws the URL away. `homebridge-blink-security` calls this out explicitly in
> `src/devices/index.ts:1111`, and it is the bug that makes most naive live-view attempts fail.

## Live view endpoints

Version differs per device class — this is not cosmetic, the wrong version 404s:

| Device | Endpoint |
| --- | --- |
| Camera (XT/XT2/wired) | `POST /api/v6/accounts/{acct}/networks/{net}/cameras/{id}/liveview` |
| Owl (Mini) | `POST /api/v2/accounts/{acct}/networks/{net}/owls/{id}/liveview` |
| Doorbell (Lotus) | `POST /api/v2/accounts/{acct}/networks/{net}/doorbells/{id}/liveview` |

blinkpy historically used `/api/v5/...` for cameras; it was broken by an upstream change and fixed in
[blinkpy#367](https://github.com/fronzbot/blinkpy/issues/367) / PR #389. `homebridge-blink-security`
is on v6. `lostblink` requests v6 and falls back to v5 — see `lostblink/blink/liveview.py`.

Request body:

```json
{ "intent": "liveview", "motion_event_start_time": "" }
```

`motion_event_start_time` as `""` or `null` means "start now". Passing a real motion timestamp asks
for the stream anchored to that event.

Response:

```json
{
  "command_id": 1234567890,
  "join_available": true,
  "join_state": "available",
  "server": "rtsps://lv3-app-u002.immedia-semi.com:443/<opaque>?client_id=247&blinkRTSP=true",
  "duration": 300,
  "continue_interval": 30,
  "continue_warning": 10,
  "submit_logs": true,
  "new_command": true,
  "media_id": null,
  "options": {}
}
```

### Session lifetime — the numbers that matter

| Field | Typical | Meaning |
| --- | --- | --- |
| `duration` | 300 | Hard cap in seconds. The server tears the stream down at this point, period. |
| `continue_interval` | 30 | You must signal liveness at least this often or you get dropped early. |
| `continue_warning` | 10 | Seconds of warning before expiry. |

Renewal is a fresh liveview POST, not a flag on the existing one. `lostblink` re-requests at
`duration - 20s` and hot-swaps the tunnel underneath a persistent ffmpeg process so the RTSP output
never drops. See `docs/protocol/04-session-lifecycle.md`.

**These cameras are battery powered.** A 300s live session is expensive. Continuous live view will
flatten AA lithiums in days. This is a physics constraint, not a software one — see the battery
guidance in the root README.

## Media / motion discovery

There is **no push API**. Every integration polls:

- `GET /api/v1/accounts/{acct}/media/changed?since={iso8601}&page={n}` — the media list.
- `GET /api/v3/accounts/{acct}/homescreen` — device state incl. `motion_detected`, `last_record`.

Realistic latency from motion to your knowing about it is **5–20 seconds**, and the Blink app itself
is not much faster. Anyone claiming sub-second motion push is describing the *live stream* already
being open, not detection.

### Rate limiting

Undocumented, and Blink does not return `Retry-After`. Observed community consensus:

- Homescreen polling faster than ~10s risks throttling; faster than ~5s risks a temporary ban.
- blinkpy self-throttles `refresh()` internally (default 30s) — this is why upstream blinkbridge's
  `poll_interval: 1` does not actually poll every second, and also why its per-camera `refresh()`
  loop is wasteful rather than instantly fatal.

`lostblink` does **one** `refresh()` per cycle for all cameras and treats 429/`busy` as a hard
backoff signal.

## Local storage (Sync Module 2 USB)

Clips on a Sync Module's USB stick are **not** in the normal media list. They need a separate
manifest dance:

1. `POST .../networks/{net}/sync_modules/{sm}/local_storage/manifest/request` → command id
2. Poll the command
3. `GET .../local_storage/manifest/{manifest_id}` → clip list
4. `POST .../local_storage/manifest/{manifest_id}/clip/request/{clip_id}` → command id
5. Poll, then download

Upstream blinkbridge never does this, which is the root cause of
[blinkbridge#1](https://github.com/roger-/blinkbridge/issues/1): local-only cameras return zero
clips, `save_latest_clip()` returns `None`, and `None` reaches `subprocess.Popen`. See
`docs/upstream-bug-audit.md` **B-01**.

Two projects sidestep the API entirely by intercepting the USB writes at the hardware level —
[OVR92/BlinkPi](https://github.com/OVR92/BlinkPi) and
[renanfernandes/watchman](https://github.com/renanfernandes/watchman). Both are excellent and both
are orthogonal to live view; see `docs/prior-art/`.
