# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig  # Import the real AppConfig for type hinting
from proxy import AsyncProxy, BedrockProtocol

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_app_config():
    """Fixture for a mock AppConfig."""
    config = MagicMock(spec=AppConfig)
    server_config = MagicMock()
    server_config.name = "test_server"
    server_config.container_name = "test_container"
    config.game_servers = [server_config]
    # Use the correct attribute names from the new config model
    config.player_check_interval = 30
    config.server_stop_timeout = 10
    config.idle_timeout = 60
    return config


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    return AsyncMock()


@pytest.fixture
def mock_metrics_manager():
    """Fixture for a mock MetricsManager."""
    with patch("proxy.MetricsManager") as mock:
        yield mock()


@pytest.fixture
def proxy(mock_app_config, mock_docker_manager, mock_metrics_manager):
    """Fixture for an AsyncProxy instance."""
    return AsyncProxy(mock_app_config, mock_docker_manager)


@pytest.mark.asyncio
async def test_ensure_server_started(proxy, mock_docker_manager):
    """Test that a server is started if not already running."""
    server_config = proxy.app_config.game_servers[0]
    # Set the ready event to not-set to simulate a stopped server
    proxy._ready_events[server_config.name].clear()
    state = proxy._server_state[server_config.name]
    state["is_running"] = False

    await proxy._ensure_server_started(server_config)

    mock_docker_manager.start_server.assert_awaited_once_with(server_config)
    assert proxy._server_state[server_config.name]["is_running"] is True
    assert proxy._ready_events[server_config.name].is_set()


@pytest.mark.asyncio
async def test_ensure_server_already_running(proxy, mock_docker_manager):
    """Test that start is not called if the server is already running."""
    server_config = proxy.app_config.game_servers[0]
    # Set the ready event to indicate the server is already running
    proxy._ready_events[server_config.name].set()

    await proxy._ensure_server_started(server_config)

    mock_docker_manager.start_server.assert_not_called()


@pytest.mark.asyncio
@patch("proxy.asyncio.sleep", new_callable=AsyncMock)
@patch("proxy.time.time")
# CORRECTED: Patch the method we need to control
@patch("proxy.AsyncProxy._get_player_count", new_callable=AsyncMock)
async def test_monitor_server_activity_stops_idle_server(
    mock_get_players, mock_time, mock_sleep, proxy
):
    """Test that the monitor stops an idle server."""
    # Setup the mock to simulate an empty server
    mock_get_players.return_value = 0

    server_config = proxy.app_config.game_servers[0]
    state = proxy._server_state[server_config.name]
    state["is_running"] = True
    state["last_activity"] = 1000
    # Simulate time passing beyond the idle timeout
    mock_time.return_value = 1000 + proxy.app_config.idle_timeout + 1

    # Allow the monitor loop to run once, then exit
    mock_sleep.side_effect = [None, asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await proxy._monitor_server_activity()

    # Assert that the player count was checked
    mock_get_players.assert_awaited_once()
    # Assert that the server was stopped
    proxy.docker_manager.stop_server.assert_awaited_once_with(
        server_config.container_name, proxy.app_config.server_stop_timeout
    )
    assert state["is_running"] is False


def test_bedrock_protocol_init(proxy, mock_app_config):
    """Test BedrockProtocol initialization."""
    server_config = mock_app_config.game_servers[0]
    protocol = BedrockProtocol(proxy, server_config)
    assert protocol.proxy is proxy
    assert protocol.server_config is server_config
