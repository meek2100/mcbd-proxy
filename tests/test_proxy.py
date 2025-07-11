import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from proxy import NetherBridgeProxy, UdpProxyProtocol


@pytest.fixture
def mock_settings():
    """Fixture for mock proxy settings."""
    return ProxySettings(
        idle_timeout_seconds=0.5,  # Reduced for faster test execution
        player_check_interval_seconds=0.1,  # Reduced for faster test execution
        query_timeout_seconds=5,
        server_ready_max_wait_time_seconds=120,
        initial_boot_ready_max_wait_time_seconds=180,
        server_startup_delay_seconds=5,
        initial_server_query_delay_seconds=10,
        log_level="INFO",
        log_formatter="json",
        healthcheck_stale_threshold_seconds=60,
        proxy_heartbeat_interval_seconds=15,
        tcp_listen_backlog=128,
        max_concurrent_sessions=-1,
        prometheus_enabled=True,
        prometheus_port=8000,
    )


@pytest.fixture
def mock_java_server_config():
    """Fixture for a mock Java server configuration."""
    return ServerConfig(
        name="TestJavaServer",
        server_type="java",
        listen_port=25565,
        container_name="test_java_server",
        internal_port=25565,
    )


@pytest.fixture
def mock_bedrock_server_config():
    """Fixture for a mock Bedrock server configuration."""
    return ServerConfig(
        name="TestBedrockServer",
        server_type="bedrock",
        listen_port=19132,
        container_name="test_bedrock_server",
        internal_port=19132,
    )


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    manager = MagicMock(spec=DockerManager)
    manager.start_server = AsyncMock(return_value=True)
    manager.stop_server = AsyncMock(return_value=True)
    manager.is_container_running = AsyncMock(return_value=False)
    manager.wait_for_server_query_ready = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def shutdown_event():
    """Fixture for an asyncio shutdown event."""
    return asyncio.Event()


@pytest.fixture
def reload_event():
    """Fixture for an asyncio reload event."""
    return asyncio.Event()


@pytest.fixture
def mock_metrics():
    """Fixture for mock Prometheus metric objects."""
    metrics = MagicMock()
    metrics.ACTIVE_SESSIONS = MagicMock()
    metrics.RUNNING_SERVERS = MagicMock()
    metrics.BYTES_TRANSFERRED = MagicMock()
    metrics.SERVER_STARTUP_DURATION = MagicMock()
    return metrics


@pytest.fixture
def proxy(
    mock_settings,
    mock_docker_manager,
    shutdown_event,
    reload_event,
    mock_metrics,
):
    """
    Fixture to create a NetherBridgeProxy instance with mocked dependencies.
    """
    return NetherBridgeProxy(
        mock_settings,
        [],
        mock_docker_manager,
        shutdown_event,
        reload_event,
        mock_metrics.ACTIVE_SESSIONS,
        mock_metrics.RUNNING_SERVERS,
        mock_metrics.BYTES_TRANSFERRED,
        mock_metrics.SERVER_STARTUP_DURATION,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_connection_starts_stopped_server(
    proxy, mock_java_server_config, mock_docker_manager
):
    """
    Tests that a TCP connection attempt to a stopped server
    triggers the server to start.
    """
    proxy.servers_list = [mock_java_server_config]
    proxy.servers_config_map = {
        mock_java_server_config.listen_port: mock_java_server_config
    }
    proxy.server_states[mock_java_server_config.container_name]["status"] = (
        "stopped"  # Ensure initial state is stopped
    )

    # Configure realistic stream mocks for client-side
    reader = AsyncMock()
    reader.read.side_effect = [b"client_data", b""]  # Simulate some data
    # reader.at_eof is a synchronous method, so use MagicMock
    reader.at_eof = MagicMock(side_effect=[False, False, True])

    writer = MagicMock()  # writer.write is a synchronous method
    writer.drain = AsyncMock()
    # writer.close is a synchronous method
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 1234)

    # Mock asyncio.open_connection for the internal server connection
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        mock_server_reader = AsyncMock()
        mock_server_reader.read.side_effect = [b"server_data", b""]
        mock_server_reader.at_eof = MagicMock(side_effect=[False, False, True])

        mock_server_writer = MagicMock()  # writer.write is a synchronous method
        mock_server_writer.drain = AsyncMock()
        mock_server_writer.close = MagicMock()
        mock_server_writer.wait_closed = AsyncMock()

        mock_open.return_value = (mock_server_reader, mock_server_writer)

        # Call the method under test
        await proxy._handle_tcp_connection(reader, writer, mock_java_server_config)

    mock_docker_manager.start_server.assert_awaited_once_with(
        mock_java_server_config, proxy.settings
    )

    # Assert client-side stream interactions
    writer.write.assert_called_with(b"server_data")  # Data proxied from server
    writer.drain.assert_awaited_once()
    writer.wait_closed.assert_awaited_once()

    # Assert server-side stream interactions (proxied from client)
    mock_server_writer.write.assert_called_with(
        b"client_data"
    )  # Data proxied from client
    mock_server_writer.drain.assert_awaited_once()
    mock_server_writer.wait_closed.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_udp_datagram_starts_stopped_server(
    proxy, mock_bedrock_server_config, mock_docker_manager
):
    """
    Tests that a UDP datagram to a stopped server
    triggers the server to start.
    """
    proxy.servers_list = [mock_bedrock_server_config]
    proxy.servers_config_map = {
        mock_bedrock_server_config.listen_port: mock_bedrock_server_config
    }
    proxy.server_states[mock_bedrock_server_config.container_name]["status"] = (
        "stopped"  # Ensure initial state is stopped
    )

    # Configure a transport mock with synchronous methods
    transport = MagicMock()
    transport.sendto = MagicMock()
    transport.close = MagicMock()
    transport.is_closing.return_value = False

    # Mock asyncio.get_running_loop().create_datagram_endpoint
    mock_ep = AsyncMock(return_value=(transport, MagicMock()))
    with patch("asyncio.get_running_loop") as mock_get_loop:
        mock_get_loop.return_value.create_datagram_endpoint = mock_ep
        await proxy._handle_udp_datagram(
            b"p", ("1.2.3.4", 5), mock_bedrock_server_config
        )

    mock_docker_manager.start_server.assert_awaited_once_with(
        mock_bedrock_server_config, proxy.settings
    )
    # Ensure transport.close() is called when session cleans up
    transport.close.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_stops_idle_server(
    proxy, mock_java_server_config, mock_docker_manager, shutdown_event
):
    """
    Tests that the monitor stops a server that has been idle for too long.
    """
    proxy.servers_list = [mock_java_server_config]
    proxy.servers_config_map = {
        mock_java_server_config.listen_port: mock_java_server_config
    }
    container_name = mock_java_server_config.container_name
    state = proxy.server_states[container_name]

    # Patch time.time to control its progression deterministically
    mock_current_time = 1000.0
    with patch("time.time") as mock_time:
        mock_time.side_effect = [
            mock_current_time,  # Initial time for monitor loop
            mock_current_time + 10,  # For heartbeat check if needed
            mock_current_time
            + proxy.settings.player_check_interval_seconds * 1,  # First check
            mock_current_time
            + proxy.settings.player_check_interval_seconds * 2,  # Second check
            # Ensure enough time passes for idle timeout
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.1,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.2,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.3,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.4,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.5,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.6,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.7,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.8,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.9,
            mock_current_time + proxy.settings.idle_timeout_seconds + 1.0,
        ]

        # Manually set initial state for the test
        state["status"] = "running"
        # Set last_activity to be well beyond idle_timeout_seconds at start_time
        state["last_activity"] = (
            mock_current_time - proxy.settings.idle_timeout_seconds - 5
        )
        state["sessions"] = 0  # No active sessions

        # Run the monitor activity in a separate task
        monitor_task = asyncio.create_task(proxy._monitor_activity())

        # Wait just enough for the monitor to run its checks and stop the server
        # We need to wait for the actual asynchronous operations to complete
        # Increase the sleep duration to give it ample time
        await asyncio.sleep(
            proxy.settings.player_check_interval_seconds * 10
            + proxy.settings.idle_timeout_seconds
            + 1.0  # Add a larger buffer for execution
        )

        # Now, set the shutdown event and wait for the monitor task to finish
        shutdown_event.set()
        monitor_task.cancel()  # Send cancellation to the monitor task
        await asyncio.gather(
            monitor_task, return_exceptions=True
        )  # Await its completion

    mock_docker_manager.stop_server.assert_awaited_once_with(container_name)
    # FIX: Assert for "stopped" status as per proxy.py's implementation
    assert proxy.server_states[container_name]["status"] == "stopped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_does_not_stop_active_server(
    proxy, mock_java_server_config, mock_docker_manager, shutdown_event
):
    """
    Tests that the monitor does not stop a server that is active
    (has active sessions).
    """
    proxy.servers_list = [mock_java_server_config]
    proxy.servers_config_map = {
        mock_java_server_config.listen_port: mock_java_server_config
    }
    container_name = mock_java_server_config.container_name
    state = proxy.server_states[container_name]

    # Patch time.time to control its progression deterministically
    mock_current_time = 1000.0
    with patch("time.time") as mock_time:
        mock_time.side_effect = [
            mock_current_time,  # Initial time
            mock_current_time + 10,  # For heartbeat check if needed
            mock_current_time + proxy.settings.player_check_interval_seconds * 1,
            mock_current_time + proxy.settings.player_check_interval_seconds * 2,
            # Enough time passes, but sessions are active
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.1,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.2,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.3,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.4,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.5,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.6,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.7,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.8,
            mock_current_time + proxy.settings.idle_timeout_seconds + 0.9,
            mock_current_time + proxy.settings.idle_timeout_seconds + 1.0,
        ]

        # Manually set initial state for the test
        state["status"] = "running"
        state["last_activity"] = mock_current_time - 100  # Idle time, but with sessions
        state["sessions"] = 1  # Active sessions

        # Run the monitor activity in a separate task
        monitor_task = asyncio.create_task(proxy._monitor_activity())

        # Wait long enough for the monitor to perform checks
        await asyncio.sleep(
            proxy.settings.player_check_interval_seconds * 10
            + proxy.settings.idle_timeout_seconds
            + 1.0  # Add a larger buffer for execution
        )

        # Now, set the shutdown event and wait for the monitor task to finish
        shutdown_event.set()
        monitor_task.cancel()  # Send cancellation to the monitor task
        await asyncio.gather(
            monitor_task, return_exceptions=True
        )  # Await its completion

    mock_docker_manager.stop_server.assert_not_awaited()
    assert proxy.server_states[container_name]["status"] == "running"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_udp_protocol_datagram_received(proxy, mock_bedrock_server_config):
    """
    Tests that the UdpProxyProtocol correctly calls _handle_udp_datagram
    when a datagram is received.
    """
    proxy.servers_list = [mock_bedrock_server_config]
    proxy.servers_config_map = {
        mock_bedrock_server_config.listen_port: mock_bedrock_server_config
    }
    proxy.server_states[mock_bedrock_server_config.container_name]["status"] = (
        "stopped"  # Ensure initial state is stopped
    )

    # Mock _handle_udp_datagram on the proxy instance
    proxy._handle_udp_datagram = AsyncMock()

    # Correctly instantiate UdpProxyProtocol with server_config
    protocol = UdpProxyProtocol(proxy, mock_bedrock_server_config)

    data = b"some_udp_data"
    addr = ("127.0.0.1", 12345)
    protocol.datagram_received(data, addr)

    # Allow the async task created by datagram_received to run
    await asyncio.sleep(0)

    # Assert that _handle_udp_datagram was called with correct arguments,
    # including the server_config that was passed during protocol init.
    proxy._handle_udp_datagram.assert_awaited_once_with(
        data, addr, mock_bedrock_server_config
    )
