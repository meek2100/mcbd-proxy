# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy, BedrockProtocol

pytestmark = pytest.mark.unit


# Custom exception to break test loops cleanly
class StopTestLoop(Exception):
    pass


@pytest.fixture
def mock_java_server_config():
    """Fixture for a mock Java GameServerConfig."""
    return GameServerConfig(
        name="test_java",
        game_type="java",
        container_name="test_container_java",
        internal_port=25565,
        listen_port=25565,
    )


@pytest.fixture
def mock_bedrock_server_config():
    """Fixture for a mock Bedrock GameServerConfig."""
    return GameServerConfig(
        name="test_bedrock",
        game_type="bedrock",
        container_name="test_container_bedrock",
        internal_port=19132,
        listen_port=19132,
    )


@pytest.fixture
def mock_app_config(mock_java_server_config, mock_bedrock_server_config):
    """Fixture for a mock AppConfig."""
    config = MagicMock(spec=AppConfig)
    config.game_servers = [mock_java_server_config, mock_bedrock_server_config]
    config.player_check_interval = 0.01
    config.server_stop_timeout = 10
    config.idle_timeout = 60
    config.tcp_listen_backlog = 128
    return config


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    manager = AsyncMock()
    manager.stop_server.side_effect = StopTestLoop()
    return manager


@pytest.fixture
def mock_metrics_manager():
    """Fixture for a mock MetricsManager."""
    with patch("proxy.MetricsManager") as mock:
        yield mock()


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager, mock_metrics_manager):
    """Fixture for an AsyncProxy instance."""
    return AsyncProxy(mock_app_config, mock_docker_manager)


@pytest.mark.asyncio
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    tcp_task = asyncio.create_task(asyncio.sleep(0.1))

    proxy.server_tasks = {"listeners": [task1], "monitor": task2}
    proxy.active_tcp_sessions = {tcp_task: "server"}

    proxy._shutdown_handler()

    await asyncio.sleep(0)  # Allow cancellation to propagate

    assert task1.cancelled()
    assert task2.cancelled()
    assert tcp_task.cancelled()


@pytest.mark.asyncio
@patch("proxy.asyncio.start_server", new_callable=AsyncMock)
async def test_start_listener_tcp(mock_start_server, proxy, mock_java_server_config):
    """Verify _start_listener correctly sets up a TCP server."""
    # We need to cancel the task to prevent serve_forever() from running forever
    mock_start_server.return_value.serve_forever.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await proxy._start_listener(mock_java_server_config)

    mock_start_server.assert_awaited_once_with(
        proxy._handle_tcp_connection,  # Correctly checks for the bound method
        mock_java_server_config.proxy_host,
        mock_java_server_config.proxy_port,
        backlog=proxy.app_config.tcp_listen_backlog,
    )


@pytest.mark.asyncio
@patch("asyncio.get_running_loop")
async def test_start_listener_udp(mock_get_loop, proxy, mock_bedrock_server_config):
    """Verify _start_listener correctly sets up a UDP endpoint."""
    mock_loop = mock_get_loop.return_value
    mock_loop.create_datagram_endpoint.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await proxy._start_listener(mock_bedrock_server_config)

    mock_loop.create_datagram_endpoint.assert_awaited_once()
    # Check that the protocol factory is a lambda creating a BedrockProtocol
    protocol_factory = mock_loop.create_datagram_endpoint.call_args[0][0]
    assert isinstance(protocol_factory(), BedrockProtocol)
    # Check that it's binding to the correct local address
    local_addr = mock_loop.create_datagram_endpoint.call_args[1]["local_addr"]
    assert local_addr == (
        mock_bedrock_server_config.proxy_host,
        mock_bedrock_server_config.proxy_port,
    )


@pytest.mark.asyncio
async def test_ensure_server_started(proxy, mock_docker_manager):
    """Test that a server is started if not already running."""
    server_config = proxy.app_config.game_servers[0]
    proxy._ready_events[server_config.name].clear()
    proxy._server_state[server_config.name]["is_running"] = False
    mock_docker_manager.start_server.return_value = True

    await proxy._ensure_server_started(server_config)

    mock_docker_manager.start_server.assert_awaited_once_with(server_config)
    assert proxy._server_state[server_config.name]["is_running"] is True
    assert proxy._ready_events[server_config.name].is_set()


@pytest.mark.asyncio
async def test_ensure_server_already_running(proxy, mock_docker_manager):
    """Test that start is not called if the server is already running."""
    server_config = proxy.app_config.game_servers[0]
    proxy._ready_events[server_config.name].set()
    mock_docker_manager.is_container_running.return_value = True

    await proxy._ensure_server_started(server_config)

    mock_docker_manager.start_server.assert_not_called()


@pytest.mark.asyncio
@patch("proxy.time.time")
async def test_monitor_server_activity_stops_idle_server(
    mock_time, proxy, mock_docker_manager
):
    """Test that the monitor stops an idle server with 0 sessions."""
    # This side effect will allow the loop to run once, then exit cleanly.
    proxy.app_config.player_check_interval = 1000
    mock_docker_manager.is_container_running.return_value = True

    server_config = proxy.app_config.game_servers[0]
    proxy._server_state[server_config.name]["is_running"] = True
    # Set activity time far in the past
    proxy._server_state[server_config.name]["last_activity"] = 1000
    mock_time.return_value = 1000 + proxy.app_config.idle_timeout + 1

    with pytest.raises(StopTestLoop):
        await proxy._monitor_server_activity()

    mock_docker_manager.stop_server.assert_awaited_once_with(
        server_config.container_name, proxy.app_config.server_stop_timeout
    )
