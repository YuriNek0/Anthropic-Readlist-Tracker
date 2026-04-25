from __future__ import annotations

import logging
import subprocess
from pathlib import Path


def git_clone(url: str, path: Path, logger: logging.Logger) -> bool:
    try:
        logger.info(f"Cloning {url} into {path}")
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Git clone failed: {e.stderr}")
        return False


def git_pull(path: Path, logger: logging.Logger) -> bool:
    try:
        logger.info(f"Pulling updates in {path}")
        result = subprocess.run(
            ["git", "pull", "origin", "HEAD"],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
        logger.debug(f"Git pull output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Git pull failed: {e.stderr}")
        return False


def ensure_repo_available(url: str, path: Path, logger: logging.Logger) -> bool:
    if path.is_dir():
        return git_pull(path, logger)
    return git_clone(url, path, logger)
