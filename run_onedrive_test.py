#!/usr/bin/env python3
"""Run the OneDrive-specific daemon tests from a dedicated entrypoint."""

import argparse
import os
import sys
import unittest
from pathlib import Path


def _load_test_module(live: bool = False):
    project_root = Path(__file__).parent
    sys.path.insert(0, str(project_root / "src"))
    sys.path.insert(0, str(project_root))

    if live:
        from tests import test_onedrive_daemon_live

        return unittest.defaultTestLoader.loadTestsFromModule(test_onedrive_daemon_live)

    from tests import test_onedrive_daemon

    return unittest.defaultTestLoader.loadTestsFromModule(test_onedrive_daemon)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run OneDrive-focused daemon tests")
    parser.add_argument("--live", action="store_true", help="Run live OneDrive tests")
    parser.add_argument(
        "--credentials",
        help="Optional YAML file containing OneDrive credentials and email overrides",
    )
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="Keep remote OneDrive artifacts after live test completes",
    )
    args = parser.parse_args()

    if args.credentials:
        credentials_path = Path(args.credentials)
        if not credentials_path.is_file():
            raise SystemExit(f"Credentials file not found: {credentials_path}")
        os.environ["ONEDRIVE_TEST_CREDENTIALS_PATH"] = str(credentials_path)

    if args.live and "ONEDRIVE_TEST_CREDENTIALS_PATH" not in os.environ:
        raise SystemExit("--live requires --credentials to run against real OneDrive")

    if args.keep_remote:
        os.environ["ONEDRIVE_TEST_KEEP_REMOTE"] = "1"

    if args.live:
        os.environ["ONEDRIVE_TEST_SEND_ERROR_EMAIL"] = "1"

    suite = _load_test_module(live=args.live)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
