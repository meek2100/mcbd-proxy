import sys
from unittest.mock import MagicMock, patch

import pytest

from main import main, perform_health_check

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


@patch("main.sys.exit")
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat_file, mock_exit):
    """Tests that the health check fails if the heartbeat file is missing."""
    mock_heartbeat_file.exists.return_value = False
    perform_health_check()
    mock_exit.assert_called_with(1)


@patch("main.sys.exit")
@patch("main.time.time", return_value=100)
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_heartbeat_is_stale(
    mock_heartbeat_file, mock_time, mock_exit
):
    """Tests that the health check fails if the heartbeat is stale."""
    mock_heartbeat_file.exists.return_value = True
    mock_heartbeat_file.read_text.return_value = "0"
    perform_health_check()
    mock_exit.assert_called_with(1)


@patch("main.asyncio.run")
@patch("main.NetherBridgeProxy")
@patch("main.load_application_config")
@patch("main.configure_logging")
@patch("main.start_http_server")
def test_main_execution_flow(
    mock_start_http,
    mock_configure_logging,
    mock_load_config,
    mock_proxy,
    mock_asyncio_run,
):
    """Tests the main execution flow of the application."""
    mock_settings = MagicMock()
    mock_settings.prometheus_enabled = True
    mock_servers = [MagicMock()]
    mock_load_config.side_effect = [
        (mock_settings, mock_servers),
        (mock_settings, mock_servers),
    ]
    mock_asyncio_run.return_value = False  # Simulate no reload request

    with patch.object(sys, "argv", ["main.py"]):
        main()

    assert mock_load_config.call_count == 2
    mock_proxy.assert_called_once_with(mock_settings, mock_servers)
    mock_asyncio_run.assert_called_once()
