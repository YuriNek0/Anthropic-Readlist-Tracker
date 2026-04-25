from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path(__file__).parent / "config.yaml",
    Path(__file__).parent.parent.parent / "config.yaml",
]

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
)


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
    sender_name: str = ""
    share_recipients: list[str] = field(default_factory=list)
    share_domain_filter_enabled: bool = False
    share_domain: str = ""
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
    render_concurrency: int = 1
    upload_concurrency: int = 1


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
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse config file {path}: {e}")
        except OSError as e:
            raise ValueError(f"Failed to read config file {path}: {e}")

        if not isinstance(data, dict):
            raise ValueError(f"Config file {path} is empty")

        azure_data = data.get("azure", {})
        if not isinstance(azure_data, dict) or not azure_data:
            raise ValueError("Missing required 'azure' section in config")
        _require_field(azure_data, "tenant_id", "azure.tenant_id")
        _require_field(azure_data, "client_id", "azure.client_id")
        _require_field(azure_data, "client_secret", "azure.client_secret")

        user_data = data.get("user", {})
        if not isinstance(user_data, dict) or not user_data:
            raise ValueError("Missing required 'user' section in config")
        _require_field(user_data, "email", "user.email")

        email_data = data.get("email", {})
        if not isinstance(email_data, dict) or not email_data:
            raise ValueError("Missing required 'email' section in config")
        _require_field(email_data, "sender", "email.sender")
        _require_recipients(email_data, "email.recipients")
        share_recipients = email_data.get("share_recipients")
        if share_recipients is None:
            share_recipients = []
        elif not isinstance(share_recipients, list):
            raise ValueError(
                "'email.share_recipients' must be a list of email addresses"
            )

        normalized_share_recipients = [
            str(item).strip()
            for item in share_recipients
            if isinstance(item, (str, int, float))
        ]
        normalized_share_recipients = [
            item for item in normalized_share_recipients if item
        ]

        share_domain_filter_enabled = email_data.get(
            "share_domain_filter_enabled", False
        )
        if not isinstance(share_domain_filter_enabled, bool):
            raise ValueError("'email.share_domain_filter_enabled' must be a boolean")

        share_domain = email_data.get("share_domain", "")
        if share_domain is None:
            share_domain = ""
        if not isinstance(share_domain, str):
            raise ValueError("'email.share_domain' must be a string")
        share_domain = share_domain.strip()

        repos_data = data.get("repos", [])
        if not isinstance(repos_data, list) or not repos_data:
            raise ValueError("Missing required 'repos' (must be a list) in config")
        if len(repos_data) == 0:
            raise ValueError("At least one repo must be configured in 'repos'")

        repos: list[RepoConfig] = []
        for i, repo_data in enumerate(repos_data):
            if not isinstance(repo_data, dict):
                raise ValueError(f"Repo at index {i} must be an object")

            _require_field(repo_data, "name", f"repos[{i}].name")
            _require_field(repo_data, "url", f"repos[{i}].url")
            _require_field(repo_data, "local_path", f"repos[{i}].local_path")
            _require_field(repo_data, "version_file", f"repos[{i}].version_file")
            _require_field(
                repo_data, "discover_patterns", f"repos[{i}].discover_patterns"
            )

            patterns = repo_data["discover_patterns"]
            if not isinstance(patterns, list) or len(patterns) == 0:
                raise ValueError(
                    f"repos[{i}].discover_patterns must be a non-empty list"
                )

            repos.append(
                RepoConfig(
                    name=repo_data["name"],
                    url=repo_data["url"],
                    local_path=repo_data["local_path"],
                    version_file=repo_data["version_file"],
                    discover_patterns=patterns,
                    manifest_file=repo_data.get("manifest_file"),
                )
            )

        daemon_data = data.get("daemon", {})
        if daemon_data is None:
            daemon_data = {}
        if not isinstance(daemon_data, dict):
            raise ValueError("'daemon' section must be an object")

        normalized_daemon_data = dict(daemon_data)
        if "random_delay_max_hours" in normalized_daemon_data:
            normalized_daemon_data["random_delay_max_hours"] = _require_int_at_least(
                normalized_daemon_data["random_delay_max_hours"],
                "daemon.random_delay_max_hours",
                minimum=0,
            )
        if "render_concurrency" in normalized_daemon_data:
            normalized_daemon_data["render_concurrency"] = _require_int_at_least(
                normalized_daemon_data["render_concurrency"],
                "daemon.render_concurrency",
                minimum=1,
            )
        if "upload_concurrency" in normalized_daemon_data:
            normalized_daemon_data["upload_concurrency"] = _require_int_at_least(
                normalized_daemon_data["upload_concurrency"],
                "daemon.upload_concurrency",
                minimum=1,
            )

        return cls(
            azure=AzureConfig(**azure_data),
            user=UserConfig(**user_data),
            email=EmailConfig(
                **{
                    **email_data,
                    "share_recipients": normalized_share_recipients,
                    "share_domain_filter_enabled": share_domain_filter_enabled,
                    "share_domain": share_domain,
                }
            ),
            repos=repos,
            daemon=DaemonConfig(
                **normalized_daemon_data,
            ),
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


def _require_field(data: dict, field_name: str, label: str) -> None:
    if not data.get(field_name):
        raise ValueError(f"Missing required '{label}' in config")


def _require_recipients(data: dict, label: str) -> None:
    recipients = data.get("recipients")
    if not isinstance(recipients, list) or len(recipients) == 0:
        raise ValueError(
            f"Missing required '{label}' (must be a non-empty list) in config"
        )


def _require_int_at_least(value: object, label: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"'{label}' must be an integer >= {minimum}")

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{label}' must be an integer >= {minimum}")

    if parsed < minimum:
        raise ValueError(f"'{label}' must be an integer >= {minimum}")

    return parsed
