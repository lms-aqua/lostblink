# Attribution and licensing

`lostblink` is licensed **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)). That is a requirement
inherited from the work it derives from, not a preference — the reasoning is below.

## Derived code

### BitWise-0x/homebridge-blink-security — GPL-3.0

- **Source:** https://github.com/BitWise-0x/homebridge-blink-security
- **File:** `src/lib/proxy.ts` (classes `ImmiTunnel`, `ImmiFrameStripper`, `RtspToH264Proxy`)
- **Used in:** [`lostblink/proxy/immi.py`](lostblink/proxy/immi.py),
  [`lostblink/proxy/rtsp.py`](lostblink/proxy/rtsp.py)
- **Nature:** direct port of the protocol logic from TypeScript to Python. The 122-byte IMMI
  connection header layout, the 9-byte frame format and message types, both keepalive packet
  constants, the RTSP negotiation sequence, and the interleaved-RTP de-framing are all taken from
  that implementation.
- **Also informed:** the protocol write-ups in [`docs/protocol/`](docs/protocol/), and the finding
  that the liveview command must not be polled to completion (`src/devices/index.ts:1111`).

**A Python port of GPL-3.0 code is a derivative work.** `lostblink` is therefore GPL-3.0-or-later,
and anyone distributing it or a modified version must do the same and make source available.

This project would not exist without that repository. It is the only public implementation that
actually decodes Blink's live-view transports, and its source comments — particularly the ones about
ffmpeg discarding pre-`PLAY` RTP and about not filtering on the `0x47` sync byte — are the real
documentation for this protocol.

## Dependencies

### fronzbot/blinkpy — MIT

- **Source:** https://github.com/fronzbot/blinkpy
- **Used as:** a runtime dependency for authentication, device discovery, the homescreen/media
  feeds, and clip download.
- No blinkpy code is copied. MIT is compatible with GPL-3.0.

## Referenced, not copied

### roger-/blinkbridge — **no license (all rights reserved)**

- **Source:** https://github.com/roger-/blinkbridge
- **Relationship:** conceptual ancestor. The idea of publishing a looped still frame to MediaMTX so
  downstream NVRs never see the stream drop comes from here, and
  [`docs/upstream-bug-audit.md`](docs/upstream-bug-audit.md) analyses its source in detail.
- **No code from blinkbridge is present in this repository.** Every module was written from scratch
  against a different architecture (asyncio throughout, live view as a first-class path, dataclass
  config, atomic file handling).
- Because it carries no license file it is all-rights-reserved: it may be read and learned from, but
  not redistributed or forked into a derivative. The bug audit quotes short identifiers and line
  references as commentary and criticism.

### MattTW/BlinkMonitorProtocol — **no license (all rights reserved)**

- **Source:** https://github.com/MattTW/BlinkMonitorProtocol
- **Relationship:** the reference for Blink's REST surface — endpoint paths, the async command
  model, and the live-view session timing fields. Cited and linked throughout
  [`docs/protocol/01-blink-rest-api.md`](docs/protocol/01-blink-rest-api.md).
- No text is reproduced from it.

### Also studied

Each documented in [`docs/prior-art/`](docs/prior-art/README.md); no code taken from any of them.

| Project | License | What it contributed here |
| --- | --- | --- |
| [colinbendell/homebridge-blink-for-home](https://github.com/colinbendell/homebridge-blink-for-home) | MIT (archived) | Issue #20 documents the ffmpeg `SIGILL` failure mode that started this |
| [adrian-dobre/BlinkWebUI](https://github.com/adrian-dobre/BlinkWebUI) | GPL-3.0 | The clearest public statement of the `immis://` wall |
| [OVR92/BlinkPi](https://github.com/OVR92/BlinkPi) | MIT | The three-stage clip validation ladder (size → stability → ffprobe) |
| [renanfernandes/watchman](https://github.com/renanfernandes/watchman) | none | USB-interception approach as a subscription-free alternative |

## Trademarks

Blink and the Blink logo are trademarks of Amazon.com, Inc. or its affiliates. Immedia
Semiconductor is an Amazon subsidiary. This project is not affiliated with, endorsed by, or
supported by any of them. Names are used only to identify the hardware and services this software
interoperates with.
