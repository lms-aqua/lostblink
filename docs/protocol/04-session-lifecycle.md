# Live session lifecycle — making 300-second sessions look continuous

Blink hard-caps a live-view session at `duration` seconds (observed: always 300). At the cap the
server tears the connection down. There is no extend, no renew flag, no `CONTINUE` message —
the only way to keep watching is to **request a brand new session**.

This document is how `lostblink` turns a series of 300-second sessions into an RTSP stream that
never drops, which is what downstream NVRs require.

## The three timers

From the liveview response:

```json
{ "duration": 300, "continue_interval": 30, "continue_warning": 10 }
```

| | | What we do |
| --- | ---: | --- |
| `duration` | 300 s | Hard kill. Plan around it; never let it surprise you. |
| `continue_interval` | 30 s | Liveness floor. IMMI keepalives (10 s) already beat it comfortably. |
| `continue_warning` | 10 s | Too late to be useful for a seamless handover. |

We do **not** wait for `continue_warning`. Renewal starts at `duration - 20 s` (T+280) so a new
session is fully established and producing keyframes before the old one dies.

## State machine

```
                     ┌──────────┐
                     │   IDLE   │  publishing looped still frame
                     └────┬─────┘
        trigger (motion / on-demand / always-on)
                          ▼
                   ┌─────────────┐   liveview POST fails / 409 busy
                   │  REQUESTING ├──────────────────────────► BACKOFF ──┐
                   └──────┬──────┘                                      │
                     URL returned                                       │
                          ▼                                             │
                   ┌─────────────┐   TLS closes / no data in 10 s       │
                   │ CONNECTING  ├──────────────────────────► BACKOFF ──┤
                   └──────┬──────┘                                      │
                    first TS bytes                                      │
                          ▼                                             │
                   ┌─────────────┐                                      │
              ┌───►│    LIVE     │                                      │
              │    └──────┬──────┘                                      │
              │           │ T+280s                                      │
              │           ▼                                             │
              │    ┌─────────────┐  new session healthy                 │
              └────┤  RENEWING   │  (overlap handover)                  │
                   └──────┬──────┘                                      │
                          │ renewal failed, or no trigger, or budget hit │
                          ▼                                             ▼
                     ┌──────────┐ ◄────────────────────────────────────┘
                     │   IDLE   │
                     └──────────┘
```

## Overlap handover — why the RTSP output never drops

The naive approach is: session dies → open a new one. That leaves a 2–5 second hole while the
liveview command round-trips and TLS reconnects. Frigate treats a hole like that as a stream failure
and alarms.

Instead, **the ffmpeg process that publishes to MediaMTX is never restarted.** Only the source
underneath it is swapped.

```
   t=0                      t=280      t=300
   │                          │          │
   ├─ session A ──────────────┼──────────┤            (server kills A at 300)
   │                          ├─ session B ─────────────────────────►
   │                          │  connect, wait for keyframe
   │                          │          │
   ├─ ffmpeg ────────────────────────────────────────────────────────►  (never restarts)
   │                          │          │
   └─ RTSP out ──────────────────────────────────────────────────────►  (never drops)
                              └── switchover happens here, once B has a keyframe
```

Concretely, in `lostblink/stream/publisher.py`:

1. At T+280 request session B and bring its tunnel up in parallel with A.
2. Feed B's bytes into a holding buffer; **do not** write them downstream yet.
3. Scan B for an MPEG-TS packet carrying an H.264 IDR — splicing mid-GOP produces visible corruption.
4. On the first IDR: stop reading A, write B's buffered bytes from that IDR onward, close A.
5. ffmpeg sees one continuous MPEG-TS byte stream with a discontinuity it can absorb.

The TS continuity counters and PCR **will** jump at the splice. ffmpeg is told to expect it:

```
-fflags +genpts+igndts+discardcorrupt   # rebuild timing, tolerate the seam
-copyts -start_at_zero                  # keep a monotonic output timeline
```

If B is not healthy by the time A dies, we fail over to the still-frame loop rather than dropping
the RTSP session. **The output stream is the invariant.** Everything else is best-effort.

## Battery — the constraint that actually governs design

This matters more than any of the above.

Blink cameras are battery powered (2× AA lithium, rated ~2 years). Radio-on time is what drains
them, and a live session is continuous radio-on. Community measurements put continuous live view at
roughly **1–2 % of battery per minute** on XT2-class hardware.

That means:

| Mode | Practical battery life |
| --- | --- |
| Normal motion-only use | ~1–2 years |
| Live view on every motion event (busy driveway) | weeks |
| Continuous 24/7 live view | **1–3 days** |

`lostblink` therefore ships with live view **off by default**, and offers three modes:

| Mode | Behaviour | Use for |
| --- | --- | --- |
| `off` | Clip-and-still bridge only (upstream behaviour, fixed) | Battery cameras you care about |
| `on_motion` | Open a live session when motion fires; one session, then back to stills | The useful middle |
| `always` | Continuously renew | **Mains-powered only** — wired Floodlight, Mini, Doorbell on a transformer |

Plus hard safety rails, all configurable:

- `max_sessions_per_hour` (default 6) — a per-camera budget; exceeded → refuse and log.
- `min_seconds_between_sessions` (default 60) — debounces a camera that is retriggering constantly.
- `daily_live_seconds_budget` (default 1800) — total live time per camera per day.
- Battery state is read from the homescreen; below the configured floor, live view is **refused**
  regardless of mode, and the reason is logged once per hour rather than every attempt.

`always` mode on a battery camera prints a startup warning naming the camera. It is not blocked —
it is your camera — but you will not enable it by accident.

## Errors and backoff

| Condition | Handling |
| --- | --- |
| `409` / body matches `busy` | Sync Module is running another command. Exponential backoff 1 s → 10 s, cap at `timeout`. |
| `429` | Hard backoff, 60 s minimum, and pause *all* live requests account-wide. |
| TLS closes immediately after the header | Treat the URL as dead. Re-request once; if it happens twice, back off 5 minutes — this usually means the camera is unreachable. |
| No bytes within 10 s of connecting | Same as above. |
| Auth failure mid-session | Stop all live activity, surface `AUTH_REQUIRED`. Never retry auth in a loop. |

Backoff is per-camera, and a camera in backoff keeps publishing its still-frame loop. A camera that
cannot do live view is still a working camera.
