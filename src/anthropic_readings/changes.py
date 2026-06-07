from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DATE_FORMATS
from .models import ChangeType, TrackedDocument


def compute_file_hash(filepath: Path) -> Optional[str]:
    try:
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:16]
    except Exception:
        return None


def parse_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None

    normalized = date_str.replace("Z", "")

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def detect_changes(
    current_docs: dict[str, TrackedDocument],
    previous_docs: dict[str, TrackedDocument],
    repo_path: Path,
    logger: logging.Logger,
) -> list[TrackedDocument]:
    changed: list[TrackedDocument] = []

    for path, doc in current_docs.items():
        previous_doc = previous_docs.get(path)
        if doc.content_hash is None:
            doc.content_hash = compute_file_hash(repo_path / path)

        if not previous_doc:
            doc.change_type = ChangeType.NEW
            logger.info(f"NEW document: {path}")
            changed.append(doc)
            continue

        if _document_changed(doc, previous_doc):
            doc.change_type = ChangeType.CHANGED
            logger.info(f"CHANGED document: {path} ({previous_doc.date} -> {doc.date})")
            changed.append(doc)

    return changed


def _document_changed(doc: TrackedDocument, previous_doc: TrackedDocument) -> bool:
    if doc.content_hash is not None and previous_doc.content_hash is not None:
        return doc.content_hash != previous_doc.content_hash

    current_date = parse_date(doc.date)
    previous_date = parse_date(previous_doc.date)
    return current_date != previous_date
