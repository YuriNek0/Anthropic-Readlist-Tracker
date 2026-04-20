#!/usr/bin/env python3
"""
Anthropic Readings Daemon

A daemon that automatically:
1. Checks for updates in anthropics/claude-cookbooks and anthropics/courses
2. Renders new/changed documents to PDF
3. Sends email notifications with PDF attachments

Usage:
    python daemon.py --once              # Run once (for systemd oneshot or testing)
    python daemon.py --once --config /path/to/config.yaml  # With custom config
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import tempfile
import shutil
import subprocess
import sys
import zipfile
import yaml
import schedule
from slugify import slugify
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from azure.core.credentials import AccessToken
from msgraph.generated.models.message import Message
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.file_attachment import FileAttachment
from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
    SendMailPostRequestBody,
)


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path(__file__).parent / "config.yaml",
    Path(__file__).parent.parent / "config.yaml",
]


@dataclass
class AzureConfig:
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""


@dataclass
class UserConfig:
    email: str = ""
    password: str = ""


@dataclass
class EmailConfig:
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    subject_prefix: str = "[Anthropic Readings]"


@dataclass
class RepoConfig:
    name: str = ""
    url: str = ""
    local_path: str = ""
    version_file: str = ""
    discover_patterns: list[str] = field(default_factory=list)
    manifest_file: Optional[str] = None


@dataclass
class DaemonConfig:
    log_level: str = "INFO"
    random_delay_max_hours: int = 1


class RawAccessTokenProvider:
    def __init__(self, token: str):
        self.token = token

    def get_token(self, *scopes, **kwargs):
        # Returns the existing token; note that it won't be auto-refreshed
        return AccessToken(self.token, expires_on=3600)


@dataclass
class Config:
    azure: AzureConfig = field(default_factory=AzureConfig)
    user: UserConfig = field(default_factory=UserConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    repos: list[RepoConfig] = field(default_factory=list)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    output_dir: str = "outputs"
    is_production: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        azure_cfg = AzureConfig(**data.get("azure", {}))
        user_cfg = UserConfig(**data.get("user", {}))
        email_cfg = EmailConfig(**data.get("email", {}))

        repos = []
        for repo_data in data.get("repos", []):
            repos.append(RepoConfig(**repo_data))

        daemon_cfg = DaemonConfig(**data.get("daemon", {}))

        return cls(
            azure=azure_cfg,
            user=user_cfg,
            email=email_cfg,
            repos=repos,
            daemon=daemon_cfg,
            output_dir=data.get("paths", {}).get("output_dir", "outputs"),
            is_production=data.get("is_production", False),
        )

    @classmethod
    def from_defaults(cls) -> "Config":
        return cls(
            azure=AzureConfig(),
            user=UserConfig(),
            email=EmailConfig(),
            repos=[
                RepoConfig(
                    name="cookbooks",
                    url="https://github.com/anthropics/claude-cookbooks.git",
                    local_path="claude-cookbooks",
                    version_file="cookbook-version.json",
                    discover_patterns=["*.ipynb", "*.md"],
                    manifest_file="registry.yaml",
                ),
                RepoConfig(
                    name="courses",
                    url="https://github.com/anthropics/courses.git",
                    local_path="claude-courses",
                    version_file="courses-version.json",
                    discover_patterns=["*.ipynb", "*.md"],
                    manifest_file=None,
                ),
            ],
            daemon=DaemonConfig(),
            output_dir="outputs",
            is_production=False,
        )


# =============================================================================
# Data Models
# =============================================================================


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


# =============================================================================
# Logging Setup
# =============================================================================


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("anthropic-daemon")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


# =============================================================================
# Git Operations
# =============================================================================


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


# =============================================================================
# Document Discovery
# =============================================================================


def discover_cookbooks(
    repo_path: Path, manifest_file: str, logger: logging.Logger
) -> dict[str, TrackedDocument]:
    manifest_path = repo_path / manifest_file
    if not manifest_path.is_file():
        logger.error(f"Manifest file not found: {manifest_path}")
        return {}

    with open(manifest_path, "r") as f:
        registry = yaml.safe_load(f)

    documents = {}
    for item in registry:
        path = item.get("path", "")
        title = item.get("title", "")
        date = item.get("date", "")

        if not path or not title:
            continue

        doc = TrackedDocument(path=path, title=title, date=date)
        documents[path] = doc

    logger.info(f"Discovered {len(documents)} cookbooks from registry")
    return documents


def discover_courses(
    repo_path: Path, patterns: list[str], logger: logging.Logger
) -> dict[str, TrackedDocument]:
    documents = {}

    for pattern in patterns:
        if pattern == "*.ipynb":
            file_pattern = "*.ipynb"
        elif pattern == "*.md":
            file_pattern = "*.md"
        else:
            file_pattern = pattern

        for filepath in repo_path.rglob(file_pattern):
            if "AmazonBedrock" in str(filepath):
                continue

            rel_path = filepath.relative_to(repo_path)
            path_str = str(rel_path)

            title = rel_path.stem.replace("-", " ").replace("_", " ").title()

            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                date = mtime.strftime("%Y-%m-%d")
            except Exception:
                date = datetime.now().strftime("%Y-%m-%d")

            doc = TrackedDocument(path=path_str, title=title, date=date)
            documents[path_str] = doc

    logger.info(f"Discovered {len(documents)} course documents")
    return documents


def compute_file_hash(filepath: Path) -> Optional[str]:
    try:
        with open(filepath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


# =============================================================================
# Version Tracking
# =============================================================================


def load_version_file(path: Path) -> dict[str, TrackedDocument]:
    if not path.is_file():
        return {}

    try:
        with open(path, "r") as f:
            data = json.load(f)

        documents = {}
        for item in data:
            doc = TrackedDocument(
                path=item.get("path", ""),
                title=item.get("title", ""),
                date=item.get("date", ""),
                content_hash=item.get("content_hash"),
            )
            documents[doc.path] = doc

        return documents
    except (json.JSONDecodeError, KeyError) as e:
        return {}


def save_version_file(path: Path, documents: dict[str, TrackedDocument]) -> None:
    data = []
    for doc in documents.values():
        data.append(
            {
                "path": doc.path,
                "title": doc.title,
                "date": doc.date,
                "content_hash": doc.content_hash,
            }
        )

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# =============================================================================
# Change Detection
# =============================================================================


def detect_changes(
    current_docs: dict[str, TrackedDocument],
    previous_docs: dict[str, TrackedDocument],
    repo_path: Path,
    logger: logging.Logger,
) -> list[TrackedDocument]:
    changed = []

    for path, doc in current_docs.items():
        if path not in previous_docs:
            doc.change_type = ChangeType.NEW
            changed.append(doc)
            logger.info(f"NEW document: {path}")
            continue

        prev_doc = previous_docs[path]

        curr_date = parse_date(doc.date)
        prev_date = parse_date(prev_doc.date)

        if curr_date != prev_date:
            doc.change_type = ChangeType.CHANGED
            changed.append(doc)
            logger.info(f"CHANGED document: {path} ({prev_doc.date} -> {doc.date})")
            continue

    return changed


def parse_date(date_str: str) -> Optional[datetime]:
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


# =============================================================================
# PDF Rendering
# =============================================================================


def slugify_title(title: str) -> str:
    return slugify(title)


def render_document_to_pdf(
    doc: TrackedDocument,
    repo_path: Path,
    output_dir: Path,
    logger: logging.Logger,
    repo_name: str = "",
) -> RenderedDocument:
    source_path = repo_path / doc.path

    if not source_path.is_file():
        logger.warning(f"Source file not found: {source_path}")
        return RenderedDocument(
            doc=doc, repo_name=repo_name, error=f"Source file not found: {source_path}"
        )

    try:
        mtime = datetime.fromtimestamp(source_path.stat().st_mtime)
        date_str = mtime.strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.now().strftime("%Y-%m-%d")

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="anthropic-render-"))
        temp_source = temp_dir / source_path.name
        shutil.copy2(source_path, temp_source)

        if source_path.suffix == ".ipynb":
            import json

            nb_path = temp_source
            with open(nb_path, "r") as f:
                nb_data = json.load(f)
            if "metadata" in nb_data and "widgets" in nb_data["metadata"]:
                del nb_data["metadata"]["widgets"]
                with open(nb_path, "w") as f:
                    json.dump(nb_data, f)
                logger.debug(f"Removed .metadata.widgets from {doc.path}")

        doc_dir = output_dir / date_str
        doc_dir.mkdir(parents=True, exist_ok=True)

        name = f"{slugify_title(doc.title)}.pdf"

        if source_path.suffix == ".md":
            cmd = [
                "pandoc",
                str(temp_source),
                "-o",
                str(doc_dir / name),
                "--pdf-engine=weasyprint",
            ]
            logger.info(f"Rendering {source_path} to PDF via pandoc")
        else:
            cmd = [
                "jupyter",
                "nbconvert",
                "--to=webpdf",
                "--allow-chromium-download",
                f"--output={name}",
                f"--output-dir={doc_dir}",
                str(temp_source),
            ]
            logger.info(f"Rendering {source_path} to PDF via nbconvert")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            error_msg = (
                result.stderr.split("\n")[-3] if result.stderr else "Unknown error"
            )
            logger.error(f"PDF rendering failed for {doc.path}: {error_msg}")
            return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)

        pdf_path = doc_dir / name
        if pdf_path.is_file():
            logger.info(f"PDF created: {pdf_path}")
            return RenderedDocument(doc=doc, repo_name=repo_name, pdf_path=pdf_path)
        else:
            error_msg = "PDF file not created"
            logger.error(error_msg)
            return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)

    except subprocess.TimeoutExpired:
        error_msg = "PDF rendering timed out"
        logger.error(f"{doc.path}: {error_msg}")
        return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"PDF rendering failed for {doc.path}: {error_msg}")
        return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)
    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir)
            logger.debug(f"Cleaned up temp dir: {temp_dir}")


# =============================================================================
# Email Operations (Microsoft Graph API)
# =============================================================================


async def send_email_with_attachments(
    subject: str,
    body: str,
    recipients: list[str],
    attachment_paths: list[Path],
    config: Config,
    logger: logging.Logger,
) -> bool:
    if (
        not config.azure.tenant_id
        or not config.azure.client_id
        or not config.azure.client_secret
    ):
        logger.warning(
            "Azure credentials not configured (need tenant_id, client_id), skipping email"
        )
        return False

    try:
        import msal
        from msgraph import GraphServiceClient
    except ImportError:
        logger.error(
            "MS Graph dependencies not installed. Run: uv pip install msgraph-sdk msal"
        )
        return False

    try:
        app = msal.ConfidentialClientApplication(
            client_id=config.azure.client_id,
            client_credential=config.azure.client_secret,
            authority=f"https://login.microsoftonline.com/{config.azure.tenant_id}",
        )

        if config.user.email and config.user.password:
            result = app.acquire_token_by_username_password(
                username=config.user.email,
                password=config.user.password,
                scopes=["User.Read", "Mail.Send"],
            )
        else:
            flow = app.acquire_token_device_code(
                scopes=["User.Read", "Mail.Send"],
                account=config.user.email if config.user.email else None,
            )
            if "message" in flow:
                logger.info(f"Device code: {flow['message']}")
            result = app.acquire_token_by_device_code(flow)

        if "access_token" not in result:
            error_desc = result.get("error_description", "Unknown error")
            logger.error(f"Authentication failed: {error_desc}")
            return False

        client = GraphServiceClient(
            credentials=RawAccessTokenProvider(result["access_token"])
        )

        attachments = []
        if attachment_paths:
            for path in attachment_paths:
                if path.is_file():
                    with open(path, "rb") as f:
                        content = f.read()
                    attachments.append(
                        FileAttachment(
                            odata_type="#microsoft.graph.fileAttachment",
                            name=path.name,
                            content_bytes=content,
                            content_type=f"application/{path.name.split('.')[-1]}",
                        )
                    )

        message = Message(
            subject=subject,
            body=ItemBody(content_type=BodyType.Html, content=body),
            to_recipients=[
                Recipient(email_address=EmailAddress(address=r)) for r in recipients
            ],
            attachments=attachments,
        )

        request_body = SendMailPostRequestBody(message=message, save_to_sent_items=True)
        result = await client.me.send_mail.post(request_body)

        logger.info(f"Email sent to {recipients}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise e
        return False


# =============================================================================
# Course Zip Helpers
# =============================================================================


def group_by_top_folder(
    docs: list[RenderedDocument],
) -> dict[str, list[RenderedDocument]]:
    folders: dict[str, list[RenderedDocument]] = {}
    for doc in docs:
        parts = Path(doc.doc.path).parts
        if parts:
            top_folder = parts[0]
            if top_folder not in folders:
                folders[top_folder] = []
            folders[top_folder].append(doc)
    return folders


def create_course_zip(
    folder_name: str,
    docs: list[RenderedDocument],
    output_dir: Path,
    logger: logging.Logger,
) -> Optional[Path]:
    if not docs:
        return None

    valid_docs = [d for d in docs if d.pdf_path and d.pdf_path.is_file()]
    if not valid_docs:
        return None

    zip_name = f"{slugify(folder_name)}.zip"
    zip_path = output_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in valid_docs:
            if doc.pdf_path and doc.pdf_path.is_file():
                zf.write(doc.pdf_path, doc.pdf_path.name)
                logger.info(f"Added {doc.pdf_path.name} to {zip_name}")

    logger.info(f"Created zip: {zip_path}")
    return zip_path


# =============================================================================
# Error Reporting
# =============================================================================


async def send_error_email(
    error_message: str,
    config: Config,
    logger: logging.Logger,
) -> None:
    if not config.is_production:
        logger.debug("Not in production mode, skipping error email")
        return

    if not config.user.email:
        logger.warning("User email not configured, cannot send error email")
        return

    if not config.email.sender or not config.email.recipients:
        logger.warning("Email not configured, cannot send error email")
        return

    body = f"""
    <h2>Anthropic Readings Daemon Error</h2>
    <p>The daemon encountered an error:</p>
    <pre>{error_message}</pre>
    <p>Please check the logs for more details.</p>
    """

    subject = "Anthropic Readings Daemon Error"
    await send_email_with_attachments(
        subject, body, config.email.recipients, [], config, logger
    )


# =============================================================================
# Main Processing
# =============================================================================


async def process_repo(
    repo_config: RepoConfig,
    config: Config,
    logger: logging.Logger,
) -> list[RenderedDocument]:
    repo_path = Path(repo_config.local_path)
    version_path = Path(repo_config.version_file)

    if not repo_path.is_dir():
        if not git_clone(repo_config.url, repo_path, logger):
            return []
    else:
        if not git_pull(repo_path, logger):
            return []

    if repo_config.manifest_file:
        current_docs = discover_cookbooks(repo_path, repo_config.manifest_file, logger)
    else:
        current_docs = discover_courses(
            repo_path, repo_config.discover_patterns, logger
        )

    if not current_docs:
        logger.warning(f"No documents discovered for {repo_config.name}")
        return []

    previous_docs = load_version_file(version_path)

    changed_docs = detect_changes(current_docs, previous_docs, repo_path, logger)

    if not changed_docs:
        logger.info(f"No changes detected for {repo_config.name}")
        save_version_file(version_path, current_docs)
        return []

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered = []
    for doc in changed_docs:
        rendered_doc = render_document_to_pdf(
            doc, repo_path, output_dir, logger, repo_config.name
        )
        rendered.append(rendered_doc)

    for doc in current_docs.values():
        doc.content_hash = compute_file_hash(repo_path / doc.path)

    save_version_file(version_path, current_docs)

    return rendered


async def run_daemon(config: Config, logger: logging.Logger) -> None:
    logger.info("Starting Anthropic Readings Daemon")

    all_rendered = []

    try:
        for repo_config in config.repos:
            rendered = await process_repo(repo_config, config, logger)
            all_rendered.extend(rendered)

        new_docs = [d for d in all_rendered if d.doc.change_type == ChangeType.NEW]
        changed_docs = [
            d for d in all_rendered if d.doc.change_type == ChangeType.CHANGED
        ]

        if not new_docs and not changed_docs:
            logger.info("No changes detected, skipping email")
            logger.info("Daemon run completed successfully")
            return

        if config.email.sender and config.email.recipients:
            subject = f"{config.email.subject_prefix} New Published Readings"
            body_lines = [
                "<h2>Anthropic Readings Update</h2>",
            ]
            attachment_paths = []

            output_dir = Path(config.output_dir)

            all_docs = new_docs + changed_docs
            cookbook_docs = [d for d in all_docs if d.repo_name == "cookbooks"]
            course_docs = [d for d in all_docs if d.repo_name == "courses"]

            failed_docs = [d for d in all_docs if d.error]

            if cookbook_docs:
                body_lines.append("<h3>Cookbooks</h3>")
                body_lines.append("<ul>")
                for doc in cookbook_docs:
                    change_label = (
                        "New" if doc.doc.change_type == ChangeType.NEW else "Updated"
                    )
                    if doc.error:
                        body_lines.append(
                            f"<li><strong>{doc.doc.title}</strong> ({change_label}) - ERROR: {doc.error}</li>"
                        )
                    else:
                        body_lines.append(
                            f"<li><strong>{doc.doc.title}</strong> ({change_label})</li>"
                        )
                        if doc.pdf_path:
                            attachment_paths.append(doc.pdf_path)
                body_lines.append("</ul>")

            if course_docs:
                body_lines.append("<h3>Anthropic Courses</h3>")
                folders = group_by_top_folder(course_docs)
                for folder_name, folder_docs in folders.items():
                    body_lines.append(f"<h4>{folder_name}</h4>")
                    body_lines.append("<ul>")
                    for doc in folder_docs:
                        change_label = (
                            "New"
                            if doc.doc.change_type == ChangeType.NEW
                            else "Updated"
                        )
                        if doc.error:
                            body_lines.append(
                                f"<li><strong>{doc.doc.title}</strong> ({change_label}) - ERROR: {doc.error}</li>"
                            )
                        else:
                            body_lines.append(
                                f"<li><strong>{doc.doc.title}</strong> ({change_label})</li>"
                            )
                    body_lines.append("</ul>")

                    successful = [d for d in folder_docs if not d.error]
                    if successful:
                        try:
                            zip_path = create_course_zip(
                                folder_name, successful, output_dir, logger
                            )
                            attachment_paths.append(zip_path)
                        except Exception as e:
                            body_lines.append("<h3>Failed to create ZIP</h3>")
                            body_lines.append(f"<p>{str(e)}</p>")

            if failed_docs:
                body_lines.append("<h3>Failed to render:</h3>")
                body_lines.append("<ul>")
                for doc in failed_docs:
                    body_lines.append(
                        f"<li><strong>{doc.doc.title}</strong> ({doc.doc.path}): {doc.error}</li>"
                    )
                body_lines.append("</ul>")

            body = "\n".join(body_lines)

            await send_email_with_attachments(
                subject, body, config.email.recipients, attachment_paths, config, logger
            )

        logger.info("Daemon run completed successfully")

    except Exception as e:
        error_msg = f"Error updating Anthropic Readlist: {e}"
        logger.error(error_msg)
        await send_error_email(error_msg, config, logger)
        raise e


# =============================================================================
# CLI Entry Point
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description="Anthropic Readings Daemon")
    parser.add_argument(
        "--once", action="store_true", help="Run once and exit (for systemd)"
    )
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument(
        "--check", action="store_true", help="Check configuration and exit"
    )
    args = parser.parse_args()

    config_path = None
    if args.config:
        config_path = Path(args.config)
    else:
        for p in DEFAULT_CONFIG_PATHS:
            if p.is_file():
                config_path = p
                break

    if config_path and config_path.is_file():
        config = Config.from_yaml(config_path)
        logger = setup_logging(config.daemon.log_level)
        logger.info(f"Loaded config from {config_path}")
    else:
        config = Config.from_defaults()
        logger = setup_logging()
        if not config_path:
            logger.warning("No config file found, using defaults")
        else:
            logger.info("Using default configuration")

    if args.check:
        logger.info("Configuration check passed")
        logger.info(f"Production mode: {config.is_production}")
        logger.info(f"Repos: {[r.name for r in config.repos]}")
        logger.info(f"Output dir: {config.output_dir}")
        return

    if args.once:
        import asyncio

        asyncio.run(run_daemon(config, logger))
    else:
        if not schedule:
            logger.error("schedule library not installed. Run: uv pip install schedule")
            logger.info("Falling back to --once mode")
            import asyncio

            asyncio.run(run_daemon(config, logger))
            return

        logger.info("Starting daemon with daily schedule")
        schedule.repeat(lambda: asyncio.run(run_daemon(config, logger)))

        try:
            while True:
                schedule.run_pending()
                import time

                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Daemon stopped by user")


if __name__ == "__main__":
    main()
