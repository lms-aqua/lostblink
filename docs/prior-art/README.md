# Prior art — everyone who has attacked the Blink problem

Every serious open-source Blink project, what it solved, what it did not, and what `lostblink` takes
from it. Metadata captured 2026-08-02.

There are exactly **three** strategies for getting video out of a Blink camera, and every project
below is one of them:

1. **Cloud clip polling** — ask Blink's API for motion clips after the fact. Easy, always ~10–30 s
   behind, needs a subscription for cloud clips.
2. **USB interception** — impersonate a flash drive on the Sync Module 2 and catch clips as they are
   written. No subscription, no cloud, but clips-only and needs hardware.
3. **Live view protocol** — request a live session and decode the proprietary stream. Real-time,
   works without a subscription, hard, and until now essentially undocumented.

Only strategy 3 gives live video. Only one project in the wild had actually solved it, and it was
buried inside a HomeKit plugin.

---

## The landscape

| Project | Lang | License | Stars | Strategy | Live video? |
| --- | --- | --- | ---: | --- | --- |
| [fronzbot/blinkpy](#fronzbotblinkpy) | Python | MIT | 777 | 1 | ✗ (URL only) |
| [MattTW/BlinkMonitorProtocol](#matttwblinkmonitorprotocol) | Docs | **none** | 523 | — | documents it |
| [roger-/blinkbridge](#roger-blinkbridge) | Python | **none** | 120 | 1 | ✗ |
| [colinbendell/homebridge-blink-for-home](#colinbendellhomebridge-blink-for-home) | JS/TS | MIT | 94 | 3 | partial, abandoned |
| [OVR92/BlinkPi](#ovr92blinkpi) | Python | MIT | 56 | 2 | ✗ |
| [adrian-dobre/BlinkWebUI](#adrian-dobreblinkwebui) | TS/React | GPL-3.0 | 33 | 1 | ✗ (blocked by IMMI) |
| [renanfernandes/watchman](#renanfernandeswatchman) | Python | **none** | 13 | 2 | ✗ |
| [**BitWise-0x/homebridge-blink-security**](#bitwise-0xhomebridge-blink-security) | TS | GPL-3.0 | 11 | **3** | **✓ both protocols** |

Note the inverse relationship between stars and how much of the problem is solved. The 11-star repo
is the one that cracked it.

---

## fronzbot/blinkpy

**[github.com/fronzbot/blinkpy](https://github.com/fronzbot/blinkpy)** · Python · MIT · 777★ ·
active (pushed 2026-07-25) · default branch `dev`

The de-facto Python client, and what Home Assistant's Blink integration is built on. Async
(`aiohttp`), covers auth (now OAuth2+PKCE), homescreen, media list, clip download, arm/disarm,
thumbnails, and local storage manifests.

**What it gives us:** everything below the live stream. `lostblink` depends on it and deliberately
implements **no** authentication of its own — auth is the fastest-moving, highest-risk surface, and
blinkpy has maintainers tracking it.

**Where it stops:** `camera.get_liveview()` returns the URL string and nothing consumes it.
[Issue #343](https://github.com/fronzbot/blinkpy/issues/343) — "*get_liveview() returns a immis
protocol link*" — has sat open with no protocol answer. The endpoint itself broke when Blink moved
versions ([#367](https://github.com/fronzbot/blinkpy/issues/367), fixed by PR #389); it is on `/v5`
where current app builds use `/v6` for cameras.

**Traps we hit:**
- `refresh()` is internally throttled (~30 s). Calling it in a tight loop does not do what the
  caller thinks — see bug **B-13**.
- The credentials blob includes the hardware/unique id. Regenerating it re-triggers 2FA and looks
  hostile to Blink. Persist it.
- The auth flow changed under blinkbridge and broke it (blinkbridge commit `ee52227`). Pin it.

---

## MattTW/BlinkMonitorProtocol

**[github.com/MattTW/BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol)** ·
documentation · **no license** · 523★ · last pushed 2025-07-30

The Rosetta Stone. Observational documentation of the REST surface, reverse-engineered from the
mobile app: login/logout/PIN, homescreen, notification config, arm/disarm, schedules, camera
motion/thumbnail/clip/snooze/liveview, the media-changed feed, and the command-polling model.

Where the `duration: 300` / `continue_interval: 30` / `continue_warning: 10` live-view session
numbers are written down, and where the async command model (poll `GET
/network/{net}/command/{id}`, `status_code` 908 = in progress) is explained.

**Where it stops:** [`camera/liveview.md`](https://github.com/MattTW/BlinkMonitorProtocol/blob/master/camera/liveview.md)
documents how to *obtain* the stream URL and then stops at the URL.
[Issue #12 "Live streaming"](https://github.com/MattTW/BlinkMonitorProtocol/issues/12) is the thread
where the community tried and failed to get past it. No rate-limit documentation exists anywhere.

**License caution:** no license file means all rights reserved. We cite and link it; we do not copy
text from it.

---

## roger-/blinkbridge

**[github.com/roger-/blinkbridge](https://github.com/roger-/blinkbridge)** · Python · **no license**
· 120★ · pushed 2026-03-06

The direct ancestor. Polls blinkpy for motion clips, uses ffmpeg to extract the last frame, builds a
~0.5 s still video, and loops it into MediaMTX via the concat demuxer — so downstream consumers
(Frigate, Scrypted) see an RTSP stream that never drops.

**The good idea, and we keep it.** Frigate disconnects and alarms on a stream that stops. Looping
the last frame keeps the session continuously valid. `lostblink` retains this as the fallback layer
beneath live view.

**Where it stops:** ~30 s behind reality by construction, and the frame is frozen between events. No
live view at all. 24 defects catalogued in [`../upstream-bug-audit.md`](../upstream-bug-audit.md),
including the still-open [issue #1](https://github.com/roger-/blinkbridge/issues/1) (local-storage
cameras crash the process).

**License caution:** **no license file — all rights reserved.** We may read it and learn from it; we
may not redistribute it or a derivative. `lostblink` is an independent implementation, is private,
and copies no blinkbridge code. See [`../../NOTICE.md`](../../NOTICE.md).

---

## colinbendell/homebridge-blink-for-home

**[github.com/colinbendell/homebridge-blink-for-home](https://github.com/colinbendell/homebridge-blink-for-home)**
· JS/TS · MIT · 94★ · **archived 2025-04-24**

The first serious attempt at live view. README described it as WIP — "*Liveview (currently on Gen1
cameras)*" — and it never got further.

**Historically important for the failure mode it documents.**
[Issue #20](https://github.com/colinbendell/homebridge-blink-for-home/issues/20), open since Nov
2020: *"LiveView doesn't work for XT2, Blink, new indoor, new outdoor cameras — ffmpeg exited with
code: null and signal: SIGILL"*.

That signal is the fingerprint of the two problems in
[`../protocol/03-rtsp-mpegts.md`](../protocol/03-rtsp-mpegts.md): ffmpeg's RTSP demuxer discarding
the pre-`PLAY` keyframe, and the payload being MPEG-TS-over-RTP rather than H.264. Feeding the
result to a decoder produces exactly this. Five years of "Blink live view doesn't work with ffmpeg"
folklore traces back to here.

Archived, so nothing lands upstream. MIT, so its ideas are freely reusable.

---

## OVR92/BlinkPi

**[github.com/OVR92/BlinkPi](https://github.com/OVR92/BlinkPi)** · Python · MIT · 56★ · pushed
2026-05-18

Strategy 2, done well. A Pi Zero 2 W plugs into a Sync Module 2's USB port and presents itself as a
flash drive using the Linux `g_mass_storage` gadget. The SM2 writes motion clips to it; every 30 s
the Pi loop-mounts the backing image read-only, validates new clips (size → stability window →
`ffprobe`), and pushes them to SMB or any rclone remote.

Three systemd units: `blink-gadget.service` (boot), `blink-sync.timer` (30 s), `blink-wipe.timer`
(nightly 03:00). Destinations are an ABC so new backends are cheap.

**Worth stealing:** the three-stage clip validation. "File exists" is not "file is complete" — the
SM2 is still writing. Size check, then a stability window, then a real `ffprobe` parse. `lostblink`
uses the same ladder before handing any clip to the pipeline.

**Where it stops:** clips only, 30 s granularity, needs hardware and physical access. No live view,
and by design it never talks to Blink's API at all.

---

## adrian-dobre/BlinkWebUI

**[github.com/adrian-dobre/BlinkWebUI](https://github.com/adrian-dobre/BlinkWebUI)** · TypeScript /
React / MaterialUI · GPL-3.0 · 33★ · last pushed 2023-01-08

A browser front-end for Blink — recordings (play, download, delete), enable/disable cameras,
arm/disarm networks, module inventory. Frontend-only, talks to a companion `BlinkWebService`.
Developed against EU servers and XT2 hardware.

**Why it is in this list:** its README contains the clearest statement of the wall everyone hit —
*"Unable to open a live view to the camera, it seems that it uses a proprietary protocol (immis)"*.
An independent, competent developer who reverse-engineered the REST API from the mobile app, and
still stopped dead at `immis://`.

That sentence is the entire justification for
[`../protocol/02-immi-protocol.md`](../protocol/02-immi-protocol.md).

Unmaintained since early 2023, so its endpoint set predates the OAuth2 migration.

---

## renanfernandes/watchman

**[github.com/renanfernandes/watchman](https://github.com/renanfernandes/watchman)** · Python /
Flask · **no license** · 13★ · pushed 2026-07-11

Strategy 2 again, arrived at independently. Pi as a virtual USB drive for a Sync Module 2, intercept
motion `.mp4`s, archive locally, browse and play them over a Flask web UI. `watchman.py` does
detection and archival, `web.py` serves the UI, systemd runs both.

Its distinguishing trick: after detecting a write it **temporarily disconnects the virtual drive**,
moves the file into the archive, and reconnects — avoiding the read-while-writing corruption that a
naive loop-mount hits.

No subscription required, no cloud dependency. No live view, no RTSP, no streaming. **No license —
all rights reserved**, so it is reference-only.

---

## BitWise-0x/homebridge-blink-security

**[github.com/BitWise-0x/homebridge-blink-security](https://github.com/BitWise-0x/homebridge-blink-security)**
· TypeScript · **GPL-3.0** · 11★ · active (pushed 2026-07-26)

**This is the one that solved it.** A Homebridge plugin exposing Blink cameras, doorbells and sirens
to Apple Home — and buried in `src/lib/proxy.ts` (~800 lines) is a complete, working implementation
of *both* Blink live-view protocols.

- `ImmiTunnel` — TLS to the IMMI host, the 122-byte binary connection header, the 9-byte
  type/seq/length frame stripper, and both keepalive timers (`0x0A` at 10 s, `0x12` at 1 s).
- `RtspToH264Proxy` — despite the name, emits **MPEG-TS**, not H.264. Performs RTSP negotiation
  itself over TLS, de-frames interleaved RTP, strips RTP headers, and serves clean TS on a local
  socket — bypassing ffmpeg's RTSP demuxer entirely.

The source comments are the actual documentation for this protocol. Two in particular are worth the
whole repo:

> *"ffmpeg's RTSP state machine discards all RTP data received before it transitions to
> RTSP_STATE_STREAMING (after PLAY), losing the initial keyframe."*

> *"Previously we checked for 0x47 sync byte but that rejected frames where the IMMI/MPEG-TS
> boundaries didn't align, dropping PAT/PMT tables needed for audio detection."*

Both are hard-won and both are load-bearing. Elsewhere it is the only project that documents the
`immis://HOST/CONNECTION_ID__IMDS_SERIAL?client_id=N` URL layout, that live view must use `/v6` for
cameras and `/v2` for owls and doorbells, and — critically — that the liveview command must **not**
be polled to completion because it stays deliberately incomplete for the session's lifetime
(`src/devices/index.ts:1111`).

**What `lostblink` takes:** `lostblink/proxy/immi.py` and `lostblink/proxy/rtsp.py` are Python ports
of `src/lib/proxy.ts`. That makes them a derivative work, which is why **`lostblink` is GPL-3.0**.
Attribution in [`../../NOTICE.md`](../../NOTICE.md).

**Where it stops — and why this project exists:** it is a *Homebridge plugin*. The stream is
transcoded to SRTP for HomeKit and terminates there. There is no RTSP output, nothing for Frigate or
Scrypted or an NVR to consume, and live view only runs while a HomeKit client is actively watching.
The protocol work is trapped inside a HomeKit integration.

`lostblink` lifts it out and points it at MediaMTX.

---

## What was actually missing

Nobody had connected the two halves:

- blinkbridge had **RTSP output** and the never-drop stream trick, but only stale clips.
- homebridge-blink-security had **live video**, but only into HomeKit.

Neither had documentation of the wire protocol that anyone could act on, which is why BlinkWebUI,
blinkpy, BlinkMonitorProtocol and bling-desktop all independently stopped at the same wall.

`lostblink` = the live-view protocol, written down properly, in Python, with RTSP out.
