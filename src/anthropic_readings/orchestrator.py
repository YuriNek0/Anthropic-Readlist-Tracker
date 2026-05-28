from __future__ import annotations

import asyncio
import logging
import shutil
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from .changes import compute_file_hash, detect_changes
from .config import Config
from .discovery import discover_cookbooks, discover_courses
from .mailer import send_email_with_links
from .graph import (
    create_sharing_link,
    delete_from_appfolder,
    download_version_from_onedrive,
    begin_graph_run,
    end_graph_run,
    upload_pdf_to_onedrive,
    upload_version_to_onedrive,
)
from .models import ChangeType, RenderedDocument, TrackedDocument
from .rendering import build_output_relpath_by_doc, render_document_to_pdf
from .repository import ensure_repo_available
from .mailer import send_error_email


class _ErrorCollector(logging.Handler):
    def __init__(self, message_buffer: list[str], max_messages: int = 40):
        super().__init__()
        self.message_buffer = message_buffer
        self.max_messages = max_messages

    def emit(self, record: logging.LogRecord) -> None:
        self.message_buffer.append(self.format(record))
        if len(self.message_buffer) > self.max_messages:
            del self.message_buffer[0 : len(self.message_buffer) - self.max_messages]


class _UploadLinkStageError(Exception):
    def __init__(self, message: str, uploaded_pdf_item_ids: list[str]):
        super().__init__(message)
        self.uploaded_pdf_item_ids = uploaded_pdf_item_ids


def _build_error_context(messages: list[str]) -> str:
    if not messages:
        return "<no captured error details>"

    return "\n".join(f"- {message}" for message in messages)


def group_by_top_folder(
    docs: list[RenderedDocument],
) -> dict[str, list[RenderedDocument]]:
    folders: dict[str, list[RenderedDocument]] = defaultdict(list)
    for doc in docs:
        parts = Path(doc.doc.path).parts
        if parts:
            folders[parts[0]].append(doc)
    return folders


def _build_email_body(
    all_docs: list[RenderedDocument],
    uploaded_items: list[tuple[RenderedDocument, str, str]],
) -> str:
    by_path = {doc.doc.path: link for doc, _item_id, link in uploaded_items}
    failed_docs = [d for d in all_docs if d.error]
    cookbook_docs = [d for d in all_docs if d.repo_name == "cookbooks"]
    course_docs = [d for d in all_docs if d.repo_name == "courses"]

    def _doc_line(doc: RenderedDocument, link: str | None) -> str:
        change_label = "New" if doc.doc.change_type == ChangeType.NEW else "Updated"
        if doc.error:
            return f"<li>{doc.doc.title} ({change_label}) - ERROR: {doc.error}</li>"
        if link:
            return f"<li><a href='{link}'>{doc.doc.title}</a> ({change_label}) - {doc.doc.path}</li>"
        return (
            f"<li>{doc.doc.title} ({change_label}) - WARNING: "
            f"Failed to upload or create link</li>"
        )

    body_lines = ["<h2>Anthropic Readings Update</h2>"]

    if cookbook_docs:
        body_lines.append("<h3>Anthropic Cookbooks</h3>")
        body_lines.append("<ul>")
        for doc in cookbook_docs:
            body_lines.append(_doc_line(doc, by_path.get(doc.doc.path)))
        body_lines.append("</ul>")

    if course_docs:
        body_lines.append("<h3>Anthropic Courses</h3>")
        for folder_name, folder_docs in group_by_top_folder(course_docs).items():
            body_lines.append(f"<h4>{folder_name}</h4>")
            body_lines.append("<ul>")
            for doc in folder_docs:
                body_lines.append(_doc_line(doc, by_path.get(doc.doc.path)))
            body_lines.append("</ul>")

    if failed_docs:
        body_lines.append("<h3>Failed to render:</h3>")
        body_lines.append("<ul>")
        for doc in failed_docs:
            body_lines.append(f"<li>{doc.doc.title} ({doc.doc.path}): {doc.error}</li>")
        body_lines.append("</ul>")

    return "\n".join(body_lines)


def _concurrency_limit(value: object, fallback: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback

    if parsed < 1:
        return fallback
    return parsed


def _run_async_in_thread(async_func, *args):
    return asyncio.run(async_func(*args))


async def _render_documents_concurrently(
    docs: list[TrackedDocument],
    repo_path: Path,
    output_dir: Path,
    logger: logging.Logger,
    repo_name: str,
    max_concurrency: int,
    render_timeout_seconds: int,
    output_relpath_by_doc: dict[str, str] | None = None,
    on_rendered=None,
) -> list[RenderedDocument]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _render_single(doc: TrackedDocument) -> RenderedDocument:
        async with semaphore:
            rendered_doc = await asyncio.to_thread(
                render_document_to_pdf,
                doc,
                repo_path,
                output_dir,
                logger,
                repo_name,
                output_relpath_by_doc,
                render_timeout_seconds,
            )

        if on_rendered is not None:
            await on_rendered(rendered_doc)

        return rendered_doc

    tasks = [_render_single(doc) for doc in docs]
    return await asyncio.gather(*tasks)


async def process_repo(
    repo_config,
    config: Config,
    logger: logging.Logger,
    on_rendered=None,
) -> tuple[list[RenderedDocument], dict[str, TrackedDocument]]:
    repo_path = Path(repo_config.local_path)
    version_file_name = Path(repo_config.version_file).name

    if not ensure_repo_available(repo_config.url, repo_path, logger):
        return [], {}

    if repo_config.manifest_file:
        current_docs = discover_cookbooks(repo_path, repo_config.manifest_file, logger)
    else:
        current_docs = discover_courses(
            repo_path, repo_config.discover_patterns, logger
        )

    if not current_docs:
        logger.warning(f"No documents discovered for {repo_config.name}")
        return [], {}

    previous_docs = await download_version_from_onedrive(
        version_file_name,
        config,
        logger,
        repo_name=repo_config.name,
    )
    if previous_docs is None:
        previous_docs = {}

    for doc in current_docs.values():
        doc.content_hash = compute_file_hash(repo_path / doc.path)

    changed_docs = detect_changes(current_docs, previous_docs, repo_path, logger)
    if not changed_docs:
        logger.info(f"No changes detected for {repo_config.name}")
        return [], current_docs

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_relpath_by_doc = build_output_relpath_by_doc(
        current_docs,
        repo_config.name,
    )

    render_concurrency = _concurrency_limit(config.daemon.render_concurrency)
    rendered = await _render_documents_concurrently(
        changed_docs,
        repo_path,
        output_dir,
        logger,
        repo_config.name,
        render_concurrency,
        config.daemon.render_timeout_seconds,
        output_relpath_by_doc=output_relpath_by_doc,
        on_rendered=on_rendered,
    )

    return rendered, current_docs


async def _rollback_uploaded_items(
    config: Config,
    logger: logging.Logger,
    uploaded_pdf_item_ids: list[str],
    uploaded_version_ids: list[str],
) -> list[str]:
    failures: list[str] = []

    if uploaded_version_ids:
        logger.warning("Rolling back uploaded version metadata files")
    for item_id in uploaded_version_ids:
        deleted = await delete_from_appfolder(item_id, config, logger, permanent=True)
        if not deleted:
            failures.append(f"version item {item_id}")
            logger.error(
                "Failed to permanently delete uploaded version metadata item during rollback: %s",
                item_id,
            )

    if uploaded_pdf_item_ids:
        logger.warning("Rolling back uploaded PDFs")
    for item_id in uploaded_pdf_item_ids:
        deleted = await delete_from_appfolder(item_id, config, logger, permanent=True)
        if not deleted:
            failures.append(f"pdf item {item_id}")
            logger.error(
                "Failed to permanently delete uploaded PDF item during rollback: %s",
                item_id,
            )

    return failures


def _cleanup_tree(path: Path, logger: logging.Logger, label: str) -> None:
    if not path.exists():
        return

    if path.resolve() == Path.cwd().resolve():
        try:
            logger.warning(
                "Skipping cleanup for %s because it matches current working directory: %s",
                label,
                path,
            )
        except Exception:
            logger.warning(
                "Skipping cleanup for %s because it matches current working directory",
                label,
            )
        return

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        logger.info("Cleaned up %s: %s", label, path)
    except Exception as e:
        logger.warning("Failed to cleanup %s %s: %s", label, path, e)


def _pdf_path_diagnostics(pdf_path: Path | None) -> str:
    if pdf_path is None:
        return "pdf_path=<missing>"

    exists = pdf_path.exists()
    if not exists:
        return f"pdf_path={pdf_path} (missing)"

    try:
        stat = pdf_path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
        return (
            f"pdf_path={pdf_path} (exists, size={stat.st_size} bytes, mtime={modified})"
        )
    except Exception as e:
        return f"pdf_path={pdf_path} (exists, stat_failed={e})"


def _doc_context(doc: RenderedDocument) -> str:
    return (
        f"doc=({doc.doc.path}, title={doc.doc.title!r}, change={doc.doc.change_type.name}, "
        f"repo={doc.repo_name})"
    )


def _doc_key(doc: RenderedDocument) -> tuple[str, str]:
    return doc.repo_name, doc.doc.path


def _normalize_repo_relative_parts(path: str) -> list[str]:
    return [part for part in Path(path).parts if part not in {"", ".", ".."}]


def _is_anthropic_1p_folder(folder_name: str) -> bool:
    return folder_name.strip().lower() == "anthropic 1p"


def _build_onedrive_pdf_path(doc: RenderedDocument) -> str:
    repo_name = doc.repo_name.strip() if doc.repo_name else "unknown"
    repo_name = repo_name or "unknown"
    source_parts = _normalize_repo_relative_parts(doc.doc.path)

    file_name = ""
    if doc.pdf_path is not None and doc.pdf_path.name:
        file_name = doc.pdf_path.name

    if not file_name:
        source_stem = Path(doc.doc.path).stem
        if not source_stem:
            source_stem = "document"
        file_name = f"{source_stem}.pdf"

    if repo_name.lower() == "courses":
        source_parts = [
            part for part in source_parts if not _is_anthropic_1p_folder(part)
        ]
        if source_parts:
            file_name = f"{Path(source_parts[-1]).stem}.pdf"

    parent_parts = source_parts[:-1] if source_parts else []

    return "/".join([repo_name, *parent_parts, file_name])


def _build_onedrive_version_path(repo_name: str, version_file_name: str) -> str:
    clean_repo_name = repo_name.strip() if repo_name else "unknown"
    clean_version_name = Path(version_file_name).name
    return f"{clean_repo_name}/{clean_version_name}"


async def _upload_doc(
    doc: RenderedDocument,
    config: Config,
    logger: logging.Logger,
) -> tuple[RenderedDocument, str]:
    pdf_path = doc.pdf_path
    if doc.error:
        raise Exception(f"Cannot upload failed render. {_doc_context(doc)}")
    if pdf_path is None:
        raise Exception(
            f"Cannot upload document without generated PDF path. {_doc_context(doc)}"
        )
    if not pdf_path.is_file():
        raise Exception(
            f"PDF file not available for upload. {_doc_context(doc)}; "
            f"{_pdf_path_diagnostics(pdf_path)}"
        )

    onedrive_path = _build_onedrive_pdf_path(doc)

    try:
        item_id = await asyncio.to_thread(
            _run_async_in_thread,
            upload_pdf_to_onedrive,
            pdf_path,
            onedrive_path,
            config,
            logger,
        )
    except Exception as e:
        raise Exception(
            f"Upload raised exception for {_doc_context(doc)}. {_pdf_path_diagnostics(pdf_path)} "
            f"Error: {type(e).__name__}: {e}"
        ) from e

    if not item_id:
        raise Exception(
            f"Failed to upload {_doc_context(doc)} to OneDrive path {onedrive_path!r}. "
            f"upload_pdf_to_onedrive returned no item id; "
            f"this usually indicates an authentication or permission issue. "
            f"Check Azure app consent for Files.ReadWrite and review previous "
            f"AADSTS error logs. {_pdf_path_diagnostics(pdf_path)}"
        )

    return doc, item_id


async def _create_link_for_uploaded_doc(
    uploaded_doc: tuple[RenderedDocument, str],
    config: Config,
    logger: logging.Logger,
) -> tuple[RenderedDocument, str, str]:
    doc, item_id = uploaded_doc

    logger.info("Creating sharing link for %s", _doc_context(doc))

    try:
        link = await asyncio.to_thread(
            _run_async_in_thread,
            create_sharing_link,
            item_id,
            config,
            logger,
        )
    except Exception as e:
        raise Exception(
            f"Failed to create sharing link for {_doc_context(doc)}; "
            f"item_id={item_id!r}; Error: {type(e).__name__}: {e}"
        ) from e

    if not link:
        raise Exception(
            f"Failed to create sharing link for {_doc_context(doc)}; "
            f"item_id={item_id!r}. create_sharing_link returned empty value."
        )

    return doc, item_id, link


async def _upload_docs_concurrently(
    docs: list[RenderedDocument],
    config: Config,
    logger: logging.Logger,
    max_concurrency: int,
) -> tuple[list[tuple[RenderedDocument, str, str]], list[str]]:
    logger.info(
        "Starting concurrent PDF upload for %s docs (max_concurrency=%s)",
        len(docs),
        max_concurrency,
    )

    upload_semaphore = asyncio.Semaphore(max_concurrency)

    async def _upload_single(
        doc: RenderedDocument,
    ) -> tuple[RenderedDocument, str]:
        async with upload_semaphore:
            return await _upload_doc(doc, config, logger)

    tasks = [_upload_single(doc) for doc in docs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    uploaded_docs: list[tuple[RenderedDocument, str]] = []
    errors: list[str] = []
    for result in results:
        if isinstance(result, Exception):
            errors.append(f"{type(result).__name__}: {result}")
        else:
            uploaded_docs.append(result)

    uploaded_pdf_item_ids = [item_id for _, item_id in uploaded_docs]

    if errors:
        first_error = errors[0]
        raise _UploadLinkStageError(
            f"One or more PDF uploads failed ({len(errors)} errors). "
            f"First error: {first_error}",
            uploaded_pdf_item_ids,
        )

    logger.info(
        "Starting concurrent sharing link creation for %s uploaded PDFs (max_concurrency=%s)",
        len(uploaded_docs),
        max_concurrency,
    )

    link_semaphore = asyncio.Semaphore(max_concurrency)

    async def _create_link_single(
        uploaded_doc: tuple[RenderedDocument, str],
    ) -> tuple[RenderedDocument, str, str]:
        async with link_semaphore:
            return await _create_link_for_uploaded_doc(uploaded_doc, config, logger)

    link_tasks = [_create_link_single(uploaded_doc) for uploaded_doc in uploaded_docs]
    link_results = await asyncio.gather(*link_tasks, return_exceptions=True)

    uploaded_items: list[tuple[RenderedDocument, str, str]] = []
    link_errors: list[str] = []
    for result in link_results:
        if isinstance(result, Exception):
            link_errors.append(f"{type(result).__name__}: {result}")
        else:
            uploaded_items.append(result)

    if link_errors:
        first_error = link_errors[0]
        raise _UploadLinkStageError(
            f"One or more sharing link creations failed ({len(link_errors)} errors). "
            f"First error: {first_error}",
            uploaded_pdf_item_ids,
        )

    logger.info("Created sharing links for %s uploaded PDFs", len(uploaded_items))

    return uploaded_items, uploaded_pdf_item_ids


async def run_daemon(config: Config, logger: logging.Logger) -> None:
    logger.info("Starting Anthropic Readings Daemon")
    begin_graph_run()

    captured_error_messages: list[str] = []
    error_handler = _ErrorCollector(captured_error_messages)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(error_handler)

    all_rendered: list[RenderedDocument] = []
    all_updated_docs: dict[str, dict[str, TrackedDocument]] = {}
    repo_version_files: dict[str, str] = {}
    uploaded_pdf_item_ids: list[str] = []
    uploaded_version_ids: list[str] = []
    uploaded_items: list[tuple[RenderedDocument, str, str]] = []
    uploaded_doc_keys: set[tuple[str, str]] = set()
    upload_stage_errors: list[str] = []
    output_dir = Path(config.output_dir)
    repo_paths = [Path(repo_config.local_path) for repo_config in config.repos]

    try:
        should_upload = bool(config.email.sender and config.email.recipients)
        upload_concurrency = _concurrency_limit(config.daemon.upload_concurrency)
        upload_semaphore = asyncio.Semaphore(upload_concurrency)
        link_semaphore = asyncio.Semaphore(upload_concurrency)

        async def _upload_rendered_doc(rendered_doc: RenderedDocument) -> None:
            if not should_upload:
                return

            if rendered_doc.error:
                return

            if rendered_doc.doc.change_type not in {ChangeType.NEW, ChangeType.CHANGED}:
                return

            rendered_key = _doc_key(rendered_doc)
            if rendered_key in uploaded_doc_keys:
                return

            async with upload_semaphore:
                try:
                    uploaded_doc, item_id = await _upload_doc(
                        rendered_doc,
                        config,
                        logger,
                    )
                except Exception as e:
                    upload_stage_errors.append(f"{type(e).__name__}: {e}")
                    return

            async with link_semaphore:
                try:
                    uploaded_doc, item_id, link = await _create_link_for_uploaded_doc(
                        (uploaded_doc, item_id),
                        config,
                        logger,
                    )
                except Exception as e:
                    upload_stage_errors.append(f"{type(e).__name__}: {e}")
                    return

                uploaded_items.append((uploaded_doc, item_id, link))
                uploaded_pdf_item_ids.append(item_id)
                uploaded_doc_keys.add(rendered_key)

        if should_upload:
            logger.info(
                "Streaming render->upload->link pipeline enabled (upload_concurrency=%s, link_concurrency=%s)",
                upload_concurrency,
                upload_concurrency,
            )

        for repo_config in config.repos:
            rendered, updated_docs = await process_repo(
                repo_config,
                config,
                logger,
                on_rendered=_upload_rendered_doc if should_upload else None,
            )
            all_rendered.extend(rendered)
            all_updated_docs[repo_config.name] = updated_docs
            repo_version_files[repo_config.name] = Path(repo_config.version_file).name

        if not all_rendered:
            logger.info("No changes detected, skipping email")
            logger.info("Daemon run completed successfully")
            return

        if should_upload:
            new_docs = [d for d in all_rendered if d.doc.change_type == ChangeType.NEW]
            changed_docs = [
                d for d in all_rendered if d.doc.change_type == ChangeType.CHANGED
            ]
            all_docs = new_docs + changed_docs

            subject = f"{config.email.subject_prefix} New Published Readings"

            remaining_docs_to_upload = [
                doc
                for doc in all_docs
                if not doc.error and _doc_key(doc) not in uploaded_doc_keys
            ]

            if remaining_docs_to_upload:
                logger.info(
                    "Uploading %s docs after render phase fallback",
                    len(remaining_docs_to_upload),
                )
                try:
                    fallback_items, fallback_ids = await _upload_docs_concurrently(
                        remaining_docs_to_upload,
                        config,
                        logger,
                        upload_concurrency,
                    )
                except _UploadLinkStageError as e:
                    uploaded_pdf_item_ids.extend(e.uploaded_pdf_item_ids)
                    upload_stage_errors.append(str(e))
                else:
                    uploaded_items.extend(fallback_items)
                    uploaded_pdf_item_ids.extend(fallback_ids)
                    for uploaded_doc, _item_id, _link in fallback_items:
                        uploaded_doc_keys.add(_doc_key(uploaded_doc))

            if upload_stage_errors:
                first_error = upload_stage_errors[0]
                raise _UploadLinkStageError(
                    f"One or more upload/link operations failed ({len(upload_stage_errors)} errors). "
                    f"First error: {first_error}",
                    list(dict.fromkeys(uploaded_pdf_item_ids)),
                )

            body = _build_email_body(all_rendered, uploaded_items)
            email_sent = await send_email_with_links(
                subject, body, config.email.recipients, config, logger
            )
            if not email_sent:
                raise Exception("Failed to send update email")

            for repo_name, version_file_name in repo_version_files.items():
                updated_docs = all_updated_docs.get(repo_name, {})
                if updated_docs:
                    onedrive_version_path = _build_onedrive_version_path(
                        repo_name, version_file_name
                    )
                    version_id = await upload_version_to_onedrive(
                        version_file_name,
                        updated_docs,
                        config,
                        logger,
                        onedrive_path=onedrive_version_path,
                    )
                    if not version_id:
                        raise Exception(
                            f"Failed to upload version file {version_file_name} for repo {repo_name} "
                            f"to OneDrive path {onedrive_version_path!r}. "
                            f"Expected OneDrive item id but upload_version_to_onedrive returned none. "
                            f"Tracked docs: {len(updated_docs)}"
                        )
                    uploaded_version_ids.append(version_id)

            logger.info("Daemon run completed successfully")
    except Exception as e:
        if isinstance(e, _UploadLinkStageError):
            uploaded_pdf_item_ids = list(
                dict.fromkeys([*uploaded_pdf_item_ids, *e.uploaded_pdf_item_ids])
            )

        rollback_failures = await _rollback_uploaded_items(
            config,
            logger,
            uploaded_pdf_item_ids,
            uploaded_version_ids,
        )

        error_msg = f"Error updating Anthropic Readings: {type(e).__name__}: {e}"
        if rollback_failures:
            rollback_text = "\n".join(f"- {failure}" for failure in rollback_failures)
            error_msg = f"{error_msg}\n\nRollback cleanup failures:\n{rollback_text}"
        error_msg = f"{error_msg}\n\nCaptured Error Log Entries:\n{_build_error_context(captured_error_messages)}"
        logger.error(error_msg)
        await send_error_email(error_msg, config, logger)
        raise e

    finally:
        logger.removeHandler(error_handler)
        end_graph_run()
        _cleanup_tree(output_dir, logger, "output tree")
        for repo_path in repo_paths:
            _cleanup_tree(repo_path, logger, "repo tree")
