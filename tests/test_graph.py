#!/usr/bin/env python3
"""Graph helper tests."""

import unittest
import tempfile
import sys
from pathlib import Path
import yaml
import logging
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from anthropic_readings.config import Config
import anthropic_readings.graph as graph


class TestExtractLinkUrl(unittest.TestCase):
    def test_extract_link_url_from_graph_invite_payload_list(self):
        payload = [
            {"id": "a", "link": {"webUrl": "https://graph.share/first"}},
            {"id": "b", "link": {"webUrl": "https://graph.share/second"}},
        ]

        self.assertEqual(graph._extract_link_url(payload), "https://graph.share/first")

    def test_extract_link_url_from_value_array(self):
        payload = {
            "value": [
                {
                    "link": {
                        "webUrl": "https://graph.share/from-value",
                    }
                }
            ]
        }
        self.assertEqual(
            graph._extract_link_url(payload),
            "https://graph.share/from-value",
        )

    def test_extract_link_url_from_link_object(self):
        payload = {"link": {"web_url": "https://graph.share/from-object"}}
        self.assertEqual(
            graph._extract_link_url(payload), "https://graph.share/from-object"
        )


class TestShareRecipients(unittest.TestCase):
    def test_share_recipients_fallback_to_recipients(self):
        cfg = Config.from_defaults()
        cfg.email.share_recipients = []
        cfg.email.recipients = ["fallback@example.com"]

        self.assertEqual(
            graph._share_recipients_for_links(cfg),
            ["fallback@example.com"],
        )

    def test_share_recipients_prefers_explicit(self):
        cfg = Config.from_defaults()
        cfg.email.share_recipients = ["explicit@example.com"]
        cfg.email.recipients = ["fallback@example.com"]

        self.assertEqual(
            graph._share_recipients_for_links(cfg),
            ["explicit@example.com"],
        )

    def test_share_recipients_domain_filter_applies_when_enabled(self):
        cfg = Config.from_defaults()
        cfg.email.share_recipients = ["user1@company.com", "user2@external.com"]
        cfg.email.recipients = ["fallback@external.com"]
        cfg.email.share_domain_filter_enabled = True
        cfg.email.share_domain = "company.com"

        self.assertEqual(
            graph._share_recipients_for_links(cfg),
            ["user1@company.com"],
        )

    def test_share_recipients_domain_filter_disabled_keeps_all(self):
        cfg = Config.from_defaults()
        cfg.email.share_recipients = ["user1@company.com", "user2@external.com"]
        cfg.email.share_domain_filter_enabled = False
        cfg.email.share_domain = "company.com"

        self.assertEqual(
            graph._share_recipients_for_links(cfg),
            ["user1@company.com", "user2@external.com"],
        )

    def test_create_share_link_for_specific_users_uses_item_web_url_when_invite_has_no_link(
        self,
    ):
        cfg = Config.from_defaults()
        cfg.email.share_recipients = ["user1@company.com"]

        logger = logging.getLogger("test_invite_fallback_item_weburl")
        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_graph_request_json",
                side_effect=[
                    (
                        200,
                        {
                            "value": [
                                {
                                    "id": "perm-1",
                                    "roles": ["read"],
                                    "grantedTo": {
                                        "user": {"email": "user1@company.com"}
                                    },
                                }
                            ]
                        },
                    ),
                    (200, {"webUrl": "https://onedrive.example/file"}),
                ],
            ) as mock_request,
        ):
            url = graph._create_share_link_for_specific_users(
                "01ABCDEFGH123",
                cfg,
                logger,
            )

        self.assertEqual(url, "https://onedrive.example/file")
        self.assertEqual(mock_request.call_count, 2)


class TestConfigShareRecipients(unittest.TestCase):
    def _make_config_file(self, payload: dict[str, object]) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fp:
            yaml.safe_dump(payload, fp)
            return fp.name

    def test_config_loads_share_recipients(self):
        payload = {
            "azure": {
                "tenant_id": "tid",
                "client_id": "cid",
                "client_secret": "secret",
            },
            "user": {"email": "user@example.com", "password": ""},
            "email": {
                "sender": "noreply@example.com",
                "recipients": ["receiver@example.com"],
                "share_recipients": ["share1@example.com", "share2@example.com"],
                "share_domain_filter_enabled": True,
                "share_domain": "example.com",
            },
            "repos": [
                {
                    "name": "courses",
                    "url": "https://example.local/fake.git",
                    "local_path": "./path",
                    "version_file": "courses-version.json",
                    "discover_patterns": ["*.md"],
                }
            ],
        }

        path = self._make_config_file(payload)
        try:
            config = Config.from_yaml(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(
            config.email.share_recipients,
            ["share1@example.com", "share2@example.com"],
        )
        self.assertTrue(config.email.share_domain_filter_enabled)
        self.assertEqual(config.email.share_domain, "example.com")
        self.assertEqual(
            graph._share_recipients_for_links(config), config.email.share_recipients
        )

    def test_config_defaults_share_recipients_to_empty(self):
        payload = {
            "azure": {
                "tenant_id": "tid",
                "client_id": "cid",
                "client_secret": "secret",
            },
            "user": {"email": "user@example.com", "password": ""},
            "email": {
                "sender": "noreply@example.com",
                "recipients": ["receiver@example.com"],
            },
            "repos": [
                {
                    "name": "courses",
                    "url": "https://example.local/fake.git",
                    "local_path": "./path",
                    "version_file": "courses-version.json",
                    "discover_patterns": ["*.md"],
                }
            ],
        }

        path = self._make_config_file(payload)
        try:
            config = Config.from_yaml(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(config.email.share_recipients, [])
        self.assertEqual(
            graph._share_recipients_for_links(config), ["receiver@example.com"]
        )

    def test_config_loads_daemon_concurrency_settings(self):
        payload = {
            "azure": {
                "tenant_id": "tid",
                "client_id": "cid",
                "client_secret": "secret",
            },
            "user": {"email": "user@example.com", "password": ""},
            "email": {
                "sender": "noreply@example.com",
                "recipients": ["receiver@example.com"],
            },
            "repos": [
                {
                    "name": "courses",
                    "url": "https://example.local/fake.git",
                    "local_path": "./path",
                    "version_file": "courses-version.json",
                    "discover_patterns": ["*.md"],
                }
            ],
            "daemon": {
                "render_concurrency": 3,
                "upload_concurrency": 5,
            },
        }

        path = self._make_config_file(payload)
        try:
            config = Config.from_yaml(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(config.daemon.render_concurrency, 3)
        self.assertEqual(config.daemon.upload_concurrency, 5)

    def test_config_rejects_invalid_daemon_concurrency(self):
        payload = {
            "azure": {
                "tenant_id": "tid",
                "client_id": "cid",
                "client_secret": "secret",
            },
            "user": {"email": "user@example.com", "password": ""},
            "email": {
                "sender": "noreply@example.com",
                "recipients": ["receiver@example.com"],
            },
            "repos": [
                {
                    "name": "courses",
                    "url": "https://example.local/fake.git",
                    "local_path": "./path",
                    "version_file": "courses-version.json",
                    "discover_patterns": ["*.md"],
                }
            ],
            "daemon": {
                "render_concurrency": 0,
                "upload_concurrency": 1,
            },
        }

        path = self._make_config_file(payload)
        try:
            with self.assertRaises(ValueError):
                Config.from_yaml(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)


class TestGraphAuthentication(unittest.TestCase):
    def _make_token(self, scopes: str) -> str:
        header = (
            graph.base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}')
            .decode("ascii")
            .rstrip("=")
        )
        payload = (
            graph.base64.urlsafe_b64encode(
                graph.json.dumps({"scp": scopes}).encode("utf-8")
            )
            .decode("ascii")
            .rstrip("=")
        )
        return f"{header}.{payload}.sig"

    def test_acquire_access_token_uses_confidential_password_grant(self):
        config = Config.from_defaults()
        config.azure.tenant_id = "tenant"
        config.azure.client_id = "client"
        config.azure.client_secret = "secret"
        config.user.email = "user@example.com"
        config.user.password = "password"

        logger = MagicMock()
        token = self._make_token("User.Read Files.ReadWrite")

        with patch.object(
            graph,
            "_acquire_confidential_password_token",
            return_value={"access_token": token},
        ) as mock_password_grant:
            access_token = graph._acquire_access_token(
                config,
                logger,
                graph.ONEDRIVE_SCOPES,
            )

        self.assertEqual(access_token, token)
        mock_password_grant.assert_called_once_with(
            config,
            logger,
            graph.ONEDRIVE_SCOPES,
        )

    def test_acquire_access_token_rejects_token_missing_requested_scope(self):
        config = Config.from_defaults()
        config.azure.tenant_id = "tenant"
        config.azure.client_id = "client"
        config.azure.client_secret = "secret"
        config.user.email = "user@example.com"
        config.user.password = "password"

        logger = MagicMock()
        token = self._make_token("User.Read Mail.Send")

        with patch.object(
            graph,
            "_acquire_confidential_password_token",
            return_value={"access_token": token},
        ):
            access_token = graph._acquire_access_token(
                config,
                logger,
                graph.ONEDRIVE_SCOPES,
            )

        self.assertIsNone(access_token)
        logger.error.assert_any_call(
            "Authentication succeeded but token is missing required scopes %s. Granted scopes: %s",
            ["Files.ReadWrite"],
            ["Mail.Send", "User.Read"],
        )


class TestUploadToAppFolderAsync(unittest.IsolatedAsyncioTestCase):
    def test_get_graph_client_acquires_once_per_run_when_active(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_graph_client_run_cache")

        client = object()

        with patch.object(
            graph, "_build_graph_client", return_value=client
        ) as mock_build:
            graph.begin_graph_run()
            try:
                first = graph.get_graph_client(
                    config,
                    logger,
                    graph.ONEDRIVE_SCOPES,
                )
                second = graph.get_graph_client(
                    config,
                    logger,
                    graph.ONEDRIVE_SCOPES,
                )

            finally:
                graph.end_graph_run()

            self.assertIs(first, client)
            self.assertIs(first, second)
            mock_build.assert_called_once_with(
                config,
                logger,
                graph.ONEDRIVE_SCOPES,
            )

    def test_get_graph_client_caches_separately_per_scope_when_active(
        self,
    ):
        config = Config.from_defaults()
        logger = logging.getLogger("test_graph_client_run_cache_per_scope")

        one_drive_client = object()
        mail_client = object()

        with patch.object(
            graph,
            "_build_graph_client",
            side_effect=[one_drive_client, mail_client],
        ) as mock_build:
            graph.begin_graph_run()
            try:
                one_drive = graph.get_graph_client(
                    config, logger, graph.ONEDRIVE_SCOPES
                )
                mail = graph.get_graph_client(config, logger, graph.MAIL_SCOPES)

            finally:
                graph.end_graph_run()

            self.assertIs(one_drive, one_drive_client)
            self.assertIs(mail, mail_client)
            self.assertEqual(mock_build.call_count, 2)
            self.assertEqual(
                mock_build.call_args_list,
                [
                    ((config, logger, graph.ONEDRIVE_SCOPES), {}),
                    ((config, logger, graph.MAIL_SCOPES), {}),
                ],
            )

    def test_get_graph_client_not_cached_when_run_inactive(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_graph_client_no_cache")
        with patch.object(
            graph,
            "_build_graph_client",
            side_effect=lambda *_args, **_kwargs: object(),
        ) as mock_build:
            first = graph.get_graph_client(config, logger, graph.ONEDRIVE_SCOPES)
            second = graph.get_graph_client(config, logger, graph.ONEDRIVE_SCOPES)

        self.assertIsNot(first, second)
        self.assertEqual(mock_build.call_count, 2)

    async def test_upload_to_appfolder_deletes_existing_file_before_upload(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_upload_to_appfolder_predelete")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_lookup_item_id_by_name",
                AsyncMock(return_value="existing-id"),
            ) as mock_lookup,
            patch.object(
                graph, "delete_from_appfolder", AsyncMock(return_value=True)
            ) as mock_delete,
            patch.object(
                graph,
                "_graph_request_json",
                return_value=(201, {"id": "new-id"}),
            ) as mock_upload,
        ):
            item_id = await graph.upload_to_appfolder(
                "demo.pdf", b"pdf-data", config, logger
            )

            self.assertEqual(item_id, "new-id")
            mock_lookup.assert_awaited_once()
            mock_delete.assert_awaited_once_with(
                "existing-id",
                config,
                logger,
                permanent=True,
            )
            self.assertTrue(
                any(
                    args[0] == "PUT"
                    and args[1] == "/me/drive/special/approot:/demo.pdf:/content"
                    for args, _ in mock_upload.call_args_list
                )
            )

    async def test_upload_to_appfolder_retries_after_conflict(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_upload_to_appfolder_retry")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_lookup_item_id_by_name",
                AsyncMock(return_value="existing-id"),
            ) as mock_lookup,
            patch.object(
                graph, "delete_from_appfolder", AsyncMock(return_value=True)
            ) as mock_delete,
            patch.object(
                graph,
                "_graph_request_json",
                side_effect=[
                    (403, {"error": {"code": "accessDenied"}}),
                    (201, {"id": "new-id"}),
                ],
            ) as mock_upload,
        ):
            item_id = await graph.upload_to_appfolder(
                "demo.pdf", b"pdf-data", config, logger
            )

            self.assertEqual(item_id, "new-id")
            self.assertEqual(mock_upload.call_count, 2)
            self.assertEqual(mock_lookup.await_count, 1)
            mock_delete.assert_awaited_once_with(
                "existing-id",
                config,
                logger,
                permanent=True,
            )

    async def test_delete_from_appfolder_non_permanent_prefers_item_id_delete(self):
        logger = logging.getLogger("test_delete_from_appfolder_not_found")
        config = Config.from_defaults()

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_lookup_item_name_by_id",
                AsyncMock(return_value="demo.pdf"),
            ) as mock_lookup_name,
            patch.object(
                graph,
                "_delete_via_rest_approot_path",
                AsyncMock(return_value=True),
            ) as mock_delete_path,
            patch.object(
                graph,
                "_delete_via_rest_item_id",
                AsyncMock(return_value=True),
            ) as mock_delete_by_id,
        ):
            result = await graph.delete_from_appfolder("01ABCDEFGH123", config, logger)

            self.assertTrue(result)
            mock_delete_by_id.assert_awaited_once_with(
                "01ABCDEFGH123",
                config,
                logger,
                permanent=False,
            )
            mock_lookup_name.assert_not_called()
            mock_delete_path.assert_not_called()

    async def test_delete_from_appfolder_permanent_uses_item_id_delete_endpoint(self):
        logger = logging.getLogger("test_delete_from_appfolder_permanent")
        config = Config.from_defaults()

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_lookup_item_id_by_name",
                AsyncMock(return_value="01ABCDEFGH123"),
            ) as mock_lookup_id,
            patch.object(
                graph,
                "_delete_via_rest_approot_path",
                AsyncMock(return_value=True),
            ) as mock_delete_path,
            patch.object(
                graph,
                "_delete_via_rest_item_id",
                AsyncMock(return_value=True),
            ) as mock_delete_by_id,
        ):
            result = await graph.delete_from_appfolder(
                "demo.pdf",
                config,
                logger,
                permanent=True,
            )

            self.assertTrue(result)
            mock_lookup_id.assert_awaited_once_with(
                "demo.pdf",
                config,
                logger,
            )
            mock_delete_by_id.assert_awaited_once_with(
                "01ABCDEFGH123",
                config,
                logger,
                permanent=True,
            )
            mock_delete_path.assert_not_called()

    async def test_lookup_item_id_by_name_does_not_retry_on_not_found(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_lookup_item_id_not_found")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_graph_request_json",
                return_value=(404, None),
            ) as mock_request,
            patch.object(graph.asyncio, "sleep", AsyncMock()) as mock_sleep,
        ):
            item_id = await graph._lookup_item_id_by_name(
                "courses/lesson/intro.pdf",
                config,
                logger,
            )

        self.assertIsNone(item_id)
        self.assertEqual(mock_request.call_count, 1)
        mock_sleep.assert_not_called()

    async def test_lookup_item_id_by_name_retries_on_transient_error(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_lookup_item_id_retry")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_graph_request_json",
                side_effect=[(503, None), (200, {"id": "item-123"})],
            ) as mock_request,
            patch.object(graph.asyncio, "sleep", AsyncMock()) as mock_sleep,
        ):
            item_id = await graph._lookup_item_id_by_name(
                "courses/lesson/intro.pdf",
                config,
                logger,
            )

        self.assertEqual(item_id, "item-123")
        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_awaited_once()

    async def test_lookup_item_name_by_id_does_not_retry_on_not_found(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_lookup_item_name_not_found")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_graph_request_json",
                return_value=(404, None),
            ) as mock_request,
            patch.object(graph.asyncio, "sleep", AsyncMock()) as mock_sleep,
        ):
            name = await graph._lookup_item_name_by_id("01ABC", config, logger)

        self.assertIsNone(name)
        self.assertEqual(mock_request.call_count, 1)
        mock_sleep.assert_not_called()

    async def test_delete_via_rest_item_id_treats_not_found_as_success(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_delete_item_id_not_found")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(graph, "_graph_request_json", return_value=(404, None)),
        ):
            deleted = await graph._delete_via_rest_item_id(
                "01ABCDEFGH123",
                config,
                logger,
                permanent=True,
            )

        self.assertTrue(deleted)

    async def test_delete_via_rest_item_id_uses_post_for_permanent_delete(self):
        config = Config.from_defaults()
        logger = logging.getLogger("test_delete_item_id_method_permanent")

        client = MagicMock()
        client._graph_access_token = "token"

        with (
            patch.object(graph, "get_graph_client", return_value=client),
            patch.object(
                graph,
                "_graph_request_json",
                return_value=(204, None),
            ) as mock_request,
        ):
            deleted = await graph._delete_via_rest_item_id(
                "01ABCDEFGH123",
                config,
                logger,
                permanent=True,
            )

        self.assertTrue(deleted)
        mock_request.assert_called_once()
        called_args = mock_request.call_args.args
        self.assertEqual(called_args[0], "POST")
        self.assertEqual(
            called_args[1],
            "/me/drive/items/01ABCDEFGH123/permanentDelete",
        )


if __name__ == "__main__":
    unittest.main()
