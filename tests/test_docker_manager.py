# tests/test_docker_manager.py
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import AppConfig, GameServerConfig
from docker_manager import DockerManager

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_app_config():
    """Provides a mock AppConfig for the DockerManager."""
    config = MagicMock(spec=AppConfig)
    config.server_startup_timeout = 0.2
    config.server_query_interval = 0.1
    return config


@pytest.fixture
def mock_server_config():
    """Provides a mock server config object for testing isolated methods."""
    config = MagicMock(spec=GameServerConfig)
    config.game_type = "bedrock"
    config.host = "localhost"
    config.query_port = 19132
    config.container_name = "test-container"
    return config


@pytest_asyncio.fixture
async def docker_manager(mock_app_config):
    """Provides a DockerManager instance with a mocked aiodocker client."""
    with patch("docker_manager.aiodocker.Docker"):
        manager = DockerManager(mock_app_config)
        manager.docker = AsyncMock()
        yield manager


@patch("docker_manager.BedrockServer.async_lookup")
async def test_wait_for_server_query_ready_timeout(
    mock_async_lookup, docker_manager, mock_server_config
):
    """
    Tests that the readiness probe correctly times out if the target server
    never responds.
    """
    mock_async_lookup.side_effect = asyncio.TimeoutError
    await docker_manager.wait_for_server_query_ready(mock_server_config)
    mock_async_lookup.assert_awaited()


async def test_is_container_running_exists_and_running(docker_manager):
    """Tests status check for a running container."""
    mock_container = AsyncMock()
    mock_container.show.return_value = {"State": {"Running": True}}

    @asynccontextmanager
    async def get_container_mock(*args, **kwargs):
        yield mock_container

    docker_manager.get_container = get_container_mock
    assert await docker_manager.is_container_running("test-container") is True


async def test_is_container_running_exists_and_stopped(docker_manager):
    """Tests status check for a stopped (exited) container."""
    mock_container = AsyncMock()
    mock_container.show.return_value = {"State": {"Running": False}}

    @asynccontextmanager
    async def get_container_mock(*args, **kwargs):
        yield mock_container

    docker_manager.get_container = get_container_mock
    assert await docker_manager.is_container_running("test-container") is False
