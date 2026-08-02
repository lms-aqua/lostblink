"""CLI entry point.

Two subcommands, deliberately separated:

``lostblink auth``
    Interactive first-time login, including the 2FA prompt.
``lostblink run``
    The service. Never reads stdin.

Upstream calls ``input()`` for the 2FA code from inside the async service
(bug B-24). That works in the documented ``docker compose run`` flow, but when a
token expires under ``docker compose up`` there is no TTY, ``input()`` raises
``EOFError``, and the container crash-loops with a misleading error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from .app import Application
from .config import Config, ConfigError, secure_credentials_file

log = logging.getLogger("lostblink")


def _setup_logging(level: str) -> None:
    try:
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            highlighter=NullHighlighter(), rich_tracebacks=True, show_path=False
        )
        fmt = "%(message)s"
    except ImportError:  # rich is a nicety, not a requirement
        handler = logging.StreamHandler(sys.stderr)
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    logging.basicConfig(format=fmt, datefmt="[%X]", handlers=[handler], level="WARNING")
    logging.getLogger("lostblink").setLevel(level.upper())


async def _connect(config: Config, *, interactive: bool) -> Any:
    """Authenticate with Blink and return a started ``Blink`` instance."""
    from aiohttp import ClientSession
    from blinkpy.auth import Auth, LoginError
    from blinkpy.blinkpy import Blink
    from blinkpy.helpers.util import json_load

    cred_path = config.paths.credentials
    session = ClientSession()
    blink = Blink(session=session)

    if cred_path.exists():
        secure_credentials_file(cred_path)
        log.info("using saved credentials from %s", cred_path)
        blink.auth = Auth(await json_load(cred_path), no_prompt=True, session=session)
    else:
        if not (config.blink.username and config.blink.password):
            await session.close()
            raise ConfigError(
                "no saved credentials and no username/password configured. "
                "Set blink.login in the config file, or LOSTBLINK_USERNAME / "
                "LOSTBLINK_PASSWORD in the environment."
            )
        log.info("logging in as %s", config.blink.username)
        blink.auth = Auth(
            {"username": config.blink.username, "password": config.blink.password},
            no_prompt=True,
            session=session,
        )

    try:
        # blinkpy reports most login failures by returning False and logging,
        # rather than raising -- so the return value must be checked.
        started = await blink.start()

        # 2FA is likewise a flag, not an exception: start() returns with
        # key_required set, then the caller sends the code and re-runs setup.
        if getattr(blink, "key_required", False):
            if not interactive:
                raise ConfigError(
                    "two-factor authentication required. Run 'lostblink auth' "
                    "once from a terminal to complete it."
                )
            code = input("Enter the Blink verification code: ").strip()
            if not code:
                raise ConfigError("no verification code entered")
            await blink.auth.send_auth_key(blink, code)
            await blink.setup_post_verify()
            if getattr(blink, "key_required", False):
                raise ConfigError("verification code was rejected")
        elif not started:
            hint = (
                "check the username and password in your config"
                if not cred_path.exists()
                else f"saved credentials may have expired -- delete {cred_path} "
                     "and run 'lostblink auth' again"
            )
            raise ConfigError(f"could not log in to Blink: {hint}")

        await blink.save(cred_path)
        secure_credentials_file(cred_path)
    except LoginError as exc:
        await session.close()
        raise ConfigError(f"login failed: {exc}") from exc
    except BaseException:
        # Never leak the aiohttp session: an unclosed connector is finalised
        # during interpreter teardown and produces a confusing ImportError.
        await session.close()
        raise

    log.info("authenticated")
    return blink


async def _shutdown_on_signal(event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # add_signal_handler is not implemented on Windows.
            loop.add_signal_handler(sig, event.set)


async def _cmd_auth(config: Config) -> int:
    blink = await _connect(config, interactive=True)
    cameras = list(blink.cameras.keys())
    await blink.auth.session.close()
    print(f"\nAuthentication saved to {config.paths.credentials}")
    print(f"Discovered {len(cameras)} camera(s):")
    for name in cameras:
        print(f"  - {name}")
    print("\nNow run: lostblink run")
    return 0


async def _cmd_run(config: Config) -> int:
    blink = await _connect(config, interactive=False)
    app = Application(config)
    shutdown = asyncio.Event()
    await _shutdown_on_signal(shutdown)

    task = asyncio.create_task(app.start(blink), name="lostblink")
    stopper = asyncio.create_task(shutdown.wait(), name="shutdown")

    done, _ = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if task in done:
        stopper.cancel()
        try:
            task.result()
        except Exception as exc:
            log.error("%s", exc)
            exit_code = 1
    else:
        log.info("shutting down")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await app.close()
    with contextlib.suppress(Exception):
        await blink.auth.session.close()
    # Give aiohttp's SSL transports a moment to unwind cleanly.
    await asyncio.sleep(0.25)
    return exit_code


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lostblink",
        description="Live RTSP streams from Blink cameras.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="path to config.json (default: $LOSTBLINK_CONFIG or ./config.json)",
    )
    parser.add_argument("--log-level", default=None, help="override log_level")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("auth", help="interactive first-time login (prompts for 2FA)")
    sub.add_parser("run", help="run the bridge (default)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _setup_logging(args.log_level or config.log_level)

    command = args.command or "run"
    runner = _cmd_auth if command == "auth" else _cmd_run

    try:
        return asyncio.run(runner(config))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
