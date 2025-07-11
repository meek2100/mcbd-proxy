# tests/test_main.py

import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Adjust sys.path to ensure modules can be found when tests are run.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the module being tested
from main import main, perform_health_check


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.perform_health_check")
async def test_main_runs_health_check(mock_perform_health):
    """
    Tests that the main function correctly calls perform_health_check
    when the '--healthcheck' argument is provided.
    """
    await main()
    mock_perform_health.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.sys.argv", ["main.py"])
@patch("main.load_application_config")
@patch("main.DockerManager", new_callable=AsyncMock)
@patch("main.NetherBridgeProxy", new_callable=AsyncMock)
@patch("main.asyncio.get_running_loop")
@patch("main.start_metrics_server")  # Mock the metrics server start
async def test_main_execution_flow(
    mock_start_metrics_server,
    mock_get_running_loop,
    mock_proxy_class,
    mock_docker_manager_class,
    mock_load_config,
):
    """
    Tests the main asynchronous execution flow, ensuring that config is loaded,
    DockerManager and proxy are instantiated, signal handlers are set,
    and the app runs.
    """
    # Setup mocks
    mock_settings = MagicMock()
    mock_settings.log_level = "INFO"
    mock_settings.log_formatter = "console"
    mock_settings.prometheus_enabled = False
    mock_settings.docker_url = "mock_docker_url"

    mock_servers = [MagicMock()]
    mock_load_config.return_value = (mock_settings, mock_servers)

    mock_docker_manager_instance = mock_docker_manager_class.return_value
    mock_proxy_instance = mock_proxy_class.return_value

    # Mock the run method of the proxy to prevent it from running indefinitely
    mock_proxy_instance.run = AsyncMock()

    # Mock asyncio.get_running_loop().add_signal_handler
    mock_loop = AsyncMock()
    mock_get_running_loop.return_value = mock_loop
    mock_loop.add_signal_handler.return_value = None  # No specific return needed

    # Mock Path.mkdir and Path.unlink for config_path operations
    with patch("main.Path") as MockPath:
        mock_config_path = MockPath.return_value
        mock_config_path.mkdir.return_value = None
        mock_config_path.unlink.return_value = None
        mock_config_path.__truediv__.return_value = (  # Mock division for / operator
            MagicMock(exists=MagicMock(return_value=False))
        )

        await main()

        mock_load_config.assert_called_once()
        mock_docker_manager_class.assert_called_once_with(mock_settings.docker_url)
        mock_proxy_class.assert_called_once_with(
            mock_settings,
            mock_servers,
            mock_docker_manager_instance,
            mock_proxy_instance.shutdown_event,  # These are attributes of the AsyncMock
            mock_proxy_instance.reload_event,  # and are dynamically created.
            mock_proxy_instance.active_sessions_metric,
            mock_proxy_instance.running_servers_metric,
            mock_proxy_instance.bytes_transferred_metric,
            mock_proxy_instance.server_startup_duration_metric,
            mock_config_path,
        )
        mock_proxy_instance.run.assert_awaited_once()

        # Verify signal handlers are set for non-Windows (or at least attempted)
        if os.name != "nt":
            mock_loop.add_signal_handler.assert_any_call(
                signal.SIGINT, MagicMock(name="handle_signal")
            )
            mock_loop.add_signal_handler.assert_any_call(
                signal.SIGTERM, MagicMock(name="handle_signal")
            )
            mock_loop.add_signal_handler.assert_any_call(
                signal.SIGHUP, MagicMock(name="handle_reload_signal")
            )
        else:
            mock_loop.add_signal_handler.assert_not_called()

        # Assert clean up after main() finishes
        mock_proxy_instance._close_listeners.assert_awaited_once()
        mock_proxy_instance._shutdown_all_sessions.assert_awaited_once()
        mock_docker_manager_instance.close.assert_awaited_once()


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch(
    "main.load_application_config", return_value=(MagicMock(), [MagicMock()])
)  # Mock config loading
def test_health_check_fails_if_file_missing(mock_load_config, mock_path_class):
    """
    Tests that the health check fails with exit code 1 if the
    heartbeat file does not exist.
    """
    mock_config_path = mock_path_class.return_value
    mock_heartbeat_file = (
        mock_config_path.__truediv__.return_value
    )  # config_path / "heartbeat.txt"
    mock_heartbeat_file.exists.return_value = False

    with pytest.raises(SystemExit) as e:
        perform_health_check(mock_config_path)
    assert e.value.code == 1
    mock_heartbeat_file.exists.assert_called_once()


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch("time.time")
def test_health_check_fails_if_heartbeat_is_stale(mock_time, mock_path_class):
    """
    Tests that the health check fails with exit code 1 if the heartbeat
    file is older than the configured threshold.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold_seconds=60)
    mock_load_config_return = (mock_settings, [MagicMock()])

    # Mock load_application_config only for settings necessary for health check
    with patch("main.load_application_config", return_value=mock_load_config_return):
        mock_config_path = mock_path_class.return_value
        mock_heartbeat_file = (
            mock_config_path.__truediv__.return_value
        )  # config_path / "heartbeat.txt"
        mock_heartbeat_file.exists.return_value = True

        mock_time.return_value = 1000  # Current time
        mock_heartbeat_file.read_text.return_value = "900"  # Stale timestamp

        with pytest.raises(SystemExit) as e:
            perform_health_check(mock_config_path)
        assert e.value.code == 1
        mock_heartbeat_file.exists.assert_called_once()
        mock_heartbeat_file.read_text.assert_called_once()
        # No longer checking settings.healthcheck_stale_threshold_seconds as
        # healthcheck does not load full config, uses hardcoded 60s.
        # This test should reflect the actual healthcheck function's logic.


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch("time.time")
def test_health_check_succeeds(mock_time, mock_path_class):
    """
    Tests that the health check succeeds if the heartbeat file is fresh.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold_seconds=60)
    mock_load_config_return = (mock_settings, [MagicMock()])

    with patch("main.load_application_config", return_value=mock_load_config_return):
        mock_config_path = mock_path_class.return_value
        mock_heartbeat_file = (
            mock_config_path.__truediv__.return_value
        )  # config_path / "heartbeat.txt"
        mock_heartbeat_file.exists.return_value = True

        mock_time.return_value = 1000  # Current time
        # Timestamp just slightly less than current, within 60s threshold
        mock_heartbeat_file.read_text.return_value = "950"

        with pytest.raises(SystemExit) as e:
            perform_health_check(mock_config_path)
        assert e.value.code == 0
        mock_heartbeat_file.exists.assert_called_once()
        mock_heartbeat_file.read_text.assert_called_once()
