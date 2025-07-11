# tests/test_main.py
import asyncio
import signal  # Added: Import the signal module
import sys
from unittest.mock import MagicMock, patch

import pytest
import structlog

# Removed unused imports: os, time, pathlib.Path, threading
# prometheus_client.start_http_server, config.load_application_config,
# docker_manager.DockerManager, main.HEARTBEAT_FILE,
# main.configure_logging, proxy.NetherBridgeProxy
# These are referenced as strings in patch decorators or
# are handled implicitly by the application flow.
from main import DEFAULT_HEARTBEAT_INTERVAL, _heartbeat_writer, main


@pytest.fixture
def mock_settings():
    """Fixture for a mocked ProxySettings object."""
    settings = MagicMock()
    settings.prometheus_enabled = True
    settings.prometheus_port = 8000
    settings.log_level = "info"
    settings.log_formatter = "console"
    settings.servers_config_path = "test_servers.json"
    settings.proxy_heartbeat_interval_seconds = DEFAULT_HEARTBEAT_INTERVAL
    settings.player_check_interval_seconds = 60
    settings.idle_timeout_seconds = 300
    settings.server_ready_max_wait_time_seconds = 300
    settings.query_timeout_seconds = 5
    settings.docker_network_gateway_ip = "172.17.0.1"  # Example IP
    return settings


@pytest.fixture
def mock_servers():
    """Fixture for a list of mocked ServerConfig objects."""
    server1 = MagicMock()
    server1.name = "Java Test"
    server1.container_name = "test-mc-java"
    server1.server_type = "java"
    server1.listen_port = 25565
    server1.internal_port = 25565
    server1.idle_timeout_seconds = None  # Use default

    server2 = MagicMock()
    server2.name = "Bedrock Test"
    server2.container_name = "test-mc-bedrock"
    server2.server_type = "bedrock"
    server2.listen_port = 19132
    server2.internal_port = 19132
    server2.idle_timeout_seconds = None

    return [server1, server2]


@pytest.mark.unit
@patch("main.asyncio.run")
@patch("main.NetherBridgeProxy")
@patch("main.DockerManager", autospec=True)  # Patch DockerManager class
@patch("main.load_application_config")
@patch("prometheus_client.start_http_server")  # Patch directly from prometheus_client
@patch("main.threading.Thread", autospec=True)  # Patch threading.Thread
@patch("main.HEARTBEAT_FILE")  # Patch the Path object for file operations
@patch("os.getenv", return_value="console")  # Patch directly from os module
@patch("main.configure_logging")  # Patch configure_logging from main
def test_main_execution_flow(
    mock_configure_logging,
    mock_getenv,
    mock_heartbeat_file,
    mock_thread,
    mock_start_http_server,
    mock_load_config,
    mock_docker_manager_class,
    mock_proxy_class,
    mock_asyncio_run,
    mock_settings,
    mock_servers,
):
    """Tests the main execution flow of the application."""

    # Set up mock for load_application_config
    mock_load_config.side_effect = [
        (mock_settings, mock_servers),
        (mock_settings, mock_servers),  # For a potential reload loop
    ]

    # Configure mock_asyncio_run to simulate no reload on first run
    mock_asyncio_run.return_value = False

    # Mock the instances created by the mocked classes
    mock_docker_manager_instance = mock_docker_manager_class.return_value
    mock_proxy_instance = mock_proxy_class.return_value

    # Ensure proxy._shutdown_event is a mock of asyncio.Event
    mock_proxy_instance._shutdown_event = MagicMock(spec=asyncio.Event)
    mock_proxy_instance._shutdown_event.is_set.return_value = (
        False  # For monitor thread loop
    )

    # Mock the _monitor_servers_activity and _run_proxy_loop methods
    mock_proxy_instance._monitor_servers_activity = MagicMock()
    mock_proxy_instance._run_proxy_loop.return_value = False  # Simulate no reload

    # Mock _ensure_all_servers_stopped_on_startup
    mock_proxy_instance._ensure_all_servers_stopped_on_startup = MagicMock()

    # Mock the proxy's logger
    mock_proxy_instance.logger = MagicMock(spec=structlog.stdlib.BoundLogger)
    mock_proxy_instance.logger.info = MagicMock()
    mock_proxy_instance.logger.warning = MagicMock()
    mock_proxy_instance.logger.error = MagicMock()

    # Patch signal.signal here, as the `main` function directly calls it.
    # This must be done inside the test function or applied as a decorator to it.
    with patch("signal.signal") as mock_signal_signal:
        with patch.object(sys, "argv", ["main.py"]):
            main()  # Call main function

        # Assertions for signal handlers
        if hasattr(signal, "SIGHUP"):
            mock_signal_signal.assert_any_call(
                signal.SIGHUP, mock_proxy_instance.signal_handler
            )
        mock_signal_signal.assert_any_call(
            signal.SIGINT, mock_proxy_instance.signal_handler
        )
        mock_signal_signal.assert_any_call(
            signal.SIGTERM, mock_proxy_instance.signal_handler
        )

    # Assertions outside the signal patch context
    mock_load_config.assert_called_once()
    mock_docker_manager_class.assert_called_once()  # DockerManager instantiated
    mock_docker_manager_instance.connect.assert_called_once()
    mock_proxy_class.assert_called_once_with(mock_settings, mock_servers)
    mock_proxy_instance._ensure_all_servers_stopped_on_startup.assert_called_once()
    mock_start_http_server.assert_called_once_with(mock_settings.prometheus_port)

    # Assert that main.py's _heartbeat_writer is started in a thread
    mock_thread.assert_any_call(
        target=_heartbeat_writer,
        args=(
            mock_heartbeat_file,
            mock_settings.proxy_heartbeat_interval_seconds,
            mock_proxy_instance._shutdown_event,
        ),
        daemon=True,
    )
    mock_thread.return_value.start.assert_called_once()

    # Assert that the monitor thread is also started
    mock_thread.assert_any_call(
        target=mock_proxy_instance._monitor_servers_activity, daemon=True
    )
    assert mock_thread.call_count >= 2  # One for heartbeat, one for monitor

    # Assert asyncio.run was called with the correct function
    mock_asyncio_run.assert_called_once_with(
        main.run_proxy_instance_async(mock_proxy_instance)
    )
