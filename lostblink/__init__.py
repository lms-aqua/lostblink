"""lostblink -- live RTSP streams from Blink cameras.

Two things Blink does not give you, in one place:

* **Live video.** Both of Blink's proprietary live-view transports (``immis://``
  and its non-standard ``rtsps://``) implemented in :mod:`lostblink.proxy`.
* **An RTSP endpoint** any NVR can consume, published to MediaMTX, that never
  drops -- live when a session is up, a looped still frame when it is not.

See ``docs/`` for the protocol write-ups.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
