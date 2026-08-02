# Upstream bug audit — `roger-/blinkbridge` @ `548341f`

Full read of every source file in [roger-/blinkbridge](https://github.com/roger-/blinkbridge) as of
commit `548341f` (pushed 2026-03-06). 24 findings. Each carries the file:line, the failure it
produces, and how `lostblink` addresses it.

This is not a hit piece — blinkbridge is a genuinely clever idea (loop a still frame so downstream
NVRs never see the stream drop) and it is the reason this project exists. It is a ~600-line
proof-of-concept that got popular, and these are the things that break when it meets real accounts.

Severity: **S1** breaks the process · **S2** breaks a camera or loses footage · **S3** correctness /
efficiency / maintenance.

---

## S1 — crashes

### B-01 · `None` clip path reaches `subprocess.Popen` — *this is issue #1*
`main.py:27` → `blink.py:97-124` → `stream_server.py:117` → `ffmpeg.py:57`

`save_latest_clip()` returns `None` when no clip is found (`blink.py:114`). `start_stream()` passes
that straight into `stream_server.start_server()`, which reaches
`subprocess.Popen(['ffmpeg', ..., None, ...])`:

```
TypeError: expected str, bytes or os.PathLike object, not NoneType
```

Triggered reliably by any camera on **local storage only** — those clips are not in the normal media
list at all (see `docs/protocol/01-blink-rest-api.md`), so the list is always empty. This is
[blinkbridge#1](https://github.com/roger-/blinkbridge/issues/1), still open, and the README ships a
warning instead of a fix.

**lostblink:** `save_latest_clip` returns `Optional[Path]` and every caller must handle `None`; a
camera with no clip enters `NO_MEDIA` state and publishes a generated placeholder card rather than
crashing. Local-storage clips are fetched properly via the manifest flow.

### B-02 · `config.py` only parses on Python 3.12+
`config.py:31`

```python
RTSP_URL = f'rtsp://{CONFIG['rtsp_server']['address']}:{CONFIG['rtsp_server']['port']}'
```

Reusing single quotes inside a single-quoted f-string is PEP 701, new in **3.12**. On 3.11 and
earlier this is a `SyntaxError` at import — the package will not even load. It survives only because
the Dockerfile pulls `python:alpine` (unpinned, currently 3.13). Anyone running from source on an
LTS distro hits a syntax error with no useful message.

**lostblink:** targets 3.11+, CI matrix on 3.11/3.12/3.13, `ruff` configured with `target-version =
"py311"` so this class of thing is caught at lint time.

### B-03 · `is_running()` raises `AttributeError` before the server starts
`stream_server.py:106-107`

`self.process` is only assigned in `_run_server()`, which is the **last** statement of
`start_server()`. If `_make_concat_files()` or `add_video()` raises — and B-06/B-07/B-08 all make
that likely — the object is left without `.process`. The main loop then calls `ss.is_running()` at
`main.py:39` and `main.py:85` and dies with `AttributeError: 'StreamServer' object has no attribute
'process'`, taking the whole process down rather than just that camera.

**lostblink:** `process` is initialised to `None` in `__init__`; `is_running()` is total.

### B-04 · unhandled exception in the still-video thread is invisible, then fatal
`ffmpeg.py:113-137`

`StillVideoCreator` runs `_run()` on a bare `threading.Thread`. Any exception — the `assert` at
`:126`, the `KeyError` at B-08, a `ffmpeg` failure — prints a traceback to stderr and kills the
thread. `wait()` is `thread.join()`, which **returns normally** regardless. The caller then executes
`self._enqueue_clip(next_still_video)` at `stream_server.py:97` pointing at a file that was never
created. ffmpeg's concat demuxer hits a missing file and exits; the stream dies; the supervisor
restarts it into the same failure until `max_failures` disables the camera permanently.

A silent failure that manifests three layers away as a permanently disabled camera.

**lostblink:** all subprocess work is `asyncio`, exceptions propagate to the caller, and a failed
still-frame generation is a handled state transition, not a lost exception.

---

## S2 — lost footage / broken cameras

### B-05 · motion is missed whenever `motion_detected` clears between polls
`blink.py:141`

```python
if not camera.attributes['motion_detected'] or self.camera_last_record[...] == ...['last_record']:
    return None
```

Requires `motion_detected` to be **true at the instant we poll**. Blink clears that flag once the
event ends. A clip recorded and finished inside one poll interval leaves `motion_detected == False`
with a **new `last_record`** — and gets skipped entirely. The clip is never downloaded and the
stream keeps showing the previous still.

The `last_record` change is the reliable signal on its own; the `motion_detected` conjunct only
subtracts.

**lostblink:** keys purely on `last_record` / media-id advancing, with `motion_detected` used as a
hint for when to open live view, never as a gate on downloading.

### B-06 · all cameras race on one shared `last_frame.jpg`
`ffmpeg.py:121`

```python
still_image_file_name = PATH_VIDEOS / 'last_frame.jpg'
```

A **module-level constant path**, written by `VideoToLastFrame`, read by `FrameToVideo`, then
`unlink()`ed at `:134` — inside a per-camera background thread. With two or more cameras, threads
overlap: camera A writes the jpg, camera B overwrites it, camera A encodes B's frame into A's
stream, whichever finishes first unlinks it and the other gets
`FileNotFoundError` (which is then swallowed by B-04).

Symptoms in the wild: cameras showing each other's images, and intermittent unexplained stream
death. Gets worse with more cameras, which is why it is under-reported.

**lostblink:** every intermediate artefact is in a per-camera, per-generation temp directory with a
unique name; nothing is shared.

### B-07 · `assert` kills the pipeline on any clip without audio
`ffmpeg.py:126`

```python
assert all((params_audio, params_video))
```

`StreamParameters.wait()` returns `{}` for a missing track (`ffmpeg.py:36-38`). Blink clips are
frequently **video-only** — muted cameras, some Mini firmware, most local-storage clips. `{}` is
falsy, the assertion fails, and B-04 turns it into a dead camera.

Also: `assert` is stripped entirely under `python -O`, which would convert this into a `KeyError`
further down instead. Never use `assert` for runtime validation.

**lostblink:** audio is optional throughout. No audio track → generate silent video, or mux
`anullsrc` only when the source genuinely had audio.

### B-08 · `KeyError: 'bit_rate'` on H.264 in MP4
`ffmpeg.py:88`

```python
'-b:v', params_video['bit_rate'],
```

`ffprobe -show_streams` **omits** `bit_rate` for many H.264-in-MP4 streams — it is not a required
field and Blink's muxer often does not write it. Straight `KeyError`, swallowed by B-04.

`profile` (`:89`) and `level` (`:90`) have the same exposure, and `level` in particular is an
integer that ffmpeg wants in a different form than ffprobe reports it.

**lostblink:** every probe field is accessed through a defaulting accessor; missing bitrate falls
back to a sane CRF encode.

### B-09 · `anullsrc` given a channel count where a layout is expected
`ffmpeg.py:82`

```python
f"anullsrc=channel_layout={params_audio['channels']}:sample_rate={...}"
```

`channels` from ffprobe is a **count** (`1`, `2`). `channel_layout` wants a layout name (`mono`,
`stereo`). Recent ffmpeg tolerates a numeric layout; older builds and some distro builds error with
`Invalid channel layout "1"`. The correct field is `channel_layout`, which ffprobe does report.

**lostblink:** maps count → layout name explicitly, prefers ffprobe's `channel_layout` when present.

### B-10 · concat target file is rewritten underneath a reading ffmpeg
`stream_server.py:62-72` vs `:47-60`

`_enqueue_clip()` opens `{name}_next.concat` with `'w'` — truncating it — while the long-running
ffmpeg from `_run_server()` may be opening that exact file as the concat demuxer loops. There is no
atomicity: ffmpeg can read a zero-length or half-written file and abort with `Invalid data found
when processing input`.

The `wait_until_file_open()` dance at `stream_server.py:92` is an attempt to time around this, but
it synchronises on the *video* file, not the concat file, and is a race either way (B-11).

**lostblink:** concat files are written to a temp name and `os.replace()`d — atomic on POSIX — so a
reader sees either the whole old file or the whole new one.

### B-11 · `wait_until_file_open` is a racy 10s timeout on a fast path
`utils.py:47-62`, called from `stream_server.py:92`

Polls `/proc/{pid}/fd` every 100 ms waiting to observe ffmpeg holding the clip open. A ~0.5 s still
video can be opened and closed **entirely between two polls**, so the file is never observed open
and the function raises `TimeoutError` after 10 s. That propagates out of `add_video()` →
`start_stream()` → the main loop's bare `except` at `main.py:77`, which closes the stream server.

It also blocks the event loop: `add_video` is called from `async def start_stream` but
`wait_until_file_open` is synchronous `time.sleep`, so up to 10 s of **every camera** stalling on
one camera's race.

**lostblink:** no `/proc` inspection at all. Sequencing is done with atomic renames and ffmpeg's own
progress output; nothing sleeps on the event loop.

### B-12 · `unlink()` without `missing_ok` on the previous still
`stream_server.py:102`

```python
self.current_still_video.unlink()
```

Raises `FileNotFoundError` if the file is already gone — trivially reachable after a container
restart with a `tmpfs` `/working` (which `compose.yaml:9-12` actively recommends), or after B-06's
race. Kills `add_video`.

**lostblink:** `missing_ok=True` everywhere, plus a real temp-dir lifecycle.

---

## S3 — correctness, efficiency, hygiene

### B-13 · one full `blink.refresh()` **per camera, per poll** — the ban risk
`blink.py:138` inside `check_for_motion`, called per camera from `main.py:74-76`

`await self.blink.refresh()` fetches the entire homescreen — every network, every camera. Calling it
once per camera multiplies the API load by camera count for zero extra information. With 6 cameras
that is 6 identical homescreen fetches per cycle.

blinkpy self-throttles `refresh()` internally, which is the only reason `poll_interval: 1` in the
shipped `config.json` is not an instant ban — the README's "can be changed at risk of the Blink
server banning you" is describing a hazard the code is actively walking toward.

**lostblink:** exactly one refresh per cycle feeding all cameras, an explicit floor on the interval,
and 429/`busy` handled as backoff rather than retried immediately.

### B-14 · naive local `datetime.now()` compared against Blink's UTC
`blink.py:94`

```python
dt_past = datetime.now() - timedelta(days=CONFIG['blink']['history_days'])
self.metadata = await self.blink.get_videos_metadata(since=str(dt_past), stop=2)
```

`datetime.now()` is naive local time, stringified and sent as a UTC-interpreted `since`. West of UTC
you request a window that extends into the future; east of it you silently lose hours. Masked by the
90-day default, real at small `history_days`.

Same class of bug at `blink.py:29-31`, which parses ISO timestamps into aware datetimes and compares
them against each other — that part is correct — but the two code paths disagree about tz-awareness.

**lostblink:** UTC-aware throughout; `datetime.now(timezone.utc)`, and a lint rule banning bare
`datetime.now()`.

### B-15 · `self.metadata` may be `None`, and is iterated unguarded
`blink.py:110`

`refresh_metadata()` assigns whatever `get_videos_metadata` returns; on an API hiccup that is `None`.
`save_latest_clip` then does `next((m for m in self.metadata ...))` → `TypeError: 'NoneType' object
is not iterable`.

Also on that line, a double-`if` generator: `next((m for m in self.metadata if m['device_name'] ==
camera_name if not m['deleted'] and ...))`. Legal Python, equivalent to `and`, but it reads as a typo
and hides intent.

**lostblink:** metadata normalised to a list on ingest; empty is a valid state.

### B-16 · "latest clip" trusts API ordering instead of sorting
`blink.py:110-111`

`next(...)` takes the **first** matching entry from `self.metadata` and calls it the latest. Nothing
sorts by time. `find_most_recent_clip_url` twenty lines up (`blink.py:19`) *does* sort correctly —
so the codebase already knows better in one path and not the other. When the media list comes back
in a different order, the stream shows an old clip and never corrects.

**lostblink:** always sort by timestamp descending; ordering is never assumed.

### B-17 · failure bookkeeping lives on ad-hoc attributes
`main.py:68-69`, `100-101`, read at `:87`, `:92`, `:95`

`failure_count` and `datetime_started` are monkey-patched onto `StreamServer` instances from outside
the class. Any construction path that misses them — and `start_stream()` returns an object without
them — makes `ss.failure_count` an `AttributeError` at `main.py:87`. It happens to be safe today
because both call sites assign immediately after, which is a coincidence one refactor away from a
crash.

**lostblink:** supervision state is a real dataclass owned by the supervisor.

### B-18 · once a camera is disabled it never comes back
`main.py:87-90`

After `max_failures` (default 3), the camera is `pop()`ed from `self.stream_servers` and there is no
path that ever re-adds it. A transient network blip during startup permanently loses a camera until
someone restarts the container — with no log line saying it will never recover.

**lostblink:** exponential backoff with a ceiling and a permanent retry floor; a camera can always
come back, and the log says when the next attempt is.

### B-19 · bare `except Exception` closes the stream on *any* error
`main.py:77-79`

A transient `aiohttp` timeout in `check_for_motion` is treated identically to a genuine stream
failure: the healthy, running ffmpeg is killed. Combined with B-18, three transient network errors
permanently disable a working camera.

**lostblink:** errors are classified — transient (retry, keep streaming), auth (re-auth), fatal
(restart the pipeline).

### B-20 · cameras processed strictly sequentially
`main.py:74-76`

Each camera's download and ffmpeg work happens in series inside the poll loop, so total latency is
the sum across cameras. It is on upstream's own TODO ("Process cameras in parallel and reduce
latency"). Worse, the synchronous `wait_until_file_open` (B-11) can add up to 10 s of dead time that
every other camera waits behind.

**lostblink:** one shared API refresh, then per-camera pipelines as independent asyncio tasks.

### B-21 · unpinned base image and unpinned dependencies
`Dockerfile:2,8`

```dockerfile
FROM python:alpine
RUN ... pip install rich blinkpy aiohttp
```

No tag on the base image and no version constraint on any dependency. Two builds a week apart can
differ in Python minor version and in blinkpy major version. Given that blinkpy's auth flow has
changed twice in the last year — and that fixing the fallout is literally commit `ee52227` in this
repo's history — this is the highest-probability future breakage in the project.

There is also no `requirements.txt` or lockfile anywhere.

**lostblink:** pinned base image digest, `requirements.txt` with hashes, `pyproject.toml` with
floors and ceilings, Dependabot.

### B-22 · no `depends_on`, so first boot races
`compose.yaml:1-20`

`blinkbridge` and `mediamtx` start simultaneously. ffmpeg tries to publish to `mediamtx:8554` before
it is listening, fails, and burns a `failure_count` — up to three of them at once (B-18), which can
permanently disable cameras on a cold start.

`mediamtx` also runs with no config, so its defaults govern the RTSP paths, and nothing constrains
who may publish.

**lostblink:** `depends_on` with a healthcheck, publish retried with backoff, and a shipped
`mediamtx.yml` that restricts publishing to the bridge.

### B-23 · credentials at rest with default permissions
`blink.py:86,90`

`.cred.json` holds the Blink OAuth refresh token and the hardware id — full account access — and is
written via `blink.save()` with whatever umask applies (typically `0644`). It sits in a
bind-mounted `./config` directory on the host.

Adding it to `.gitignore` (commit `95fcd55`, "*security risk*") addressed the commit hazard but not
the on-disk one. Anyone who has ever `docker compose`d this in a shared directory should assume the
token is readable by every local user.

**lostblink:** `0600` on write, `chmod` enforced at startup with a warning if it was wrong,
and support for supplying credentials via env/secret instead of a file.

### B-24 · `input()` for 2FA inside an async service
`blink.py:66`

```python
twofa_code = input("Enter your 2FA code: ")
```

A blocking, synchronous `input()` on the event loop inside an `async def`. Works in the documented
`docker compose run` flow, but if a token expires while running under `docker compose up` (detached,
no TTY), `input()` raises `EOFError` on a closed stdin and the service crash-loops with a confusing
error rather than saying "re-authentication needed".

**lostblink:** an explicit `lostblink auth` subcommand for the interactive flow; the service itself
never reads stdin and reports an actionable `AUTH_REQUIRED` state instead.

---

## Not bugs — things that look wrong and are not

Worth recording so nobody "fixes" them:

- **`stream_server.py:53-58` writes the same file twice into the concat list.** Deliberate. The
  concat demuxer needs at least two entries to loop cleanly without a visible seam.
- **`option safe 0` repeated after each `file` line.** Also deliberate, with a comment. `safe` does
  not propagate into nested concat files; it must be restated.
- **`ffmpeg.py:52` `-vf scale=out_range=pc`, marked `# HACK`.** It is a real fix for a real problem —
  Blink clips are limited-range and the still-frame re-encode otherwise shifts levels, making the
  frozen frame visibly darker than the live clip.
- **`-fps_mode drop` at `stream_server.py:39`.** Correct for a looping still; keeps the muxer from
  inflating timestamps.
- **The whole still-frame-loop concept.** It is the good idea in this project. Downstream NVRs like
  Frigate disconnect and alarm on a stream that stops; looping the last frame keeps the RTSP session
  continuously valid. `lostblink` keeps this behaviour as the fallback layer beneath live view.

---

## Coverage

| Severity | Count |
| --- | ---: |
| S1 — process crash | 4 |
| S2 — lost footage / dead camera | 8 |
| S3 — correctness / efficiency / hygiene | 12 |
| **Total** | **24** |

Upstream's own TODO list — better error handling, cleanup, hardware acceleration, parallel cameras,
ONVIF with motion events — is tracked in `docs/architecture.md`; all five are addressed except ONVIF,
which is deferred.
