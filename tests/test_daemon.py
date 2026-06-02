#!/usr/bin/env python3
"""
Unit tests for Anthropic Readings Daemon
"""

import sys
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import anthropic_readings.daemon as daemon
import anthropic_readings.mailer as _mailer
import anthropic_readings.orchestrator as _orchestrator
import anthropic_readings.rendering as _rendering
from anthropic_readings.core.link_rewrite import rewrite_markdown_links
from anthropic_readings.core.link_rewrite import rewrite_notebook_markdown_cells

sys.modules.setdefault("mailer", _mailer)
sys.modules.setdefault("orchestrator", _orchestrator)


logging.getLogger("asyncio").setLevel(logging.ERROR)


class TestConfigFromDefaults(unittest.TestCase):
    def test_default_config_has_two_repos(self):
        config = daemon.Config.from_defaults()
        self.assertEqual(len(config.repos), 2)
        self.assertEqual(config.repos[0].name, "cookbooks")
        self.assertEqual(config.repos[1].name, "courses")

    def test_default_cookbooks_config(self):
        config = daemon.Config.from_defaults()
        cookbooks = config.repos[0]
        self.assertEqual(cookbooks.name, "cookbooks")
        self.assertIn("anthropics", cookbooks.url)
        self.assertEqual(cookbooks.manifest_file, "registry.yaml")

    def test_default_courses_config(self):
        config = daemon.Config.from_defaults()
        courses = config.repos[1]
        self.assertEqual(courses.name, "courses")
        self.assertIn("anthropics", courses.url)
        self.assertIsNone(courses.manifest_file)
        self.assertIn("*.md", courses.discover_patterns)

    def test_default_daemon_concurrency_config(self):
        config = daemon.Config.from_defaults()
        self.assertEqual(config.daemon.render_concurrency, 1)
        self.assertEqual(config.daemon.render_timeout_seconds, 600)
        self.assertEqual(config.daemon.upload_concurrency, 1)


class TestTrackedDocument(unittest.TestCase):
    def test_tracked_document_creation(self):
        doc = daemon.TrackedDocument(
            path="examples/python/hello.py",
            title="Hello World",
            date="2024-01-15",
        )
        self.assertEqual(doc.path, "examples/python/hello.py")
        self.assertEqual(doc.title, "Hello World")
        self.assertEqual(doc.date, "2024-01-15")
        self.assertEqual(doc.change_type, daemon.ChangeType.UNCHANGED)


class TestChangeDetection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.temp_dir) / "repo"
        self.repo_path.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_new_document_detected(self):
        current = {
            "examples/new.ipynb": daemon.TrackedDocument(
                path="examples/new.ipynb",
                title="New Notebook",
                date="2024-01-15",
            )
        }
        previous = {}

        changed = daemon.detect_changes(current, previous, self.repo_path, MagicMock())
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].change_type, daemon.ChangeType.NEW)
        self.assertEqual(changed[0].path, "examples/new.ipynb")

    def test_date_changed_detected(self):
        current = {
            "examples/test.ipynb": daemon.TrackedDocument(
                path="examples/test.ipynb",
                title="Test Notebook",
                date="2024-01-20",
            )
        }
        previous = {
            "examples/test.ipynb": daemon.TrackedDocument(
                path="examples/test.ipynb",
                title="Test Notebook",
                date="2024-01-15",
            )
        }

        changed = daemon.detect_changes(current, previous, self.repo_path, MagicMock())
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].change_type, daemon.ChangeType.CHANGED)

    def test_unchanged_document_not_in_list(self):
        current = {
            "examples/test.ipynb": daemon.TrackedDocument(
                path="examples/test.ipynb",
                title="Test Notebook",
                date="2024-01-15",
            )
        }
        previous = {
            "examples/test.ipynb": daemon.TrackedDocument(
                path="examples/test.ipynb",
                title="Test Notebook",
                date="2024-01-15",
            )
        }

        changed = daemon.detect_changes(current, previous, self.repo_path, MagicMock())
        self.assertEqual(len(changed), 0)

    def test_multiple_changes_detected(self):
        current = {
            "examples/new.ipynb": daemon.TrackedDocument(
                path="examples/new.ipynb",
                title="New Notebook",
                date="2024-01-15",
            ),
            "examples/updated.ipynb": daemon.TrackedDocument(
                path="examples/updated.ipynb",
                title="Updated Notebook",
                date="2024-01-20",
            ),
            "examples/unchanged.ipynb": daemon.TrackedDocument(
                path="examples/unchanged.ipynb",
                title="Unchanged Notebook",
                date="2024-01-15",
            ),
        }
        previous = {
            "examples/updated.ipynb": daemon.TrackedDocument(
                path="examples/updated.ipynb",
                title="Updated Notebook",
                date="2024-01-15",
            ),
            "examples/unchanged.ipynb": daemon.TrackedDocument(
                path="examples/unchanged.ipynb",
                title="Unchanged Notebook",
                date="2024-01-15",
            ),
        }

        changed = daemon.detect_changes(current, previous, self.repo_path, MagicMock())
        self.assertEqual(len(changed), 2)
        changed_paths = {d.path for d in changed}
        self.assertIn("examples/new.ipynb", changed_paths)
        self.assertIn("examples/updated.ipynb", changed_paths)

    def test_hash_change_detected_with_same_date(self):
        doc_path = self.repo_path / "examples" / "same-date-changed.ipynb"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("v1")

        current = {
            "examples/same-date-changed.ipynb": daemon.TrackedDocument(
                path="examples/same-date-changed.ipynb",
                title="Same Date Notebook",
                date="2024-01-15",
            )
        }
        previous = {
            "examples/same-date-changed.ipynb": daemon.TrackedDocument(
                path="examples/same-date-changed.ipynb",
                title="Same Date Notebook",
                date="2024-01-15",
                content_hash="abcdef123456",
            )
        }

        changed = daemon.detect_changes(current, previous, self.repo_path, MagicMock())
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].change_type, daemon.ChangeType.CHANGED)


class TestDocumentDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.temp_dir) / "repo"
        self.repo_path.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_discover_courses_finds_ipynb_and_md(self):
        (self.repo_path / "test.ipynb").touch()
        (self.repo_path / "readme.md").touch()

        docs = daemon.discover_courses(self.repo_path, ["*.ipynb", "*.md"], MagicMock())

        self.assertEqual(len(docs), 2)
        paths = {d.path for d in docs.values()}
        self.assertIn("test.ipynb", paths)
        self.assertIn("readme.md", paths)

    def test_discover_courses_excludes_amazonbedrock(self):
        (self.repo_path / "test.ipynb").touch()
        (self.repo_path / "AmazonBedrock").mkdir()
        (self.repo_path / "AmazonBedrock" / "test.ipynb").touch()

        docs = daemon.discover_courses(self.repo_path, ["*.ipynb"], MagicMock())

        self.assertEqual(len(docs), 1)
        self.assertIn("test.ipynb", docs.keys())


class TestSlugifyTitle(unittest.TestCase):
    def test_slugify_removes_special_chars(self):
        result = daemon.slugify_title("Hello World! @2024")
        self.assertIsInstance(result, str)
        self.assertNotIn("@", result)
        self.assertNotIn("!", result)

    def test_slugify_lowercase(self):
        result = daemon.slugify_title("HELLO WORLD")
        self.assertEqual(result.lower(), result)


class TestParseDate(unittest.TestCase):
    def test_parse_standard_date(self):
        result = daemon.parse_date("2024-01-15")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_parse_iso_datetime(self):
        result = daemon.parse_date("2024-01-15T10:30:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 10)
        self.assertEqual(result.minute, 30)

    def test_parse_datetime_with_milliseconds_and_z(self):
        result = daemon.parse_date("2024-01-15T10:30:00.123456Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)
        self.assertEqual(result.hour, 10)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.second, 0)
        self.assertEqual(result.microsecond, 123456)

    def test_parse_invalid_returns_none(self):
        result = daemon.parse_date("not-a-date")
        self.assertIsNone(result)


class TestBuildRenderOutputPath(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.output_root = Path(self.temp_dir) / "outputs"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_cookbook_output_path(self):
        doc = daemon.TrackedDocument(path="some/path.md", title="Hello World", date="")
        folder, file_name = daemon.build_render_output_path(
            self.output_root, "2024-01-15", "cookbooks", doc
        )

        self.assertEqual(folder, self.output_root / "2024-01-15" / "some")
        self.assertEqual(file_name, "hello-world.pdf")

    def test_courses_output_path_nested(self):
        doc = daemon.TrackedDocument(
            path="Course One/Chapter 01/Deep-Dive.md", title="Course One", date=""
        )
        folder, file_name = daemon.build_render_output_path(
            self.output_root, "2024-01-15", "courses", doc
        )

        self.assertEqual(
            folder,
            self.output_root / "2024-01-15" / "course-one" / "chapter-01",
        )
        self.assertEqual(file_name, "deep-dive.pdf")

    def test_courses_output_path_root_document(self):
        doc = daemon.TrackedDocument(path="Welcome.md", title="Welcome", date="")
        folder, file_name = daemon.build_render_output_path(
            self.output_root, "2024-01-15", "courses", doc
        )

        self.assertEqual(folder, self.output_root / "2024-01-15")
        self.assertEqual(file_name, "welcome.pdf")

    def test_cookbook_output_path_with_leading_index_prefix(self):
        doc = daemon.TrackedDocument(
            path="examples/01_intro_to_tools.md",
            title="Intro To Tools",
            date="",
        )
        folder, file_name = daemon.build_render_output_path(
            self.output_root, "2024-01-15", "cookbooks", doc
        )

        self.assertEqual(folder, self.output_root / "2024-01-15" / "examples")
        self.assertEqual(file_name, "01-intro-to-tools.pdf")


class TestRewriteMarkdownLinks(unittest.TestCase):
    def test_rewrites_relative_md_and_ipynb_links_to_pdf(self):
        output_map = {
            "lessons/intro.md": "2024-01-15/lessons/intro.pdf",
            "lessons/demo.ipynb": "2024-01-15/lessons/demo.pdf",
            "lessons/notes.md": "2024-01-15/lessons/notes.pdf",
        }
        markdown = (
            "See [Demo](./demo.ipynb) and [Notes](notes.md#section). "
            "Keep [External](https://example.com/page.md)."
        )

        rewritten = rewrite_markdown_links(markdown, "lessons/intro.md", output_map)

        self.assertIn("[Demo](demo.pdf)", rewritten)
        self.assertIn("[Notes](notes.pdf#section)", rewritten)
        self.assertIn("[External](https://example.com/page.md)", rewritten)


class TestRewriteNotebookLinks(unittest.TestCase):
    def test_rewrites_notebook_markdown_cell_links_to_pdf(self):
        output_map = {
            "lessons/intro.ipynb": "2024-01-15/lessons/intro.pdf",
            "lessons/references.md": "2024-01-15/lessons/references.pdf",
        }
        notebook = {
            "cells": [
                {
                    "cell_type": "markdown",
                    "source": ["See [Refs](references.md) for details.\n"],
                }
            ]
        }

        changed = rewrite_notebook_markdown_cells(
            notebook,
            "lessons/intro.ipynb",
            output_map,
        )

        self.assertTrue(changed)
        self.assertEqual(
            notebook["cells"][0]["source"][0],
            "See [Refs](references.pdf) for details.\n",
        )


class TestRenderDocumentToPdf(unittest.TestCase):
    def test_pdf_stylesheet_overrides_notebook_overflow_layout(self):
        stylesheet = _rendering.PDF_RENDER_STYLESHEET

        self.assertIn(".jp-InputArea", stylesheet)
        self.assertIn(".jp-OutputArea-child", stylesheet)
        self.assertIn("display: block !important", stylesheet)
        self.assertIn("overflow: visible !important", stylesheet)
        self.assertIn("white-space: pre-wrap !important", stylesheet)
        self.assertIn("word-break: break-all !important", stylesheet)
        self.assertIn(".jp-InputPrompt", stylesheet)
        self.assertIn("display: none !important", stylesheet)

    def test_failed_renderer_with_short_stderr_reports_original_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_path = temp_path / "repo"
            output_dir = temp_path / "outputs"
            repo_path.mkdir()
            (repo_path / "notes.md").write_text("# Notes\n", encoding="utf-8")

            doc = daemon.TrackedDocument(
                path="notes.md",
                title="Notes",
                date="2024-01-15",
            )
            failed_result = MagicMock(
                returncode=1,
                stderr="renderer failed\n",
                stdout="",
            )

            with patch(
                "anthropic_readings.rendering.subprocess.run",
                return_value=failed_result,
            ) as mock_run:
                rendered = daemon.render_document_to_pdf(
                    doc,
                    repo_path,
                    output_dir,
                    MagicMock(),
                    repo_name="courses",
                    render_timeout_seconds=123,
                )

            self.assertEqual(rendered.error, "renderer failed")
            call_args = mock_run.call_args
            if call_args is None:
                self.fail("subprocess.run was not called")
            cmd = call_args.args[0]
            css_arg = next(arg for arg in cmd if arg.startswith("--css="))
            self.assertEqual(Path(css_arg.split("=", 1)[1]).name, "pdf-render.css")
            self.assertEqual(call_args.kwargs["timeout"], 123)

    def test_notebook_renderer_uses_html_then_weasyprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_path = temp_path / "repo"
            output_dir = temp_path / "outputs"
            repo_path.mkdir()
            (repo_path / "lesson.ipynb").write_text(
                '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}',
                encoding="utf-8",
            )

            doc = daemon.TrackedDocument(
                path="lesson.ipynb",
                title="Lesson",
                date="2024-01-15",
            )

            def fake_run(cmd, **kwargs):
                if cmd[0] == "jupyter":
                    output_dir_arg = next(arg for arg in cmd if arg.startswith("--output-dir="))
                    output_arg = next(arg for arg in cmd if arg.startswith("--output="))
                    html_path = Path(output_dir_arg.split("=", 1)[1]) / (
                        output_arg.split("=", 1)[1] + ".html"
                    )
                    html_path.write_text("<html><body>lesson</body></html>", encoding="utf-8")
                elif cmd[0] == "weasyprint":
                    Path(cmd[-1]).write_text("pdf", encoding="utf-8")
                return MagicMock(returncode=0, stderr="", stdout="")

            with patch(
                "anthropic_readings.rendering.subprocess.run",
                side_effect=fake_run,
            ) as mock_run:
                rendered = daemon.render_document_to_pdf(
                    doc,
                    repo_path,
                    output_dir,
                    MagicMock(),
                    repo_name="courses",
                    render_timeout_seconds=123,
                )

            self.assertIsNone(rendered.error)
            self.assertIsNotNone(rendered.pdf_path)
            self.assertEqual(mock_run.call_count, 2)
            html_cmd = mock_run.call_args_list[0].args[0]
            pdf_cmd = mock_run.call_args_list[1].args[0]
            self.assertEqual(html_cmd[:4], ["jupyter", "nbconvert", "--to=html", "--embed-images"])
            self.assertNotIn("--to=webpdf", html_cmd)
            self.assertEqual(pdf_cmd[:2], ["weasyprint", "--stylesheet"])
            self.assertEqual(Path(pdf_cmd[2]).name, "pdf-render.css")


class TestRunDaemonFlow(unittest.IsolatedAsyncioTestCase):
    async def test_process_repo_preserves_previous_version_for_failed_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_path = temp_path / "courses-repo"
            repo_path.mkdir()
            (repo_path / "lesson.md").write_text("updated", encoding="utf-8")

            config = daemon.Config.from_defaults()
            config.output_dir = str(temp_path / "outputs")
            repo_config = daemon.RepoConfig(
                name="courses",
                url="https://example.local/courses.git",
                local_path=str(repo_path),
                version_file="courses-version.json",
                discover_patterns=["*.md"],
                manifest_file=None,
            )

            current_doc = daemon.TrackedDocument(
                path="lesson.md", title="Lesson", date="2024-01-02"
            )
            previous_doc = daemon.TrackedDocument(
                path="lesson.md",
                title="Lesson",
                date="2024-01-01",
                content_hash="old-hash",
            )

            def render_failed(doc, *_args):
                return daemon.RenderedDocument(
                    doc=doc,
                    repo_name="courses",
                    error="render failed",
                )

            with (
                patch("orchestrator.ensure_repo_available", return_value=True),
                patch("orchestrator.discover_courses", return_value={current_doc.path: current_doc}),
                patch(
                    "orchestrator.download_version_from_onedrive",
                    AsyncMock(return_value={previous_doc.path: previous_doc}),
                ),
                patch("orchestrator.render_document_to_pdf", side_effect=render_failed),
            ):
                rendered, updated_docs = await daemon.process_repo(
                    repo_config,
                    config,
                    MagicMock(),
                )

            self.assertEqual(len(rendered), 1)
            self.assertEqual(rendered[0].error, "render failed")
            self.assertEqual(updated_docs["lesson.md"].date, "2024-01-01")
            self.assertEqual(updated_docs["lesson.md"].content_hash, "old-hash")

    async def test_process_repo_omits_new_failed_render_from_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_path = temp_path / "courses-repo"
            repo_path.mkdir()
            (repo_path / "new-lesson.md").write_text("new", encoding="utf-8")

            config = daemon.Config.from_defaults()
            config.output_dir = str(temp_path / "outputs")
            repo_config = daemon.RepoConfig(
                name="courses",
                url="https://example.local/courses.git",
                local_path=str(repo_path),
                version_file="courses-version.json",
                discover_patterns=["*.md"],
                manifest_file=None,
            )

            current_doc = daemon.TrackedDocument(
                path="new-lesson.md", title="New Lesson", date="2024-01-02"
            )

            def render_failed(doc, *_args):
                return daemon.RenderedDocument(
                    doc=doc,
                    repo_name="courses",
                    error="render failed",
                )

            with (
                patch("orchestrator.ensure_repo_available", return_value=True),
                patch("orchestrator.discover_courses", return_value={current_doc.path: current_doc}),
                patch(
                    "orchestrator.download_version_from_onedrive",
                    AsyncMock(return_value={}),
                ),
                patch("orchestrator.render_document_to_pdf", side_effect=render_failed),
            ):
                rendered, updated_docs = await daemon.process_repo(
                    repo_config,
                    config,
                    MagicMock(),
                )

            self.assertEqual(len(rendered), 1)
            self.assertEqual(rendered[0].error, "render failed")
            self.assertNotIn("new-lesson.md", updated_docs)

    async def test_run_daemon_processes_all_repos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")

            pdf_1 = temp_path / "book-one.pdf"
            pdf_2 = temp_path / "book-two.pdf"
            pdf_1.write_text("pdf-one")
            pdf_2.write_text("pdf-two")

            doc_one = daemon.TrackedDocument(
                path="book-one.md", title="Book One", date="2024-01-01"
            )
            doc_one.change_type = daemon.ChangeType.NEW
            doc_two = daemon.TrackedDocument(
                path="book-two.md", title="Book Two", date="2024-01-01"
            )
            doc_two.change_type = daemon.ChangeType.CHANGED

            rendered_one = [
                daemon.RenderedDocument(
                    doc=doc_one,
                    repo_name="cookbooks",
                    pdf_path=pdf_1,
                )
            ]
            rendered_two = [
                daemon.RenderedDocument(
                    doc=doc_two,
                    repo_name="courses",
                    pdf_path=pdf_2,
                )
            ]

            docs_one = {
                doc_one.path: daemon.TrackedDocument(
                    path=doc_one.path, title=doc_one.title, date=doc_one.date
                )
            }
            docs_two = {
                doc_two.path: daemon.TrackedDocument(
                    path=doc_two.path, title=doc_two.title, date=doc_two.date
                )
            }

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(
                        side_effect=[(rendered_one, docs_one), (rendered_two, docs_two)]
                    ),
                ) as mock_process_repo,
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(side_effect=["pdf-item-1", "pdf-item-2"]),
                ) as mock_upload_pdf,
                patch(
                    "orchestrator.create_sharing_link",
                    AsyncMock(return_value="https://example.com/link"),
                ) as mock_create_link,
                patch(
                    "orchestrator.send_email_with_links",
                    AsyncMock(return_value=True),
                ) as mock_send_email,
                patch(
                    "orchestrator.upload_version_to_onedrive",
                    AsyncMock(side_effect=["version-1", "version-2"]),
                ) as mock_upload_version,
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                self.assertEqual(mock_process_repo.await_count, 2)
                self.assertEqual(mock_upload_pdf.await_count, 2)
                self.assertEqual(mock_create_link.await_count, 2)
                self.assertEqual(mock_send_email.await_count, 1)
                self.assertEqual(mock_upload_version.await_count, 2)

                mock_upload_pdf.assert_has_awaits(
                    [
                        call(pdf_1, "cookbooks/book-one.pdf", config, logger),
                        call(pdf_2, "courses/book-two.pdf", config, logger),
                    ],
                    any_order=False,
                )

                mock_upload_version.assert_has_awaits(
                    [
                        call(
                            "cookbook-version.json",
                            docs_one,
                            config,
                            logger,
                            onedrive_path="cookbooks/cookbook-version.json",
                        ),
                        call(
                            "courses-version.json",
                            docs_two,
                            config,
                            logger,
                            onedrive_path="courses/courses-version.json",
                        ),
                    ],
                    any_order=False,
                )

    async def test_run_daemon_courses_upload_path_ignores_anthropic_1p_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")
            config.repos = [
                daemon.RepoConfig(
                    name="courses",
                    url="https://example.local/courses.git",
                    local_path=str(temp_path / "courses"),
                    version_file="courses-version.json",
                    discover_patterns=["*.md"],
                    manifest_file=None,
                )
            ]

            pdf_path = temp_path / "lesson.pdf"
            pdf_path.write_text("pdf")

            course_doc = daemon.TrackedDocument(
                path="Anthropic 1P/Course One/intro.md",
                title="Intro",
                date="2024-01-01",
            )
            course_doc.change_type = daemon.ChangeType.NEW

            rendered = [
                daemon.RenderedDocument(
                    doc=course_doc,
                    repo_name="courses",
                    pdf_path=pdf_path,
                )
            ]
            updated_docs = {
                course_doc.path: daemon.TrackedDocument(
                    path=course_doc.path,
                    title=course_doc.title,
                    date=course_doc.date,
                )
            }

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[(rendered, updated_docs)]),
                ),
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(return_value="pdf-id"),
                ) as mock_upload_pdf,
                patch(
                    "orchestrator.create_sharing_link",
                    AsyncMock(return_value="https://example.com/link"),
                ),
                patch(
                    "orchestrator.send_email_with_links",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "orchestrator.upload_version_to_onedrive",
                    AsyncMock(return_value="version-id"),
                ),
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                mock_upload_pdf.assert_awaited_once_with(
                    pdf_path,
                    "courses/Course One/intro.pdf",
                    config,
                    logger,
                )

    async def test_run_daemon_cookbook_upload_path_uses_generated_pdf_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")
            config.repos = [
                daemon.RepoConfig(
                    name="cookbooks",
                    url="https://example.local/cookbooks.git",
                    local_path=str(temp_path / "cookbooks"),
                    version_file="cookbook-version.json",
                    discover_patterns=["*.md"],
                    manifest_file="registry.yaml",
                )
            ]

            pdf_path = temp_path / "01-intro-to-tools.pdf"
            pdf_path.write_text("pdf")

            doc = daemon.TrackedDocument(
                path="examples/01_intro_to_tools.md",
                title="Intro To Tools",
                date="2024-01-01",
            )
            doc.change_type = daemon.ChangeType.NEW

            rendered = [
                daemon.RenderedDocument(
                    doc=doc,
                    repo_name="cookbooks",
                    pdf_path=pdf_path,
                )
            ]
            updated_docs = {
                doc.path: daemon.TrackedDocument(
                    path=doc.path,
                    title=doc.title,
                    date=doc.date,
                )
            }

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[(rendered, updated_docs)]),
                ),
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(return_value="pdf-id"),
                ) as mock_upload_pdf,
                patch(
                    "orchestrator.create_sharing_link",
                    AsyncMock(return_value="https://example.com/link"),
                ),
                patch(
                    "orchestrator.send_email_with_links",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "orchestrator.upload_version_to_onedrive",
                    AsyncMock(return_value="version-id"),
                ),
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                mock_upload_pdf.assert_awaited_once_with(
                    pdf_path,
                    "cookbooks/examples/01-intro-to-tools.pdf",
                    config,
                    logger,
                )

    async def test_run_daemon_returns_without_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")
            config.repos[0].local_path = str(temp_path / "cookbooks-repo")
            config.repos[1].local_path = str(temp_path / "courses-repo")

            stale_output_root = Path(config.output_dir) / "2024-01-01"
            stale_output_root.mkdir(parents=True)
            Path(config.repos[0].local_path).mkdir()
            Path(config.repos[1].local_path).mkdir()

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[([], {}), ([], {})]),
                ) as mock_process_repo,
                patch(
                    "orchestrator.send_email_with_links",
                    AsyncMock(return_value=True),
                ) as mock_send_email,
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

                self.assertEqual(mock_process_repo.await_count, 2)
                mock_send_email.assert_not_called()

            self.assertFalse(stale_output_root.exists())
            self.assertFalse(Path(config.output_dir).exists())
            self.assertFalse(Path(config.repos[0].local_path).exists())
            self.assertFalse(Path(config.repos[1].local_path).exists())

    async def test_run_daemon_invokes_graph_run_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")

            call_order: list[str] = []

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[([], {}), ([], {})]),
                ),
                patch(
                    "orchestrator.begin_graph_run",
                    side_effect=lambda: call_order.append("begin"),
                ),
                patch(
                    "orchestrator.end_graph_run",
                    side_effect=lambda: call_order.append("end"),
                ),
            ):
                logger = MagicMock()
                await daemon.run_daemon(config, logger)

            self.assertEqual(call_order, ["begin", "end"])

    async def test_run_daemon_finally_runs_after_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")

            output_root = Path(config.output_dir)
            pdf_path = output_root / "2024-01-01" / "book-one.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_text("pdf-one")

            doc_one = daemon.TrackedDocument(
                path="book-one.md", title="Book One", date="2024-01-01"
            )
            doc_one.change_type = daemon.ChangeType.NEW

            rendered_one = [
                daemon.RenderedDocument(
                    doc=doc_one,
                    repo_name="cookbooks",
                    pdf_path=pdf_path,
                )
            ]

            docs_one = {
                doc_one.path: daemon.TrackedDocument(
                    path=doc_one.path, title=doc_one.title, date=doc_one.date
                )
            }

            call_order: list[str] = []

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[(rendered_one, docs_one), ([], {})]),
                ),
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(return_value=None),
                ),
                patch("orchestrator.send_error_email", AsyncMock(return_value=None)),
                patch(
                    "orchestrator.begin_graph_run",
                    side_effect=lambda: call_order.append("begin"),
                ),
                patch(
                    "orchestrator.end_graph_run",
                    side_effect=lambda: call_order.append("end"),
                ),
            ):
                logger = MagicMock()
                with self.assertRaises(Exception):
                    await daemon.run_daemon(config, logger)

            self.assertEqual(call_order, ["begin", "end"])

    async def test_run_daemon_cleans_outputs_after_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")
            config.repos[0].local_path = str(temp_path / "cookbooks-repo")
            config.repos[1].local_path = str(temp_path / "courses-repo")

            output_root = Path(config.output_dir)
            pdf_path = output_root / "2024-01-01" / "book-one.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_text("pdf-one")
            Path(config.repos[0].local_path).mkdir()
            Path(config.repos[1].local_path).mkdir()

            doc_one = daemon.TrackedDocument(
                path="book-one.md", title="Book One", date="2024-01-01"
            )
            doc_one.change_type = daemon.ChangeType.NEW

            rendered_one = [
                daemon.RenderedDocument(
                    doc=doc_one,
                    repo_name="cookbooks",
                    pdf_path=pdf_path,
                )
            ]

            docs_one = {
                doc_one.path: daemon.TrackedDocument(
                    path=doc_one.path, title=doc_one.title, date=doc_one.date
                )
            }

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(side_effect=[(rendered_one, docs_one), ([], {})]),
                ),
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "orchestrator.send_error_email",
                    AsyncMock(return_value=None),
                ),
            ):
                logger = MagicMock()
                with self.assertRaises(Exception):
                    await daemon.run_daemon(config, logger)

            self.assertFalse(pdf_path.exists())
            self.assertFalse(pdf_path.parent.exists())
            self.assertFalse(Path(config.output_dir).exists())
            self.assertFalse(Path(config.repos[0].local_path).exists())
            self.assertFalse(Path(config.repos[1].local_path).exists())

    async def test_run_daemon_includes_captured_errors_in_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = daemon.Config.from_defaults()
            config.email.sender = "noreply@example.com"
            config.email.recipients = ["reader@example.com"]
            config.output_dir = str(temp_path / "outputs")

            output_root = Path(config.output_dir)
            pdf_path = output_root / "2024-01-01" / "book-one.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_text("pdf-one")

            doc_one = daemon.TrackedDocument(
                path="book-one.md", title="Book One", date="2024-01-01"
            )
            doc_one.change_type = daemon.ChangeType.NEW

            rendered_one = [
                daemon.RenderedDocument(
                    doc=doc_one,
                    repo_name="cookbooks",
                    pdf_path=pdf_path,
                )
            ]

            docs_one = {
                doc_one.path: daemon.TrackedDocument(
                    path=doc_one.path, title=doc_one.title, date=doc_one.date
                )
            }

            async def upload_with_error(_pdf_path, *_args):
                _pdf_path_logger = _args[2]
                _pdf_path_logger.error(
                    "Simulated graph upload failure: 403 accessDenied"
                )
                return None

            logger = logging.getLogger(f"orch.run_daemon.capture.{temp_path}")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            logger.addHandler(logging.NullHandler())

            with (
                patch(
                    "orchestrator.process_repo",
                    AsyncMock(
                        side_effect=[
                            (rendered_one, docs_one),
                            ([], {}),
                        ]
                    ),
                ),
                patch(
                    "orchestrator.upload_pdf_to_onedrive",
                    AsyncMock(side_effect=upload_with_error),
                ) as mock_upload_pdf,
                patch(
                    "orchestrator.upload_version_to_onedrive",
                    AsyncMock(return_value="version-id"),
                ) as mock_upload_version,
                patch(
                    "orchestrator.send_error_email",
                    AsyncMock(return_value=None),
                ) as mock_send_error,
            ):
                with self.assertRaises(Exception):
                    await daemon.run_daemon(config, logger)

            self.assertEqual(mock_upload_pdf.await_count, 1)
            mock_upload_version.assert_not_called()
            self.assertEqual(mock_send_error.await_count, 1)

            error_message = mock_send_error.await_args.args[0]
            self.assertIn("Captured Error Log Entries:", error_message)
            self.assertIn(
                "Simulated graph upload failure: 403 accessDenied", error_message
            )
            self.assertIn("Failed to upload", error_message)

            self.assertFalse(pdf_path.exists())
            self.assertFalse(pdf_path.parent.exists())
            self.assertFalse(output_root.exists())


class TestErrorEmailBehavior(unittest.IsolatedAsyncioTestCase):
    def _mock_graph_client(self):
        client = MagicMock()
        send_mail_post = AsyncMock()

        me = MagicMock()
        me.send_mail = MagicMock()
        me.send_mail.post = send_mail_post
        client.me = me

        return client, send_mail_post

    async def test_send_error_email_skips_when_not_in_production(self):
        config = daemon.Config.from_defaults()
        config.user.email = "ops@example.com"
        config.email.sender = "noreply@example.com"
        config.email.recipients = ["reader@example.com"]

        logger = MagicMock()
        with patch("mailer.get_graph_client") as mock_get_graph_client:
            await daemon.send_error_email("failure", config, logger)
            mock_get_graph_client.assert_not_called()

    async def test_send_error_email_sends_when_in_production(self):
        config = daemon.Config.from_defaults()
        config.user.email = "ops@example.com"
        config.email.sender = "noreply@example.com"
        config.email.recipients = ["reader@example.com"]
        config.is_production = True

        client, send_mail_post = self._mock_graph_client()
        logger = MagicMock()

        with patch(
            "mailer.get_graph_client", return_value=client
        ) as mock_get_graph_client:
            await daemon.send_error_email("failure", config, logger)
            mock_get_graph_client.assert_called_once_with(
                config, logger, daemon.MAIL_SCOPES
            )
            send_mail_post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
