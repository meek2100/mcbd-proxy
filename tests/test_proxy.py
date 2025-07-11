# tests/test_proxy.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from requests import patch

from proxy import NetherBridgeProxy


@pytest.fixture
def mock_settings():
    """Provides mock proxy settings."""
    settings = MagicMock()
    settings.player_check_interval_seconds = 30
    settings.idle_timeout_seconds = 60
    return settings


@pytest.fixture
def mock_server_config():
    """Provides a mock server configuration."""
    config = MagicMock()
    config.name = "TestServer"
    config.container_name = "test-server-container"
    return config


@pytest.fixture
def mock_docker_manager():
    """Provides a mock DockerManager."""
    return AsyncMock()


@pytest.fixture
def proxy(mock_settings, mock_server_config, mock_docker_manager):
    """Provides a NetherBridgeProxy instance with mocks."""
    return NetherBridgeProxy(
        settings=mock_settings,
        servers=[mock_server_config],
        docker_manager=mock_docker_manager,
        shutdown_event=asyncio.Event(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_connection_starts_stopped_server(
    proxy, mock_server_config, mock_docker_manager
):
    """
    Tests that a connection to a stopped server triggers a startup.
    """
    # Arrange
    reader, writer = AsyncMock(), AsyncMock()
    proxy.server_states[mock_server_config.container_name]["status"] = "stopped"
    mock_docker_manager.start_server.return_value = True

    # Mock the proxy data forwarding to prevent it from running forever
    proxy._proxy_data = AsyncMock()
    # Mock open_connection to avoid real network calls
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (AsyncMock(), AsyncMock())

        # Act
        await proxy._handle_connection(reader, writer, mock_server_config)

        # Assert
        mock_docker_manager.start_server.assert_awaited_once_with(
            mock_server_config, proxy.settings
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_stops_idle_server(
    proxy, mock_server_config, mock_docker_manager
):
    """
    Tests that the activity monitor shuts down an idle server.
    """
    # Arrange
    # Set server state to running, but with 0 sessions and old activity
    state = proxy.server_states[mock_server_config.container_name]
    state["status"] = "running"
    state["sessions"] = 0
    state["last_activity"] = 0  # Way in the past

    # Make the monitor loop run only once
    proxy._shutdown_event.is_set.side_effect = [False, True]

    # Act
    await proxy._monitor_activity()

    # Assert
    mock_docker_manager.stop_server.assert_awaited_once_with(
        mock_server_config.container_name
    )
