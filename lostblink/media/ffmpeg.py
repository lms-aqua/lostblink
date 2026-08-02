"""Async ffmpeg/ffprobe wrappers.

Everything upstream does with ``subprocess.Popen`` plus a background thread is
done here with ``asyncio.create_subprocess_exec``. That is not stylistic: the
thread version swallows every exception (bug B-04), so a failure surfaces three
layers away as a permanently disabled camera.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: ffprobe reports a channel count; ffmpeg's anullsrc wants a layout name.
#: Passing the count works on new ffmpeg and errors on older builds (bug B-09).
_CHANNEL_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


class FFmpegError(RuntimeError):
    """An ffmpeg or ffprobe invocation failed."""

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr.strip()
        tail = self.stderr.splitlines()[-3:] if self.stderr else ["(no output)"]
        super().__init__(f"{command} exited {returncode}: {' | '.join(tail)}")


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """What ffprobe could tell us. Every field is optional on purpose.

    Upstream indexes probe output directly and dies on a missing key -- most
    often ``bit_rate``, which ffprobe simply omits for a lot of H.264-in-MP4
    (bug B-08). Blink clips are also frequently video-only, which upstream
    turns into a fatal assertion (bug B-07).
    """

    width: int = 1280
    height: int = 720
    fps: str = "15/1"
    pix_fmt: str = "yuv420p"
    profile: str | None = None
    level: str | None = None
    bit_rate: str | None = None
    time_base_den: str = "90000"
    has_audio: bool = False
    audio_sample_rate: str = "16000"
    audio_channels: int = 1

    @property
    def audio_layout(self) -> str:
        return _CHANNEL_LAYOUTS.get(self.audio_channels, "mono")


class FFmpeg:
    """Thin async wrapper around the ffmpeg toolchain."""

    def __init__(
        self,
        binary: str = "ffmpeg",
        probe_binary: str = "ffprobe",
        *,
        loglevel: str = "error",
        hwaccel: str = "",
        hwaccel_device: str = "",
    ) -> None:
        self.binary = binary
        self.probe_binary = probe_binary
        self.loglevel = loglevel
        self.hwaccel = hwaccel
        self.hwaccel_device = hwaccel_device

    def check_available(self) -> None:
        """Fail at startup with a clear message rather than mid-stream."""
        for name in (self.binary, self.probe_binary):
            if shutil.which(name) is None:
                raise FFmpegError(name, 127, f"{name} not found on PATH")

    @property
    def common_args(self) -> list[str]:
        return ["-hide_banner", "-loglevel", self.loglevel, "-y"]

    @property
    def hwaccel_args(self) -> list[str]:
        """Decoder hardware acceleration, if configured.

        On upstream's TODO list. Only ever applied to *decode*; the publish path
        is a stream copy, so there is nothing to accelerate there.
        """
        if not self.hwaccel:
            return []
        args = ["-hwaccel", self.hwaccel]
        if self.hwaccel_device:
            args += ["-hwaccel_device", self.hwaccel_device]
        return args

    # -- probing -----------------------------------------------------------

    async def probe(self, source: Path | str) -> StreamInfo:
        """Read stream parameters, defaulting anything ffprobe omits."""
        args = [
            self.probe_binary,
            "-hide_banner",
            "-loglevel", "error",
            "-show_streams",
            "-print_format", "json",
            str(source),
        ]
        stdout, stderr, code = await _run(args)
        if code != 0:
            raise FFmpegError("ffprobe", code, stderr)

        try:
            streams = json.loads(stdout).get("streams", [])
        except json.JSONDecodeError as exc:
            raise FFmpegError("ffprobe", 0, f"unparseable output: {exc}") from exc

        video = _first(streams, "video")
        audio = _first(streams, "audio")
        if video is None:
            raise FFmpegError("ffprobe", 0, f"{source} has no video stream")

        return StreamInfo(
            width=int(video.get("width") or 1280),
            height=int(video.get("height") or 720),
            fps=str(video.get("r_frame_rate") or "15/1"),
            pix_fmt=str(video.get("pix_fmt") or "yuv420p"),
            profile=_maybe_str(video.get("profile")),
            level=_maybe_str(video.get("level")),
            bit_rate=_maybe_str(video.get("bit_rate")),
            time_base_den=_denominator(video.get("time_base")),
            has_audio=audio is not None,
            audio_sample_rate=str((audio or {}).get("sample_rate") or "16000"),
            audio_channels=int((audio or {}).get("channels") or 1),
        )

    # -- still-frame generation -------------------------------------------

    async def make_still_video(
        self,
        source: Path,
        destination: Path,
        *,
        duration: float = 0.5,
        info: StreamInfo | None = None,
    ) -> Path:
        """Freeze a clip's last frame into a short loopable video.

        This is upstream's good idea, kept: looping a still keeps the RTSP
        session continuously valid so downstream NVRs never see it drop.

        Unlike upstream, the intermediate JPEG lives in a per-call temp
        directory. Upstream writes every camera's frame to one shared
        ``last_frame.jpg`` from concurrent threads, so cameras overwrite each
        other's images and race on the unlink (bug B-06).
        """
        info = info or await self.probe(source)

        temp_dir = destination.parent / f".{destination.stem}.tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        frame = temp_dir / "frame.jpg"
        staging = temp_dir / destination.name

        try:
            await self._extract_last_frame(source, frame)
            await self._frame_to_video(frame, staging, info, duration)
            # Atomic publish, so nothing ever reads a half-written file.
            staging.replace(destination)
        finally:
            _rmtree(temp_dir)

        return destination

    async def _extract_last_frame(self, source: Path, out: Path) -> None:
        args = [
            self.binary,
            *self.common_args,
            *self.hwaccel_args,
            "-sseof", "-1.0",
            "-i", str(source),
            "-update", "1",
            "-pix_fmt", "yuv420p",
            # Blink clips are limited-range; without this the frozen frame is
            # visibly darker than the live clip it followed.
            "-vf", "scale=out_range=pc",
            "-q:v", "1",
            str(out),
        ]
        _, stderr, code = await _run(args)
        if code != 0 or not out.exists():
            raise FFmpegError("ffmpeg (last frame)", code, stderr)

    async def _frame_to_video(
        self, frame: Path, out: Path, info: StreamInfo, duration: float
    ) -> None:
        args = [self.binary, *self.common_args, "-loop", "1", "-i", str(frame)]

        # Only mux silence if the source actually had audio. Upstream asserts
        # both tracks exist and kills the pipeline on video-only clips (B-07).
        if info.has_audio:
            args += [
                "-f", "lavfi",
                "-i", f"anullsrc=channel_layout={info.audio_layout}"
                      f":sample_rate={info.audio_sample_rate}",
            ]

        args += [
            "-c:v", "libx264",
            "-pix_fmt", info.pix_fmt,
            "-t", f"{duration:g}",
            "-vf", f"scale={info.width}:{info.height},fps={info.fps}",
        ]

        # Every one of these is optional in ffprobe output.
        if info.bit_rate:
            args += ["-b:v", info.bit_rate]
        else:
            args += ["-crf", "23"]
        if info.profile:
            args += ["-profile:v", info.profile.lower().replace(" ", "")]

        args += [
            "-movflags", "faststart",
            "-video_track_timescale", info.time_base_den,
            "-fps_mode", "passthrough",
        ]
        if info.has_audio:
            args += [
                "-c:a", "aac",
                "-ar", info.audio_sample_rate,
                "-ac", str(info.audio_channels),
                "-shortest",
            ]
        args.append(str(out))

        _, stderr, code = await _run(args)
        if code != 0 or not out.exists():
            raise FFmpegError("ffmpeg (still video)", code, stderr)

    # -- publishing --------------------------------------------------------

    def live_publish_args(self, rtsp_url: str, *, transport: str = "tcp") -> list[str]:
        """ffmpeg argv for republishing a live MPEG-TS stream to MediaMTX.

        Input is raw MPEG-TS on stdin, because that is what both Blink
        transports produce once de-framed. The stream is copied, not
        transcoded -- Blink already gives us H.264/AAC.

        The timing flags matter for session handover: at a splice the TS
        continuity counters and PCR jump, and without these ffmpeg treats that
        as corruption and bails. See docs/protocol/04-session-lifecycle.md.
        """
        return [
            self.binary,
            *self.common_args,
            "-fflags", "+genpts+igndts+discardcorrupt",
            "-analyzeduration", "2000000",
            "-probesize", "2000000",
            "-f", "mpegts",
            "-i", "pipe:0",
            "-c", "copy",
            "-copyts",
            "-start_at_zero",
            "-muxdelay", "0",
            "-f", "rtsp",
            "-rtsp_transport", transport,
            rtsp_url,
        ]

    def still_loop_args(self, concat_file: Path, rtsp_url: str,
                        *, transport: str = "tcp") -> list[str]:
        """ffmpeg argv for the looped still-frame fallback stream."""
        return [
            self.binary,
            *self.common_args,
            "-fflags", "+igndts+genpts",
            "-re",
            "-stream_loop", "-1",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-flush_packets", "0",
            "-c", "copy",
            "-fps_mode", "drop",
            "-f", "rtsp",
            "-rtsp_transport", transport,
            rtsp_url,
        ]


# -- module helpers --------------------------------------------------------


async def _run(args: Sequence[str]) -> tuple[str, str, int]:
    log.debug("exec: %s", " ".join(args))
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
        process.returncode or 0,
    )


def _first(streams: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """Find a stream by ``codec_type``.

    Upstream matches on codec *name* (`aac`, `h264`), so an Opus or HEVC clip
    looks like it has no streams at all.
    """
    return next((s for s in streams if s.get("codec_type") == kind), None)


def _maybe_str(value: Any) -> str | None:
    return None if value in (None, "", "N/A") else str(value)


def _denominator(time_base: Any) -> str:
    if isinstance(time_base, str) and "/" in time_base:
        return time_base.split("/", 1)[1]
    return "90000"


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
