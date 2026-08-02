# The RTSPS path (`rtsps://`) — and why ffmpeg alone cannot do it

> Derived from [`BitWise-0x/homebridge-blink-security`](https://github.com/BitWise-0x/homebridge-blink-security)
> `src/lib/proxy.ts` (GPL-3.0). Ported in `lostblink/proxy/rtsp.py`.

Used by **XT and XT2** cameras. Superficially this looks like the easy case — it says RTSP right
there in the URL — and that is exactly why so many attempts have failed at it.

## URL format

```
rtsps://lv3-app-u002.immedia-semi.com:443/<opaque-path>?client_id=247&blinkRTSP=true
```

Port is 443 and the transport is TLS. `rtsps` is RTSP-over-TLS, not a typo for `rtsp`.

## Why `ffmpeg -i rtsps://...` fails

This is the crux, and it explains a decade of `SIGILL` and "ffmpeg won't accept the MPEG-TS
fragments" reports ([homebridge-blink-for-home#20](https://github.com/colinbendell/homebridge-blink-for-home/issues/20),
[frigate#8935](https://github.com/blakeblackshear/frigate/discussions/8935)).

Two independent problems, and you must fix both:

### 1. Blink auto-plays after SETUP

A standards-compliant RTSP server sends nothing until it receives `PLAY`. Blink's server starts
pushing interleaved RTP **as soon as `SETUP` completes**, before the client has sent `PLAY`.

ffmpeg's RTSP state machine discards every RTP packet that arrives before it transitions to
`RTSP_STATE_STREAMING`. Since the initial burst — **containing the IDR keyframe** — arrives during
that window, ffmpeg throws it away. What is left is a stream of P-frames referencing a keyframe that
never arrived. Decoders respond to that with anything from grey mush to a hard abort.

You cannot fix this with ffmpeg flags. The discard is unconditional and happens before any
demuxer-level option applies.

### 2. The payload is MPEG-TS, not H.264

The SDP is explicit about it:

```
m=video 0 RTP/AVP 33
a=rtpmap:33 MP2T/90000
```

Payload type 33 is `MP2T` — **MPEG-2 Transport Stream over RTP**. So the layering is:

```
TLS → RTSP interleaved framing → RTP → MPEG-TS → H.264 + AAC
```

Most integrations assume `RTP → H.264` and try to reassemble NAL units per RFC 6184. That produces
garbage, because the RTP payload is a TS packet run, not a fragmentation unit.

## The fix: speak RTSP yourself, hand ffmpeg raw TS

Do the negotiation in your own code, strip two layers of framing, and give ffmpeg a plain
`-f mpegts` byte stream on a local socket. ffmpeg's RTSP demuxer is bypassed entirely, so neither
problem can bite.

### Negotiation sequence

All over one TLS socket to `HOST:443`, certificate verification **disabled** (same as IMMI — Blink's
live-view certs do not validate).

```
OPTIONS  rtsp://HOST/<path>  RTSP/1.0     CSeq: 1      (non-fatal, ignore failures)
DESCRIBE rtsp://HOST/<path>  RTSP/1.0     CSeq: 2      Accept: application/sdp
SETUP    <track-url>         RTSP/1.0     CSeq: 3      Transport: RTP/AVP/TCP;unicast;interleaved=0-1
PLAY     rtsp://HOST/<path>  RTSP/1.0     CSeq: 4      Range: npt=0.000-
```

Details that matter:

- **`User-Agent: Immedia WalnutPlayer`.** The reference client sends this. Use it.
- Note the URI scheme in the request line is `rtsp://`, not `rtsps://`, even though the socket is
  TLS. That is normal RTSP-over-TLS behaviour.
- **`<track-url>`** comes from the SDP: find the `m=video` section, take its `a=control:` value. If
  it starts with `rtsp://` use it as-is; otherwise append it to the base URI. Guessing `/trackID=0`
  works on some units and 454s on others.
- **Send `PLAY` even though the server already started.** Some units do not auto-play, and it is
  harmless on the ones that do.
- **Interleaved TCP transport is mandatory.** You are inside a TLS tunnel; UDP is not an option.

### Parsing responses while RTP is already flowing

Because the server auto-plays, interleaved binary frames arrive *interleaved with the RTSP text
responses you are waiting for*. Your response parser must skip them:

> While the buffer starts with `0x24` (`$`), read the 4-byte interleaved header, skip
> `4 + length` bytes, and repeat — **then** look for the RTSP status line.

Miss this and `DESCRIBE` appears to hang, because you are trying to parse binary RTP as ASCII
headers. This is the second-most-common failure after the keyframe discard.

Then: read headers to `\r\n\r\n`, take `Content-Length`, wait for that many body bytes, and keep the
remainder in the buffer — the tail after a response frequently already contains video.

### Interleaved frame de-framing

```
┌──────┬─────────┬──────────────┬────────────────┐
│ '$'  │ channel │ length (BE16)│ RTP packet     │
│ 0x24 │ 1 byte  │ 2 bytes      │ `length` bytes │
└──────┴─────────┴──────────────┴────────────────┘
```

Channel `0` is video RTP (as requested by `interleaved=0-1`); channel `1` is RTCP — drop it.

If the buffer does not start with `0x24`, scan forward to the next `0x24` and resynchronise. Do not
abort: a mid-stream desync is recoverable.

### RTP header stripping

For each channel-0 packet, compute the header length properly — fixed 12 bytes is wrong when CSRCs
or extensions are present:

```
header_len = 12 + (byte0 & 0x0F) * 4              # CSRC count
if byte0 & 0x10:                                   # X — extension present
    ext_words  = u16be(header_len + 2)
    header_len += 4 + ext_words * 4
```

Then handle padding: if `byte0 & 0x20` is set, the final byte of the packet is the padding length —
subtract it from the end. Guard against a padding length that would underrun the header.

What remains between `header_len` and `end` is **MPEG-TS**. Write it straight through, in order, no
buffering, no reordering.

> RTP sequence numbers are present but the reference implementation does not reorder on them. Over
> TCP-interleaved transport packets cannot arrive out of order, so ordering is guaranteed by the
> transport. Ignoring them is correct here.

### Handing off to ffmpeg

Serve the resulting byte stream on a local TCP port (or a pipe) and run:

```
ffmpeg -f mpegts -i tcp://127.0.0.1:<port> ...
```

Buffer anything that arrives before ffmpeg connects and flush it on connect, or you drop the
keyframe again at the last hurdle — having gone to all this trouble specifically to keep it.

`lostblink` uses a pipe rather than a socket for the IMMI path and a socket for RTSP, matching the
reference; both are implemented behind one interface in `lostblink/proxy/base.py`.

## Comparison to the IMMI path

| | RTSPS (XT/XT2) | IMMI (Mini/Doorbell/Indoor/Outdoor) |
| --- | --- | --- |
| Handshake | RTSP text: OPTIONS/DESCRIBE/SETUP/PLAY | One 122-byte binary blob |
| Framing | `$`+channel+len → RTP → TS | 9-byte type/seq/len → TS |
| Keepalive | RTSP session timeout; none needed in practice | Mandatory 0x0A @10s + 0x12 @1s |
| Auth | In the opaque URL path | In the opaque `connection_id` |
| Payload | MPEG-TS | MPEG-TS |
| TLS cert valid | No | No |

Both converge on MPEG-TS, which is why `lostblink` has exactly one downstream pipeline for both.
