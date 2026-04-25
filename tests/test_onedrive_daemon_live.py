#!/usr/bin/env python3
"""Live OneDrive flow tests for daemon orchestration."""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import anthropic_readings.orchestrator as orchestrator
import yaml
from unittest.mock import AsyncMock, patch

import anthropic_readings.daemon as daemon
import anthropic_readings.graph as graph
from anthropic_readings.logging_setup import setup_logging

sys.modules.setdefault("orchestrator", orchestrator)


class TestOneDriveFlowLive(unittest.IsolatedAsyncioTestCase):
    def _configure_debug_logger(self) -> logging.Logger:
        logger = setup_logging("DEBUG")
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
        return logger

    def _create_sample_test_repo(self, repo_root: Path) -> dict[str, str]:
        repo_root.mkdir(parents=True, exist_ok=True)

        markdown_first = repo_root / "lessons" / "intro.md"
        markdown_second = repo_root / "lessons" / "notes.md"
        notebook = repo_root / "lessons" / "demo.ipynb"
        markdown_first.parent.mkdir(parents=True, exist_ok=True)

        markdown_first.write_text("# Intro\n")
        markdown_second.write_text("# Notes\n")
        notebook.write_text(
            json.dumps(
                {
                    "cells": [],
                    "metadata": {
                        "kernelspec": {
                            "display_name": "Python 3",
                            "language": "python",
                            "name": "python3",
                        },
                        "language_info": {
                            "name": "python",
                            "version": "3.12",
                        },
                    },
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            )
        )

        return {
            "markdown_first": str(markdown_first),
            "markdown_second": str(markdown_second),
            "notebook": str(notebook),
        }

    def _fake_render(self):
        def _render(
            doc,
            repo_path,
            resolved_output_dir,
            _logger,
            repo_name="",
            _output_relpath_by_doc=None,
        ):
            try:
                date_str = datetime.fromtimestamp(
                    (repo_path / doc.path).stat().st_mtime
                ).strftime("%Y-%m-%d")
            except Exception:
                date_str = "2024-01-01"

            folder, file_name = daemon.build_render_output_path(
                resolved_output_dir,
                date_str,
                repo_name,
                doc,
            )
            pdf_path = folder / file_name
            pdf_path.write_text(f"rendered:{doc.path}:{repo_name}")
            return daemon.RenderedDocument(
                doc=doc,
                repo_name=repo_name,
                pdf_path=pdf_path,
            )

        return _render

    def _apply_test_credentials(
        self, config: daemon.Config, credentials_path: str
    ) -> None:
        credentials_path = Path(credentials_path)
        with credentials_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if not isinstance(data, dict):
            raise ValueError("OneDrive credential config must be a mapping")

        azure_data = data.get("azure", {})
        if isinstance(azure_data, dict):
            if azure_data.get("tenant_id") is not None:
                config.azure.tenant_id = str(azure_data["tenant_id"])
            if azure_data.get("client_id") is not None:
                config.azure.client_id = str(azure_data["client_id"])
            if azure_data.get("client_secret") is not None:
                config.azure.client_secret = str(azure_data["client_secret"])

        user_data = data.get("user", {})
        if isinstance(user_data, dict):
            if user_data.get("email") is not None:
                config.user.email = str(user_data["email"])
            if user_data.get("password") is not None:
                config.user.password = str(user_data["password"])

        email_data = data.get("email", {})
        if isinstance(email_data, dict):
            if email_data.get("sender") is not None:
                config.email.sender = str(email_data["sender"])
            if "recipients" in email_data:
                recipients = email_data["recipients"]
                if not isinstance(recipients, list):
                    raise ValueError(
                        "OneDrive credentials config field email.recipients must be a list"
                    )
                config.email.recipients = [str(item) for item in recipients]
            if "share_recipients" in email_data:
                share_recipients = email_data["share_recipients"]
                if not isinstance(share_recipients, list):
                    raise ValueError(
                        "OneDrive credentials config field email.share_recipients must be a list"
                    )
                config.email.share_recipients = [str(item) for item in share_recipients]
            if "share_domain_filter_enabled" in email_data:
                enabled = email_data["share_domain_filter_enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError(
                        "OneDrive credentials config field email.share_domain_filter_enabled must be a boolean"
                    )
                config.email.share_domain_filter_enabled = enabled
            if "share_domain" in email_data and email_data["share_domain"] is not None:
                config.email.share_domain = str(email_data["share_domain"])

    def _load_live_config(self, temp_path: Path) -> daemon.Config:
        credentials_path = os.getenv("ONEDRIVE_TEST_CREDENTIALS_PATH")
        if not credentials_path:
            self.skipTest("ONEDRIVE_TEST_CREDENTIALS_PATH is not set")

        config = daemon.Config.from_defaults()
        config.email.sender = ""
        config.email.recipients = []
        self._apply_test_credentials(config, credentials_path)

        if not config.azure.tenant_id:
            self.skipTest("azure.tenant_id is required for live test")
        if not config.azure.client_id:
            self.skipTest("azure.client_id is required for live test")
        if not config.azure.client_secret:
            self.skipTest("azure.client_secret is required for live test")

        if (
            config.azure.tenant_id.startswith("your-")
            or config.azure.client_id.startswith("your-")
            or config.azure.client_secret.startswith("your-")
        ):
            self.skipTest("Credentials file contains example placeholders")

        if not config.user.email:
            self.skipTest("user.email is required for live test")
        if not config.email.sender:
            self.skipTest("email.sender is required for live test")
        if not config.email.recipients:
            self.skipTest("email.recipients must contain at least one address")

        config.is_production = self._should_send_error_email()

        config.repos = [
            daemon.RepoConfig(
                name="courses",
                url="https://example.local/fake.git",
                local_path=str(temp_path / "sample-repo"),
                version_file="courses-version.json",
                discover_patterns=["*.ipynb", "*.md"],
                manifest_file=None,
            )
        ]
        config.output_dir = str(temp_path / "outputs")
        return config

    def _should_cleanup(self) -> bool:
        keep = os.getenv("ONEDRIVE_TEST_KEEP_REMOTE")
        return str(keep).lower() not in {"1", "true", "yes", "y"}

    def _should_send_error_email(self) -> bool:
        return os.getenv("ONEDRIVE_TEST_SEND_ERROR_EMAIL", "").lower() in {
            "1",
            "true",
            "yes",
            "y",
        }

    async def _cleanup_remote_items(
        self,
        item_ids: list[str | None],
        config: daemon.Config,
        logger,
    ) -> None:
        failures: list[str] = []
        for item_id in set(item_ids):
            if item_id:
                deleted = await orchestrator.delete_from_appfolder(
                    item_id,
                    config,
                    logger,
                    permanent=True,
                )
                if not deleted:
                    failures.append(f"item_id={item_id}")

        if failures:
            raise AssertionError(
                "Failed to permanently delete OneDrive items: " + ", ".join(failures)
            )

    async def _cleanup_remote_items_by_name(
        self,
        file_names: list[str],
        config: daemon.Config,
        logger,
    ) -> None:
        failures: list[str] = []
        for file_name in file_names:
            item_id = await graph._lookup_item_id_by_name(file_name, config, logger)
            if item_id:
                deleted = await orchestrator.delete_from_appfolder(
                    item_id,
                    config,
                    logger,
                    permanent=True,
                )
                if not deleted:
                    failures.append(f"path={file_name}, item_id={item_id}")

        if failures:
            raise AssertionError(
                "Failed to permanently delete OneDrive named items: "
                + ", ".join(failures)
            )

    async def _cleanup_remote_folders(
        self,
        folder_paths: list[str],
        config: daemon.Config,
        logger,
    ) -> None:
        failures: list[str] = []
        for folder_path in sorted(set(folder_paths), key=len, reverse=True):
            deleted = await orchestrator.delete_from_appfolder(
                folder_path,
                config,
                logger,
                permanent=True,
            )
            if not deleted:
                failures.append(folder_path)

        if failures:
            raise AssertionError(
                "Failed to permanently delete OneDrive folders: " + ", ".join(failures)
            )

    async def test_run_daemon_live_creates_remote_files_and_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "sample-repo"
            created = self._create_sample_test_repo(repo_root)
            config = self._load_live_config(temp_path)
            logger = self._configure_debug_logger()

            remote_item_ids: list[str | None] = []
            expected_pdf_names = [
                "courses/lessons/intro.pdf",
                "courses/lessons/notes.pdf",
                "courses/lessons/demo.pdf",
            ]
            expected_version_files = ["courses/courses-version.json"]
            expected_folder_paths = ["courses/lessons", "courses"]

            tracked_version: dict[str, daemon.TrackedDocument] = {}
            first_upload_paths: list[Path] = []
            first_upload_ids: list[str | None] = []
            first_links: list[str | None] = []
            first_version_ids: list[str | None] = []
            first_email_calls: list[tuple[str, str, list[str]]] = []

            second_upload_paths: list[Path] = []
            second_upload_ids: list[str | None] = []
            second_links: list[str | None] = []
            second_version_ids: list[str | None] = []
            second_email_calls: list[tuple[str, str, list[str]]] = []

            original_upload_pdf = orchestrator.upload_pdf_to_onedrive
            original_create_link = orchestrator.create_sharing_link
            original_send_email = orchestrator.send_email_with_links
            original_upload_version = orchestrator.upload_version_to_onedrive

            await self._cleanup_remote_items_by_name(
                expected_pdf_names + expected_version_files,
                config,
                logger,
            )
            await self._cleanup_remote_folders(
                expected_folder_paths,
                config,
                logger,
            )

            async def capture_upload_pdf(pdf_path, *args):
                item_id = await original_upload_pdf(pdf_path, *args)
                first_upload_paths.append(Path(pdf_path))
                first_upload_ids.append(item_id)
                remote_item_ids.append(item_id)
                return item_id

            async def capture_create_link(item_id, *args):
                link = await original_create_link(item_id, *args)
                first_links.append(link)
                return link

            async def capture_upload_version(
                version_file, documents, *_args, **_kwargs
            ):
                nonlocal tracked_version
                tracked_version = {
                    path: daemon.TrackedDocument(
                        path=doc.path,
                        title=doc.title,
                        date=doc.date,
                        content_hash=doc.content_hash,
                    )
                    for path, doc in documents.items()
                }
                item_id = await original_upload_version(
                    version_file,
                    documents,
                    *_args,
                    **_kwargs,
                )
                first_version_ids.append(item_id)
                remote_item_ids.append(item_id)
                return item_id

            async def capture_send_email(subject, body, recipients, *_args):
                first_email_calls.append((subject, body, recipients))
                return await original_send_email(subject, body, recipients, *_args)

            try:
                with (
                    patch("orchestrator.ensure_repo_available", return_value=True),
                    patch(
                        "orchestrator.download_version_from_onedrive",
                        AsyncMock(return_value=None),
                    ),
                    patch("orchestrator.render_document_to_pdf", self._fake_render()),
                    patch("orchestrator.upload_pdf_to_onedrive", capture_upload_pdf),
                    patch("orchestrator.create_sharing_link", capture_create_link),
                    patch(
                        "orchestrator.upload_version_to_onedrive",
                        capture_upload_version,
                    ),
                    patch("orchestrator.send_email_with_links", capture_send_email),
                ):
                    await daemon.run_daemon(config, logger)

                self.assertEqual(len(first_upload_paths), 3)
                self.assertEqual(len(first_upload_ids), 3)
                self.assertEqual(len(first_links), 3)
                self.assertEqual(len(first_version_ids), 1)
                self.assertEqual(len(first_email_calls), 1)
                self.assertTrue(
                    all(item and item.suffix == ".pdf" for item in first_upload_paths)
                )
                self.assertTrue(all(item is not None for item in first_upload_ids))
                self.assertTrue(
                    all(link and link.startswith("http") for link in first_links)
                )

                subject, body, recipients = first_email_calls[0]
                self.assertIn("New", subject)
                self.assertIn("Anthropic Readings Update", body)
                self.assertIn("Intro", body)
                self.assertIn("Notes", body)
                self.assertIn("Demo", body)
                self.assertEqual(recipients, config.email.recipients)

                updated_payload = tracked_version
                Path(created["markdown_first"]).parent.mkdir(
                    parents=True, exist_ok=True
                )
                Path(created["markdown_first"]).write_text("# Intro v2\n")

                async def capture_upload_pdf_second(pdf_path, *args):
                    item_id = await original_upload_pdf(pdf_path, *args)
                    second_upload_paths.append(Path(pdf_path))
                    second_upload_ids.append(item_id)
                    remote_item_ids.append(item_id)
                    return item_id

                async def capture_create_link_second(item_id, *args):
                    link = await original_create_link(item_id, *args)
                    second_links.append(link)
                    return link

                async def capture_upload_version_second(
                    version_file,
                    documents,
                    *_args,
                    **_kwargs,
                ):
                    item_id = await original_upload_version(
                        version_file,
                        documents,
                        *_args,
                        **_kwargs,
                    )
                    second_version_ids.append(item_id)
                    remote_item_ids.append(item_id)
                    return item_id

                async def capture_send_email_second(subject, body, recipients, *_args):
                    second_email_calls.append((subject, body, recipients))
                    return await original_send_email(subject, body, recipients, *_args)

                async def cached_version_payload(*_args, **_kwargs):
                    return updated_payload

                with (
                    patch("orchestrator.ensure_repo_available", return_value=True),
                    patch(
                        "orchestrator.download_version_from_onedrive",
                        AsyncMock(side_effect=cached_version_payload),
                    ),
                    patch("orchestrator.render_document_to_pdf", self._fake_render()),
                    patch(
                        "orchestrator.upload_pdf_to_onedrive", capture_upload_pdf_second
                    ),
                    patch(
                        "orchestrator.create_sharing_link", capture_create_link_second
                    ),
                    patch(
                        "orchestrator.upload_version_to_onedrive",
                        capture_upload_version_second,
                    ),
                    patch(
                        "orchestrator.send_email_with_links", capture_send_email_second
                    ),
                ):
                    await daemon.run_daemon(config, logger)

                self.assertEqual(len(second_upload_paths), 1)
                self.assertEqual(len(second_links), 1)
                self.assertEqual(len(second_upload_ids), 1)
                self.assertEqual(len(second_version_ids), 1)
                self.assertEqual(len(second_email_calls), 1)
                _, second_body, _ = second_email_calls[0]
                self.assertIn("Intro", second_body)
                self.assertNotIn("Notes", second_body)
                self.assertNotIn("Demo", second_body)

            finally:
                if self._should_cleanup():
                    await self._cleanup_remote_items(
                        remote_item_ids
                        + first_upload_ids
                        + second_upload_ids
                        + first_version_ids
                        + second_version_ids,
                        config,
                        logger,
                    )
                    await self._cleanup_remote_items_by_name(
                        expected_pdf_names + expected_version_files,
                        config,
                        logger,
                    )
                    await self._cleanup_remote_folders(
                        expected_folder_paths,
                        config,
                        logger,
                    )


if __name__ == "__main__":
    unittest.main()
