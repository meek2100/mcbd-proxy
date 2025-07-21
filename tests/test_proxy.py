# tests/test_proxy.py
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy, BedrockProtocol

pytestmark = pytest.mark.unit


class StopTestLoop(Exception):
    """Custom exception to cleanly break out of test loops."""


# --- Fixtures ---


@pytest.fixture
def mock_server_config():
    """Provides a mock server config for general use."""
    return GameServerConfig(
        name="test_server",
        game_type="java",
        container_name="test_container",
        port=25565,
        proxy_port=25565,
        host="test_container",
    )


@pytest.fixture
def mock_docker_manager():
    """Provides a mock DockerManager."""
    manager = AsyncMock()
    manager.is_container_running.return_value = False
    manager.start_server.return_value = True
    return manager


@pytest.fixture
def mock_app_config(mock_server_config):
    """Provides a mock AppConfig containing one server."""
    config = MagicMock(spec=AppConfig)
    config.game_servers = [mock_server_config]
    config.player_check_interval = 0.01
    config.server_stop_timeout = 10
    config.idle_timeout = 0.1
    config.tcp_listen_backlog = 128
    config.max_concurrent_sessions = -1
    return config


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager):
    """Provides an AsyncProxy instance with mocked dependencies."""
    with patch("proxy.MetricsManager"):
        yield AsyncProxy(mock_app_config, mock_docker_manager)


@pytest.fixture
def mock_tcp_streams():
    """Provides correctly mocked asyncio StreamReader and StreamWriter."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    writer.is_closing.return_value = False
    return reader, writer


# --- AsyncProxy Tests ---


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks(proxy):
    """Verify the shutdown method cancels all registered tasks."""
    loop = asyncio.get_running_loop()
    task1 = loop.create_task(asyncio.sleep(0.1))
    task2 = loop.create_task(asyncio.sleep(0.1))
    tcp_task = loop.create_task(asyncio.sleep(0.1))
    proxy.server_tasks = {"listeners": [task1], "monitor": task2}
    proxy.active_tcp_sessions = {tcp_task: "server"}

    await proxy.shutdown()

    assert task1.cancelled()
    assert task2.cancelled()
    assert tcp_task.cancelled()


@pytest.mark.asyncio
@patch("proxy.asyncio.open_connection")
async def test_handle_tcp_connection_success(
    mock_open_conn, proxy, mock_server_config, mock_tcp_streams
):
    """Test the full lifecycle of a successful TCP connection."""
    client_reader, client_writer = mock_tcp_streams
    server_reader, server_writer = mock_tcp_streams
    mock_open_conn.return_value = (server_reader, server_writer)
    proxy._ensure_server_started = AsyncMock()
    proxy._proxy_data = AsyncMock()

    await proxy._handle_tcp_connection(client_reader, client_writer, mock_server_config)

    proxy._ensure_server_started.assert_awaited_once_with(mock_server_config)
    proxy.metrics_manager.inc_active_connections.assert_called_once()
    assert proxy._proxy_data.await_count == 2
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_data_handles_connection_reset(proxy, mock_tcp_streams):
    """Test that _proxy_data handles ConnectionResetError gracefully."""
    reader, writer = mock_tcp_streams
    reader.read.side_effect = ConnectionResetError

    # The method should catch the error and not re-raise it.
    await proxy._proxy_data(reader, writer, "test_server", "c2s")

    writer.close.assert_called_once()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_monitor_stops_idle_server(proxy, mock_docker_manager):
    """Tests that the monitor task stops an idle server."""
    server_config = proxy.app_config.game_servers[0]
    proxy._server_state[server_config.name]["is_running"] = True
    proxy._server_state[server_config.name]["last_activity"] = (
        time.time() - proxy.app_config.idle_timeout - 1
    )
    mock_docker_manager.is_container_running.return_value = True

    with patch("asyncio.sleep", side_effect=[None, StopTestLoop()]):
        try:
            await proxy._monitor_server_activity()
        except StopTestLoop:
            pass  # Expected exit for test

    mock_docker_manager.stop_server.assert_awaited_once_with(
        server_config.container_name, proxy.app_config.server_stop_timeout
    )


# --- BedrockProtocol Tests ---


@pytest.fixture
def mock_bedrock_server_config():
    """Fixture for a mock Bedrock GameServerConfig."""
    return GameServerConfig(
        name="test_bedrock",
        game_type="bedrock",
        container_name="test_container_bedrock",
        port=19132,
        proxy_port=19132,
        host="test_container_bedrock",
    )


@pytest.fixture
def bedrock_protocol(proxy, mock_bedrock_server_config):
    """Provides a BedrockProtocol instance for testing."""
    protocol = BedrockProtocol(proxy, mock_bedrock_server_config)
    protocol.transport = AsyncMock()
    # Cancel the automatic cleanup task to isolate tests
    protocol.cleanup_task.cancel()
    return protocol


@pytest.mark.asyncio
async def test_bedrock_new_client_creates_session(bedrock_protocol, proxy):
    """Verify a new UDP client creates a session and starts backend connection."""
    addr = ("127.0.0.1", 12345)
    data = b"ping"
    proxy._ensure_server_started = AsyncMock()
    # Mock the datagram endpoint creation
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.create_datagram_endpoint = AsyncMock()
        bedrock_protocol.datagram_received(data, addr)

    assert addr in bedrock_protocol.client_map
    proxy.metrics_manager.inc_active_connections.assert_called_once()
    assert bedrock_protocol.client_map[addr]["task"] is not None


@pytest.mark.asyncio
async def test_bedrock_client_queues_data_before_backend_ready(bedrock_protocol):
    """Verify packets are queued if the backend connection is not ready."""
    addr = ("127.0.0.1", 12345)
    # Simulate a session where the backend connection task has been created
    # but has not yet established the protocol.
    bedrock_protocol.client_map[addr] = {
        "task": MagicMock(),
        "last_activity": time.time(),
        "protocol": None,  # The backend is not ready
        "queue": [],
    }

    bedrock_protocol.datagram_received(b"packet1", addr)
    bedrock_protocol.datagram_received(b"packet2", addr)

    assert bedrock_protocol.client_map[addr]["queue"] == [b"packet1", b"packet2"]


@pytest.mark.asyncio
async def test_bedrock_backend_connection_sends_queued_data(bedrock_protocol, proxy):
    """Verify that once the backend connection is made, queued data is sent."""
    addr = ("127.0.0.1", 12345)
    proxy._ensure_server_started = AsyncMock()
    # Setup a mock backend transport and protocol
    mock_backend_transport = AsyncMock()
    mock_backend_protocol = MagicMock()

    # Pre-populate the client map with queued data
    bedrock_protocol.client_map[addr] = {
        "protocol": None,
        "queue": [b"packet1", b"packet2"],
    }

    with patch("asyncio.get_running_loop") as mock_loop:
        # Make create_datagram_endpoint return our mocked objects
        mock_loop.return_value.create_datagram_endpoint.return_value = (
            mock_backend_transport,
            mock_backend_protocol,
        )
        await bedrock_protocol._create_backend_connection(addr, b"initial_packet")

    # Assert that the initial packet and all queued packets were sent
    mock_backend_transport.sendto.assert_any_call(b"initial_packet")
    mock_backend_transport.sendto.assert_any_call(b"packet1")
    mock_backend_transport.sendto.assert_any_call(b"packet2")
    assert not bedrock_protocol.client_map[addr]["queue"]  # Queue should be empty
