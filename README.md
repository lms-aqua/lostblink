# lostblink

**Live RTSP from Blink cameras — including actual live view, not just motion clips.**

Blink cameras have no RTSP output, no local API, and no documented way to get live video. Every
open-source attempt at live streaming has stopped at the same wall: the two proprietary transports
Blink's own app uses, `immis://` and a non-standard `rtsps://`, which nobody had written down.

This repo does two things:

1. **Documents both protocols, byte by byte** — see [`docs/`](docs/). As far as I can tell this is
   the only public specification of the IMMI wire format.
2. **Implements them in Python** and publishes the result to MediaMTX as a normal RTSP stream that
   Frigate, Scrypted, Home Assistant, or any NVR can consume.

> **Status: alpha.** The protocol layer is implemented and unit-tested against the documented wire
> format (81 tests, no account needed). It has not yet been run end-to-end against live hardware —
> see [Verification status](#verification-status) before you trust it with anything.

---

## Why this exists

The two halves of the problem had each been solved, separately, by projects that could not talk to
each other:

| | Live video? | RTSP out? |
| --- | :---: | :---: |
| [roger-/blinkbridge](https://github.com/roger-/blinkbridge) | ✗ — 30s-old clips, frozen frames | ✓ |
| [BitWise-0x/homebridge-blink-security](https://github.com/BitWise-0x/homebridge-blink-security) | ✓ — both protocols | ✗ — HomeKit/SRTP only |
| **lostblink** | **✓** | **✓** |

Full landscape of all eight projects in [`docs/prior-art/`](docs/prior-art/README.md).

## How it works

```
                    ┌─────────────────────────────────────────┐
                    │              Blink cloud                 │
                    │   POST .../liveview  →  stream URL       │
                    └────────────┬────────────────────────────┘
                                 │
             ┌───────────────────┴───────────────────┐
             │                                       │
      immis://…  (Mini, Indoor/Outdoor,      rtsps://…  (XT, XT2)
                  Doorbell, Floodlight)
             │                                       │
   ┌─────────▼──────────┐               ┌────────────▼───────────┐
   │  ImmiTunnel        │               │  RtspTunnel            │
   │  122-byte header   │               │  OPTIONS/DESCRIBE/     │
   │  9-byte de-framing │               │  SETUP/PLAY, then      │
   │  0x0A + 0x12 alive │               │  RTP de-framing        │
   └─────────┬──────────┘               └────────────┬───────────┘
             │                                       │
             └──────────────┬────────────────────────┘
                            │  MPEG-TS
                     ┌──────▼───────┐
                     │   ffmpeg     │  stream copy, never restarted
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐
                     │   MediaMTX   │  rtsp://host:8554/front_door
                     └──────┬───────┘
                            │
                  Frigate · Scrypted · HA · VLC
```

Blink caps a live session at **300 seconds**. Rather than let the stream drop, lostblink opens the
replacement session 20 seconds early, waits for a keyframe, and splices — the ffmpeg process
publishing to MediaMTX is never restarted, so downstream consumers never see an interruption. See
[`docs/protocol/04-session-lifecycle.md`](docs/protocol/04-session-lifecycle.md).

When live view is off, unavailable, or budget-limited, it falls back to blinkbridge's genuinely good
idea: loop the last motion frame as a still video so the RTSP session stays continuously valid.

## ⚠️ Battery

**This is the constraint that should drive your configuration.** Blink cameras are battery powered
and a live session is continuous radio-on:

| Mode | Battery life |
| --- | --- |
| `off` — clip/still bridge only | ~1–2 years (normal) |
| `on_motion` — one session per event | weeks to months, depending on traffic |
| `always` — continuous | **1–3 days** |

`always` is for mains-powered devices only — wired Floodlight, Mini, or a Doorbell on a transformer.
The default is `on_motion` with a 6-sessions-per-hour cap, a 60-second cooldown, and a 30-minute
daily budget per camera. If a camera reports low battery, live view is refused regardless of mode.

## Quick start

```bash
git clone https://github.com/lms-aqua/lostblink.git && cd lostblink
cp config/config.example.json config/config.json
```

Edit `config/config.json` with your Blink credentials, then authenticate once (this prompts for the
2FA code Blink emails or texts you):

```bash
docker compose run --rm lostblink auth
```

Then start it:

```bash
docker compose up -d
```

Streams appear at `rtsp://<host>:8554/<camera_name>`, lowercased with spaces as underscores — a
camera named "Front Door" becomes `rtsp://host:8554/front_door`. Check it:

```bash
ffplay rtsp://localhost:8554/front_door
```

### Running from source

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
LOSTBLINK_CONFIG=config/config.json python -m lostblink auth
LOSTBLINK_CONFIG=config/config.json python -m lostblink run
```

Requires Python 3.11+ and `ffmpeg`/`ffprobe` on PATH.

## Configuration

Everything lives in `config/config.json`; see
[`config/config.example.json`](config/config.example.json) for the annotated version. The keys worth
knowing:

| Key | Default | Notes |
| --- | --- | --- |
| `live.mode` | `on_motion` | `off` · `on_motion` · `always` |
| `live.mains_powered` | `[]` | Cameras exempt from the battery rails |
| `live.daily_seconds_budget` | `1800` | Per camera, per day |
| `blink.poll_interval` | `30` | Floored at 10s — faster gets you nothing and risks a ban |
| `ffmpeg.hwaccel` | `""` | `qsv`, `vaapi`, `cuda`. Decode only |
| `rtsp.address` | `mediamtx` | Where to publish |

Credentials can come from `LOSTBLINK_USERNAME` / `LOSTBLINK_PASSWORD` instead of the config file, so
they can be Docker secrets rather than sitting in a bind mount.

## Verification status

Being straight about what has and has not been tested:

| | Status |
| --- | --- |
| IMMI URL parsing, 122-byte header, 9-byte de-framing | ✅ unit-tested (33 tests) |
| RTSP negotiation, SDP parsing, RTP/interleaved de-framing | ✅ unit-tested (37 tests) |
| MPEG-TS keyframe detection for splicing | ✅ unit-tested (11 tests) |
| Protocol correctness vs. real Blink servers | ⚠️ **not yet verified** |
| Session renewal / handover against real hardware | ⚠️ **not yet verified** |
| End-to-end into Frigate | ⚠️ **not yet verified** |

The wire format is ported from a working GPL-3.0 implementation and the unit tests assert every
documented offset, but a port is not a proof. If you run this against real cameras, the failure-mode
tables in [`docs/protocol/02-immi-protocol.md`](docs/protocol/02-immi-protocol.md) tell you what each
symptom means.

```bash
pip install -e ".[dev]" && pytest -q
```

## What was fixed relative to blinkbridge

24 defects catalogued with file:line in [`docs/upstream-bug-audit.md`](docs/upstream-bug-audit.md).
The ones you would actually hit:

- **Local-storage cameras crash the process** — `None` reaches `subprocess.Popen`. This is upstream
  issue #1, still open.
- **Won't import on Python 3.11 or earlier** — a PEP 701 nested-quote f-string.
- **Cameras show each other's images** — all cameras share one `last_frame.jpg`, written from
  concurrent threads.
- **Clips without audio kill the camera** — an `assert` in a thread whose exception is swallowed.
- **Motion is missed** whenever the event ends between two polls.
- **One full API refresh per camera per poll** — the ban risk the README warns about.
- **A camera disabled after 3 failures never comes back.**

## Documentation

| | |
| --- | --- |
| [Blink REST API](docs/protocol/01-blink-rest-api.md) | Endpoints, auth, the async command model, rate limits |
| [**IMMI protocol**](docs/protocol/02-immi-protocol.md) | Byte-level spec of `immis://`. The one that did not exist. |
| [RTSPS path](docs/protocol/03-rtsp-mpegts.md) | Why `ffmpeg -i rtsps://…` cannot work, and what to do instead |
| [Session lifecycle](docs/protocol/04-session-lifecycle.md) | 300s cap, seamless handover, battery budgets |
| [Prior art](docs/prior-art/README.md) | All eight projects, what each solved and where it stopped |
| [Upstream bug audit](docs/upstream-bug-audit.md) | 24 findings in blinkbridge, with line numbers |

## Licensing and attribution

**GPL-3.0-or-later.** Not a preference — `lostblink/proxy/immi.py` and `lostblink/proxy/rtsp.py` are
Python ports of `src/lib/proxy.ts` from
[BitWise-0x/homebridge-blink-security](https://github.com/BitWise-0x/homebridge-blink-security),
which is GPL-3.0, making them derivative works. Full attribution in [`NOTICE.md`](NOTICE.md).

Note also that [roger-/blinkbridge](https://github.com/roger-/blinkbridge) has **no license file**,
so it is all-rights-reserved. lostblink contains none of its code — the architecture was
reimplemented from a reading of it, and the bug audit cites it as commentary.

## Disclaimer

Not affiliated with, endorsed by, or supported by Blink, Immedia Semiconductor, or Amazon. This
documents a private protocol between a customer's own client and a service they are already
authorised to use, reached with their own credentials. It grants no access the Blink app does not
already grant and works only on cameras you own.

Using it may violate Amazon's terms of service, and Amazon can change or break the protocol at any
time without notice. Do not rely on this for anything safety-critical.
