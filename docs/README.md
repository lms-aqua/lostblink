# lostblink documentation

Everything learned about getting video out of a Blink camera, written down so the next person does
not have to reverse-engineer it again.

## Protocol

Read these in order if you are trying to understand how live view works.

| | |
| --- | --- |
| [**01 — Blink REST API**](protocol/01-blink-rest-api.md) | Hosts and tiers, the device hierarchy, auth, the async command model, the liveview endpoints, media discovery, rate limits, and local storage. |
| [**02 — The IMMI protocol**](protocol/02-immi-protocol.md) | Byte-level spec of `immis://`: URL layout, the 122-byte connection header, the 9-byte frame format, both keepalive packets, and a failure-mode table. **This document did not previously exist in public.** |
| [**03 — The RTSPS path**](protocol/03-rtsp-mpegts.md) | Why `ffmpeg -i rtsps://…` cannot work on Blink, the negotiation sequence, and interleaved-RTP de-framing. |
| [**04 — Session lifecycle**](protocol/04-session-lifecycle.md) | The 300-second cap, seamless keyframe handover, battery economics, and backoff. |

## Analysis

| | |
| --- | --- |
| [**Prior art**](prior-art/README.md) | All eight open-source Blink projects: the three strategies, what each solved, and exactly where each stopped. |
| [**Upstream bug audit**](upstream-bug-audit.md) | 24 defects in `roger-/blinkbridge` with file:line, failure mode, and fix. |
| [**Architecture**](architecture.md) | How lostblink is put together and why. |

## The short version

Blink gives you no RTSP, no local API, and no documentation. The live-view REST call hands back a
stream URL in one of two proprietary flavours:

- **`immis://`** — Mini, Indoor/Outdoor, Doorbell, Floodlight. A 122-byte binary handshake, then
  MPEG-TS inside 9-byte frames, with two mandatory keepalive timers.
- **`rtsps://`** — XT, XT2. Looks like RTSP but auto-plays before `PLAY` and carries MPEG-TS over
  RTP, so ffmpeg's own demuxer discards the keyframe and fails.

Both converge on MPEG-TS once you strip the framing, which is why one downstream pipeline handles
both. Sessions last 300 seconds and must be renewed by opening a fresh one and splicing at a
keyframe.

**Dispatch on the URL scheme, never on the camera model.** Firmware revisions move cameras between
families; the scheme Blink returns always tells the truth.
