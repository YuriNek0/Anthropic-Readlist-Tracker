from __future__ import annotations

from datetime import datetime
import re
from pathlib import Path

from slugify import slugify

from ..models import TrackedDocument

_LEADING_INDEX_RE = re.compile(r"^(\d{2})(?:[^\d].*)?$")


def slugify_title(title: str) -> str:
    return slugify(title, separator="-")


def resolve_output_date(date_value: str) -> str:
    if isinstance(date_value, str):
        normalized = date_value.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", normalized):
            return normalized

        if normalized:
            candidate = normalized.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return datetime.now().strftime("%Y-%m-%d")


def _cookbook_index_prefix(doc: TrackedDocument) -> str | None:
    stem = Path(doc.path).stem
    match = _LEADING_INDEX_RE.match(stem)
    if not match:
        return None
    return match.group(1)


def _cookbook_file_name(doc: TrackedDocument) -> str:
    slugified_title = slugify_title(doc.title)
    index_prefix = _cookbook_index_prefix(doc)
    if not index_prefix:
        return f"{slugified_title}.pdf"
    return f"{index_prefix}-{slugified_title}.pdf"


def build_output_relative_path(
    date_str: str,
    repo_name: str,
    doc: TrackedDocument,
) -> Path:
    date_dir = Path(date_str)

    if repo_name.lower() == "courses":
        parts = Path(doc.path).parts
        if len(parts) <= 1:
            return date_dir / f"{slugify_title(doc.title)}.pdf"

        folder_rel = Path(*[slugify_title(part) for part in parts[:-1]])
        file_name = f"{slugify_title(Path(parts[-1]).stem)}.pdf"
        return date_dir / folder_rel / file_name

    if repo_name.lower() == "cookbooks":
        source_parts = Path(doc.path).parts
        parent_parts = source_parts[:-1] if source_parts else []
        parent_dir = Path(*parent_parts) if parent_parts else Path()
        return date_dir / parent_dir / _cookbook_file_name(doc)

    return date_dir / f"{slugify_title(doc.title)}.pdf"


def build_render_output_path(
    output_root: Path,
    date_str: str,
    repo_name: str,
    doc: TrackedDocument,
) -> tuple[Path, str]:
    relative_pdf_path = build_output_relative_path(date_str, repo_name, doc)
    folder = output_root / relative_pdf_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    return folder, relative_pdf_path.name
