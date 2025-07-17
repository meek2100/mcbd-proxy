# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from proxy import AsyncProxy, BedrockProtocol  # Ensure BedrockProtocol is imported

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
    # Default to having both servers for general proxy tests
    config.game_servers = [mock_java_server_config, mock_bedrock_server_config]
    config.player_check_interval = 0.01  # Small interval for faster tests
    config.server_stop_timeout = 10
    config.idle_timeout = 0.1  # Small timeout for faster tests
    config.tcp_listen_backlog = 128
    config.max_concurrent_sessions = -1
    config.initial_boot_ready_max_wait = 180
    config.initial_server_query_delay = 10
    return config


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    manager = AsyncMock()
    manager.is_container_running.side_effect = [False, True]
    manager.stop_server.return_value = True  # Ensure stop_server succeeds by default
    return manager


@pytest.fixture
def mock_metrics_manager():
    """Fixture for a mock MetricsManager."""
    with patch("proxy.MetricsManager") as mock:
        mock_instance = mock.return_value
        mock_instance.inc_active_connections = MagicMock()
        mock_instance.dec_active_connections = MagicMock()
        mock_instance.observe_startup_duration = MagicMock()
        mock_instance.inc_bytes_transferred = MagicMock()
        yield mock_instance


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager, mock_metrics_manager):
    """Fixture for an AsyncProxy instance."""
    return AsyncProxy(mock_app_config, mock_docker_manager)


@pytest.fixture
def mock_tcp_streams():
    """Provides mock asyncio StreamReader and StreamWriter."""
    reader = AsyncMock()
    writer = AsyncMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)

    # FIX: Ensure awaited methods are explicitly set with AsyncMock
    # and return_value is appropriate for each.
    reader.read = AsyncMock(side_effect=[b"some data", b""])  # Simulates data then EOF
    reader.at_eof = MagicMock(
        side_effect=[False, True]
    )  # at_eof does not need to be awaited
    writer.write = MagicMock()  # Not awaited, just called
    writer.drain = AsyncMock()
    writer.close = AsyncMock(return_value=None)  # Explicitly returns None on close
    writer.wait_closed = AsyncMock(return_value=None)
    return reader, writer


@pytest.fixture
@patch("proxy.asyncio.create_task")  # Patch create_task for unit test isolation
def bedrock_protocol(mock_create_task, proxy, mock_bedrock_server_config):
    """
    Fixture for a BedrockProtocol instance with a mocked proxy.
    Patches asyncio.create_task to prevent RuntimeError: no running event loop.
    """
    # mock_create_task.return_value should be an awaitable (mock) object
    mock_create_task.return_value = AsyncMock()

    protocol = BedrockProtocol(proxy, mock_bedrock_server_config)
    protocol.transport = AsyncMock()
    protocol.cleanup_task = AsyncMock()
    protocol.cleanup_task.cancel()
    return protocol


@pytest.mark.asyncio
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    tcp_session_task = asyncio.create_task(asyncio.sleep(0.1))
    await asyncio.sleep(0.001)

    proxy.server_tasks = {"listeners": [task1], "monitor": task2}
    proxy.active_tcp_sessions = {tcp_session_task: "server"}

    proxy._shutdown_handler()

    await asyncio.sleep(0.01)

    assert task1.cancelled()
    assert task2.cancelled()
    assert tcp_session_task.cancelled()


@pytest.mark.asyncio
@patch("proxy.asyncio.start_server", new_callable=AsyncMock)
async def test_start_listener_tcp(mock_start_server, proxy, mock_java_server_config):
    """Verify _start_listener correctly sets up a TCP server."""
    mock_serve_forever = AsyncMock(side_effect=asyncio.CancelledError)
    mock_start_server.return_value.serve_forever = mock_serve_forever

    listener_task = asyncio.create_task(proxy._start_listener(mock_java_server_config))
    await asyncio.sleep(0.01)
    await listener_task

    mock_start_server.assert_awaited_once_with(
        ANY,  # Accept any callable as the first argument
        mock_java_server_config.proxy_host,
        mock_java_server_config.proxy_port,
        backlog=proxy.app_config.tcp_listen_backlog,
    )
    mock_serve_forever.assert_awaited_once()


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
    client_writer.wait_closed.assert_awaited_once()  # Ensure wait_closed is called
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
    client_writer.wait_closed.assert_awaited_once()  # Ensure wait_closed is called
    proxy.metrics_manager.dec_active_connections.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_data_flow(proxy, mock_metrics_manager):
    """Test the _proxy_data method forwards data and updates metrics."""
    mock_reader = AsyncMock()
    mock_writer = AsyncMock()
    test_data = b"some test data"

    mock_reader.at_eof = MagicMock(side_effect=[False, True])
    mock_reader.read = AsyncMock(side_effect=[test_data, b""])
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.close = AsyncMock(return_value=None)
    mock_writer.wait_closed = AsyncMock(return_value=None)

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
@patch("proxy.asyncio.gather", new_callable=AsyncMock)
async def test_reload_configuration(
    mock_gather,
    mock_create_task,
    mock_load_config,
    proxy,
    mock_bedrock_server_config,
):
    """Verify the configuration reload process."""
    old_listener_task = AsyncMock()
    proxy.server_tasks["listeners"] = [old_listener_task]

    dummy_tcp_task = AsyncMock()
    proxy.active_tcp_sessions = {dummy_tcp_task: "some_server"}

    mock_gather.return_value = AsyncMock()

    new_config = MagicMock(spec=AppConfig)
    new_config.game_servers = [mock_bedrock_server_config]
    new_config.player_check_interval = 0.01
    new_config.server_stop_timeout = 10
    new_config.idle_timeout = 0.1
    new_config.tcp_listen_backlog = 128
    new_config.max_concurrent_sessions = -1
    new_config.initial_boot_ready_max_wait = 180
    new_config.initial_server_query_delay = 10
    mock_load_config.return_value = new_config

    mock_create_task.return_value = AsyncMock()

    proxy._ensure_all_servers_stopped_on_startup = AsyncMock()

    await proxy._reload_configuration()

    # Assert old tasks were passed to gather for cancellation
    # FIX: Assert that gather was called with arguments including ANY for the tasks
    mock_gather.assert_awaited_with(ANY, return_exceptions=True)
    # The gather will be called twice: once for active sessions, once for listeners.
    assert mock_gather.await_count == 2  # Check both gather calls happened

    assert not proxy.active_tcp_sessions
    mock_load_config.assert_called_once()
    assert proxy.docker_manager.app_config == new_config
    proxy._ensure_all_servers_stopped_on_startup.assert_awaited_once()

    mock_create_task.assert_called_once_with(
        proxy._start_listener(mock_bedrock_server_config)
    )


@pytest.mark.asyncio
async def test_handle_tcp_connection_rejects_max_sessions(
    proxy, mock_java_server_config, mock_tcp_streams
):
    """Verify TCP connections are rejected when max_concurrent_sessions is hit."""
    proxy.app_config.max_concurrent_sessions = 1
    dummy_tcp_task = AsyncMock()
    proxy.active_tcp_sessions = {dummy_tcp_task: "server1"}
    await asyncio.sleep(0.01)

    client_reader, client_writer = mock_tcp_streams
    proxy._ensure_server_started = AsyncMock()

    await proxy._handle_tcp_connection(
        client_reader, client_writer, mock_java_server_config
    )

    proxy._ensure_server_started.assert_not_called()
    client_writer.close.assert_awaited_once()
    client_writer.wait_closed.assert_awaited_once()  # Ensure wait_closed is called
    assert proxy.metrics_manager.dec_active_connections.call_count == 1
    dummy_tcp_task.cancel()
    await asyncio.sleep(0.01)


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

    # FIX: Ensure game_servers is a list containing only the target server
    # to avoid iterating over unintended servers in this specific test
    proxy.app_config.game_servers = [mock_bedrock_server_config]

    proxy._server_state[server_name]["is_running"] = True
    proxy._server_state[server_name]["last_activity"] = (
        mocker.patch("time.time", return_value=1000).start()
        - proxy.app_config.idle_timeout
        - 10
    )

    proxy.docker_manager.is_container_running.side_effect = [
        True,
        False,
        StopTestLoop(),
    ]
    proxy.docker_manager.stop_server = AsyncMock(
        return_value=True
    )  # Ensure it returns True

    mock_bedrock_protocol_instance = MagicMock(client_map={})
    proxy.udp_protocols[server_name] = mock_bedrock_protocol_instance

    monitor_task = asyncio.create_task(proxy._monitor_server_activity())

    try:
        await monitor_task
    except StopTestLoop:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if not monitor_task.done():
            monitor_task.cancel()
            await asyncio.sleep(0)

    proxy.docker_manager.stop_server.assert_awaited_once_with(
        container_name, proxy.app_config.server_stop_timeout
    )
    assert not proxy._server_state[server_name]["is_running"]
    proxy._ready_events[server_name].is_set.assert_called_once_with()
    assert not proxy._ready_events[server_name].is_set()
