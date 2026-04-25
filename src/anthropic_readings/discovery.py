from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

from .models import TrackedDocument


def discover_cookbooks(
    repo_path: Path, manifest_file: str, logger: logging.Logger
) -> dict[str, TrackedDocument]:
    manifest_path = repo_path / manifest_file
    if not manifest_path.is_file():
        logger.error(f"Manifest file not found: {manifest_path}")
        return {}

    try:
        with open(manifest_path, "r") as f:
            registry = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to read manifest file {manifest_path}: {e}")
        return {}

    if not isinstance(registry, list):
        logger.error(f"Manifest file {manifest_path} has an unexpected format")
        return {}

    documents: dict[str, TrackedDocument] = {}
    for item in registry:
        if not isinstance(item, dict):
            continue

        path = item.get("path", "")
        title = item.get("title", "")
        date = item.get("date", "")

        if not path or not title:
            continue

        documents[path] = TrackedDocument(path=path, title=title, date=date)

    logger.info(f"Discovered {len(documents)} cookbooks from registry")
    return documents


def discover_courses(
    repo_path: Path, patterns: list[str], logger: logging.Logger
) -> dict[str, TrackedDocument]:
    documents: dict[str, TrackedDocument] = {}

    for pattern in patterns:
        for filepath in repo_path.rglob(pattern):
            if "AmazonBedrock" in str(filepath):
                continue

            rel_path = filepath.relative_to(repo_path)
            path_str = str(rel_path)

            title = rel_path.stem.replace("-", " ").replace("_", " ").title()
            try:
                date = datetime.fromtimestamp(filepath.stat().st_mtime).strftime(
                    "%Y-%m-%d"
                )
            except Exception:
                date = datetime.now().strftime("%Y-%m-%d")

            documents[path_str] = TrackedDocument(path=path_str, title=title, date=date)

    logger.info(f"Discovered {len(documents)} course documents")
    return documents
