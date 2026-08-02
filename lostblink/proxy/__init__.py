"""Live-view transports.

One entry point: hand :func:`open_tunnel` whatever URL the liveview API returned
and get back a connected :class:`~lostblink.proxy.base.LiveTunnel` emitting
MPEG-TS. The caller never needs to know which protocol it got.
"""

from __future__ import annotations

import logging

from .base import FIRST_BYTE_TIMEOUT, LiveTunnel, TunnelError
from .immi import ImmiTunnel, ImmiUrl, build_connection_header, is_immi_url
from .rtsp import RtspTunnel, RtspUrl, is_rtsp_url

__all__ = [
    "FIRST_BYTE_TIMEOUT",
    "ImmiTunnel",
    "ImmiUrl",
    "LiveTunnel",
    "RtspTunnel",
    "RtspUrl",
    "TunnelError",
    "build_connection_header",
    "is_immi_url",
    "is_rtsp_url",
    "make_tunnel",
    "open_tunnel",
]

log = logging.getLogger(__name__)


def make_tunnel(url: str) -> LiveTunnel:
    """Build the right tunnel for ``url``, without connecting.

    Dispatch is on the **URL scheme**, never on the camera model. The model-to-
    protocol mapping is not reliable -- firmware revisions move cameras between
    families -- but the scheme Blink hands back always tells the truth.
    """
    if is_immi_url(url):
        return ImmiTunnel.from_url(url)
    if is_rtsp_url(url):
        return RtspTunnel.from_url(url)
    scheme = url.split("://", 1)[0] if "://" in url else url[:16]
    raise TunnelError(f"unsupported live-view scheme: {scheme!r}")


async def open_tunnel(url: str) -> LiveTunnel:
    """Build and connect a tunnel. Returns once video is actually flowing.

    Raises :class:`TunnelError` if the session is refused, times out, or the
    scheme is one we do not speak.
    """
    tunnel = make_tunnel(url)
    await tunnel.open()
    return tunnel
