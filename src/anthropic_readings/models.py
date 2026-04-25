from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class ChangeType(Enum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass
class TrackedDocument:
    path: str
    title: str
    date: str
    content_hash: Optional[str] = None
    change_type: ChangeType = ChangeType.UNCHANGED


@dataclass
class RenderedDocument:
    doc: TrackedDocument
    repo_name: str = ""
    pdf_path: Optional[Path] = None
    error: Optional[str] = None
