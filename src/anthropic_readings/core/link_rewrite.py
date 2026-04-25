from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import SplitResult, urlsplit, urlunsplit

_MARKDOWN_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")


def _normalize_repo_path(path: str) -> str:
    normalized_parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if normalized_parts:
                normalized_parts.pop()
            continue
        normalized_parts.append(part)
    return "/".join(normalized_parts)


def _resolve_repo_relative_path(current_doc_path: str, linked_path: str) -> str:
    if linked_path.startswith("/"):
        return _normalize_repo_path(linked_path.lstrip("/"))

    current_parent = PurePosixPath(current_doc_path).parent
    return _normalize_repo_path(str(current_parent / linked_path))


def _split_markdown_link_target(target: str) -> tuple[str, str, bool]:
    value = target.strip()

    if value.startswith("<") and ">" in value:
        closing = value.find(">")
        return value[1:closing], value[closing + 1 :], True

    if " " not in value:
        return value, "", False

    link_value, trailing = value.split(" ", 1)
    return link_value, f" {trailing}", False


def _rewrite_link_target(
    target: str,
    current_doc_path: str,
    output_relpath_by_doc: dict[str, str],
) -> str | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None

    if not parsed.path:
        return None

    if not parsed.path.lower().endswith((".md", ".ipynb")):
        return None

    normalized_current = _normalize_repo_path(current_doc_path)
    current_output_path = output_relpath_by_doc.get(normalized_current)
    if not current_output_path:
        return None

    linked_doc_path = _resolve_repo_relative_path(current_doc_path, parsed.path)
    linked_output_path = output_relpath_by_doc.get(linked_doc_path)
    if not linked_output_path:
        return None

    relative_target = posixpath.relpath(
        linked_output_path,
        start=str(PurePosixPath(current_output_path).parent),
    )

    rewritten = SplitResult(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=relative_target,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(rewritten)


def rewrite_markdown_links(
    markdown: str,
    current_doc_path: str,
    output_relpath_by_doc: dict[str, str] | None,
) -> str:
    if not markdown or not output_relpath_by_doc:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        label = match.group(1)
        raw_target = match.group(2)
        link_value, trailing, wrapped = _split_markdown_link_target(raw_target)
        rewritten_target = _rewrite_link_target(
            link_value,
            current_doc_path,
            output_relpath_by_doc,
        )
        if not rewritten_target:
            return match.group(0)

        if wrapped:
            return f"[{label}](<{rewritten_target}>{trailing})"
        return f"[{label}]({rewritten_target}{trailing})"

    return _MARKDOWN_LINK_RE.sub(_replace, markdown)


def rewrite_notebook_markdown_cells(
    notebook_data: dict[str, object],
    current_doc_path: str,
    output_relpath_by_doc: dict[str, str] | None,
) -> bool:
    cells = notebook_data.get("cells")
    if not isinstance(cells, list):
        return False

    changed = False
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if cell.get("cell_type") != "markdown":
            continue

        source = cell.get("source")
        if isinstance(source, str):
            rewritten = rewrite_markdown_links(
                source,
                current_doc_path,
                output_relpath_by_doc,
            )
            if rewritten != source:
                cell["source"] = rewritten
                changed = True
            continue

        if isinstance(source, list):
            updated = []
            any_line_changed = False
            for line in source:
                if not isinstance(line, str):
                    updated.append(line)
                    continue

                rewritten = rewrite_markdown_links(
                    line,
                    current_doc_path,
                    output_relpath_by_doc,
                )
                if rewritten != line:
                    any_line_changed = True
                updated.append(rewritten)

            if any_line_changed:
                cell["source"] = updated
                changed = True

    return changed
