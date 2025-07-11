# tests/test_docker_manager.py
from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest
from docker.errors import APIError, NotFound

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager


@pytest.fixture
def mock_docker_client():
    """Provides a mocked Docker client instance."""
    with patch("docker.from_env") as mock_from_env:
        client = MagicMock(spec=docker.DockerClient)
        client.containers = MagicMock()
        client.ping = MagicMock()
        mock_from_env.return_value = client
        yield client


@pytest.fixture
def manager(mock_docker_client):
    """Provides a DockerManager instance with a mocked client."""
    return DockerManager()


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
    """Provides a sample ProxySettings."""
    return ProxySettings(
        idle_timeout_seconds=300,
        player_check_interval_seconds=60,
        server_ready_max_wait_time_seconds=120,
    )


@pytest.mark.asyncio
async def test_start_server_already_running(
    manager, mock_container, server_config, settings
):
    """Ensures start_server does not re-start a running container."""
    mock_container.status = "running"
    manager.client.containers.get.return_value = mock_container
    manager._wait_for_server_ready = AsyncMock(return_value=True)

    result = await manager.start_server(server_config, settings)

    assert result is True
    manager.client.containers.get.assert_called_once_with("mc-java")
    mock_container.start.assert_not_called()
    manager._wait_for_server_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_server_from_stopped_state(
    manager, mock_container, server_config, settings
):
    """Ensures start_server starts an existing but stopped container."""
    mock_container.status = "exited"
    manager.client.containers.get.return_value = mock_container
    manager._wait_for_server_ready = AsyncMock(return_value=True)

    result = await manager.start_server(server_config, settings)

    assert result is True
    manager.client.containers.get.assert_called_once_with("mc-java")
    mock_container.start.assert_called_once()
    manager._wait_for_server_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_server_container_not_found(manager, server_config, settings):
    """Ensures start_server returns False if the container is not found."""
    manager.client.containers.get.side_effect = NotFound("not found")
    manager._wait_for_server_ready = AsyncMock()

    result = await manager.start_server(server_config, settings)

    assert result is False
    manager._wait_for_server_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_running_server(manager, mock_container):
    """Ensures stop_server correctly stops a running container."""
    mock_container.status = "running"
    manager.client.containers.get.return_value = mock_container

    result = await manager.stop_server("mc-java")

    assert result is True
    mock_container.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stop_server_api_error(manager, mock_container, caplog):
    """Ensures stop_server handles Docker API errors gracefully."""
    mock_container.status = "running"
    manager.client.containers.get.return_value = mock_container
    manager.client.containers.get.return_value.stop.side_effect = APIError(
        "failed to stop"
    )

    result = await manager.stop_server("mc-java")

    assert result is False
    assert "Docker API error" in caplog.text
