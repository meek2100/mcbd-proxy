# tests/test_proxy.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig
from proxy import Proxy

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


@pytest.fixture
def mock_config():
    """Provides a mock AppConfig instance for tests."""
    return MagicMock(spec=AppConfig)


@pytest.fixture
def mock_docker_manager():
    """Provides a mock DockerManager instance for tests."""
    return AsyncMock()


@pytest.fixture
def mock_metrics_manager():
    """Provides a mock MetricsManager instance for tests."""
    # Mock the counter objects within MetricsManager
    manager = MagicMock()
    manager.connections_total = MagicMock()
    manager.connections_proxied = MagicMock()
    manager.connections_failed = MagicMock()
    return manager


@pytest.fixture
def proxy_instance(mock_config, mock_docker_manager, mock_metrics_manager):
    """Fixture to create a Proxy instance with mocked dependencies."""
    return Proxy(mock_config, mock_docker_manager, mock_metrics_manager)


@pytest.fixture
def mock_streams():
    """Fixture to create mock asyncio stream reader and writer."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    return reader, writer


@pytest.mark.asyncio
async def test_handle_client_success(proxy_instance, mock_docker_manager, mock_streams):
    """
    Tests the successful handling of a client connection, including
    proxying data.
    """
    mock_docker_manager.ensure_server_running.return_value = "172.17.0.5"
    reader, writer = mock_streams
    reader.read.side_effect = [b"some data", b""]  # Simulate one read then EOF

    # Patch asyncio.open_connection to avoid real network calls
    with patch("asyncio.open_connection") as mock_open_connection:
        mock_backend_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_backend_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_backend_reader.read.side_effect = [b"backend data", b""]
        mock_open_connection.return_value = (
            mock_backend_reader,
            mock_backend_writer,
        )

        await proxy_instance.handle_client(reader, writer)

        # Verify server was checked and connection was opened
        mock_docker_manager.ensure_server_running.assert_awaited_once()
        mock_open_connection.assert_awaited_once_with("172.17.0.5", 0)

        # Verify data was written to the backend
        mock_backend_writer.write.assert_called_once_with(b"some data")
        await mock_backend_writer.drain()

        # Verify backend response was written to the client
        writer.write.assert_called_once_with(b"backend data")
        await writer.drain()


@pytest.mark.asyncio
async def test_handle_client_no_backend_ip(
    proxy_instance, mock_docker_manager, mock_metrics_manager, mock_streams
):
    """
    Tests that the connection is closed if the backend server's IP
    cannot be obtained.
    """
    mock_docker_manager.ensure_server_running.return_value = None
    reader, writer = mock_streams

    await proxy_instance.handle_client(reader, writer)

    # Verify connection was closed and failure was logged
    writer.close.assert_awaited_once()
    mock_metrics_manager.connections_failed.inc.assert_called_once()


@pytest.mark.asyncio
async def test_shuttle_data(mock_streams):
    """Tests the data shuttling logic between two streams."""
    reader, writer = mock_streams
    reader.at_eof.side_effect = [False, True]  # Read once, then EOF
    reader.read.return_value = b"live data"

    proxy = Proxy(MagicMock(), MagicMock(), MagicMock())
    await proxy.shuttle_data(reader, writer, "test")

    # Verify data was read and written correctly
    reader.read.assert_awaited_with(4096)
    writer.write.assert_called_once_with(b"live data")
    writer.drain.assert_awaited_once()
    writer.close.assert_awaited_once()
