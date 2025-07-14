# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, GameServerConfig
from docker_manager import DockerManager
from proxy import AsyncProxy, BedrockProtocol

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_app_config():
    """Provides a mock AppConfig for testing the proxy."""
    server_java = MagicMock(spec=GameServerConfig)
    server_java.name = "mc-java"
    server_java.game_type = "java"
    server_java.host = "localhost"
    server_java.port = 25565
    server_java.pre_warm = True

    server_bedrock = MagicMock(spec=GameServerConfig)
    server_bedrock.name = "mc-bedrock"
    server_bedrock.game_type = "bedrock"
    server_bedrock.pre_warm = False

    config = MagicMock(spec=AppConfig)
    config.game_servers = [server_java, server_bedrock]
    config.is_prometheus_enabled = False
    return config


@pytest.fixture
def mock_docker_manager():
    """Provides a mock DockerManager."""
    return AsyncMock(spec=DockerManager)


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager):
    """Provides an instance of the AsyncProxy for testing."""
    return AsyncProxy(mock_app_config, mock_docker_manager)


async def test_startup_orchestration(proxy):
    """
    Verifies the main startup logic: listeners, monitors, and pre-warming.
    """
    with (
        patch.object(proxy, "_start_listener", new_callable=AsyncMock) as mock_listener,
        patch.object(
            proxy, "_monitor_server_activity", new_callable=AsyncMock
        ) as mock_monitor,
        patch.object(
            proxy, "_ensure_server_started", new_callable=AsyncMock
        ) as mock_ensure,
        patch("asyncio.get_running_loop") as mock_get_loop,
        patch("asyncio.gather", new_callable=AsyncMock),
    ):
        mock_loop = MagicMock()
        mock_get_loop.return_value = mock_loop

        await proxy.start()

        assert mock_listener.call_count == 2
        mock_monitor.assert_awaited_once()
        mock_ensure.assert_awaited_once_with(proxy.app_config.game_servers[0])


async def test_ensure_server_started_when_not_running(proxy):
    """
    Tests that _ensure_server_started calls the DockerManager to start
    a server that is not currently running.
    """
    server_config = proxy.app_config.game_servers[0]
    proxy._server_state[server_config.name]["is_running"] = False
    await proxy._ensure_server_started(server_config)
    proxy.docker_manager.start_server.assert_awaited_once_with(server_config)
    assert proxy._server_state[server_config.name]["is_running"] is True


async def test_ensure_server_started_when_already_running(proxy):
    """
    Tests that _ensure_server_started does not attempt to start a server
    that is already marked as running.
    """
    server_config = proxy.app_config.game_servers[0]
    proxy._server_state[server_config.name]["is_running"] = True
    await proxy._ensure_server_started(server_config)
    proxy.docker_manager.start_server.assert_not_awaited()


@patch("asyncio.open_connection", new_callable=AsyncMock)
async def test_handle_tcp_connection(mock_open_conn, proxy):
    """
    Tests the end-to-end handling of a single TCP client connection,
    including data proxying.
    """
    mock_client_reader, mock_client_writer = AsyncMock(), AsyncMock()
    mock_server_reader, mock_server_writer = AsyncMock(), AsyncMock()
    mock_open_conn.return_value = (mock_server_reader, mock_server_writer)
    with patch.object(proxy, "_proxy_data", new_callable=AsyncMock):
        server_config = proxy.app_config.game_servers[0]
        await proxy._handle_tcp_connection(
            mock_client_reader, mock_client_writer, server_config
        )
        mock_open_conn.assert_awaited_once_with(server_config.host, server_config.port)


async def test_bedrock_protocol_datagram_received(proxy):
    """
    Tests that the BedrockProtocol correctly handles an incoming datagram
    from a new client.
    """
    server_config = proxy.app_config.game_servers[1]
    protocol = BedrockProtocol(proxy, server_config)
    protocol.transport = AsyncMock()
    with patch.object(
        protocol, "_create_backend_connection", new_callable=AsyncMock
    ) as mock_create_backend:
        client_addr = ("127.0.0.1", 12345)
        test_data = b"test_packet"
        protocol.datagram_received(test_data, client_addr)
        await asyncio.sleep(0)
        mock_create_backend.assert_awaited_once_with(client_addr, test_data)
