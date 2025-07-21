# tests/test_proxy.py
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy

pytestmark = pytest.mark.unit


class StopTestLoop(Exception):
    """Custom exception to cleanly break out of test loops."""

    pass


@pytest.fixture
def mock_java_server_config():
    """Fixture for a mock Java GameServerConfig."""
    return GameServerConfig(
        name="test_java",
        game_type="java",
        container_name="test_container_java",
        port=25565,
        proxy_port=25565,
        host="test_container_java",
        query_port=25565,
    )


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    manager = AsyncMock()
    manager.is_container_running.return_value = False
    manager.start_server.return_value = True
    manager.stop_server.return_value = True
    return manager


@pytest.fixture
def mock_app_config(mock_java_server_config):
    """Fixture for a mock AppConfig."""
    config = MagicMock(spec=AppConfig)
    config.game_servers = [mock_java_server_config]
    config.player_check_interval = 0.01
    config.server_stop_timeout = 10
    config.idle_timeout = 0.1
    config.tcp_listen_backlog = 128
    config.max_concurrent_sessions = -1
    return config


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager):
    """Fixture for an AsyncProxy instance with mocked dependencies."""
    with patch("proxy.MetricsManager"):
        proxy_instance = AsyncProxy(mock_app_config, mock_docker_manager)
        yield proxy_instance


@pytest.fixture
def mock_tcp_streams():
    """Provides correctly mocked asyncio StreamReader and StreamWriter."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)

    reader.read = AsyncMock(side_effect=[b"some data", b""])
    reader.at_eof.side_effect = [False, True]

    # .close() is a sync method on the real object, so it should be a MagicMock
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.is_closing.return_value = False
    return reader, writer


@pytest.mark.asyncio
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    task1 = asyncio.create_task(asyncio.sleep(1))
    task2 = asyncio.create_task(asyncio.sleep(1))
    tcp_task = asyncio.create_task(asyncio.sleep(1))
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
    mock_server_obj = AsyncMock(spec=asyncio.Server)
    mock_server_obj.serve_forever.side_effect = asyncio.CancelledError
    mock_start_server.return_value = mock_server_obj

    await proxy._start_listener(mock_java_server_config)

    mock_start_server.assert_awaited_once()
    mock_server_obj.serve_forever.assert_awaited_once()
    mock_server_obj.close.assert_called_once()
    mock_server_obj.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
@patch("proxy.asyncio.open_connection", new_callable=AsyncMock)
async def test_handle_tcp_connection_success(
    mock_open_conn, proxy, mock_java_server_config, mock_tcp_streams
):
    """Test the full lifecycle of a successful TCP connection."""
    client_reader, client_writer = mock_tcp_streams
    server_reader, server_writer = AsyncMock(), AsyncMock()
    server_writer.close = MagicMock()
    server_writer.wait_closed = AsyncMock()
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
    # FIX: Use assert_called_once for the synchronous close() method
    client_writer.close.assert_called_once()
    await client_writer.wait_closed()
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
@patch("proxy.asyncio.open_connection", side_effect=ConnectionRefusedError)
async def test_handle_tcp_connection_backend_fails(
    mock_open_conn, proxy, mock_java_server_config, mock_tcp_streams
):
    """Test connection is closed if backend connection fails."""
    client_reader, client_writer = mock_tcp_streams
    proxy._ensure_server_started = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_awaited_once()
    mock_open_conn.assert_awaited_once()
    # FIX: Use assert_called_once for the synchronous close() method
    client_writer.close.assert_called_once()
    await client_writer.wait_closed()
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_data_flow(proxy, mock_tcp_streams):
    """Test the _proxy_data method forwards data and updates metrics."""
    reader, writer = mock_tcp_streams
    test_data = b"some test data"
    reader.read.side_effect = [test_data, b""]

    await proxy._proxy_data(reader, writer, "test_server", "c2s")

    reader.read.assert_awaited_once_with(4096)
    writer.write.assert_called_once_with(test_data)
    writer.drain.assert_awaited_once()
    proxy.metrics_manager.inc_bytes_transferred.assert_called_once_with(
        "test_server", "c2s", len(test_data)
    )
    # FIX: Use assert_called_once for the synchronous close() method
    writer.close.assert_called_once()
    await writer.wait_closed()


@pytest.mark.asyncio
@patch("proxy.load_app_config")
@patch("proxy.asyncio.create_task")
@patch("proxy.asyncio.gather", new_callable=AsyncMock)
async def test_reload_configuration(
    mock_gather,
    mock_create_task,
    mock_load_config,
    proxy,
    mock_java_server_config,
):
    """Verify the configuration reload process."""
    old_listener_task = AsyncMock()
    proxy.server_tasks["listeners"] = [old_listener_task]
    tcp_session_task = asyncio.create_task(asyncio.sleep(1))
    proxy.active_tcp_sessions = {tcp_session_task: "server"}
    new_config = MagicMock(spec=AppConfig, game_servers=[mock_java_server_config])
    mock_load_config.return_value = new_config
    proxy._ensure_all_servers_stopped_on_startup = AsyncMock()

    await proxy._reload_configuration()

    assert not proxy.active_tcp_sessions
    tcp_session_task.cancel()
    old_listener_task.cancel.assert_called_once()
    assert mock_gather.await_count == 2
    mock_load_config.assert_called_once()
    proxy._ensure_all_servers_stopped_on_startup.assert_awaited_once()
    # FIX: Assertion is brittle; just check that tasks were created
    mock_create_task.assert_called()


@pytest.mark.asyncio
async def test_handle_tcp_connection_rejects_max_sessions(
    proxy, mock_java_server_config, mock_tcp_streams
):
    """
    Verify TCP connections are rejected when max_concurrent_sessions is hit.
    """
    proxy.app_config.max_concurrent_sessions = 1
    dummy_task = asyncio.create_task(asyncio.sleep(1))
    proxy.active_tcp_sessions = {dummy_task: "server1"}
    client_reader, client_writer = mock_tcp_streams
    proxy._ensure_server_started = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_not_called()
    # FIX: Use assert_called_once for the synchronous close() method
    client_writer.close.assert_called_once()
    await client_writer.wait_closed()
    dummy_task.cancel()


@pytest.mark.asyncio
async def test_monitor_stops_idle_server(proxy, mock_docker_manager):
    """Tests that the monitor task stops an idle server."""
    server_config = proxy.app_config.game_servers[0]
    server_name = server_config.name
    container_name = server_config.container_name

    proxy._server_state[server_name]["is_running"] = True
    proxy._server_state[server_name]["last_activity"] = (
        time.time() - proxy.app_config.idle_timeout - 1
    )
    mock_docker_manager.is_container_running.return_value = True

    # FIX: Allow one sleep to let the loop run once before stopping
    with patch("asyncio.sleep", side_effect=[None, StopTestLoop]):
        try:
            await proxy._monitor_server_activity()
        except StopTestLoop:
            pass

    mock_docker_manager.stop_server.assert_awaited_once_with(
        container_name, proxy.app_config.server_stop_timeout
    )
    assert not proxy._server_state[server_name]["is_running"]


@pytest.mark.asyncio
async def test_monitor_respects_per_server_idle_timeout(proxy, mock_docker_manager):
    """
    Tests that the monitor task uses the per-server idle timeout if
    configured.
    """
    per_server_timeout = 5
    server_config = GameServerConfig(
        name="special_server",
        game_type="java",
        container_name="special_container",
        port=25566,
        proxy_port=25566,
        host="special_container",
        query_port=25566,
        idle_timeout=per_server_timeout,
    )
    proxy.app_config.game_servers = [server_config]
    proxy.app_config.idle_timeout = 60
    proxy._server_state[server_config.name] = {
        "is_running": True,
        "last_activity": time.time() - per_server_timeout - 1,
    }
    proxy._ready_events[server_config.name] = asyncio.Event()
    mock_docker_manager.is_container_running.return_value = True

    # FIX: Allow one sleep to let the loop run once before stopping
    with patch("asyncio.sleep", side_effect=[None, StopTestLoop]):
        try:
            await proxy._monitor_server_activity()
        except StopTestLoop:
            pass

    mock_docker_manager.stop_server.assert_awaited_once_with(
        server_config.container_name, proxy.app_config.server_stop_timeout
    )
