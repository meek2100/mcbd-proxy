import asyncio
import signal
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from config import ProxySettings, ServerConfig
from proxy import NetherBridgeProxy

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


@pytest.fixture
def mock_settings():
    """Returns a mock ProxySettings object."""
    return ProxySettings(
        log_level="info",
        log_formatter="console",
        player_check_interval_seconds=1,
        idle_timeout_seconds=30,
        prometheus_enabled=False,
        prometheus_port=8000,
        polling_rate=1,
    )


@pytest.fixture
def mock_servers_config():
    """Returns a list of mock ServerConfig objects."""
    return [
        ServerConfig(
            name="Bedrock Test",
            server_type="bedrock",
            listen_port=19132,
            container_name="test-mc-bedrock",
            internal_port=19132,
            idle_timeout_seconds=None,
        ),
        ServerConfig(
            name="Java Test",
            server_type="java",
            listen_port=25565,
            container_name="test-mc-java",
            internal_port=25565,
            idle_timeout_seconds=None,
        ),
    ]


@pytest.fixture
def proxy_instance(mock_settings, mock_servers_config):
    """Returns a NetherBridgeProxy instance with mocked dependencies."""
    with patch("proxy.DockerManager") as mock_docker_manager:
        proxy = NetherBridgeProxy(mock_settings, mock_servers_config)
        proxy.docker_manager = mock_docker_manager.return_value
        return proxy


def test_proxy_initialization(proxy_instance, mock_settings, mock_servers_config):
    """Tests that the proxy initializes correctly."""
    assert proxy_instance.settings == mock_settings
    assert proxy_instance.servers_list == mock_servers_config
    assert 19132 in proxy_instance.servers_config_map
    assert 25565 in proxy_instance.servers_config_map
    # Check that ready_event is an asyncio.Event
    assert isinstance(
        proxy_instance.server_states["test-mc-bedrock"]["ready_event"],
        asyncio.Event,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGHUP is not available on Windows"
)
def test_signal_handler_sighup(proxy_instance):
    """Tests the SIGHUP signal handling for configuration reload."""
    assert not proxy_instance._reload_requested
    proxy_instance.signal_handler(signal.SIGHUP, None)
    assert proxy_instance._reload_requested
    assert proxy_instance._shutdown_event.is_set()


def test_signal_handler_sigint(proxy_instance):
    """Tests the SIGINT signal handling for graceful shutdown."""
    assert not proxy_instance._shutdown_requested
    proxy_instance.signal_handler(signal.SIGINT, None)
    assert proxy_instance._shutdown_requested
    assert proxy_instance._shutdown_event.is_set()


@patch("proxy.Thread")
@patch("proxy.asyncio.run")
def test_start_minecraft_server_task_success(
    mock_asyncio_run, mock_thread, proxy_instance, mock_servers_config
):
    """
    Tests the proxy's background start task. It should delegate to the
    DockerManager and update its internal state on success.
    """
    server_config = mock_servers_config[1]  # Java server
    container_name = server_config.container_name

    proxy_instance.docker_manager.start_server.return_value = True
    # Simulate the thread execution by calling the target method directly
    proxy_instance._start_minecraft_server_task(server_config)

    proxy_instance.docker_manager.start_server.assert_called_once_with(
        server_config, proxy_instance.settings
    )
    assert proxy_instance.server_states[container_name]["status"] == "running"
    assert proxy_instance.server_states[container_name]["ready_event"].is_set()


@patch("proxy.time.sleep")
def test_monitor_servers_activity_stops_idle_server(
    mock_sleep, proxy_instance, mock_servers_config
):
    """
    Tests that the server monitoring thread correctly identifies and stops
    an idle server.
    """
    server_config = mock_servers_config[1]
    container_name = server_config.container_name

    # Set up the initial state to be running and idle
    proxy_instance.server_states[container_name]["status"] = "running"
    proxy_instance.server_states[container_name]["last_activity"] = time.time() - 100

    # Mock the shutdown event to stop the monitor loop after one iteration
    side_effect = [False, True]
    with patch.object(
        proxy_instance._shutdown_event, "is_set", side_effect=side_effect
    ):
        with patch("proxy.ACTIVE_SESSIONS") as mock_active_sessions:
            # Mock get_metric_value to return a dict, or a mock that acts like a dict
            mock_active_sessions.get_metric_value.return_value = {
                (server_config.name,): 0
            }

            proxy_instance._monitor_servers_activity()

            # Verify that the stop_server method was called
            proxy_instance.docker_manager.stop_server.assert_called_once_with(
                container_name
            )
            assert proxy_instance.server_states[container_name]["status"] == "stopped"


@pytest.mark.asyncio
async def test_run_proxy_loop_shuts_down_on_event(proxy_instance):
    """Tests that the main proxy loop exits when the shutdown event is set."""
    proxy_instance._shutdown_event.set()  # Set event to exit immediately

    # Mock asyncio.start_server to prevent it from actually trying to bind ports
    with patch("asyncio.start_server") as mock_start_server:
        mock_server_instance = MagicMock()

        # Configure serve_forever to be an awaitable mock using AsyncMock
        # It will complete immediately when awaited.
        # Ensure that `from unittest.mock import AsyncMock` is imported if not already.
        # For simplicity, we can just assign a basic awaitable.
        async def mock_serve_forever():
            pass  # This coroutine does nothing and finishes immediately

        mock_server_instance.serve_forever.return_value = mock_serve_forever()

        mock_start_server.return_value = mock_server_instance

        await proxy_instance._run_proxy_loop()
        mock_start_server.assert_called()
