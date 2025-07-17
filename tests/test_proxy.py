# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy

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
    config.max_concurrent_sessions = -1
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


@pytest.fixture
def mock_tcp_streams():
    """Provides mock asyncio StreamReader and StreamWriter."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    return reader, writer


@pytest.mark.asyncio
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    tcp_task = asyncio.create_task(asyncio.sleep(0.1))

    proxy.server_tasks = {"listeners": [task1], "monitor": task2}
    proxy.active_tcp_sessions = {tcp_task: "server"}

    proxy._shutdown_handler()

    await asyncio.sleep(0)

    assert task1.cancelled()
    assert task2.cancelled()
    assert tcp_task.cancelled()


@pytest.mark.asyncio
@patch("proxy.asyncio.start_server", new_callable=AsyncMock)
async def test_start_listener_tcp(mock_start_server, proxy, mock_java_server_config):
    """Verify _start_listener correctly sets up a TCP server."""
    mock_start_server.return_value.serve_forever.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await proxy._start_listener(mock_java_server_config)

    mock_start_server.assert_awaited_once_with(
        proxy._handle_tcp_connection,
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


@pytest.mark.asyncio
@patch("proxy.asyncio.open_connection")
async def test_handle_tcp_connection_success(
    mock_open_conn, proxy, mock_java_server_config, mock_tcp_streams
):
    """Test the full lifecycle of a successful TCP connection."""
    client_reader, client_writer = mock_tcp_streams
    server_reader, server_writer = AsyncMock(), AsyncMock()
    mock_open_conn.return_value = (server_reader, server_writer)
    proxy._ensure_server_started = AsyncMock()
    proxy._proxy_data = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_awaited_once_with(mock_java_server_config)
    mock_open_conn.assert_awaited_once_with(
        mock_java_server_config.host, mock_java_server_config.port
    )
    assert proxy._proxy_data.await_count == 2
    client_writer.close.assert_awaited_once()
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
@patch("proxy.asyncio.open_connection", side_effect=ConnectionRefusedError)
async def test_handle_tcp_connection_backend_fails(
    mock_open_conn, proxy, mock_java_server_config, mock_tcp_streams
):
    """Test that connection is closed if backend connection fails."""
    client_reader, client_writer = mock_tcp_streams
    proxy._ensure_server_started = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_awaited_once()
    mock_open_conn.assert_awaited_once()
    client_writer.close.assert_awaited_once()
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_data_flow(proxy, mock_metrics_manager):
    """Test the _proxy_data method forwards data and updates metrics."""
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = AsyncMock(spec=asyncio.StreamWriter)

    test_data = b"some test data"
    # Simulate reading data once, then EOF
    mock_reader.read.side_effect = [test_data, b""]
    mock_reader.at_eof.side_effect = [False, True]

    await proxy._proxy_data(mock_reader, mock_writer, "test_server", "c2s")

    # Verify data flow
    mock_reader.read.assert_awaited_once_with(4096)
    mock_writer.write.assert_called_once_with(test_data)
    mock_writer.drain.assert_awaited_once()

    # Verify metrics and cleanup
    mock_metrics_manager.inc_bytes_transferred.assert_called_once_with(
        "test_server", "c2s", len(test_data)
    )
    mock_writer.close.assert_awaited_once()
    mock_writer.wait_closed.assert_awaited_once()
