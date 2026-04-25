from __future__ import annotations

import logging
import os
import sys


def _default_level() -> str:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return "DEBUG"
    return "INFO"


def setup_logging(level: str | None = None) -> logging.Logger:
    effective_level = level or _default_level()
    resolved_level = getattr(logging, effective_level.upper(), logging.INFO)

    logger = logging.getLogger("anthropic-daemon")
    logger.setLevel(resolved_level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(resolved_level)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logging.getLogger("asyncio").setLevel(logging.ERROR)

    return logger
