"""LostBlink CLI entry point.

Commands
--------
lostblink auth
    Interactive first-time Blink authentication.

    Uses blinkpy's current OAuth 2.0 + PKCE flow. If Blink requires
    two-factor authentication, the command prompts for the verification
    code and persists the resulting access token, refresh token, account
    metadata, and stable hardware UUID.

lostblink run
    Starts the LostBlink bridge using previously saved credentials.

    Service mode never reads stdin. If Blink requires a new interactive
    authentication, the service exits with a clear message directing the
    operator to run `lostblink auth`.

Designed for blinkpy >= 0.25.9, < 0.26.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from .app import Application
from .config import Config, ConfigError, secure_credentials_file

log = logging.getLogger("lostblink")

MIN_BLINKPY_VERSION = (0, 25, 9)


def _setup_logging(level: str) -> None:
    """Configure console logging."""
    try:
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            highlighter=NullHighlighter(),
            rich_tracebacks=True,
            show_path=False,
        )
        fmt = "%(message)s"
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    logging.basicConfig(
        format=fmt,
        datefmt="[%X]",
        handlers=[handler],
        level="WARNING",
    )

    logging.getLogger("lostblink").setLevel(level.upper())


def _blinkpy_version_tuple(version: str) -> tuple[int, int, int]:
    """Convert a blinkpy version string into a comparable three-part tuple."""
    parts: list[int] = []

    for piece in version.split(".")[:3]:
        digits = "".join(char for char in piece if char.isdigit())

        if not digits:
            parts.append(0)
        else:
            parts.append(int(digits))

    while len(parts) < 3:
        parts.append(0)

    return tuple(parts[:3])  # type: ignore[return-value]


def _verify_blinkpy_version() -> str:
    """Fail early when an obsolete blinkpy is installed."""
    try:
        version = importlib.metadata.version("blinkpy")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ConfigError(
            "blinkpy is not installed. Rebuild the LostBlink container."
        ) from exc

    parsed = _blinkpy_version_tuple(version)

    if parsed < MIN_BLINKPY_VERSION:
        raise ConfigError(
            f"blinkpy {version} is too old. "
            "LostBlink requires blinkpy >=0.25.9,<0.26. "
            "Rebuild the Docker image after updating requirements.txt."
        )

    if parsed >= (0, 26, 0):
        log.warning(
            "blinkpy %s is newer than the tested LostBlink range "
            "(>=0.25.9,<0.26)",
            version,
        )

    return version


async def _connect(config: Config, *, interactive: bool) -> Any:
    """Authenticate with Blink and return a fully initialized Blink instance.

    Authentication strategy:

    1. Load saved OAuth credentials if present.
    2. Otherwise use username/password from LostBlink configuration.
    3. blinkpy performs OAuth 2.0 Authorization Code + PKCE.
    4. If Blink requires 2FA:
       - interactive auth mode prompts for the verification code;
       - service mode fails with instructions to run `lostblink auth`.
    5. Persist refreshed credentials and the hardware UUID.
    """
    from aiohttp import ClientSession, CookieJar
    from blinkpy.auth import (
        Auth,
        BlinkTwoFARequiredError,
        LoginError,
    )
    from blinkpy.blinkpy import Blink
    from blinkpy.helpers.util import json_load

    blinkpy_version = _verify_blinkpy_version()

    log.info("using blinkpy %s", blinkpy_version)

    cred_path = config.paths.credentials

    # Keep Blink OAuth cookies completely isolated from anything else using
    # aiohttp in the process. Blink's OAuth flow requires cookie continuity
    # across authorize -> signin -> 2FA -> token exchange.
    cookie_jar = CookieJar(unsafe=True)

    session = ClientSession(cookie_jar=cookie_jar)

    blink = Blink(session=session)

    try:
        if cred_path.exists():
            secure_credentials_file(cred_path)

            log.info("using saved Blink credentials from %s", cred_path)

            try:
                login_data = await json_load(cred_path)
            except Exception as exc:
                raise ConfigError(
                    f"could not read saved credentials from {cred_path}: {exc}. "
                    f"Delete the file and run 'lostblink auth' again."
                ) from exc

            blink.auth = Auth(
                login_data,
                no_prompt=True,
                session=session,
            )

        else:
            username = config.blink.username
            password = config.blink.password

            if not username or not password:
                raise ConfigError(
                    "no saved Blink credentials and no username/password "
                    "configured. Set blink.login.username and "
                    "blink.login.password in config.json, or provide "
                    "LOSTBLINK_USERNAME / LOSTBLINK_PASSWORD."
                )

            log.info("starting Blink OAuth login as %s", username)

            # blinkpy >=0.25.9 automatically generates a valid UUID hardware_id
            # when one is not supplied. blink.save() persists that UUID so the
            # same virtual device identity is reused on future logins.
            blink.auth = Auth(
                {
                    "username": username,
                    "password": password,
                },
                no_prompt=True,
                session=session,
            )

        try:
            started = await blink.start()

        except BlinkTwoFARequiredError:
            if not interactive:
                raise ConfigError(
                    "Blink requires two-factor authentication. "
                    "Stop the service and run:\n\n"
                    "  docker compose run --rm lostblink auth\n\n"
                    "Then enter the verification code sent by Blink."
                ) from None

            print()
            print("Blink requires two-factor authentication.")
            print("A verification code should have been sent by Blink.")
            print()

            code = input("Enter the Blink verification code: ").strip()

            if not code:
                raise ConfigError("no Blink verification code entered")

            log.info("submitting Blink verification code")

            started = await blink.send_2fa_code(code)

            if not started:
                raise ConfigError(
                    "Blink rejected the verification code or failed "
                    "to complete OAuth authentication."
                )

            log.info("Blink two-factor authentication accepted")

        except LoginError as exc:
            raise ConfigError(f"Blink login failed: {exc}") from exc

        if not started:
            if cred_path.exists():
                raise ConfigError(
                    "Blink authentication failed using the saved credentials. "
                    f"Delete {cred_path} and run 'lostblink auth' again."
                )

            raise ConfigError(
                "Blink authentication failed before 2FA. "
                "Verify the username/password and check the preceding "
                "blinkpy OAuth error."
            )

        # Save token, refresh token, region/account information and hardware_id.
        await blink.save(cred_path)
        secure_credentials_file(cred_path)

        hardware_id = getattr(blink.auth, "hardware_id", None)

        if hardware_id:
            log.info("Blink hardware ID: %s", hardware_id)

        log.info("Blink authenticated successfully")

        return blink

    except BaseException:
        await session.close()
        raise


async def _shutdown_on_signal(event: asyncio.Event) -> None:
    """Translate SIGINT/SIGTERM into an asyncio shutdown event."""
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(
            NotImplementedError,
            AttributeError,
            RuntimeError,
        ):
            loop.add_signal_handler(sig, event.set)


async def _cmd_auth(config: Config) -> int:
    """Run interactive Blink authentication and save credentials."""
    blink = await _connect(config, interactive=True)

    try:
        cameras = list(blink.cameras.keys())

        print()
        print(f"Authentication saved to {config.paths.credentials}")
        print(f"Discovered {len(cameras)} camera(s):")

        if cameras:
            for name in cameras:
                print(f"  - {name}")
        else:
            print("  No cameras were returned by Blink.")

        print()
        print("Authentication complete.")
        print("Start LostBlink with:")
        print()
        print("  docker compose up -d")
        print()

        return 0

    finally:
        with contextlib.suppress(Exception):
            await blink.auth.session.close()

        await asyncio.sleep(0.1)


async def _cmd_run(config: Config) -> int:
    """Start the long-running LostBlink bridge."""
    blink = await _connect(config, interactive=False)

    app = Application(config)

    shutdown = asyncio.Event()

    await _shutdown_on_signal(shutdown)

    app_task = asyncio.create_task(
        app.start(blink),
        name="lostblink",
    )

    shutdown_task = asyncio.create_task(
        shutdown.wait(),
        name="shutdown",
    )

    done, pending = await asyncio.wait(
        {app_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    exit_code = 0

    if app_task in done:
        shutdown_task.cancel()

        try:
            app_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("LostBlink bridge failed: %s", exc)
            exit_code = 1

    else:
        log.info("shutdown requested")

        app_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError,
            Exception,
        ):
            await app_task

    for task in pending:
        task.cancel()

    await app.close()

    with contextlib.suppress(Exception):
        await blink.auth.session.close()

    # Allow aiohttp's SSL transports to unwind before interpreter shutdown.
    await asyncio.sleep(0.25)

    return exit_code


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse LostBlink CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="lostblink",
        description="Live RTSP streams from Blink cameras.",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help=(
            "path to config.json "
            "(default: $LOSTBLINK_CONFIG or ./config.json)"
        ),
    )

    parser.add_argument(
        "--log-level",
        default=None,
        help="override configured log level",
    )

    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser(
        "auth",
        help="interactive Blink OAuth login including 2FA",
    )

    subcommands.add_parser(
        "run",
        help="run the LostBlink bridge (default)",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """LostBlink CLI entry point."""
    args = _parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _setup_logging(args.log_level or config.log_level)

    try:
        version = _verify_blinkpy_version()
        log.debug("blinkpy version %s", version)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

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
