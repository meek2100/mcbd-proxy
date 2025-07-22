# tests/test_proxy.py
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy, BedrockProtocol

pytestmark = pytest.mark.unit


class StopTestLoop(Exception):
    """Custom exception to cleanly break out of test loops."""


# --- Fixtures ---


@pytest.fixture
def mock_java_server_config():
    """Provides a mock Java server config."""
    return GameServerConfig(
        name="test_java",
        game_type="java",
        container_name="test_container_java",
        port=25565,
        proxy_port=25565,
        host="test_container_java",
    )


@pytest.fixture
def mock_bedrock_server_config():
    """Provides a mock Bedrock server config."""
    return GameServerConfig(
        name="test_bedrock",
        game_type="bedrock",
        container_name="test_container_bedrock",
        port=19132,
        proxy_port=19132,
        host="test_container_bedrock",
    )


@pytest.fixture
def mock_docker_manager():
    """Provides a mock DockerManager with explicit async methods."""
    manager = AsyncMock()
    manager.is_container_running = AsyncMock(return_value=False)
    manager.start_server = AsyncMock(return_value=True)
    manager.stop_server = AsyncMock()  # Explicitly make this an AsyncMock
    return manager


@pytest.fixture
def mock_app_config(mock_java_server_config, mock_bedrock_server_config):
    """Provides a mock AppConfig with one of each server type."""
    config = MagicMock(spec=AppConfig)
    config.game_servers = [mock_java_server_config, mock_bedrock_server_config]
    config.player_check_interval = 0.01
    config.server_stop_timeout = 10
    config.idle_timeout = 0.1
    config.tcp_listen_backlog = 128
    config.max_concurrent_sessions = 5
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


# --- AsyncProxy Method Tests ---


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks(proxy):
    """Verify the shutdown method cancels all registered tasks."""
    loop = asyncio.get_running_loop()
    # Use tasks that run long enough to be cancelled
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
    mock_open_conn, proxy, mock_java_server_config, mock_tcp_streams
):
    """Test the full lifecycle of a successful TCP connection."""
    client_reader, client_writer = mock_tcp_streams
    server_reader, server_writer = mock_tcp_streams
    mock_open_conn.return_value = (server_reader, server_writer)
    proxy._ensure_server_started = AsyncMock()
    proxy._proxy_data = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_awaited_once_with(mock_java_server_config)
    proxy.metrics_manager.inc_active_connections.assert_called_once()
    assert proxy._proxy_data.await_count == 2
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tcp_connection_rejects_max_sessions(
    proxy, mock_java_server_config, mock_tcp_streams
):
    """Test that new TCP connections are rejected when max_sessions is reached."""
    proxy.app_config.max_concurrent_sessions = 1
    proxy.active_tcp_sessions = {MagicMock(): "fake_session"}
    _reader, writer = mock_tcp_streams

    await proxy._handle_tcp_connection(
        _reader, writer, mock_java_server_config
    )

    proxy.metrics_manager.inc_active_connections.assert_not_called()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_data_handles_connection_reset(proxy, mock_tcp_streams):
    """Test that _proxy_data handles ConnectionResetError gracefully."""
    reader, writer = mock_tcp_streams
    reader.read.side_effect = ConnectionResetError

    await proxy._proxy_data(reader, writer, "test_server", "c2s")

    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_stops_idle_server(proxy, mock_docker_manager):
    """Tests that the monitor task stops an idle server."""
    server_config = proxy.app_config.game_servers[0]
    proxy._server_state[server_config.name]["is_running"] = True
    proxy._server_state[server_config.name]["last_activity"] = (
        time.time() - proxy.app_config.idle_timeout - 1
    )
    mock_docker_manager.is_container_running.return_value = True

    with patch("asyncio.sleep", side_effect=StopTestLoop()):
        try:
            await proxy._monitor_server_activity()
        except StopTestLoop:
            pass

    mock_docker_manager.stop_server.assert_awaited_once_with(
        server_config.container_name, proxy.app_config.server_stop_timeout
    )


# --- BedrockProtocol Tests ---


@pytest_asyncio.fixture
async def bedrock_protocol(proxy, mock_bedrock_server_config):
    """Provides a BedrockProtocol instance for testing."""
    protocol = BedrockProtocol(proxy, mock_bedrock_server_config)
    # Use MagicMock for transport; its methods are not awaitable
    protocol.transport = MagicMock(spec=asyncio.DatagramTransport)
    # Manually call connection_made to start the cleanup task in a loop
    protocol.connection_made(protocol.transport)
    yield protocol
    # Cleanup the task after the test
    if protocol.cleanup_task:
        protocol.cleanup_task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bedrock_new_client_creates_session(bedrock_protocol, proxy):
    """Verify a new UDP client creates a session and starts backend connection."""
    addr = ("127.0.0.1", 12345)
    with patch.object(
        bedrock_protocol, "_create_backend_connection", new_callable=AsyncMock
    ) as mock_create_backend:
        bedrock_protocol.datagram_received(b"ping", addr)

    assert addr in bedrock_protocol.client_map
    proxy.metrics_manager.inc_active_connections.assert_called_once()
    # The method is NOT awaited, it's run in a background task.
    # We just need to ensure it was CALLED.
    mock_create_backend.assert_called_once_with(addr, b"ping")


@pytest.mark.asyncio
async def test_bedrock_client_forwards_to_backend(bedrock_protocol):
    """Verify an existing client with a ready backend forwards data directly."""
    addr = ("127.0.0.1", 12345)
    mock_backend_transport = MagicMock(spec=asyncio.DatagramTransport)
    bedrock_protocol.client_map[addr] = {
        "last_activity": time.time(),
        "protocol": MagicMock(transport=mock_backend_transport),
        "queue": [],
    }

    bedrock_protocol.datagram_received(b"player_data", addr)

    mock_backend_transport.sendto.assert_called_once_with(b"player_data")


@pytest.mark.asyncio
async def test_bedrock_cleanup_removes_client(bedrock_protocol, proxy):
    """Verify the cleanup logic correctly removes a client and its resources."""
    addr = ("127.0.0.1", 12345)
    mock_backend_protocol = MagicMock()
    mock_backend_protocol.transport = MagicMock(spec=asyncio.DatagramTransport)
    bedrock_protocol.client_map[addr] = {
        "protocol": mock_backend_protocol,
        "task": MagicMock(),
    }

    bedrock_protocol._cleanup_client(addr)

    assert addr not in bedrock_protocol.client_map
    mock_backend_protocol.transport.close.assert_called_once()
    proxy.metrics_manager.dec_active_connections.assert_called_once_with(
        "test_bedrock"
    )