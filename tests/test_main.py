import os
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

# Adjust sys.path to ensure modules can be found when tests are run.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the module being tested
from main import main, perform_health_check


@pytest.mark.unit
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.perform_health_check")
def test_main_runs_health_check(mock_perform_health):
    """
    Tests that the main function correctly calls perform_health_check
    when the '--healthcheck' argument is provided.
    """
    main()
    mock_perform_health.assert_called_once()


@pytest.mark.unit
@patch("main.sys.argv", ["main.py"])
@patch("main.load_application_config")
@patch("main.NetherBridgeProxy")
@patch("main.run_proxy_instance")
@patch("signal.signal")
@patch("main.start_http_server")  # Mock prometheus server
def test_main_execution_flow(
    mock_start_http,
    mock_signal,
    mock_run_instance,
    mock_proxy_class,
    mock_load_config,
):
    """
    Tests the main execution flow, ensuring that config is loaded,
    the proxy is instantiated, signal handlers are set, and the app is run.
    """
    mock_settings = MagicMock(
        log_level="INFO", log_formatter="console", prometheus_enabled=False
    )
    mock_servers = [MagicMock()]
    mock_load_config.return_value = (mock_settings, mock_servers)
    mock_proxy_instance = MagicMock()
    mock_proxy_class.return_value = mock_proxy_instance

    # Make the loop run only once for the test
    mock_run_instance.return_value = False

    main()

    # It's called once for prometheus check, and once for the main loop
    assert mock_load_config.call_count == 2
    mock_proxy_class.assert_called_once_with(mock_settings, mock_servers)
    mock_signal.assert_any_call(signal.SIGINT, mock_proxy_instance.signal_handler)
    mock_signal.assert_any_call(signal.SIGTERM, mock_proxy_instance.signal_handler)
    mock_run_instance.assert_called_once_with(mock_proxy_instance)


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat_file):
    """
    Tests that the health check fails with exit code 1 if the
    heartbeat file does not exist.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold_seconds=60)
    with patch(
        "main.load_application_config", return_value=(mock_settings, [MagicMock()])
    ):
        mock_heartbeat_file.is_file.return_value = False
        with pytest.raises(SystemExit) as e:
            perform_health_check()
        assert e.value.code == 1


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
@patch("time.time")
def test_health_check_fails_if_heartbeat_is_stale(mock_time, mock_heartbeat_file):
    """
    Tests that the health check fails with exit code 1 if the heartbeat
    file is older than the configured threshold.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold_seconds=60)
    with patch(
        "main.load_application_config", return_value=(mock_settings, [MagicMock()])
    ):
        mock_heartbeat_file.is_file.return_value = True
        mock_time.return_value = 1000  # Current time
        mock_heartbeat_file.read_text.return_value = "900"  # Stale timestamp

        with pytest.raises(SystemExit) as e:
            perform_health_check()
        assert e.value.code == 1
