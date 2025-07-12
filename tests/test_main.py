# tests/test_main.py

import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Adjust sys.path to ensure modules can be found when tests are run.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the module being tested
from config import ProxySettings
from main import main, perform_health_check


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.load_application_config")
@patch("main.perform_health_check")
async def test_main_runs_health_check(
    mock_perform_health, mock_load_application_config
):
    """
    Tests that the main function correctly calls perform_health_check
    when the '--healthcheck' argument is provided.
    """
    # Configure load_application_config to return mock settings
    mock_settings = MagicMock(spec=ProxySettings)
    mock_load_application_config.return_value = (mock_settings, [])

    await main()
    mock_perform_health.assert_called_once_with(
        Path("config"), mock_settings
    )  # Assert config_path and settings are passed


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.sys.argv", ["main.py"])
@patch("main.load_application_config")
@patch("main.DockerManager", new_callable=AsyncMock)
@patch("main.NetherBridgeProxy.run", new_callable=AsyncMock)  # Patch .run directly
@patch(
    "main.NetherBridgeProxy.__init__", return_value=None
)  # Patch init to prevent side effects
@patch(
    "main.NetherBridgeProxy._close_listeners", new_callable=AsyncMock
)  # Mock cleanup methods
@patch(
    "main.NetherBridgeProxy._shutdown_all_sessions", new_callable=AsyncMock
)  # Mock cleanup methods
@patch("main.asyncio.get_running_loop")
@patch("main.start_metrics_server")  # Mock the metrics server start
async def test_main_execution_flow(
    mock_start_metrics_server,
    mock_get_running_loop,
    mock_close_listeners,  # New mock
    mock_shutdown_all_sessions,  # New mock
    mock_proxy_init,  # New mock
    mock_proxy_run_method,  # This is the mock for proxy.run
    mock_docker_manager_class,
    mock_load_config,
):
    """
    Tests the main asynchronous execution flow, ensuring that config is loaded,
    DockerManager and proxy are instantiated, signal handlers are set,
    and the app runs.
    """
    # Setup mocks
    mock_settings = MagicMock(spec=ProxySettings)
    mock_settings.log_level = "INFO"
    mock_settings.log_formatter = "console"
    mock_settings.prometheus_enabled = False
    mock_settings.docker_url = "mock_docker_url"

    mock_servers = [MagicMock()]
    mock_load_config.return_value = (mock_settings, mock_servers)

    mock_docker_manager_instance = mock_docker_manager_class.return_value
    # mock_proxy_init is the patched __init__ of NetherBridgeProxy
    # We need to get a reference to the proxy instance that main.py creates
    # by letting mock_proxy_init record its 'self' argument.
    mock_proxy_instance_ref = (
        MagicMock()
    )  # Create a mock to be the 'self' passed to __init__
    mock_proxy_init.side_effect = lambda self, *args, **kwargs: setattr(
        self, "_mock_instance_ref", mock_proxy_instance_ref
    )

    # Mock asyncio.get_running_loop().add_signal_handler
    mock_loop = AsyncMock()
    mock_get_running_loop.return_value = mock_loop
    mock_loop.add_signal_handler.return_value = None  # No specific return needed

    # Mock Path.mkdir and Path.unlink for config_path operations
    with patch("main.Path") as MockPath:
        mock_config_path = MockPath.return_value
        mock_config_path.mkdir.return_value = None
        mock_config_path.unlink.return_value = None
        # Configure __truediv__ to return a mock that behaves like a Path object
        mock_config_path.__truediv__.return_value = MagicMock(spec=Path)
        mock_config_path.__truediv__.return_value.exists.return_value = False

        await main()

        mock_load_config.assert_called_once()
        mock_docker_manager_class.assert_called_once_with(mock_settings.docker_url)
        # Verify NetherBridgeProxy's __init__ was called with correct args
        mock_proxy_init.assert_called_once()

        # Assert proxy.run was called
        mock_proxy_run_method.assert_awaited_once()

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
        mock_close_listeners.assert_awaited_once()
        mock_shutdown_all_sessions.assert_awaited_once()
        mock_docker_manager_instance.close.assert_awaited_once()


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch("main.os.path.exists", return_value=False)  # Mock file existence
def test_health_check_fails_if_file_missing(
    mock_os_path_exists,
    MockPath,  # Renamed for clarity
):
    """
    Tests that the health check fails with exit code 1 if the
    heartbeat file does not exist.
    """
    mock_config_path = MockPath.return_value
    mock_heartbeat_file_instance = MagicMock(
        spec=Path
    )  # Create a specific mock instance for the file
    mock_config_path.__truediv__.return_value = (
        mock_heartbeat_file_instance  # Make __truediv__ return it
    )

    mock_heartbeat_file_instance.exists.return_value = False  # File does not exist
    mock_settings = MagicMock(
        spec=ProxySettings, healthcheck_stale_threshold_seconds=60
    )

    with pytest.raises(SystemExit) as e:
        perform_health_check(mock_config_path, mock_settings)
    assert e.value.code == 1
    mock_heartbeat_file_instance.exists.assert_called_once()


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch("main.os.path.getmtime")  # Patch os.path.getmtime
@patch("time.time")
@patch("main.os.path.exists", return_value=True)  # Ensure file is found
def test_health_check_fails_if_heartbeat_is_stale(
    mock_os_path_exists,
    mock_time,
    mock_getmtime,
    MockPath,  # Renamed for clarity
):
    """
    Tests that the health check fails with exit code 1 if the heartbeat
    file is older than the configured threshold.
    """
    mock_settings = MagicMock(
        spec=ProxySettings, healthcheck_stale_threshold_seconds=60
    )

    mock_config_path = MockPath.return_value
    mock_heartbeat_file_instance = MagicMock(spec=Path)  # Specific mock for the file
    mock_config_path.__truediv__.return_value = (
        mock_heartbeat_file_instance  # Make __truediv__ return it
    )

    mock_time.return_value = 1000  # Current time
    mock_getmtime.return_value = 900  # Stale timestamp (1000 - 900 = 100 > 60)

    with pytest.raises(SystemExit) as e:
        perform_health_check(mock_config_path, mock_settings)
    assert e.value.code == 1
    mock_os_path_exists.assert_called_once()
    mock_getmtime.assert_called_once_with(mock_heartbeat_file_instance)


@pytest.mark.unit
@patch("main.Path")  # Patch Path for file system operations
@patch("main.os.path.getmtime")  # Patch os.path.getmtime
@patch("time.time")
@patch("main.os.path.exists", return_value=True)  # Ensure file is found
def test_health_check_succeeds(
    mock_os_path_exists,
    mock_time,
    mock_getmtime,
    MockPath,  # Renamed for clarity
):
    """
    Tests that the health check succeeds if the heartbeat file is fresh.
    """
    mock_settings = MagicMock(
        spec=ProxySettings, healthcheck_stale_threshold_seconds=60
    )

    mock_config_path = MockPath.return_value
    mock_heartbeat_file_instance = MagicMock(spec=Path)  # Specific mock for the file
    mock_config_path.__truediv__.return_value = (
        mock_heartbeat_file_instance  # Make __truediv__ return it
    )

    mock_time.return_value = 1000  # Current time
    # Timestamp just slightly less than current, within 60s threshold
    mock_getmtime.return_value = 950  # (1000 - 950 = 50 < 60)

    with pytest.raises(SystemExit) as e:
        perform_health_check(mock_config_path, mock_settings)
    assert e.value.code == 0
    mock_os_path_exists.assert_called_once()
    mock_getmtime.assert_called_once_with(mock_heartbeat_file_instance)
