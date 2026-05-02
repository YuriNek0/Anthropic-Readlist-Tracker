import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import anthropic_readings.cli as cli


class _FakeDayJob:
    def __init__(self):
        self.do_calls = []

    @property
    def day(self):
        return self

    def do(self, job):
        self.do_calls.append(job)
        return job


class _FakeScheduleModule:
    def __init__(self):
        self.job = _FakeDayJob()
        self.run_pending_calls = 0

    def every(self):
        return self.job

    def run_pending(self):
        self.run_pending_calls += 1


class TestCli(unittest.TestCase):
    def test_schedule_daily_run_registers_job_that_runs_once_wrapper(self):
        fake_schedule = _FakeScheduleModule()
        config = MagicMock()
        logger = MagicMock()

        with patch("anthropic_readings.cli._run_once") as mock_run_once:
            cli._schedule_daily_run(config, logger, fake_schedule)

            self.assertEqual(len(fake_schedule.job.do_calls), 1)
            fake_schedule.job.do_calls[0]()

        mock_run_once.assert_called_once_with(config, logger)

    @patch("anthropic_readings.cli.setup_logging")
    @patch("anthropic_readings.cli.Config.from_yaml")
    @patch("anthropic_readings.cli._resolve_config_path")
    def test_main_once_runs_daemon_immediately(
        self, mock_resolve_config_path, mock_from_yaml, mock_setup_logging
    ):
        mock_resolve_config_path.return_value = "config.yaml"
        config = MagicMock()
        config.is_production = False
        config.repos = []
        config.output_dir = "outputs"
        mock_from_yaml.return_value = config
        logger = MagicMock()
        mock_setup_logging.return_value = logger

        with (
            patch.object(sys, "argv", ["anthropic-readings-daemon", "--once"]),
            patch("anthropic_readings.cli._run_once") as mock_run_once,
        ):
            cli.main()

        mock_run_once.assert_called_once_with(config, logger)

    @patch("anthropic_readings.cli.setup_logging")
    @patch("anthropic_readings.cli.Config.from_yaml")
    @patch("anthropic_readings.cli._resolve_config_path")
    def test_main_registers_daily_schedule_job(
        self, mock_resolve_config_path, mock_from_yaml, mock_setup_logging
    ):
        mock_resolve_config_path.return_value = "config.yaml"
        config = MagicMock()
        config.is_production = False
        config.repos = []
        config.output_dir = "outputs"
        mock_from_yaml.return_value = config
        logger = MagicMock()
        mock_setup_logging.return_value = logger
        fake_schedule = _FakeScheduleModule()

        with (
            patch.object(sys, "argv", ["anthropic-readings-daemon"]),
            patch.dict(sys.modules, {"schedule": fake_schedule}),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            cli.main()

        self.assertEqual(fake_schedule.run_pending_calls, 1)
        self.assertEqual(len(fake_schedule.job.do_calls), 1)
