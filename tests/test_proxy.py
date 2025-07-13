import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import Gauge

from config import AppSettings, Server
from proxy import NetherBridgeProxy


@pytest.fixture
def mock_settings():
    """Provides default AppSettings for tests."""
    return AppSettings(docker_url="unix:///var/run/docker.sock")


@pytest.fixture
def mock_server_config():
    """Provides a default Server configuration for tests."""
    return Server(
        server_name="test-server",
        listen_port=25565,
        listen_host="0.0.0.0",
        target_port=25565,
        docker_container_name="mc-test-server",
    )


@pytest.fixture
def mock_docker_manager():
    """Mocks the DockerManager with asynchronous methods."""
    manager = AsyncMock()
    manager.ensure_server_running.return_value = "172.17.0.2"
    return manager


@pytest.fixture
def mock_metrics():
    """Provides mock Prometheus gauges with correct method specs."""
    return {
        "active_sessions": MagicMock(spec=Gauge),
        "running_servers": MagicMock(spec=Gauge),
        "bytes_transferred": MagicMock(spec=Gauge),
        "server_startup_duration": MagicMock(spec=Gauge, set=MagicMock()),
    }


@pytest.fixture
def proxy_instance(
    mock_settings, mock_server_config, mock_docker_manager, mock_metrics
):
    """Initializes the NetherBridgeProxy with mocked dependencies."""
    return NetherBridgeProxy(
        settings=mock_settings,
        servers_list=[mock_server_config],
        docker_manager=mock_docker_manager,
        shutdown_event=asyncio.Event(),
        reload_event=asyncio.Event(),
        active_sessions_metric=mock_metrics["active_sessions"],
        running_servers_metric=mock_metrics["running_servers"],
        bytes_transferred_metric=mock_metrics["bytes_transferred"],
        server_startup_duration_metric=mock_metrics["server_startup_duration"],
        config_path=MagicMock(),
    )


@pytest.mark.asyncio
async def test_handle_client_success(proxy_instance, mock_server_config, mock_metrics):
    """
    Tests the full lifecycle of a client connection.
    """
    client_reader, writer = AsyncMock(), AsyncMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))

    server_reader, server_writer = AsyncMock(), AsyncMock()

    # FIX: at_eof is a sync method, so mock it with MagicMock to return bools
    client_reader.at_eof = MagicMock(side_effect=[False, True])
    client_reader.read.return_value = b"data from client"

    server_reader.at_eof = MagicMock(side_effect=[False, True])
    server_reader.read.return_value = b"data from server"

    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (server_reader, server_writer)
        await proxy_instance._handle_client(client_reader, writer, mock_server_config)

    proxy_instance.docker_manager.ensure_server_running.assert_awaited_once()
    mock_open.assert_awaited_once_with("172.17.0.2", 25565)
    server_writer.write.assert_called_with(b"data from client")
    writer.write.assert_called_with(b"data from server")


@pytest.mark.asyncio
async def test_proxy_run_and_shutdown(proxy_instance):
    """
    Tests that the main run loop starts listeners and handles shutdown.
    """
    listener = AsyncMock()
    with patch("asyncio.start_server", return_value=listener):
        run_task = asyncio.create_task(proxy_instance.run())
        await asyncio.sleep(0.01)  # Allow the task to start
        proxy_instance.shutdown_event.set()
        await run_task

    listener.close.assert_called_once()
    listener.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_client_docker_failure(proxy_instance, mock_server_config):
    """
    Tests that the client connection is terminated if the Docker container fails.
    """
    client_reader, writer = AsyncMock(), AsyncMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 54321))

    proxy_instance.docker_manager.ensure_server_running.return_value = None

    with patch("asyncio.open_connection") as mock_open:
        await proxy_instance._handle_client(client_reader, writer, mock_server_config)
        mock_open.assert_not_called()

    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
