# tests/test_main.py
"""
Unit tests for the main application entrypoint.
"""

import runpy
from unittest.mock import AsyncMock, patch

import pytest

import main


@pytest.mark.asyncio
async def test_amain_happy_path():
    """Tests the main async function runs without errors."""
    with (
        patch("main.load_app_config"),
        patch("main.configure_logging"),
        patch("main.DockerManager") as mock_docker_manager,
        patch("main.AsyncProxy") as mock_async_proxy,
    ):
        # Ensure the mock instances are also async mocks
        mock_docker_instance = AsyncMock()
        mock_docker_manager.return_value = mock_docker_instance
        mock_proxy_instance = AsyncMock()
        mock_async_proxy.return_value = mock_proxy_instance

        await main.amain()

        mock_docker_instance.close.assert_awaited_once()
        mock_proxy_instance.start.assert_awaited_once()


def test_main_entrypoint_runs_amain():
    """
    Tests that when the script is run normally, it calls asyncio.run.
    """
    with (
        patch("sys.argv", ["main.py"]),
        patch("main.amain", new_callable=AsyncMock) as mock_amain,
        patch("main.asyncio.run") as mock_run,
    ):
        # Use runpy to execute the module's __main__ block
        runpy.run_module("main", run_name="__main__")

        mock_amain.assert_called_once()
        mock_run.assert_called_once_with(mock_amain.return_value)


def test_main_entrypoint_healthcheck():
    """
    Tests that when run with '--healthcheck', it calls the health_check function.
    """
    with (
        patch("sys.argv", ["main.py", "--healthcheck"]),
        patch("main.health_check") as mock_health_check,
        patch("main.asyncio.run"),
    ):  # Patch run to prevent execution
        # We expect SystemExit to be called by health_check
        with pytest.raises(SystemExit):
            runpy.run_module("main", run_name="__main__")

        mock_health_check.assert_called_once()


def test_health_check_success():
    """
    Tests that the health_check function exits with 0 on success.
    """
    with patch("main.load_app_config"), patch("sys.exit") as mock_exit:
        main.health_check()
        mock_exit.assert_called_once_with(0)


def test_health_check_failure():
    """
    Tests that the health_check function exits with 1 on configuration failure.
    """
    with (
        patch("main.load_app_config", side_effect=Exception("Config error")),
        patch("sys.exit") as mock_exit,
    ):
        main.health_check()
        mock_exit.assert_called_once_with(1)
