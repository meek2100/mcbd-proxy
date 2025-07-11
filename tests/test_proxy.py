import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from proxy import NetherBridgeProxy, UdpProxyProtocol


@pytest.fixture
def mock_settings():
    return ProxySettings(idle_timeout_seconds=60)


@pytest.fixture
def mock_java_server_config():
    return ServerConfig(
        name="TestJavaServer",
        server_type="java",
        listen_port=25565,
        container_name="test_java_server",
        internal_port=25565,
    )


@pytest.fixture
def mock_bedrock_server_config():
    return ServerConfig(
        name="TestBedrockServer",
        server_type="bedrock",
        listen_port=19132,
        container_name="test_bedrock_server",
        internal_port=19132,
    )


@pytest.fixture
def mock_docker_manager():
    manager = MagicMock(spec=DockerManager)
    manager.start_server = AsyncMock(return_value=True)
    manager.stop_server = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def shutdown_event():
    return asyncio.Event()


@pytest.fixture
def proxy(mock_settings, mock_docker_manager, shutdown_event):
    return NetherBridgeProxy(mock_settings, [], mock_docker_manager, shutdown_event)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_connection_starts_stopped_server(
    proxy, mock_java_server_config, mock_docker_manager
):
    proxy.servers = [mock_java_server_config]
    proxy.server_states[mock_java_server_config.container_name]["status"] = "stopped"

    reader = AsyncMock()
    reader.at_eof.side_effect = [False, True]
    writer = AsyncMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    writer.close = MagicMock()

    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (reader, writer)
        await proxy._handle_tcp_connection(reader, writer, mock_java_server_config)

    mock_docker_manager.start_server.assert_awaited_once_with(
        mock_java_server_config, proxy.settings
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_udp_datagram_starts_stopped_server(
    proxy, mock_bedrock_server_config, mock_docker_manager
):
    proxy.servers = [mock_bedrock_server_config]
    state = proxy.server_states[mock_bedrock_server_config.container_name]
    state["status"] = "stopped"

    mock_create_endpoint = AsyncMock(return_value=(AsyncMock(), AsyncMock()))

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.create_datagram_endpoint = mock_create_endpoint
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
    proxy.servers = [mock_java_server_config]
    state = proxy.server_states[mock_java_server_config.container_name]
    state["status"] = "running"
    state["sessions"] = 0
    state["last_activity"] = time.time() - 100

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
    proxy.servers = [mock_java_server_config]
    state = proxy.server_states[mock_java_server_config.container_name]
    state["status"] = "running"
    state["sessions"] = 1
    state["last_activity"] = time.time()

    with patch.object(shutdown_event, "is_set", side_effect=[False, True]):
        await proxy._monitor_activity()

    mock_docker_manager.stop_server.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_udp_protocol_datagram_received(proxy):
    server_config = MagicMock()
    proxy._handle_udp_datagram = AsyncMock()

    protocol = UdpProxyProtocol(proxy, server_config)
    test_addr = ("192.168.1.100", 12345)
    test_data = b"ping"

    protocol.datagram_received(test_data, test_addr)
    await asyncio.sleep(0)

    proxy._handle_udp_datagram.assert_awaited_once_with(
        test_data, test_addr, server_config
    )
