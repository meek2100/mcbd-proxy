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
    config.player_check_interval = 0.01  # Small interval for faster tests
    config.server_stop_timeout = 10
    config.idle_timeout = 0.1  # Small timeout for faster tests
    config.tcp_listen_backlog = 128
    config.max_concurrent_sessions = -1
    return config


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    manager = AsyncMock()
    # Mock stop_server to avoid actual Docker calls and allow test to control exit
    # Manager will return false initially, then true after start attempt.
    # We explicitly mock start_server for tests that need it.
    manager.is_container_running.side_effect = [False, True]
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


@pytest.fixture
def bedrock_protocol(proxy, mock_bedrock_server_config):
    """Fixture for a BedrockProtocol instance with a mocked proxy."""
    protocol = BedrockProtocol(proxy, mock_bedrock_server_config)
    protocol.transport = AsyncMock()
    # Cancel the actual cleanup task to prevent interference in unit tests
    protocol.cleanup_task.cancel()
    return protocol


@pytest.mark.asyncio
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    tcp_task = asyncio.create_task(asyncio.sleep(0.1))

    proxy.server_tasks = {"listeners": [task1], "monitor": task2}
    proxy.active_tcp_sessions = {tcp_task: "server"}

    proxy._shutdown_handler()

    # Allow asyncio loop to process cancellations
    await asyncio.sleep(0)

    assert task1.cancelled()
    assert task2.cancelled()
    assert tcp_task.cancelled()


@pytest.mark.asyncio
@patch("proxy.asyncio.start_server", new_callable=AsyncMock)
async def test_start_listener_tcp(mock_start_server, proxy, mock_java_server_config):
    """Verify _start_listener correctly sets up a TCP server."""
    # Mock serve_forever to raise CancelledError so the test can exit
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
    # Assert _proxy_data was called for both directions
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
    mock_reader.read.side_effect = [test_data, b""]  # Simulate one read, then EOF
    # mock_reader.at_eof.side_effect = [False, True] # at_eof is checked after read

    await proxy._proxy_data(mock_reader, mock_writer, "test_server", "c2s")

    mock_reader.read.assert_awaited_once_with(4096)
    mock_writer.write.assert_called_once_with(test_data)
    mock_writer.drain.assert_awaited_once()
    mock_metrics_manager.inc_bytes_transferred.assert_called_once_with(
        "test_server", "c2s", len(test_data)
    )
    mock_writer.close.assert_awaited_once()
    mock_writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
@patch("proxy.load_app_config")
@patch("proxy.asyncio.create_task")
async def test_reload_configuration(
    mock_create_task, mock_load_config, proxy, mock_bedrock_server_config
):
    """Verify the configuration reload process."""
    # Setup initial tasks that would be running
    old_listener_task = AsyncMock()
    proxy.server_tasks["listeners"] = [old_listener_task]
    proxy.active_tcp_sessions[AsyncMock()] = "some_server"  # Add a dummy session

    # Mock new config being loaded
    new_config = MagicMock(spec=AppConfig)
    new_config.game_servers = [mock_bedrock_server_config]
    new_config.player_check_interval = 0.01  # ensure interval is set for new config
    new_config.server_stop_timeout = 10
    new_config.idle_timeout = 0.1
    new_config.tcp_listen_backlog = 128
    new_config.max_concurrent_sessions = -1
    mock_load_config.return_value = new_config

    # Mock dependent async methods
    proxy._ensure_all_servers_stopped_on_startup = AsyncMock()
    # Mock create_task for new listeners so we can inspect calls
    mock_create_task.return_value = AsyncMock()

    await proxy._reload_configuration()

    old_listener_task.cancel.assert_called_once()
    mock_load_config.assert_called_once()
    assert proxy.docker_manager.app_config == new_config
    proxy._ensure_all_servers_stopped_on_startup.assert_awaited_once()
    # Verify new listener task was created for the new server config
    mock_create_task.assert_called_once_with(
        proxy._start_listener(mock_bedrock_server_config)
    )
    assert not proxy.active_tcp_sessions  # All sessions should be cleared


@pytest.mark.asyncio
async def test_handle_tcp_connection_rejects_max_sessions(
    proxy, mock_java_server_config, mock_tcp_streams
):
    """Verify TCP connections are rejected when max_concurrent_sessions is hit."""
    proxy.app_config.max_concurrent_sessions = 1
    # Simulate one active session already
    proxy.active_tcp_sessions = {asyncio.create_task(asyncio.sleep(10)): "server1"}
    client_reader, client_writer = mock_tcp_streams
    proxy._ensure_server_started = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    # Ensure no attempt was made to start another session or connect to backend
    proxy._ensure_server_started.assert_not_called()
    client_writer.close.assert_awaited_once()
    assert proxy.metrics_manager.dec_active_connections.call_count == 0


# --- BedrockProtocol Unit Tests ---


@pytest.mark.asyncio
async def test_bedrock_rejects_max_sessions(bedrock_protocol, proxy):
    """Verify UDP packets are dropped when max_concurrent_sessions is hit."""
    proxy.app_config.max_concurrent_sessions = 1
    # Simulate one active UDP session already
    bedrock_protocol.client_map = {("1.1.1.1", 1234): {}}
    new_addr = ("2.2.2.2", 5678)
    proxy.metrics_manager.inc_active_connections = MagicMock()  # Reset mock

    bedrock_protocol.datagram_received(b"some data", new_addr)

    # Ensure no new session was created
    assert new_addr not in bedrock_protocol.client_map
    proxy.metrics_manager.inc_active_connections.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_server_activity_stops_idle_server(
    proxy, mock_bedrock_server_config, mocker
):
    """
    Tests that the monitor task correctly identifies an idle server
    and calls the stop method.
    """
    server_name = mock_bedrock_server_config.name
    container_name = mock_bedrock_server_config.container_name

    # Set initial state for the server
    proxy._server_state[server_name]["is_running"] = True
    # Set last_activity to be well before the idle_timeout
    proxy._server_state[server_name]["last_activity"] = (
        mocker.patch("time.time", return_value=1000).start()
        - proxy.app_config.idle_timeout
        - 10
    )

    # Mock DockerManager calls
    proxy.docker_manager.is_container_running.side_effect = [
        True,  # First check: container is running
        False,  # After stop: container is stopped
        StopTestLoop(),  # Stop the monitor loop after first full iteration
    ]
    proxy.docker_manager.stop_server = AsyncMock()

    # Run the monitor task
    monitor_task = asyncio.create_task(proxy._monitor_server_activity())

    try:
        # Wait for the monitor task to complete at least one check
        # It will raise StopTestLoop to exit the test after the required check
        await monitor_task
    except StopTestLoop:
        pass  # Expected exception to terminate the test gracefully
    except asyncio.CancelledError:
        pass  # Also possible if the test finishes quickly and task is cancelled
    finally:
        monitor_task.cancel()
        await asyncio.sleep(0)  # Let event loop process cancellation

    # Assert that stop_server was called for the idle server
    proxy.docker_manager.stop_server.assert_awaited_once_with(
        container_name, proxy.app_config.server_stop_timeout
    )
    # Assert server state is updated
    assert not proxy._server_state[server_name]["is_running"]
    # Assert ready event is cleared
    proxy._ready_events[server_name].is_set.assert_called_once_with()
    assert not proxy._ready_events[server_name].is_set()
