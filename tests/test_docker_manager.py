# tests/test_docker_manager.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiodocker.exceptions import DockerError

from docker_manager import DockerManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_app_config():
    """Fixture for a mock AppConfig."""
    config = MagicMock()
    config.server_startup_timeout = 10
    config.player_check_interval = 1
    return config


@pytest.fixture
def mock_game_server_config():
    """Fixture for a mock GameServerConfig."""
    config = MagicMock()
    config.container_name = "test_container"
    config.host = "localhost"
    config.port = 25565
    config.query_port = 25565
    config.game_type = "java"
    return config


@pytest.fixture
def mock_aiodocker():
    """Fixture to mock the aiodocker.Docker client."""
    with patch("aiodocker.Docker") as mock_docker_class:
        mock_docker_instance = AsyncMock()
        mock_docker_class.return_value = mock_docker_instance
        yield mock_docker_instance


@pytest.mark.asyncio
async def test_is_container_running_true(mock_app_config, mock_aiodocker):
    """Test is_container_running returns True when container is running."""
    manager = DockerManager(mock_app_config)
    mock_container = AsyncMock()
    mock_container.show.return_value = {"State": {"Running": True}}
    mock_aiodocker.containers.get.return_value = mock_container

    running = await manager.is_container_running("test_container")

    assert running is True
    mock_aiodocker.containers.get.assert_called_once_with("test_container")


@pytest.mark.asyncio
async def test_is_container_running_false(mock_app_config, mock_aiodocker):
    """Test is_container_running returns False when container is not running."""
    manager = DockerManager(mock_app_config)
    mock_container = AsyncMock()
    mock_container.show.return_value = {"State": {"Running": False}}
    mock_aiodocker.containers.get.return_value = mock_container

    running = await manager.is_container_running("test_container")

    assert running is False


@pytest.mark.asyncio
async def test_is_container_running_not_found(mock_app_config, mock_aiodocker):
    """Test is_container_running returns False on DockerError (404)."""
    manager = DockerManager(mock_app_config)
    mock_aiodocker.containers.get.side_effect = DockerError(
        status=404, data={"message": "Container not found"}
    )
    running = await manager.is_container_running("test_container")
    assert running is False


@pytest.mark.asyncio
@patch("docker_manager.JavaServer.async_lookup")
async def test_start_server_success(
    mock_async_lookup, mock_app_config, mock_game_server_config, mock_aiodocker
):
    """Test start_server successfully starts a container."""
    manager = DockerManager(mock_app_config)
    mock_container = AsyncMock()
    mock_aiodocker.containers.get.return_value = mock_container
    mock_async_lookup.return_value.async_status.return_value = MagicMock()

    await manager.start_server(mock_game_server_config)

    mock_container.start.assert_awaited_once()


@pytest.mark.asyncio
@patch(
    "docker_manager.DockerManager.wait_for_server_query_ready", new_callable=AsyncMock
)
async def test_start_server_already_started(
    mock_wait_ready, mock_app_config, mock_game_server_config, mock_aiodocker
):
    """Test start_server handles an already started container gracefully."""
    manager = DockerManager(mock_app_config)
    mock_container = AsyncMock()
    mock_container.start.side_effect = DockerError(
        status=500, data={"message": "container already started"}
    )
    mock_aiodocker.containers.get.return_value = mock_container

    await manager.start_server(mock_game_server_config)
    mock_container.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_server_success(mock_app_config, mock_aiodocker):
    """Test stop_server successfully stops a container."""
    manager = DockerManager(mock_app_config)
    mock_container = AsyncMock()
    mock_aiodocker.containers.get.return_value = mock_container
    container_name = "test_container"
    timeout = 10

    await manager.stop_server(container_name, timeout)

    mock_container.stop.assert_awaited_once_with(t=timeout)


@pytest.mark.asyncio
async def test_close_session(mock_app_config, mock_aiodocker):
    """Test that the close method closes the aiodocker session."""
    manager = DockerManager(mock_app_config)
    await manager.close()
    mock_aiodocker.close.assert_awaited_once()


@pytest.mark.asyncio
@patch("docker_manager.JavaServer.async_lookup")
async def test_wait_for_server_query_ready_java(
    mock_async_lookup, mock_app_config, mock_game_server_config, mock_aiodocker
):
    """Test waits for Java server to be queryable."""
    manager = DockerManager(mock_app_config)
    mock_game_server_config.game_type = "java"
    mock_async_lookup.return_value.async_status = AsyncMock()

    await manager.wait_for_server_query_ready(mock_game_server_config)
    mock_async_lookup.assert_awaited_once()


@pytest.mark.asyncio
@patch("docker_manager.BedrockServer.lookup")
async def test_wait_for_server_query_ready_bedrock(
    mock_bedrock_lookup,
    mock_app_config,
    mock_game_server_config,
    mock_aiodocker,
):
    """Test waits for Bedrock server to be queryable."""
    manager = DockerManager(mock_app_config)
    mock_game_server_config.game_type = "bedrock"
    mock_bedrock_lookup.return_value.async_status = AsyncMock()

    await manager.wait_for_server_query_ready(mock_game_server_config)
    # The lookup is synchronous and run in a thread, so we check the mock was called
    mock_bedrock_lookup.assert_called_once()


@pytest.mark.asyncio
@patch("docker_manager.asyncio.sleep", new_callable=AsyncMock)
@patch("docker_manager.JavaServer.async_lookup")
async def test_wait_for_server_query_ready_retry(
    mock_async_lookup,
    mock_sleep,
    mock_app_config,
    mock_game_server_config,
    mock_aiodocker,
):
    """Test that wait_for_server_query_ready retries on failure."""
    manager = DockerManager(mock_app_config)
    mock_async_lookup.side_effect = [
        asyncio.TimeoutError,
        MagicMock(async_status=AsyncMock()),
    ]

    await manager.wait_for_server_query_ready(mock_game_server_config)

    assert mock_async_lookup.call_count == 2
    # Assert that the sleep call uses the new fixed value
    mock_sleep.assert_awaited_once_with(5)
