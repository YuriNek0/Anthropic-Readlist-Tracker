#!/usr/bin/env python3
"""
Unit tests for Anthropic Readings Daemon
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

import daemon


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


class TestVersionFileStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.version_file = Path(self.temp_dir) / "version.json"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_save_and_load_version_file(self):
        docs = {
            "examples/test.ipynb": daemon.TrackedDocument(
                path="examples/test.ipynb",
                title="Test Notebook",
                date="2024-01-15",
                content_hash="abc123",
            )
        }

        daemon.save_version_file(self.version_file, docs)
        loaded = daemon.load_version_file(self.version_file)

        self.assertEqual(len(loaded), 1)
        self.assertIn("examples/test.ipynb", loaded)
        self.assertEqual(loaded["examples/test.ipynb"].title, "Test Notebook")
        self.assertEqual(loaded["examples/test.ipynb"].content_hash, "abc123")

    def test_load_nonexistent_file_returns_empty(self):
        loaded = daemon.load_version_file(Path("/nonexistent/file.json"))
        self.assertEqual(len(loaded), 0)


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

    def test_parse_invalid_returns_none(self):
        result = daemon.parse_date("not-a-date")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
