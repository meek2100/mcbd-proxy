# tests/test_main.py
"""
Unit tests for the main application entrypoint and lifecycle.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import amain, health_check, main

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_load_config():
    """Fixture to mock config loading and return a mock object."""
    with patch("main.load_app_config") as mock:
        mock_config = MagicMock()
        mock_config.healthcheck_stale_threshold = 60
        mock.return_value = mock_config
        yield mock


@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.health_check")
def test_main_entrypoint_runs_health_check(mock_health_check_func, mock_load_config):
    """
    Tests that the main function correctly calls health_check when the
    '--healthcheck' argument is provided.
    """
    main()
    mock_health_check_func.assert_called_once()


@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat_file, mock_load_config):
    """
    Tests that the health check fails with exit code 1 if the
    heartbeat file does not exist.
    """
    mock_heartbeat_file.exists.return_value = False
    with pytest.raises(SystemExit) as e:
        health_check()
    assert e.value.code == 1


@patch("main.time.time")
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_heartbeat_is_stale(
    mock_heartbeat_file, mock_time, mock_load_config
):
    """
    Tests that the health check fails if the heartbeat file is too old.
    """
    mock_load_config.return_value.healthcheck_stale_threshold = 30
    mock_heartbeat_file.exists.return_value = True
    mock_time.return_value = 1000
    mock_heartbeat_file.read_text.return_value = "900"  # Stale timestamp

    with pytest.raises(SystemExit) as e:
        health_check()
    assert e.value.code == 1


@patch("main.time.time")
@patch("main.HEARTBEAT_FILE")
def test_health_check_passes_with_fresh_heartbeat(
    mock_heartbeat_file, mock_time, mock_load_config
):
    """
    Tests that the health check passes if the heartbeat file is recent.
    """
    mock_load_config.return_value.healthcheck_stale_threshold = 60
    mock_heartbeat_file.exists.return_value = True
    mock_time.return_value = 1000
    mock_heartbeat_file.read_text.return_value = "990"  # Fresh timestamp

    with pytest.raises(SystemExit) as e:
        health_check()
    assert e.value.code == 0


@pytest.mark.asyncio
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main._update_heartbeat", new_callable=AsyncMock)
async def test_amain_orchestration_and_shutdown(
    mock_heartbeat,
    mock_configure_logging,
    mock_async_proxy,
    mock_docker_manager,
    mock_load_config,
):
    """
    Verify `amain` orchestrates startup and that `finally` block cleans up.
    """
    mock_proxy_instance = mock_async_proxy.return_value
    mock_docker_instance = mock_docker_manager.return_value

    # Simulate a cancellation to test the finally block
    mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)

    await amain()

    # Verify startup orchestration
    mock_load_config.assert_called_once()
    mock_configure_logging.assert_called_once()
    mock_docker_manager.assert_called_once()
    mock_async_proxy.assert_called_once()
    mock_proxy_instance.start.assert_awaited_once()

    # Verify graceful shutdown in the finally block
    mock_docker_instance.close.assert_awaited_once()
