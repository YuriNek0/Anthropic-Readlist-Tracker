from __future__ import annotations

import inspect
import json
import logging
import asyncio
import threading
import time
import base64
from pathlib import Path
import urllib.error
import urllib.request
import importlib
from urllib.parse import quote, urlencode

from .config import Config
from .models import TrackedDocument


ONEDRIVE_SCOPES = ("User.Read", "Files.ReadWrite")
MAIL_SCOPES = ("User.Read", "Mail.Send")
DEFAULT_DRIVE_ID = "me"

_GRAPH_RUN_CLIENTS: dict[tuple[str, ...], object | None] = {}
_GRAPH_RUN_ACTIVE = False
_GRAPH_RUN_LOCK = threading.Lock()


def _to_graph_url(path: str) -> str:
    if path.startswith("https://") or path.startswith("http://"):
        return path

    if not path.startswith("/"):
        path = f"/{path}"

    return f"https://graph.microsoft.com/v1.0{path}"


def _consent_error_hint(
    config: Config,
    error_text: str,
    scopes: tuple[str, ...],
) -> str:
    if not error_text:
        return ""

    if "AADSTS65001" not in error_text:
        return ""

    scope_text = ", ".join(scopes)
    tenant = config.azure.tenant_id or "<tenant>"
    client_id = config.azure.client_id or "<client-id>"
    return (
        f"The app may be missing admin consent for Graph scopes [{scope_text}]. "
        f"Ask a tenant admin to grant consent at "
        f"https://login.microsoftonline.com/{tenant}/adminconsent?client_id={client_id}"
    )


def _decode_access_token_claims(access_token: str) -> dict[str, object]:
    parts = access_token.split(".")
    if len(parts) != 3:
        return {}

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}".encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}

    if not isinstance(claims, dict):
        return {}
    return claims


def _granted_scope_values(result: dict[str, object], access_token: str) -> set[str]:
    granted: set[str] = set()

    result_scope = result.get("scope")
    if isinstance(result_scope, str):
        granted.update(part.strip() for part in result_scope.split() if part.strip())

    claims = _decode_access_token_claims(access_token)
    token_scope = claims.get("scp")
    if isinstance(token_scope, str):
        granted.update(part.strip() for part in token_scope.split() if part.strip())

    return granted


def _create_msal_app(config: Config, logger: logging.Logger):
    import msal

    authority = f"https://login.microsoftonline.com/{config.azure.tenant_id}"

    logger.debug("Using MSAL ConfidentialClientApplication")
    return msal.ConfidentialClientApplication(
        client_id=config.azure.client_id,
        client_credential=config.azure.client_secret,
        authority=authority,
    )


def _acquire_confidential_password_token(
    config: Config,
    logger: logging.Logger,
    scopes: tuple[str, ...],
) -> dict[str, object] | None:
    token_url = (
        f"https://login.microsoftonline.com/{config.azure.tenant_id}/oauth2/v2.0/token"
    )
    payload = urlencode(
        {
            "client_id": config.azure.client_id,
            "client_secret": config.azure.client_secret,
            "grant_type": "password",
            "username": config.user.email,
            "password": config.user.password,
            "scope": " ".join(scopes),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
            logger.error("Authentication failed: token endpoint returned non-object")
            return None
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        try:
            result = json.loads(error_body)
        except json.JSONDecodeError:
            result = {
                "error_description": error_body or f"HTTP {getattr(e, 'code', 'error')}"
            }
        logger.debug("Confidential password grant failed: %s", error_body[:300])
        return result if isinstance(result, dict) else None
    except Exception as e:
        logger.error(f"Failed to acquire access token: {e}")
        return None


def _error_code_present(value: object, code: int) -> bool:
    if isinstance(value, list):
        return any(str(item) == str(code) for item in value)
    if isinstance(value, tuple):
        return any(str(item) == str(code) for item in value)
    if isinstance(value, set):
        return any(str(item) == str(code) for item in value)
    return False


def _get_drive_item_id(upload_result: object, logger: logging.Logger) -> str | None:
    if upload_result is None:
        return None

    return _extract_id_from_object(upload_result)


def _extract_id_from_object(
    value: object, max_depth: int = 6, _depth: int = 0, _seen: set[int] | None = None
) -> str | None:
    if _seen is None:
        _seen = set()

    if _depth >= max_depth:
        return None

    value_id = id(value)
    if value_id in _seen:
        return None
    _seen.add(value_id)

    if value is None:
        return None

    if isinstance(value, (bytes, bytearray)):
        try:
            return _extract_id_from_object(
                value.decode("utf-8", errors="ignore"),
                max_depth=max_depth,
                _depth=_depth + 1,
                _seen=_seen,
            )
        except Exception:
            return None

    if isinstance(value, str):
        value_text = value.strip()
        if len(value_text) == 36:
            return value_text
        if value_text.startswith("{") and value_text.endswith("}"):
            try:
                return _extract_id_from_object(
                    json.loads(value_text),
                    max_depth=max_depth,
                    _depth=_depth + 1,
                    _seen=_seen,
                )
            except json.JSONDecodeError:
                return None
        return None

    if isinstance(value, dict):
        candidate = value.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate

        for nested in value.values():
            candidate = _extract_id_from_object(nested, max_depth, _depth + 1, _seen)
            if candidate:
                return candidate

        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            candidate = _extract_id_from_object(item, max_depth, _depth + 1, _seen)
            if candidate:
                return candidate
        return None

    attr_value = getattr(value, "id", None)
    if isinstance(attr_value, str) and attr_value:
        return attr_value

    additional_data = getattr(value, "additional_data", None)
    if isinstance(additional_data, dict):
        candidate = additional_data.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in additional_data.values():
            candidate = _extract_id_from_object(nested, max_depth, _depth + 1, _seen)
            if candidate:
                return candidate

    try:
        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            for nested in value_dict.values():
                candidate = _extract_id_from_object(
                    nested, max_depth, _depth + 1, _seen
                )
                if candidate:
                    return candidate
    except Exception:
        pass

    return None


def _extract_link_url(payload: object) -> str | None:
    if payload is None:
        return None

    if isinstance(payload, (list, tuple)):
        for item in payload:
            link = _extract_link_url(item)
            if link:
                return link
        return None

    if isinstance(payload, (str, bytes)):
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8", errors="ignore")
            except Exception:
                return None
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        link_obj = getattr(payload, "link", None)
        if link_obj:
            link_web_url = getattr(link_obj, "web_url", None) or getattr(
                link_obj, "webUrl", None
            )
            if isinstance(link_web_url, str) and link_web_url:
                return link_web_url

            payload_dict = getattr(link_obj, "__dict__", None)
            if isinstance(payload_dict, dict):
                value = payload_dict.get("webUrl") or payload_dict.get("web_url")
                if isinstance(value, str) and value:
                    return value

        payload_dict = getattr(payload, "__dict__", None)
        if isinstance(payload_dict, dict):
            payload = payload_dict

    if isinstance(payload, dict):
        if "value" in payload:
            values = payload.get("value")
            if isinstance(values, (list, tuple)):
                for value in values:
                    link = _extract_link_url(value)
                    if link:
                        return link

        link = payload.get("link")
        if isinstance(link, dict):
            return link.get("webUrl") or link.get("web_url")
        direct = payload.get("webUrl") or payload.get("web_url")
        if isinstance(direct, str):
            return direct

    return None


def _build_share_link_payload(config: Config | None = None) -> dict[str, object]:
    return {
        "type": "edit",
        "scope": "users",
    }


def _build_create_link_request_payload() -> object:
    payload = _build_share_link_payload(None)

    candidate_request_types = [
        ("msgraph.generated.models.create_link", "CreateLink"),
        (
            "msgraph.generated.models.create_link_post_request_body",
            "CreateLinkPostRequestBody",
        ),
        (
            "msgraph.generated.models.create_link_request_body",
            "CreateLinkRequestBody",
        ),
        (
            "msgraph.generated.drives.item.items.item.create_link.create_link_post_request_body",
            "CreateLinkPostRequestBody",
        ),
    ]

    for module_name, class_name in candidate_request_types:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        request_cls = getattr(module, class_name, None)
        if not request_cls:
            continue

        for kwargs in (
            {"type": payload["type"], "scope": payload["scope"]},
            {"link_type": payload["type"], "scope": payload["scope"]},
            {
                "type": payload["type"],
                "scope": payload["scope"],
                "link_type": payload["type"],
            },
        ):
            try:
                return request_cls(**kwargs)
            except Exception:
                continue

    return payload


def _normalize_share_recipients(values: object) -> list[str]:
    if not isinstance(values, list):
        return []

    recipients: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        recipients.append(value)
    return recipients


def _normalize_domain_suffix(domain: object) -> str:
    if not isinstance(domain, str):
        return ""

    normalized = domain.strip().lower()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    return normalized


def _email_matches_domain(email: str, domain_suffix: str) -> bool:
    normalized_email = email.strip().lower()
    if "@" not in normalized_email:
        return False

    normalized_domain = _normalize_domain_suffix(domain_suffix)
    if not normalized_domain:
        return True

    return normalized_email.endswith(f"@{normalized_domain}")


def _share_recipients_for_links(config: Config) -> list[str]:
    explicit = getattr(config.email, "share_recipients", None)
    share_with = _normalize_share_recipients(explicit)
    if not share_with:
        share_with = _normalize_share_recipients(
            getattr(config.email, "recipients", None)
        )

    if not getattr(config.email, "share_domain_filter_enabled", False):
        return share_with

    allowed_domain = getattr(config.email, "share_domain", "")
    if not _normalize_domain_suffix(allowed_domain):
        return share_with

    return [
        recipient
        for recipient in share_with
        if _email_matches_domain(recipient, allowed_domain)
    ]


def _build_invite_payload(recipients: list[str]) -> str:
    payload = {
        "requireSignIn": True,
        "sendInvitation": False,
        "roles": ["read"],
        "recipients": [{"email": email} for email in recipients],
        "message": "You have been granted access to an Anthropic Reading PDF.",
        "retainInheritedPermissions": False,
    }
    return json.dumps(payload)


def _get_drive_item_web_url(
    item_id: str,
    access_token: str,
    logger: logging.Logger,
) -> str | None:
    status, payload = _graph_request_json(
        "GET",
        f"/me/drive/items/{quote(item_id, safe='')}?$select=webUrl",
        access_token,
        logger,
    )

    if not (status and 200 <= status < 300):
        return None

    if isinstance(payload, dict):
        web_url = payload.get("webUrl") or payload.get("web_url")
        if isinstance(web_url, str) and web_url:
            return web_url

    return _extract_link_url(payload)


def _create_share_link_for_specific_users(
    item_id: str,
    config: Config,
    logger: logging.Logger,
    recipients: list[str] | None = None,
) -> str | None:
    if recipients is None:
        recipients = _share_recipients_for_links(config)
    if not recipients:
        return None

    client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not client:
        return None

    access_token = getattr(client, "_graph_access_token", None)
    if not access_token:
        return None

    status, response = _graph_request_json(
        "POST",
        f"/me/drive/items/{quote(item_id, safe='')}/invite",
        access_token,
        logger,
        body=_build_invite_payload(recipients),
    )

    if not (status and 200 <= status < 300):
        logger.debug(
            "REST invite request failed for %s (status=%s) recipients=%s",
            item_id,
            status,
            recipients,
        )
        return None

    link_url = _extract_link_url(response)
    if link_url:
        return link_url

    item_web_url = _get_drive_item_web_url(item_id, access_token, logger)
    if item_web_url:
        logger.debug(
            "Invite response had no webUrl for %s; using item webUrl instead",
            item_id,
        )
        return item_web_url

    logger.debug(
        "REST invite returned no webUrl for %s for recipients %s. Response payload: %s",
        item_id,
        recipients,
        _describe_payload(response),
    )
    return None


async def _create_share_link_with_sdk(
    client_item, item_id: str, logger: logging.Logger
) -> str | None:
    if client_item is None:
        return None

    try:
        link_request = _build_create_link_request_payload()
    except Exception as e:
        logger.debug(
            "Failed to build SDK create-link request for %s; using REST fallback: %s",
            item_id,
            e,
        )
        return None

    try:
        try:
            result_link = client_item.create_link.post(link_request)
        except TypeError:
            # Different SDK releases sometimes require named body argument.
            result_link = client_item.create_link.post(body=link_request)

        if inspect.isawaitable(result_link):
            result_link = await result_link
    except Exception as e:
        logger.debug(
            "SDK create_link request failed for %s with payload %s: %s",
            item_id,
            type(link_request).__name__,
            e,
        )
        return None

    web_url = _extract_link_url(result_link)
    if web_url:
        return web_url

    logger.debug(
        "SDK create_link response missing link URL for %s. Response payload: %s",
        item_id,
        _describe_payload(result_link),
    )
    return None


def _acquire_access_token(
    config: Config, logger: logging.Logger, scopes: tuple[str, ...]
) -> str | None:
    if not config.azure.tenant_id or not config.azure.client_id:
        logger.warning("Azure credentials not configured (need tenant_id, client_id)")
        return None

    if not config.user.email and not config.azure.client_secret:
        logger.warning(
            "Azure credentials not configured (need client_secret when no delegated user is configured)"
        )
        return None

    try:
        import msal
    except ImportError:
        logger.error(
            "MS Graph dependencies not installed. Run: uv pip install msgraph-sdk msal"
        )
        return None

    try:
        if config.user.email and config.user.password:
            logger.debug("Using confidential password grant for delegated user auth")
            result = _acquire_confidential_password_token(config, logger, scopes)
            if result is None:
                return None
        else:
            app = _create_msal_app(config, logger)
            flow = app.acquire_token_device_code(
                scopes=list(scopes),
                account=config.user.email if config.user.email else None,
            )
            if "message" in flow:
                logger.info(f"Device code: {flow['message']}")
            result = app.acquire_token_by_device_code(flow)

        if "access_token" not in result:
            error_desc = result.get("error_description", "Unknown error")
            error_codes = result.get("error_codes", [])
            hint = _consent_error_hint(config, str(error_desc), scopes)
            if "AADSTS65001" in str(error_desc) or _error_code_present(
                error_codes, 65001
            ):
                logger.error(
                    "Authentication failed: %s%s",
                    error_desc,
                    f". {hint}" if hint else "",
                )
                return None

            logger.error(
                "Authentication failed: %s%s",
                error_desc,
                f". {hint}" if hint else "",
            )
            return None

        access_token = result["access_token"]
        granted_scopes = _granted_scope_values(result, access_token)
        missing_scopes = [scope for scope in scopes if scope not in granted_scopes]
        if missing_scopes:
            logger.error(
                "Authentication succeeded but token is missing required scopes %s. Granted scopes: %s",
                missing_scopes,
                sorted(granted_scopes),
            )
            return None

        logger.debug(
            "Acquired Graph token for scopes %s; granted scopes: %s",
            list(scopes),
            sorted(granted_scopes),
        )
        return access_token
    except Exception as e:
        logger.error(f"Failed to acquire access token: {e}")
        return None


def _graph_request_json(
    method: str,
    path: str,
    access_token: str,
    logger: logging.Logger,
    body: bytes | str | None = None,
    content_type: str = "application/json",
) -> tuple[int | None, object | None]:
    url = f"https://graph.microsoft.com/v1.0{path}"
    request_body = body
    if isinstance(body, str):
        request_body = body.encode("utf-8")

    request = urllib.request.Request(
        url,
        data=request_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": content_type,
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            if not payload:
                return getattr(response, "status", None), None

            text = payload.decode("utf-8", errors="ignore")
            try:
                return getattr(response, "status", None), json.loads(text)
            except json.JSONDecodeError:
                logger.debug(
                    "Graph response for %s %s was not JSON: %s",
                    method,
                    path,
                    text[:200],
                )
                return getattr(response, "status", None), None
    except urllib.error.HTTPError as e:
        error_body = None
        try:
            raw = e.read()
            if raw:
                error_body = raw.decode("utf-8", errors="ignore")
        except Exception:
            pass
        status = getattr(e, "code", None)
        if status == 404:
            return status, None

        message = error_body[:200] if error_body else "<empty>"
        if status in {400, 403}:
            logger.error(
                "Graph request failed for %s %s (status=%s): %s",
                method,
                path,
                status,
                message,
            )
        else:
            logger.debug(
                "Graph request failed for %s %s (status=%s): %s",
                method,
                path,
                status,
                message,
            )
        return status, None
    except Exception as e:
        logger.debug(
            "Graph request error for %s %s: %s",
            method,
            path,
            e,
        )
        return None, None


def _create_share_link_rest(
    item_id: str,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not client:
        return None

    access_token = getattr(client, "_graph_access_token", None)
    if not access_token:
        return None

    payload = json.dumps(_build_share_link_payload(config))
    _, response = _graph_request_json(
        "POST",
        f"/me/drive/items/{quote(item_id, safe='')}/createLink",
        access_token,
        logger,
        body=payload,
    )

    link_url = _extract_link_url(response)
    if link_url:
        return link_url

    logger.debug(
        "REST createLink returned no webUrl for %s. Response payload: %s",
        item_id,
        _describe_payload(response),
    )
    return None


async def _create_share_link_from_item_id(
    item_id: str,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not client:
        logger.error(
            "Skipping create link for item %s because Graph client initialization failed. "
            "This is commonly an authentication/consent issue for app %s on tenant %s.",
            item_id,
            config.azure.client_id or "<missing-client-id>",
            config.azure.tenant_id or "<missing-tenant-id>",
        )
        return None

    try:
        drive_item = client.drives.by_drive_id(DEFAULT_DRIVE_ID).items.by_drive_item_id(
            item_id
        )

        web_url = await _create_share_link_with_sdk(drive_item, item_id, logger)
        if web_url:
            return web_url
    except Exception as e:
        logger.debug(
            "SDK create_link request failed for %s: %s",
            item_id,
            e,
        )
        return None


async def _lookup_item_id_by_name(
    file_name: str,
    config: Config,
    logger: logging.Logger,
    client: object | None = None,
) -> str | None:
    resolved_client = client
    if not resolved_client:
        resolved_client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not resolved_client:
        return None

    access_token = getattr(resolved_client, "_graph_access_token", None)
    if not isinstance(access_token, str) or not access_token:
        return None

    encoded_name = quote(file_name, safe="/")
    for attempt in range(1, 4):
        status, payload = _graph_request_json(
            "GET",
            f"/me/drive/special/approot:/{encoded_name}?$select=id,name",
            access_token,
            logger,
        )

        if status and 200 <= status < 300:
            item_id = _get_drive_item_id(payload, logger)
            if item_id:
                logger.debug(
                    "Resolved AppFolder item by direct path lookup on attempt %s: %s",
                    attempt,
                    file_name,
                )
                return item_id
            return None

        if status == 404:
            logger.debug("AppFolder item not found by path: %s", file_name)
            return None

        if not _should_retry_graph_get(status):
            break

        if attempt < 3:
            await asyncio.sleep(_retry_wait_seconds(attempt))

    if "/" in file_name or "\\" in file_name:
        return None

    escaped_name = file_name.replace("'", "''")
    query_expr = f"name eq '{escaped_name}'"
    query_value = quote(query_expr, safe="')$(")
    children_path_variants = [
        f"/me/drive/special/approot/children?$select=id,name&$filter={query_value}",
        "/me/drive/special/approot/children?$select=id,name",
    ]

    for children_path in children_path_variants:
        for attempt in range(1, 4):
            status, payload = _graph_request_json(
                "GET",
                children_path,
                access_token,
                logger,
            )

            if status and 200 <= status < 300:
                values = (
                    payload.get("value", payload)
                    if isinstance(payload, dict)
                    else payload
                )
                if not isinstance(values, list):
                    break

                for child in values:
                    if isinstance(child, dict):
                        name = child.get("name")
                        child_id = child.get("id")
                    else:
                        name = getattr(child, "name", None)
                        child_id = _get_drive_item_id(child, logger)

                    if name == file_name and isinstance(child_id, str) and child_id:
                        logger.debug(
                            "Resolved AppFolder item by children lookup on attempt %s: %s",
                            attempt,
                            file_name,
                        )
                        return child_id

                return None

            if status == 404:
                return None

            if not _should_retry_graph_get(status):
                break

            if attempt < 3:
                await asyncio.sleep(_retry_wait_seconds(attempt))

    return None


async def _get_approot_item_id(
    config: Config,
    logger: logging.Logger,
    client: object | None = None,
) -> str | None:
    resolved_client = client
    if not resolved_client:
        resolved_client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not resolved_client:
        return None

    access_token = getattr(resolved_client, "_graph_access_token", None)
    if not isinstance(access_token, str) or not access_token:
        return None

    _, payload = _graph_request_json(
        "GET",
        "/me/drive/special/approot?$select=id,name,specialFolder",
        access_token,
        logger,
    )
    approot_id = _get_drive_item_id(payload, logger)
    if approot_id:
        logger.debug("Resolved AppFolder approot id: %s", approot_id)
        return approot_id

    logger.error("Failed to resolve AppFolder approot item id")
    return None


def _build_child_file_payload(file_name: str) -> str:
    return json.dumps(
        {
            "name": file_name,
            "file": {},
            "@microsoft.graph.conflictBehavior": "replace",
        }
    )


def _should_retry_graph_get(status: int | None) -> bool:
    if status is None:
        return True

    return status in {408, 425, 429, 500, 502, 503, 504}


def _retry_wait_seconds(attempt: int) -> float:
    return min(0.5 * attempt, 1.5)


async def _create_appfolder_file_item(
    file_name: str,
    approot_id: str,
    access_token: str,
    logger: logging.Logger,
) -> str | None:
    _, payload = _graph_request_json(
        "POST",
        f"/me/drive/items/{quote(approot_id, safe='')}/children",
        access_token,
        logger,
        body=_build_child_file_payload(file_name),
    )
    item_id = _get_drive_item_id(payload, logger)
    if item_id:
        logger.debug("Created AppFolder file item for %s: %s", file_name, item_id)
        return item_id

    logger.debug(
        "Failed to create AppFolder file item for %s. Response: %s",
        file_name,
        _describe_payload(payload),
    )
    return None


async def _lookup_item_name_by_id(
    item_id: str,
    config: Config,
    logger: logging.Logger,
    client: object | None = None,
) -> str | None:
    resolved_client = client
    if not resolved_client:
        resolved_client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not resolved_client:
        return None

    access_token = getattr(resolved_client, "_graph_access_token", None)
    if not isinstance(access_token, str) or not access_token:
        return None

    for attempt in range(1, 4):
        status, payload = _graph_request_json(
            "GET",
            f"/me/drive/items/{quote(item_id, safe='')}?$select=id,name",
            access_token,
            logger,
        )

        if status and 200 <= status < 300:
            if isinstance(payload, dict):
                name = payload.get("name")
                if isinstance(name, str) and name:
                    logger.debug(
                        "Resolved AppFolder item name by id on attempt %s: %s",
                        attempt,
                        name,
                    )
                    return name
            return None

        if status == 404:
            return None

        if not _should_retry_graph_get(status):
            break

        if attempt < 3:
            await asyncio.sleep(_retry_wait_seconds(attempt))

    logger.debug("Could not resolve AppFolder item name for id %s", item_id)
    return None


async def _predelete_existing_item(
    file_name: str,
    config: Config,
    logger: logging.Logger,
    client: object | None = None,
) -> bool:
    item_id = await _lookup_item_id_by_name(
        file_name,
        config,
        logger,
        client=client,
    )
    if not item_id:
        return True

    logger.info(
        "Pre-deleting existing AppFolder item %s before upload (item_id=%s)",
        file_name,
        item_id,
    )

    deleted = await delete_from_appfolder(
        item_id,
        config,
        logger,
        permanent=True,
    )
    if deleted:
        logger.debug(
            "Deleted existing AppFolder item %s before uploading to avoid overwrite conflicts",
            file_name,
        )
    else:
        logger.error(
            "Failed to delete existing AppFolder item %s before upload; upload may fail if overwrite is blocked",
            file_name,
        )

    return deleted


def _describe_payload(payload: object, *, max_len: int = 800) -> str:
    if payload is None:
        return "<None>"

    if isinstance(payload, dict):
        preview = payload
    else:
        preview = getattr(payload, "__dict__", None) or payload

    if isinstance(preview, (dict, list, tuple)):
        text = str(preview)
    else:
        text = repr(preview)

    text = text.replace("\n", " ")
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def _looks_like_path_name(value: str) -> bool:
    if "/" in value or "\\" in value or "." in value:
        return True

    compact = value.strip()
    if compact.startswith("01") and len(compact) >= 8 and compact.isalnum():
        return False

    if len(compact) < 20:
        return True

    has_alpha = any(ch.isalpha() for ch in compact)
    has_digit = any(ch.isdigit() for ch in compact)
    return not (has_alpha and has_digit)


async def _delete_via_rest_approot_path(
    path_name: str,
    config: Config,
    logger: logging.Logger,
    client: object | None = None,
) -> bool:
    resolved_client = client
    if not resolved_client:
        resolved_client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not resolved_client:
        return False

    access_token = getattr(resolved_client, "_graph_access_token", "")
    if not access_token:
        return False

    encoded_name = quote(path_name, safe="/")
    status, _ = _graph_request_json(
        "DELETE",
        f"/me/drive/special/approot:/{encoded_name}:",
        access_token,
        logger,
    )

    if status and 200 <= status < 300:
        logger.info(f"Deleted AppFolder path in AppFolder: {path_name}")
        return True

    if status == 404:
        logger.debug("AppFolder path already absent: %s", path_name)
        return True

    logger.debug(
        "REST approot path delete failed for %s (status=%s)", path_name, status
    )
    return False


async def _delete_via_rest_item_id(
    item_id: str,
    config: Config,
    logger: logging.Logger,
    permanent: bool,
    client: object | None = None,
) -> bool:
    resolved_client = client
    if not resolved_client:
        resolved_client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not resolved_client:
        return False

    access_token = getattr(resolved_client, "_graph_access_token", "")
    if not access_token:
        return False

    method = "DELETE"
    path = f"/me/drive/items/{quote(item_id, safe='')}"
    if permanent:
        method = "POST"
        path = f"{path}/permanentDelete"

    status, _ = _graph_request_json(
        method,
        path,
        access_token,
        logger,
    )

    if status and 200 <= status < 300:
        logger.info(
            "Deleted AppFolder item by id via REST (%s): %s",
            "permanent" if permanent else "standard",
            item_id,
        )
        return True

    if status == 404:
        logger.debug(
            "AppFolder item already absent for delete (%s): %s",
            "permanent" if permanent else "standard",
            item_id,
        )
        return True

    logger.debug(
        "REST item-id delete failed for %s (status=%s, permanent=%s)",
        item_id,
        status,
        permanent,
    )
    return False


class RawAccessTokenProvider:
    def __init__(self, token: str):
        self.token = token
        self.expires_on = int(time.time()) + 3600

    def get_token(self, *scopes, **kwargs):
        # Returns the existing token; no automatic refresh in this flow.
        from azure.core.credentials import AccessToken

        return AccessToken(self.token, expires_on=self.expires_on)


def begin_graph_run() -> None:
    """Begin a run-scoped Graph client cache for this process invocation."""
    global _GRAPH_RUN_ACTIVE
    with _GRAPH_RUN_LOCK:
        _GRAPH_RUN_ACTIVE = True
        _GRAPH_RUN_CLIENTS.clear()


def end_graph_run() -> None:
    """Clear run-scoped Graph clients at the end of a daemon run."""
    global _GRAPH_RUN_ACTIVE
    with _GRAPH_RUN_LOCK:
        _GRAPH_RUN_ACTIVE = False
        _GRAPH_RUN_CLIENTS.clear()


def _build_graph_client(
    config: Config,
    logger: logging.Logger,
    scopes: tuple[str, ...],
):
    access_token = _acquire_access_token(config, logger, scopes)
    if not access_token:
        return None

    try:
        from msgraph import GraphServiceClient

        client = GraphServiceClient(
            credentials=RawAccessTokenProvider(access_token),
            scopes=list(scopes),
        )
        setattr(client, "_graph_access_token", access_token)
        if hasattr(client, "request_adapter") and hasattr(
            client.request_adapter, "base_url"
        ):
            client.request_adapter.base_url = "https://graph.microsoft.com/v1.0"
        return client
    except ImportError:
        logger.error(
            "MS Graph dependencies not installed. Run: uv pip install msgraph-sdk msal"
        )
        return None
    except Exception as e:
        logger.error(f"Failed to initialize Graph client: {e}")
        return None


def get_graph_client(
    config: Config,
    logger: logging.Logger,
    scopes: tuple[str, ...],
):
    scope_key = tuple(scopes)

    if _GRAPH_RUN_ACTIVE:
        with _GRAPH_RUN_LOCK:
            if scope_key in _GRAPH_RUN_CLIENTS:
                return _GRAPH_RUN_CLIENTS.get(scope_key)

            client = _build_graph_client(config, logger, scopes)
            _GRAPH_RUN_CLIENTS[scope_key] = client
            return client

    client = _build_graph_client(config, logger, scopes)
    return client


async def upload_to_appfolder(
    file_name: str,
    file_content: bytes,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not client:
        logger.error(
            "Failed to upload %s. Graph client initialization failed; check Azure app consent for "
            "Files.ReadWrite scope and user permissions.",
            file_name,
        )
        return None

    access_token = getattr(client, "_graph_access_token", None)
    encoded_name = quote(file_name, safe="/")
    result_upload: object | None = None

    predelete_ok = await _predelete_existing_item(
        file_name,
        config,
        logger,
        client=client,
    )
    if not predelete_ok:
        logger.debug(
            "Proceeding with upload for %s after pre-delete failure; overwrite may still succeed or fail.",
            file_name,
        )

    content_url = f"/me/drive/special/approot:/{encoded_name}:/content"
    content_url_full = _to_graph_url(content_url)

    try:
        if isinstance(access_token, str) and access_token:
            status, payload = _graph_request_json(
                "PUT",
                content_url,
                access_token,
                logger,
                body=file_content,
                content_type="application/octet-stream",
            )

            result_upload = payload
            if status and 200 <= status < 300:
                item_id = _get_drive_item_id(payload, logger)
                if item_id:
                    return item_id

                logger.debug(
                    "REST upload for %s succeeded with status %s but no item id. "
                    "Falling back to AppFolder lookup by path.",
                    file_name,
                    status,
                )
                lookup_id = await _lookup_item_id_by_name(
                    file_name,
                    config,
                    logger,
                    client=client,
                )
                if lookup_id:
                    return lookup_id
                logger.debug(
                    "Could not resolve AppFolder item id for %s after successful REST upload despite lookup attempt. "
                    "Proceeding as failed upload for safe rollback handling.",
                    file_name,
                )
                return None
            if status in {403, 400}:
                logger.error(
                    "REST upload for %s returned status=%s. Response: %s",
                    file_name,
                    status,
                    _describe_payload(payload),
                )

                if predelete_ok:
                    retry_status, retry_payload = (None, None)
                    try:
                        retry_status, retry_payload = _graph_request_json(
                            "PUT",
                            content_url,
                            access_token,
                            logger,
                            body=file_content,
                            content_type="application/octet-stream",
                        )
                    except Exception as e:
                        logger.debug(
                            "Retry REST upload failed for %s: %s", file_name, e
                        )

                    if retry_status and 200 <= retry_status < 300:
                        result_upload = retry_payload
                        item_id = _get_drive_item_id(retry_payload, logger)
                        if item_id:
                            return item_id

                        logger.debug(
                            "REST upload retry for %s returned success but no item id. "
                            "Falling back to AppFolder lookup by path.",
                            file_name,
                        )
                        lookup_id = await _lookup_item_id_by_name(
                            file_name,
                            config,
                            logger,
                            client=client,
                        )
                        if lookup_id:
                            return lookup_id

                        logger.debug(
                            "Could not resolve AppFolder item id for %s after successful REST retry despite lookup attempt. "
                            "Proceeding as failed upload for safe rollback handling.",
                            file_name,
                        )
                        return None

                    logger.debug(
                        "REST upload retry for %s still failed (status=%s): %s",
                        file_name,
                        retry_status,
                        _describe_payload(retry_payload),
                    )
            else:
                logger.debug(
                    "REST upload for %s returned non-success status=%s. Response: %s",
                    file_name,
                    status,
                    _describe_payload(payload),
                )
    except Exception as e:
        logger.debug(f"REST upload failed for {file_name}: {e}")
        result_upload = None

    try:
        drive = client.drives.by_drive_id(DEFAULT_DRIVE_ID)
    except Exception as e:
        logger.debug("Failed to open SDK drive reference for %s: %s", file_name, e)
        logger.debug(
            "Unable to create an SDK upload path for %s. Continuing with upload failure handling.",
            file_name,
        )
        return None

    try:
        result_upload = (
            drive.items.by_drive_item_id(file_name)
            .content.with_url(content_url_full)
            .put(file_content)
        )
        if inspect.isawaitable(result_upload):
            result_upload = await result_upload
        item_id = _get_drive_item_id(result_upload, logger)
        if item_id:
            return item_id

        logger.debug(
            "SDK primary upload path for %s returned no item id. Response: %s",
            file_name,
            _describe_payload(result_upload),
        )
    except Exception as e:
        logger.debug("Primary SDK upload path failed for %s: %s", file_name, e)
        result_upload = None

    # Secondary attempt using the special-item builder path form.
    try:
        result_upload = (
            drive.special.by_drive_item_id("approot")
            .with_url(content_url_full)
            .content.put(file_content)
        )
        if inspect.isawaitable(result_upload):
            result_upload = await result_upload
        item_id = _get_drive_item_id(result_upload, logger)
        if item_id:
            return item_id

        logger.debug(
            "SDK secondary upload path for %s returned no item id. Response: %s",
            file_name,
            _describe_payload(result_upload),
        )
    except Exception as e:
        logger.debug("Secondary SDK upload path failed for %s: %s", file_name, e)
        result_upload = None

    logger.error(
        "Upload returned no item id for %s. Response payloads: %s",
        file_name,
        _describe_payload(result_upload),
    )
    return None


async def delete_from_appfolder(
    item_id: str,
    config: Config,
    logger: logging.Logger,
    permanent: bool = False,
) -> bool:
    if not item_id:
        return False

    if permanent:
        target_id = item_id
        if _looks_like_path_name(item_id):
            target_id = await _lookup_item_id_by_name(
                item_id,
                config,
                logger,
            )
            if not target_id:
                logger.debug(
                    "Permanent delete target already absent or unresolved in AppFolder: %s",
                    item_id,
                )
                return True

        if await _delete_via_rest_item_id(
            target_id,
            config,
            logger,
            permanent=True,
        ):
            logger.info(
                "Deleted AppFolder item permanently during rollback: %s",
                item_id,
            )
            return True

        logger.warning(
            "Failed permanent delete for AppFolder item %s",
            item_id,
        )
        return False

    if _looks_like_path_name(item_id):
        if await _delete_via_rest_approot_path(item_id, config, logger):
            logger.info(
                "Deleted AppFolder path during rollback: %s",
                item_id,
            )
            return True

        logger.warning(
            "Failed delete for AppFolder path %s (permanent=%s)",
            item_id,
            permanent,
        )
        return False

    if await _delete_via_rest_item_id(
        item_id,
        config,
        logger,
        permanent=False,
    ):
        logger.info(
            "Deleted AppFolder item during rollback by id: %s",
            item_id,
        )
        return True

    path = await _lookup_item_name_by_id(
        item_id,
        config,
        logger,
    )
    if not path:
        logger.debug(
            "Could not resolve AppFolder item name for rollback delete target %s",
            item_id,
        )
        return False

    if await _delete_via_rest_approot_path(path, config, logger):
        logger.info(
            "Deleted AppFolder item during rollback: %s",
            item_id,
        )
        return True

    logger.warning(
        "Failed rollback delete for AppFolder item %s (permanent=%s)",
        item_id,
        permanent,
    )
    return False


async def _download_appfolder_json(
    version_file_name: str,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    client = get_graph_client(config, logger, ONEDRIVE_SCOPES)
    if not client:
        return None

    try:
        drive = client.drives.by_drive_id(DEFAULT_DRIVE_ID)
        version_path = (
            f"/me/drive/special/approot:/{quote(version_file_name, safe='/')}:/content"
        )
        content_request = drive.items.by_drive_item_id(
            version_file_name
        ).content.with_url(_to_graph_url(version_path))
        content = content_request.get()
        if inspect.isawaitable(content):
            content = await content

        if content is None:
            return None

        if isinstance(content, str):
            return content

        if isinstance(content, (bytes, bytearray)):
            return content.decode("utf-8")

        additional_data = getattr(content, "additional_data", None)
        if isinstance(additional_data, dict):
            return additional_data.get("content")

        if isinstance(content, dict):
            return content.get("content") or content.get("value")

        logger.debug(f"Unexpected content response type: {type(content)}")
        return _describe_payload(content)
    except Exception as e:
        logger.debug(f"Version file not found in AppFolder: {e}")
        return None


def parse_version_file_content(
    raw_content: str, logger: logging.Logger
) -> dict[str, TrackedDocument] | None:
    if not raw_content:
        return None

    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse version file JSON: {e}")
        return None

    if not isinstance(data, list):
        logger.warning("Version file content is not a list")
        return None

    documents: dict[str, TrackedDocument] = {}
    for item in data:
        if not isinstance(item, dict):
            logger.debug("Skipping non-object entry in version file")
            continue

        doc = TrackedDocument(
            path=item.get("path", ""),
            title=item.get("title", ""),
            date=item.get("date", ""),
            content_hash=item.get("content_hash"),
        )
        if doc.path:
            documents[doc.path] = doc

    return documents


async def download_version_from_onedrive(
    version_file_name: str,
    config: Config,
    logger: logging.Logger,
    repo_name: str = "",
) -> dict[str, TrackedDocument] | None:
    clean_version_name = Path(version_file_name).name
    clean_repo_name = repo_name.strip()
    scoped_version_name = (
        f"{clean_repo_name}/{clean_version_name}"
        if clean_repo_name
        else clean_version_name
    )

    raw_content = await _download_appfolder_json(scoped_version_name, config, logger)
    if not raw_content and clean_repo_name:
        logger.debug(
            "Repo-scoped version file not found at %s; trying legacy root path %s",
            scoped_version_name,
            clean_version_name,
        )
        raw_content = await _download_appfolder_json(clean_version_name, config, logger)

    if not raw_content:
        return None
    return parse_version_file_content(raw_content, logger)


async def upload_version_to_onedrive(
    version_file_name: str,
    documents: dict[str, TrackedDocument],
    config: Config,
    logger: logging.Logger,
    onedrive_path: str | None = None,
) -> str | None:
    try:
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

        file_content = json.dumps(data, indent=2).encode("utf-8")
        target_path = onedrive_path or Path(version_file_name).name
        item_id = await upload_to_appfolder(target_path, file_content, config, logger)
        if item_id:
            logger.info(
                "Uploaded version file %s to OneDrive path %s",
                version_file_name,
                target_path,
            )
        return item_id
    except Exception as e:
        logger.error(f"Failed to upload version to OneDrive: {e}")
        return None


async def create_sharing_link(
    item_id: str,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    try:
        recipients = _share_recipients_for_links(config)
        if recipients:
            share_url = _create_share_link_for_specific_users(
                item_id,
                config,
                logger,
                recipients=recipients,
            )
            if share_url:
                return share_url

            logger.error(
                "Failed to create recipient-scoped sharing link for %s using invite API; "
                "no unrestricted link fallback will be created.",
                item_id,
            )
            return None

        logger.error(
            "No eligible recipients for sharing link of %s after applying configured filters.",
            item_id,
        )
        return None

    except Exception as e:
        logger.exception("Failed to create sharing link for %s: %s", item_id, e)
        return None


async def upload_pdf_to_onedrive(
    pdf_path,
    onedrive_path: str | None,
    config: Config,
    logger: logging.Logger,
) -> str | None:
    if not pdf_path.is_file():
        logger.error(f"PDF file not found: {pdf_path}")
        return None

    try:
        with open(pdf_path, "rb") as f:
            file_content = f.read()

        target_path = onedrive_path or pdf_path.name
        item_id = await upload_to_appfolder(target_path, file_content, config, logger)
        if item_id:
            logger.info("Uploaded %s to OneDrive AppFolder", target_path)
        else:
            logger.error("upload_to_appfolder returned no item id for %s", target_path)
        return item_id
    except Exception as e:
        logger.exception("Failed to upload PDF %s to OneDrive: %s", pdf_path, e)
        return None
