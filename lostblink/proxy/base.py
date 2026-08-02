"""Common plumbing for the two Blink live-view transports.

Both the IMMI and RTSPS paths converge on the same thing: an ordered stream of
MPEG-TS bytes. Everything downstream (ffmpeg, the publisher, the splicer) only
ever sees a ``LiveTunnel``, so it does not care which protocol produced it.

See ``docs/protocol/02-immi-protocol.md`` and ``docs/protocol/03-rtsp-mpegts.md``.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

#: Blink's live-view endpoints present certificates that do not validate against
#: the hostname the API just handed us. This is Blink's problem, not ours, but we
#: still have to connect. We mitigate by only ever pointing this at the exact host
#: returned by an authenticated API call, never at user-supplied input.
_INSECURE_TLS = ssl.create_default_context()
_INSECURE_TLS.check_hostname = False
_INSECURE_TLS.verify_mode = ssl.CERT_NONE

#: How long to wait for the first byte of video before declaring the session dead.
FIRST_BYTE_TIMEOUT = 10.0

#: Size of the internal hand-off buffer, in TS chunks. Bounded so a stalled
#: consumer applies backpressure instead of growing the heap without limit.
_QUEUE_MAXSIZE = 512


class TunnelError(RuntimeError):
    """The live-view transport failed in a way that ends the session."""


class LiveTunnel(abc.ABC):
    """A live MPEG-TS source.

    Subclasses connect to Blink, strip whatever framing their protocol uses, and
    push clean MPEG-TS into ``_queue``. Consumers iterate the tunnel::

        async with ImmiTunnel.from_url(url) as tunnel:
            async for chunk in tunnel:
                writer.write(chunk)
    """

    #: Human-readable protocol name, for logs.
    protocol: str = "unknown"

    def __init__(self, host: str, port: int = 443) -> None:
        self.host = host
        self.port = port
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(_QUEUE_MAXSIZE)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._first_byte = asyncio.Event()
        self.bytes_forwarded = 0

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    async def _handshake(self) -> None:
        """Send whatever the protocol needs before video starts flowing."""

    @abc.abstractmethod
    async def _pump(self) -> None:
        """Read from the socket, de-frame, and ``_emit()`` MPEG-TS until EOF."""

    async def open(self) -> None:
        """Connect, handshake, and start pumping. Returns once video is flowing."""
        log.debug("%s: connecting to %s:%d", self.protocol, self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host,
                self.port,
                ssl=_INSECURE_TLS,
                # Only send SNI for real hostnames; bare IPs must not carry it.
                server_hostname=self.host if not _is_ip(self.host) else None,
            )
        except OSError as exc:
            raise TunnelError(f"{self.protocol}: connect failed: {exc}") from exc

        try:
            await self._handshake()
        except Exception as exc:
            await self.close()
            if isinstance(exc, TunnelError):
                raise
            raise TunnelError(f"{self.protocol}: handshake failed: {exc}") from exc

        self._spawn(self._pump_guarded(), name=f"{self.protocol}-pump")

        # A silent close right after the handshake almost always means the
        # session id was rejected -- see the failure-mode table in
        # docs/protocol/02-immi-protocol.md.
        try:
            await asyncio.wait_for(self._first_byte.wait(), FIRST_BYTE_TIMEOUT)
        except TimeoutError:
            await self.close()
            raise TunnelError(
                f"{self.protocol}: no video within {FIRST_BYTE_TIMEOUT:.0f}s "
                "(expired session id, camera offline, or sync module busy)"
            ) from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass  # already gone; nothing useful to do
            self._writer = None
        self._reader = None

        # Unblock any consumer parked on __anext__.
        _terminate(self._queue)

        log.debug(
            "%s: closed after forwarding %d bytes", self.protocol, self.bytes_forwarded
        )

    async def __aenter__(self) -> LiveTunnel:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- consumption -------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:  # sentinel: tunnel finished
                return
            yield chunk

    @property
    def alive(self) -> bool:
        return not self._closed and self._writer is not None

    # -- helpers for subclasses -------------------------------------------

    def _emit(self, data: bytes) -> None:
        """Forward de-framed MPEG-TS downstream.

        Every byte of every video frame goes through here, in order. Do not
        filter on the 0x47 sync byte -- IMMI frame boundaries do not align with
        188-byte TS packets and filtering drops the PAT/PMT.
        """
        if not data or self._closed:
            return
        self.bytes_forwarded += len(data)
        if not self._first_byte.is_set():
            self._first_byte.set()
            log.info("%s: video flowing from %s", self.protocol, self.host)
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            # Consumer is not keeping up. Dropping the oldest chunk keeps us
            # closest to live, which is the whole point of this code path.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(data)
                log.warning("%s: consumer lagging, dropped a chunk", self.protocol)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def _send(self, data: bytes) -> None:
        """Write to the upstream socket, tolerating a socket that just died."""
        if self._writer is None or self._closed:
            return
        try:
            self._writer.write(data)
        except (OSError, ssl.SSLError) as exc:
            log.debug("%s: send failed: %s", self.protocol, exc)

    def _spawn(self, coro, *, name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def _pump_guarded(self) -> None:
        """Run ``_pump`` and make sure the queue is always terminated."""
        try:
            await self._pump()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s: stream ended: %s", self.protocol, exc)
        finally:
            _terminate(self._queue)

    async def _keepalive(self, payload: bytes, interval: float, label: str) -> None:
        """Send a fixed payload on a fixed cadence until the tunnel closes.

        Both IMMI keepalive timers use this. The loop exits with the tunnel --
        a naive port leaks these and spins forever on a dead socket.
        """
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if self._closed:
                    return
                self._send(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("%s: %s keepalive stopped: %s", self.protocol, label, exc)


def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _terminate(queue: asyncio.Queue[bytes | None]) -> None:
    """Push the end-of-stream sentinel, unblocking any parked consumer.

    A full queue means the consumer is already behind and will drain to the
    sentinel shortly, so dropping it is safe.
    """
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(None)
