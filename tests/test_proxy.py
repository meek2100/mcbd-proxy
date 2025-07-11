import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from proxy import NetherBridgeProxy, UdpProxyProtocol

# region: Fixtures


@pytest.fixture
def mock_settings():
    """Provides a default ProxySettings object for tests."""
    return ProxySettings(idle_timeout_seconds=60)


@pytest.fixture
def mock_java_server_config():
    """Provides a sample Java server configuration."""
    return ServerConfig(
        name="TestJavaServer",
        server_type="java",
        listen_port=25565,
        container_name="test_java_server",
        internal_port=25565,
    )


@pytest.fixture
def mock_bedrock_server_config():
    """Provides a sample Bedrock server configuration."""
    return ServerConfig(
        name="TestBedrockServer",
        server_type="bedrock",
        listen_port=19132,
        container_name="test_bedrock_server",
        internal_port=19132,
    )


@pytest.fixture
def mock_docker_manager():
    """Mocks the DockerManager for unit tests."""
    manager = MagicMock(spec=DockerManager)
    manager.start_server = AsyncMock(return_value=True)
    manager.stop_server = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def shutdown_event():
    """Provides a real asyncio.Event for testing shutdown signals."""
    return asyncio.Event()


@pytest.fixture
def proxy(mock_settings, mock_docker_manager, shutdown_event):
    """Initializes the NetherBridgeProxy with mocked dependencies."""
    servers = []  # Start with no servers, add them in tests
    return NetherBridgeProxy(
        mock_settings, servers, mock_docker_manager, shutdown_event
    )


# endregion

# region: Unit Tests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_connection_starts_stopped_server(
    proxy, mock_java_server_config, mock_docker_manager
):
    """
    Tests that a TCP connection to a stopped Java server triggers a startup.
    """
    proxy.servers = [mock_java_server_config]
    proxy.server_states[mock_java_server_config.container_name]["status"] = "stopped"

    # Mock asyncio's stream objects
    reader, writer = AsyncMock(), AsyncMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)

    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        # Ensure open_connection returns two awaitable mocks
        mock_open.return_value = (AsyncMock(), AsyncMock())
        await proxy._handle_tcp_connection(reader, writer, mock_java_server_config)

    mock_docker_manager.start_server.assert_awaited_once_with(
        mock_java_server_config, proxy.settings
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_udp_datagram_starts_stopped_server(
    proxy, mock_bedrock_server_config, mock_docker_manager
):
    """

    Tests that a UDP datagram to a stopped Bedrock server triggers a startup.
    """
    proxy.servers = [mock_bedrock_server_config]
    state = proxy.server_states[mock_bedrock_server_config.container_name]
    state["status"] = "stopped"

    # Mock the datagram endpoint to simulate forwarding
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_transport = AsyncMock()
        mock_loop.return_value.create_datagram_endpoint.return_value = (
            mock_transport,
            AsyncMock(),
        )
        await proxy._handle_udp_datagram(
            b"test packet", ("127.0.0.1", 54321), mock_bedrock_server_config
        )

    mock_docker_manager.start_server.assert_awaited_once_with(
        mock_bedrock_server_config, proxy.settings
    )
    assert state["sessions"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_stops_idle_server(
    proxy, mock_java_server_config, mock_docker_manager, shutdown_event
):
    """
    Tests that the activity monitor shuts down an idle server.
    """
    proxy.servers = [mock_java_server_config]
    state = proxy.server_states[mock_java_server_config.container_name]
    state["status"] = "running"
    state["sessions"] = 0
    # Set last activity far in the past to ensure it's idle
    state["last_activity"] = time.time() - 100

    # Patch the event to make the monitor loop run exactly once
    with patch.object(shutdown_event, "is_set", side_effect=[False, True]):
        await proxy._monitor_activity()

    mock_docker_manager.stop_server.assert_awaited_once_with(
        mock_java_server_config.container_name
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_does_not_stop_active_server(
    proxy, mock_java_server_config, mock_docker_manager, shutdown_event
):
    """
    Tests that the activity monitor does not shut down an active server.
    """
    proxy.servers = [mock_java_server_config]
    state = proxy.server_states[mock_java_server_config.container_name]
    state["status"] = "running"
    state["sessions"] = 1  # Active session
    state["last_activity"] = time.time()

    with patch.object(shutdown_event, "is_set", side_effect=[False, True]):
        await proxy._monitor_activity()

    mock_docker_manager.stop_server.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_udp_protocol_datagram_received(proxy):
    """
    Ensures the UDP protocol correctly calls the proxy's handler.
    """
    server_config = MagicMock()
    # Mock the main proxy's handler method
    proxy._handle_udp_datagram = AsyncMock()

    protocol = UdpProxyProtocol(proxy, server_config)
    test_addr = ("192.168.1.100", 12345)
    test_data = b"ping"

    protocol.datagram_received(test_data, test_addr)
    # Give asyncio a moment to run the created task
    await asyncio.sleep(0)

    proxy._handle_udp_datagram.assert_awaited_once_with(
        test_data, test_addr, server_config
    )


# endregion
