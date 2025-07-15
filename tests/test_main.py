# tests/test_main.py
"""
Unit tests for the main application entrypoint and lifecycle.
"""

from unittest.mock import MagicMock, patch

import pytest

from main import health_check, main

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_load_config():
    """Fixture to mock config loading."""
    with patch("main.load_app_config") as mock:
        mock.return_value = MagicMock()
        yield mock


@pytest.mark.unit
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.health_check")
def test_main_entrypoint_runs_health_check(mock_health_check_func):
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
    mock_heartbeat_file.exists.return_value = True
    mock_time.return_value = 1000  # Current time
    # Mock stat().st_mtime to return a stale timestamp
    mock_heartbeat_file.stat.return_value.st_mtime = 900

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
    mock_heartbeat_file.exists.return_value = True
    mock_time.return_value = 1000  # Current time
    # Mock stat().st_mtime to return a fresh timestamp
    mock_heartbeat_file.stat.return_value.st_mtime = 990

    with pytest.raises(SystemExit) as e:
        health_check()
    assert e.value.code == 0
