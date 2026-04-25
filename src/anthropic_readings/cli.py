from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, DEFAULT_CONFIG_PATHS
from .logging_setup import setup_logging
from .orchestrator import run_daemon


def _resolve_config_path(cli_path: str | None):
    if cli_path:
        return cli_path

    for path in DEFAULT_CONFIG_PATHS:
        if path.is_file():
            return str(path)

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Anthropic Readings Daemon")
    parser.add_argument(
        "--once", action="store_true", help="Run once and exit (for systemd)"
    )
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument(
        "--check", action="store_true", help="Check configuration and exit"
    )

    args = parser.parse_args()

    config_path = _resolve_config_path(args.config)
    if not config_path:
        if args.config:
            print(
                f"CONFIG ERROR: Config file not found: {args.config}", file=sys.stderr
            )
        else:
            print(
                "CONFIG ERROR: No config file found in default paths", file=sys.stderr
            )
        print(
            "Expected config.yaml with required fields: azure.*, user.email, email.*, repos.*",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        config = Config.from_yaml(Path(config_path))
    except ValueError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)

    logger = setup_logging("INFO")
    logger.info(f"Loaded config from {config_path}")

    if args.check:
        logger.info("Configuration check passed")
        logger.info(f"Production mode: {config.is_production}")
        logger.info(f"Repos: {[r.name for r in config.repos]}")
        logger.info(f"Output dir: {config.output_dir}")
        return

    import asyncio

    if args.once:
        asyncio.run(run_daemon(config, logger))
        return

    try:
        import schedule
    except ImportError:
        logger.error("schedule library not installed. Run: uv pip install schedule")
        logger.info("Falling back to --once mode")
        asyncio.run(run_daemon(config, logger))
        return

    logger.info("Starting daemon with daily schedule")
    schedule.repeat(lambda: asyncio.run(run_daemon(config, logger)))

    try:
        import time

        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user")
