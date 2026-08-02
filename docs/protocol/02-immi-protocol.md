# The IMMI protocol (`immis://`) — byte-level specification

> **This is the document that did not exist.** Every public thread about `immis://` ends with
> "proprietary, no info found" — [blinkpy#343](https://github.com/fronzbot/blinkpy/issues/343),
> [bling-desktop#26](https://github.com/lurume84/bling-desktop/issues/26),
> [BlinkWebUI's README](https://github.com/adrian-dobre/BlinkWebUI) ("*it seems that it uses a
> proprietary protocol (immis)*"), [BlinkMonitorProtocol#12](https://github.com/MattTW/BlinkMonitorProtocol/issues/12).
>
> The framing below is derived from the working GPL-3.0 implementation in
> [`BitWise-0x/homebridge-blink-security`](https://github.com/BitWise-0x/homebridge-blink-security),
> `src/lib/proxy.ts`. `lostblink/proxy/immi.py` is a Python port of that logic and is therefore
> **GPL-3.0**. See `NOTICE.md`.

"IMMI" is Immedia — Immedia Semiconductor, the company Amazon bought that became Blink. The scheme
is `immis` = IMMI-over-TLS. There is no plaintext `immi://` in the wild.

## Which cameras speak it

| Family | Device type | Live view protocol |
| --- | --- | --- |
| Blink Mini, Mini 2, Mini 2K+ | `owl` | **IMMI** |
| Blink Indoor / Outdoor (gen 3/4) | `camera` | **IMMI** |
| Video Doorbell | `lotus` | **IMMI** |
| Floodlight (wired) | `camera` | **IMMI** |
| XT, XT2 | `camera` | RTSPS — see `03-rtsp-mpegts.md` |

You cannot tell from the device type alone. **Branch on the URL scheme you get back**, never on the
model string. That is what `lostblink` does.

## URL format

```
immis://HOST:443/CONNECTION_ID__IMDS_SERIAL?client_id=CAMERA_ID
        └─┬──┘      └─────┬────┘  └──┬───┘             └───┬───┘
          │               │          │                     │
          │               │          │                     └── uint32, goes in the header
          │               │          └── Sync Module serial, first 16 bytes
          │               └── opaque session id, first 16 bytes
          └── e.g. lv3-app-u002.immedia-semi.com
```

Parsing rules, exactly as the working client does them:

- Strip the leading `/` and everything from `?` onward → `CONNECTION_ID__IMDS_SERIAL`.
- `connection_id` = everything **before** the first `__`.
- `serial` = the capture of `__IMDS_(.+)$`, truncated to 16 bytes. Absent → empty string (works).
- `client_id` = the `client_id` query parameter, parsed as an integer. Absent → `0`.

Both `connection_id` and `serial` are **truncated, not validated**. Longer values are silently cut.

## Transport

Plain TLS to `HOST:443`.

- The certificate **does not validate** against the hostname. You must disable verification
  (`rejectUnauthorized: false` in the reference; `ssl.CERT_NONE` + `check_hostname=False` in ours).
  This is Blink's problem, not ours, and it is why `lostblink` pins the connection to the exact host
  the API handed back and nothing else.
- Send SNI only when the target is a hostname, not a bare IP.
- No HTTP, no WebSocket, no upgrade handshake. Raw bytes immediately after the TLS handshake.

## Connection header — 122 bytes, big-endian

Sent by the client as the very first payload. Zero-filled, fixed size, TLV-ish but with hardcoded
lengths.

| Offset | Size | Field | Value |
| ---: | ---: | --- | --- |
| 0 | 4 | Magic | `0x00000028` (40) |
| 4 | 4 | Serial length | `16` |
| 8 | 16 | Serial | UTF-8, zero-padded. Empty is accepted. |
| 24 | 4 | Client ID | `client_id` from the URL, uint32 |
| 28 | 1 | Const | `0x01` |
| 29 | 1 | Const | `0x08` |
| 30 | 4 | Token length | `64` |
| 34 | 64 | Token | **all zeros** — no auth token required here |
| 98 | 4 | Connection ID length | `16` |
| 102 | 16 | Connection ID | UTF-8, zero-padded |
| 118 | 4 | Trailer | `0x00000001` |

Total: 122 bytes.

The striking part: **the 64-byte token field is empty**. Authorisation is carried entirely by the
opaque `connection_id`, which the REST API just minted for you and which is short-lived. Possession
of the URL is possession of the stream. Do not log liveview URLs at INFO level — `lostblink`
redacts them.

There is no server acknowledgement of the header. If it is malformed the server simply closes the
TLS socket, usually within a second and with no error. A silent close right after connect almost
always means a bad `connection_id` (expired, or truncated wrong).

## Frame format — 9-byte header

Everything after the connection header, in **both** directions, is framed:

```
┌────────┬──────────────────┬──────────────────┬───────────────┐
│ type   │ sequence         │ payload length   │ payload       │
│ 1 byte │ 4 bytes, BE u32  │ 4 bytes, BE u32  │ N bytes       │
└────────┴──────────────────┴──────────────────┴───────────────┘
  off 0     off 1..4           off 5..8           off 9..9+N
```

### Message types

| Type | Name | Direction | Notes |
| ---: | --- | --- | --- |
| `0x00` | VIDEO | server → client | Payload is **MPEG-TS**. This is the stream. |
| `0x0A` | KEEPALIVE | client → server | 9-byte header, zero length, zero payload. Every ~10s. |
| `0x12` | LATENCY_STATS | client → server | 33 bytes total. Every ~1s. |

Other types appear occasionally; the reference implementation logs them and drops them. So do we.

### Reassembly is not optional

`payload length` regularly exceeds one TLS record, so a VIDEO frame arrives split across several
reads. You must carry `payload_remaining` across chunks and keep forwarding while it is non-zero.
Treating each read as a frame produces a stream that *almost* decodes and then desyncs.

### Do not filter on the MPEG-TS sync byte

The tempting optimisation is to only forward payloads starting with `0x47`. **This is wrong**, and
the reference code carries a comment about having been bitten by it: IMMI frame boundaries do not
align with 188-byte TS packet boundaries. Filtering on `0x47` drops the PAT/PMT tables, and without
those ffmpeg never discovers the audio PID — you get silent video and a stream that takes forever to
start. Forward **every byte of every VIDEO frame**, in order, and let the TS demuxer resynchronise.

## Keepalives

Two independent timers, both client → server, both required:

```
LATENCY_STATS, every 1000 ms — 33 bytes:
12 00 00 03 e8 00 00 00 18 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00
00

   type=0x12, seq=0x000003e8 (1000), len=0x00000018 (24), then 24 bytes
   of stats with a single 0x01 at payload offset 21.

KEEPALIVE, every 10000 ms — 9 bytes, all zero except:
0a 00 00 00 00 00 00 00 00
```

The 33-byte latency packet is sent verbatim as a constant. Its fields are a client-side latency
report; the server does not appear to act on the contents, only on the arrival. Sending a static
buffer is what the working client does and it is sufficient.

Stop both timers the instant the socket closes, or you will spin a dead interval forever. This is a
real leak in naive ports.

## Payload: what you actually get

MPEG-TS containing:

- **H.264** video — typically 1280×720, ~15 fps, variable
- **AAC** audio — often present, sometimes absent entirely

Because it is TS and not raw H.264, you feed ffmpeg `-f mpegts -i -`. **Do not** try
`-f h264`. And because the first bytes you receive are mid-GOP, expect ffmpeg to emit a few
"non-existing PPS" / "no frame" warnings before the first keyframe lands. That is normal and is not
an error condition — `lostblink` suppresses them below the warning threshold for the first two
seconds.

## Failure modes, and what they mean

| Symptom | Cause |
| --- | --- |
| TLS closes ~1s after the header, no data | Bad/expired `connection_id`, or wrong `client_id` |
| TLS connects, zero bytes ever | Camera offline or Sync Module busy with another command |
| Video starts then dies at ~30s | Keepalives not being sent (or stopped by an exception) |
| Video dies at exactly ~300s | Working as designed — the session cap. Renew. |
| Green/blocky video, ffmpeg desync errors | Frame reassembly bug — you split a VIDEO payload |
| Video plays, no audio ever | You filtered on `0x47` and lost the PAT/PMT |

## Legal / ethical note

This documents a private protocol between a paying customer's own client and the service they are
already authorised to use, reached with their own credentials. It enables no access that the Blink
app does not already grant. It is not a bypass of authentication, licensing, or payment, and it
gives you nothing for cameras you do not own. Amazon's ToS still governs your account; running this
may violate it, and Amazon may change the protocol at any time.
