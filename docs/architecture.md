# Architecture

## Layout

```
lostblink/
├── __main__.py          CLI: `auth` (interactive) and `run` (service)
├── app.py               Application + CameraWorker: one shared poll, fan out
├── config.py            Dataclass config tree, validated on load
├── blink/
│   └── liveview.py      Liveview REST calls, session model, battery policy
├── proxy/
│   ├── base.py          LiveTunnel: the MPEG-TS source interface
│   ├── immi.py          immis:// -- 122-byte header, 9-byte frames
│   └── rtsp.py          rtsps:// -- RTSP negotiation, RTP de-framing
├── media/
│   └── ffmpeg.py        Async ffmpeg/ffprobe, probing, still-frame generation
└── stream/
    └── publisher.py     LivePublisher: keyframe splicing across sessions
```

## Design decisions

### One tunnel interface, two protocols

`LiveTunnel` is an async iterator of MPEG-TS chunks. `ImmiTunnel` and `RtspTunnel` differ entirely
in how they connect and de-frame, and not at all in what they produce. Everything downstream — the
publisher, the splicer, ffmpeg — is written once.

`proxy.open_tunnel(url)` picks the implementation from the **URL scheme**. Never from the camera
model: firmware revisions move cameras between protocol families, and the scheme Blink returns is
always correct.

### asyncio everywhere, no threads

Upstream runs ffmpeg via `subprocess.Popen` and does still-frame generation on a bare
`threading.Thread` whose exceptions vanish (bug B-04), so a failure surfaces three layers away as a
permanently disabled camera. Here every subprocess is `asyncio.create_subprocess_exec` and every
exception propagates to a caller that can decide what it means.

It also removes upstream's `/proc/{pid}/fd` polling (bug B-11), which is Linux-only, racy, and
blocks the event loop for up to ten seconds while every other camera waits.

### The publisher outlives the session

The single most important structural decision. Blink caps sessions at 300 seconds, but Frigate
treats a stream that stops as a failure. So the ffmpeg process publishing to MediaMTX is **started
once and never restarted** — only the tunnel feeding its stdin is swapped, at a keyframe, with the
replacement already warm.

```
Application
└── CameraWorker (one asyncio task per camera)
    └── LivePublisher (one ffmpeg process, long-lived)
        └── LiveTunnel (swapped every ~280s)
```

Consequence: `LivePublisher` owns the ffmpeg lifetime and `CameraWorker` owns the *session*
lifetime, and those are deliberately different. A failed renewal degrades to the still-frame
fallback rather than dropping the RTSP session.

### One API refresh per cycle

`Application._poll_loop` calls `blink.refresh()` once, then fans out to every `CameraWorker`
concurrently. Upstream calls a full homescreen refresh once *per camera* (bug B-13), multiplying API
load by camera count for identical data — the ban risk its own README warns about.

Cameras are then processed with `asyncio.gather`, so total latency is the slowest camera rather than
the sum (bug B-20, on upstream's TODO list).

### Policy is separate from mechanism

`LiveViewPolicy` decides *whether* a session is allowed — hourly cap, cooldown, daily budget,
battery floor. `LiveViewClient` knows *how* to request one. They are separate objects because the
battery constraint is the thing most likely to need tuning per deployment, and it should be
adjustable without touching protocol code.

### Configuration is a validated dataclass tree

Upstream's `config.py` mutates module-level globals as an import side effect, so the package cannot
be imported at all without a config file present — which makes it untestable. Here `Config.load()`
returns a validated object that gets passed down. Unknown keys warn rather than fail, because a
typo should tell you it did nothing rather than refuse to start.

## Data flow

### Live path

```
poll cycle
  └─ last_record advanced?                    (app.CameraWorker.motion_advanced)
       └─ policy allows a session?            (liveview.LiveViewPolicy.check)
            └─ POST .../liveview  → URL       (liveview.LiveViewClient.request)
                 └─ open_tunnel(url)          (proxy.open_tunnel → Immi|Rtsp)
                      └─ handshake, keepalives
                           └─ MPEG-TS chunks
                                └─ ffmpeg -f mpegts -i pipe:0 -c copy -f rtsp
                                     └─ MediaMTX → rtsp://host:8554/<camera>
```

At T+280 the worker requests a fresh session, opens a second tunnel, buffers until
`find_keyframe_offset` locates a random access point, then hands over — the same ffmpeg keeps
running.

### Fallback path

When live view is off, refused, or failing, the camera publishes a looped still video built from the
last motion clip's final frame. This is blinkbridge's idea and it is the right one: downstream NVRs
disconnect and alarm on a stream that stops, so a frozen frame beats no frame.

## Failure handling

Errors are classified rather than caught uniformly. Upstream's single bare `except Exception` kills
a healthy ffmpeg process on a transient HTTP timeout (bug B-19), and combined with its
never-recover logic (B-18) three network blips permanently lose a camera.

| Class | Response |
| --- | --- |
| Transient (timeout, refresh hiccup) | Log, keep streaming, retry next cycle |
| Busy / 409 | Exponential backoff 1s → 10s |
| Rate limited / 429 | 5-minute pause for that camera |
| Tunnel died | Count a failure, back off exponentially with a ceiling, fall back to stills |
| Auth failed | Stop live activity, surface `AUTH_REQUIRED`. Never retry auth in a loop |

`WorkerStats.backoff()` caps at `max_restart_delay_seconds` and there is no terminal state — a
camera can always come back, and the log says when the next attempt is due.

## What is deliberately not here

- **Authentication.** Delegated entirely to blinkpy. It is the fastest-moving part of Blink's
  surface and the part most likely to get an account flagged; a maintained library should own it.
- **ONVIF.** On upstream's TODO. Worth doing, out of scope for a first release.
- **Two-way audio.** The IMMI framing carries client→server messages, so it is probably reachable,
  but nothing in the reference implementation exercises it.
- **A web UI.** MediaMTX already serves HLS and WebRTC if you enable those ports.
