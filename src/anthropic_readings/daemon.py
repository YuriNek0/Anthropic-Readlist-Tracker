#!/usr/bin/env python3
"""
Anthropic Readings Daemon

Refactored into small modules for improved readability and testability.
This module remains the stable entrypoint and backward-compatible export surface.
"""

from __future__ import annotations

from .changes import compute_file_hash, detect_changes, parse_date
from .cli import main
from .config import (
    DEFAULT_CONFIG_PATHS,
    Config,
    DaemonConfig,
    DATE_FORMATS,
    EmailConfig,
    RepoConfig,
    AzureConfig,
    UserConfig,
)
from .discovery import discover_courses, discover_cookbooks
from .mailer import send_email_with_links, send_error_email
from .graph import (
    MAIL_SCOPES,
    ONEDRIVE_SCOPES,
    create_sharing_link,
    delete_from_appfolder,
    download_version_from_onedrive,
    get_graph_client,
    parse_version_file_content,
    upload_pdf_to_onedrive,
    upload_to_appfolder,
    upload_version_to_onedrive,
)
from .logging_setup import setup_logging
from .models import ChangeType, RenderedDocument, TrackedDocument
from .orchestrator import group_by_top_folder, process_repo, run_daemon
from .rendering import (
    build_render_output_path,
    render_document_to_pdf,
    slugify_title,
)
from .repository import git_clone, git_pull, ensure_repo_available


__all__ = [
    "Config",
    "DEFAULT_CONFIG_PATHS",
    "DATE_FORMATS",
    "DaemonConfig",
    "EmailConfig",
    "AzureConfig",
    "UserConfig",
    "RepoConfig",
    "ChangeType",
    "TrackedDocument",
    "RenderedDocument",
    "compute_file_hash",
    "parse_date",
    "detect_changes",
    "discover_cookbooks",
    "discover_courses",
    "build_render_output_path",
    "slugify_title",
    "render_document_to_pdf",
    "git_clone",
    "git_pull",
    "ensure_repo_available",
    "group_by_top_folder",
    "process_repo",
    "run_daemon",
    "send_email_with_links",
    "send_error_email",
    "setup_logging",
    "main",
    "ONEDRIVE_SCOPES",
    "MAIL_SCOPES",
    "get_graph_client",
    "upload_to_appfolder",
    "upload_pdf_to_onedrive",
    "create_sharing_link",
    "upload_version_to_onedrive",
    "download_version_from_onedrive",
    "delete_from_appfolder",
    "parse_version_file_content",
]


if __name__ == "__main__":
    main()
