# tests/test_docker_manager.py
from unittest.mock import AsyncMock, patch

import pytest
from aiodocker.exceptions import DockerError

from docker_manager import DockerManager

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


@pytest.fixture
def mock_aiodocker():
    """Mocks the aiodocker.Docker client constructor and its methods."""
    with patch("aiodocker.Docker", autospec=True) as mock_constructor:
        mock_client = AsyncMock()
        mock_constructor.return_value = mock_client

        # Mock container object that will be returned by client.containers.get
        mock_container = AsyncMock()
        mock_client.containers.get.return_value = mock_container

        yield mock_client, mock_container


@pytest.fixture
def docker_manager(mock_aiodocker):
    """Provides a DockerManager instance with a mocked aiodocker client."""
    return DockerManager(docker_url="unix:///var/run/docker.sock")


# --- Test ensure_server_running ---


@pytest.mark.asyncio
async def test_ensure_server_running_starts_stopped_container(
    docker_manager, mock_aiodocker
):
    """
    Tests that `ensure_server_running` correctly starts a stopped container
    and returns its IP address.
    """
    _, mock_container = mock_aiodocker

    # Configure container.show() to simulate state changes
    mock_container.show.side_effect = [
        # First call: Container is stopped
        {"State": {"Running": False}, "NetworkSettings": {"Networks": {}}},
        # Second call (after start): Container is running with an IP
        {
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.3"}}},
        },
    ]

    ip = await docker_manager.ensure_server_running("test_container")

    assert ip == "172.17.0.3"
    mock_container.start.assert_awaited_once()
    assert mock_container.show.await_count == 2


@pytest.mark.asyncio
async def test_ensure_server_running_already_running(docker_manager, mock_aiodocker):
    """
    Tests that `ensure_server_running` does not try to start a container
    that is already running.
    """
    _, mock_container = mock_aiodocker

    mock_container.show.return_value = {
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.4"}}},
    }

    ip = await docker_manager.ensure_server_running("running_container")

    assert ip == "172.17.0.4"
    mock_container.start.assert_not_awaited()
    mock_container.show.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_server_not_found(docker_manager, mock_aiodocker):
    """Tests that the method returns None when the container is not found."""
    mock_client, _ = mock_aiodocker
    mock_client.containers.get.side_effect = DockerError(
        status=404, data={"message": "Container not found"}
    )

    ip = await docker_manager.ensure_server_running("nonexistent_container")
    assert ip is None


# --- Test is_container_running ---


@pytest.mark.asyncio
async def test_is_container_running_true(docker_manager, mock_aiodocker):
    """Tests identification of a running container."""
    _, mock_container = mock_aiodocker
    mock_container.show.return_value = {"State": {"Running": True}}

    is_running = await docker_manager.is_container_running("running_one")
    assert is_running is True


@pytest.mark.asyncio
async def test_is_container_running_false(docker_manager, mock_aiodocker):
    """Tests identification of a stopped container."""
    _, mock_container = mock_aiodocker
    mock_container.show.return_value = {"State": {"Running": False}}

    is_running = await docker_manager.is_container_running("stopped_one")
    assert is_running is False


@pytest.mark.asyncio
async def test_is_container_running_not_found(docker_manager, mock_aiodocker):
    """Tests that a non-existent container is reported as not running."""
    mock_client, _ = mock_aiodocker
    mock_client.containers.get.side_effect = DockerError(
        status=404, data={"message": "Not found"}
    )

    is_running = await docker_manager.is_container_running("missing_one")
    assert is_running is False


# --- Test stop_server ---


@pytest.mark.asyncio
async def test_stop_server_when_running(docker_manager, mock_aiodocker):
    """Tests stopping a container that is currently running."""
    _, mock_container = mock_aiodocker
    mock_container.show.return_value = {"State": {"Running": True}}

    await docker_manager.stop_server("running_server")

    mock_container.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_server_when_already_stopped(docker_manager, mock_aiodocker):
    """Tests that stop is not called for an already stopped container."""
    _, mock_container = mock_aiodocker
    mock_container.show.return_value = {"State": {"Running": False}}

    await docker_manager.stop_server("stopped_server")

    mock_container.stop.assert_not_awaited()


# --- Test close ---


@pytest.mark.asyncio
async def test_close_calls_client_close(docker_manager, mock_aiodocker):
    """Tests that the `close` method correctly calls the client's close."""
    mock_client, _ = mock_aiodocker
    mock_client.close = AsyncMock()

    await docker_manager.close()

    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_docker_manager_initialization_failure():
    """Tests that initialization failure raises an exception."""
    with patch("aiodocker.Docker", side_effect=Exception("Connection failed")):
        with pytest.raises(Exception, match="Connection failed"):
            DockerManager(docker_url="invalid-url")
