# tests/test_proxy.py
"""
Unit tests for the new asynchronous AsyncProxy class.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig
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
async def test_shutdown_handler_cancels_tasks(proxy):
    """Verify the shutdown handler cancels all registered tasks."""
    # Create mock tasks
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    proxy.server_tasks = {"listeners": [task1], "monitor": task2}

    proxy._shutdown_handler()

    # Yield control to the event loop to allow cancellation to be processed
    await asyncio.sleep(0)

    assert task1.cancelled()
    assert task2.cancelled()


@pytest.mark.asyncio
async def test_ensure_server_started(proxy, mock_docker_manager):
    """Test that a server is started if not already running."""
    server_config = proxy.app_config.game_servers[0]
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
    proxy._ready_events[server_config.name].set()

    await proxy._ensure_server_started(server_config)

    mock_docker_manager.start_server.assert_not_called()


@pytest.mark.asyncio
@patch("proxy.asyncio.sleep", new_callable=AsyncMock)
@patch("proxy.time.time")
@patch("proxy.AsyncProxy._get_player_count", new_callable=AsyncMock)
async def test_monitor_server_activity_stops_idle_server(
    mock_get_players, mock_time, mock_sleep, proxy
):
    """Test that the monitor stops an idle server."""
    mock_get_players.return_value = 0
    server_config = proxy.app_config.game_servers[0]
    state = proxy._server_state[server_config.name]
    state["is_running"] = True
    state["last_activity"] = 1000
    mock_time.return_value = 1000 + proxy.app_config.idle_timeout + 1

    # Allow the monitor loop to run once, then exit
    mock_sleep.side_effect = [None, asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await proxy._monitor_server_activity()

    mock_get_players.assert_awaited_once()
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
