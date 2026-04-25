#!/usr/bin/env python3
"""OneDrive flow-focused tests for daemon orchestration."""

import json
import os
import sys
from datetime import datetime
import tempfile
import unittest
from pathlib import Path

import yaml
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import anthropic_readings.daemon as daemon
import anthropic_readings.orchestrator as _orchestrator

sys.modules.setdefault("orchestrator", _orchestrator)


class TestOneDriveFlow(unittest.IsolatedAsyncioTestCase):
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

    async def test_run_daemon_with_local_repo_updates_and_onedrive_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "sample-repo"
            created = self._create_sample_test_repo(repo_root)

            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.repos = [
                daemon.RepoConfig(
                    name="courses",
                    url="https://example.local/fake.git",
                    local_path=str(repo_root),
                    version_file="courses-version.json",
                    discover_patterns=["*.ipynb", "*.md"],
                    manifest_file=None,
                )
            ]
            config.output_dir = str(temp_path / "outputs")

            credentials_path = os.getenv("ONEDRIVE_TEST_CREDENTIALS_PATH")
            if credentials_path:
                self._apply_test_credentials(config, credentials_path)

            saved_version_docs: dict[str, daemon.TrackedDocument] = {}

            async def fake_upload_version(
                version_file: str,
                documents: dict[str, daemon.TrackedDocument],
                *_args,
                **_kwargs,
            ):
                nonlocal saved_version_docs
                saved_version_docs = {}
                for path, doc in documents.items():
                    saved_version_docs[path] = daemon.TrackedDocument(
                        path=doc.path,
                        title=doc.title,
                        date=doc.date,
                        content_hash=doc.content_hash,
                    )
                return "version-id-1"

            upload_pdf = AsyncMock(
                side_effect=lambda pdf_path, *_: f"pdf-{Path(pdf_path).name}"
            )
            create_link = AsyncMock(
                side_effect=lambda item_id, *_: f"https://example.com/{item_id}"
            )
            upload_version = AsyncMock(side_effect=fake_upload_version)
            send_email = AsyncMock(return_value=True)

            with (
                patch("orchestrator.ensure_repo_available", return_value=True),
                patch(
                    "orchestrator.download_version_from_onedrive",
                    AsyncMock(return_value=None),
                ) as mock_download,
                patch(
                    "orchestrator.render_document_to_pdf",
                    self._fake_render(),
                ),
                patch("orchestrator.upload_pdf_to_onedrive", upload_pdf),
                patch("orchestrator.create_sharing_link", create_link),
                patch("orchestrator.upload_version_to_onedrive", upload_version),
                patch("orchestrator.send_email_with_links", send_email),
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                mock_download.assert_awaited_once()
                self.assertEqual(upload_pdf.await_count, 3)
                self.assertEqual(create_link.await_count, 3)
                self.assertEqual(upload_version.await_count, 1)
                self.assertEqual(send_email.await_count, 1)

                uploaded_items = [
                    Path(call.args[0]) for call in upload_pdf.await_args_list
                ]
                self.assertEqual(len(uploaded_items), 3)
                self.assertTrue(all(item.suffix == ".pdf" for item in uploaded_items))

                uploaded_remote_paths = [
                    call.args[1] for call in upload_pdf.await_args_list
                ]
                self.assertCountEqual(
                    uploaded_remote_paths,
                    [
                        "courses/lessons/intro.pdf",
                        "courses/lessons/notes.pdf",
                        "courses/lessons/demo.pdf",
                    ],
                )

                first_run_email_body = send_email.await_args.args[1]
                self.assertIn("Intro", first_run_email_body)
                self.assertIn("Notes", first_run_email_body)
                self.assertIn("Demo", first_run_email_body)
                self.assertTrue(
                    any(
                        "https://example.com/pdf-" in line
                        for line in first_run_email_body.split("\n")
                    )
                )
                intro_link_pos = first_run_email_body.find(
                    "https://example.com/pdf-intro.pdf"
                )
                notes_link_pos = first_run_email_body.find(
                    "https://example.com/pdf-notes.pdf"
                )
                demo_link_pos = first_run_email_body.find(
                    "https://example.com/pdf-demo.pdf"
                )
                self.assertGreater(intro_link_pos, -1)
                self.assertGreater(notes_link_pos, -1)
                self.assertGreater(demo_link_pos, -1)

                self.assertLess(
                    intro_link_pos, first_run_email_body.find("lessons/intro.md")
                )
                self.assertLess(
                    notes_link_pos, first_run_email_body.find("lessons/notes.md")
                )
                self.assertLess(
                    demo_link_pos, first_run_email_body.find("lessons/demo.ipynb")
                )

            self.assertEqual(len(saved_version_docs), 3)
            self.assertIn("lessons/intro.md", saved_version_docs)
            self.assertIn("lessons/notes.md", saved_version_docs)
            self.assertIn("lessons/demo.ipynb", saved_version_docs)

            updated_payload = saved_version_docs

            created = self._create_sample_test_repo(repo_root)
            updated_first = Path(created["markdown_first"])
            updated_first.write_text("# Intro v2\n")

            upload_pdf.reset_mock()
            create_link.reset_mock()
            upload_version.reset_mock()
            send_email.reset_mock()

            async def fake_download_second(*_args, **_kwargs):
                return updated_payload

            with (
                patch("orchestrator.ensure_repo_available", return_value=True),
                patch(
                    "orchestrator.download_version_from_onedrive",
                    AsyncMock(side_effect=fake_download_second),
                ),
                patch(
                    "orchestrator.render_document_to_pdf",
                    self._fake_render(),
                ),
                patch("orchestrator.upload_pdf_to_onedrive", upload_pdf),
                patch("orchestrator.create_sharing_link", create_link),
                patch("orchestrator.upload_version_to_onedrive", upload_version),
                patch("orchestrator.send_email_with_links", send_email),
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                self.assertEqual(upload_pdf.await_count, 1)
                self.assertEqual(create_link.await_count, 1)
                self.assertEqual(upload_version.await_count, 1)
                self.assertEqual(send_email.await_count, 1)

                uploaded_file = Path(upload_pdf.await_args.args[0])
                self.assertEqual(uploaded_file.name, "intro.pdf")
                self.assertTrue(uploaded_file.suffix == ".pdf")
                self.assertEqual(
                    upload_pdf.await_args.args[1], "courses/lessons/intro.pdf"
                )

                email_body = send_email.await_args.args[1]
                self.assertIn("Intro", email_body)
                self.assertNotIn("Notes", email_body)
                self.assertNotIn("Demo", email_body)


if __name__ == "__main__":
    unittest.main()
