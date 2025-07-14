# tests/test_main.py
"""
Unit tests for the main application entrypoint.
"""

from unittest.mock import MagicMock, patch

import pytest

import main

pytestmark = pytest.mark.asyncio


async def test_main_entrypoint_runs_amain():
    """
    Tests that when the script is run normally, it calls asyncio.run
    with the amain function.
    """
    with (
        patch("main.asyncio.run") as mock_run,
        patch("main.sys.argv", ["main.py"]),
        patch("main.amain") as mock_amain,
    ):
        # HACK: To run the __main__ block, we can import it.
        # This is a bit unusual but effective for this test case.
        main.__main__

        mock_run.assert_called_once_with(mock_amain())


async def test_main_entrypoint_healthcheck():
    """
    Tests that when the script is run with '--healthcheck', it calls
    the health_check function and exits.
    """
    with (
        patch("main.health_check") as mock_health_check,
        patch("main.sys.argv", ["main.py", "--healthcheck"]),
    ):
        main.__main__
        mock_health_check.assert_called_once()


def test_health_check_success():
    """
    Tests that the health_check function exits with 0 on success.
    """
    with (
        patch("main.load_app_config", return_value=MagicMock()),
        patch("main.sys.exit") as mock_exit,
    ):
        main.health_check()
        mock_exit.assert_called_once_with(0)


def test_health_check_failure():
    """
    Tests that the health_check function exits with 1 on configuration failure.
    """
    with (
        patch("main.load_app_config", side_effect=Exception("Config error")),
        patch("main.sys.exit") as mock_exit,
    ):
        main.health_check()
        mock_exit.assert_called_once_with(1)
