# tests/test_docker_manager.py
from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest

# Don't import APIError and NotFound directly, as they are used via the docker module
from config import ProxySettings, ServerConfig
from docker_manager import DockerManager


@pytest.fixture
def mock_docker_client():
    """Provides a mocked Docker client instance."""
    with patch("docker.from_env") as mock_from_env:
        client = MagicMock(spec=docker.DockerClient)
        client.containers = MagicMock()
        mock_from_env.return_value = client
        yield client


@pytest.fixture
async def manager(mock_docker_client):
    """Provides an initialized DockerManager instance for tests."""
    instance = DockerManager()
    instance.connect = AsyncMock()
    await instance.connect()
    instance.client = mock_docker_client
    return instance


@pytest.fixture
def mock_container():
    """Provides a generic mocked Docker container."""
    container = MagicMock(spec=docker.models.containers.Container)
    container.start = MagicMock()
    container.stop = MagicMock()
    return container


@pytest.fixture
def server_config():
    """Provides a sample ServerConfig."""
    return ServerConfig(
        name="Java Test",
        server_type="java",
        listen_port=25565,
        container_name="mc-java",
        internal_port=25565,
    )


@pytest.fixture
def settings():
    """Provides a sample ProxySettings that matches the dataclass."""
    return ProxySettings(
        idle_timeout_seconds=300,
        player_check_interval_seconds=60,
        server_ready_max_wait_time_seconds=120,
    )


@pytest.mark.asyncio
async def test_start_server_container_not_found(manager, server_config, settings):
    """Ensures start_server returns False if the container is not found."""
    manager.client.containers.get.side_effect = docker.errors.NotFound("not found")
    manager._wait_for_server_ready = AsyncMock()

    result = await manager.start_server(server_config, settings)

    assert result is False
    manager._wait_for_server_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_server_api_error(manager, mock_container, caplog):
    """Ensures stop_server handles Docker API errors gracefully."""
    mock_container.status = "running"
    manager.client.containers.get.return_value = mock_container
    # Reference APIError through the imported docker module
    mock_container.stop.side_effect = docker.errors.APIError("failed to stop")

    result = await manager.stop_server("mc-java")

    assert result is False
    assert "Docker API error" in caplog.text
