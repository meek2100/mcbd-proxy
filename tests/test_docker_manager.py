from unittest.mock import AsyncMock, patch

import pytest
from aiodocker.exceptions import DockerError

from docker_manager import DockerManager


@pytest.fixture
def mock_aiodocker_client():
    """Mocks the aiodocker.Docker client and its methods."""
    mock_container = AsyncMock()
    mock_container.show.side_effect = [
        # First call: Container is stopped
        {
            "State": {"Running": False},
            "NetworkSettings": {"Networks": {}},
        },
        # Second call (after start): Container is running
        {
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.3"}}},
        },
    ]
    mock_container.start = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.get.return_value = mock_container
    return mock_client


@pytest.mark.asyncio
@patch("aiodocker.Docker")
async def test_ensure_server_running_starts_stopped_container(
    mock_docker_constructor, mock_aiodocker_client
):
    """
    Tests that `ensure_server_running` correctly starts a container that
    is initially stopped and returns its IP address.
    """
    mock_docker_constructor.return_value = mock_aiodocker_client
    manager = DockerManager(docker_url="unix_socket")

    ip = await manager.ensure_server_running("test_container")

    assert ip == "172.17.0.3"
    mock_aiodocker_client.containers.get.assert_awaited_once_with("test_container")
    assert mock_aiodocker_client.containers.get.return_value.start.awaited
    assert mock_aiodocker_client.containers.get.return_value.show.call_count == 2


@pytest.mark.asyncio
@patch("aiodocker.Docker")
async def test_ensure_server_running_for_already_running_container(
    mock_docker_constructor,
):
    """
    Tests that `ensure_server_running` does not try to start a container
    that is already running.
    """
    mock_container = AsyncMock()
    mock_container.show.return_value = {
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.4"}}},
    }
    mock_container.start = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.get.return_value = mock_container
    mock_docker_constructor.return_value = mock_client

    manager = DockerManager(docker_url="unix_socket")
    ip = await manager.ensure_server_running("running_container")

    assert ip == "172.17.0.4"
    mock_client.containers.get.assert_awaited_once_with("running_container")
    mock_container.start.assert_not_awaited()


@pytest.mark.asyncio
@patch("aiodocker.Docker")
async def test_ensure_server_not_found(mock_docker_constructor):
    """
    Tests that the method returns None when the container is not found.
    """
    mock_client = AsyncMock()
    mock_client.containers.get.side_effect = DockerError(
        status=404, data={"message": "Container not found"}
    )
    mock_docker_constructor.return_value = mock_client

    manager = DockerManager(docker_url="unix_socket")
    ip = await manager.ensure_server_running("nonexistent_container")

    assert ip is None


@pytest.mark.asyncio
@patch("aiodocker.Docker")
async def test_close_calls_client_close(mock_docker_constructor):
    """Tests that the `close` method correctly calls the client's close."""
    mock_client = AsyncMock()
    mock_client.close = AsyncMock()
    mock_docker_constructor.return_value = mock_client

    manager = DockerManager(docker_url="unix_socket")
    await manager.close()

    mock_client.close.assert_awaited_once()
