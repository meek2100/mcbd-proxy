from unittest.mock import AsyncMock, MagicMock

import aiodocker.exceptions
import pytest

from docker_manager import DockerManager


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_container_not_found():
    """
    Tests that start_server handles the case where a container doesn't exist.
    """
    manager = DockerManager(docker_url="unix://var/run/docker.sock")
    manager.client = AsyncMock()
    manager.client.containers.get.side_effect = aiodocker.exceptions.DockerError(
        status=404, data={"message": "No such container"}
    )

    server_config = MagicMock()
    settings = MagicMock()

    result = await manager.start_server(server_config, settings)
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_api_error():
    """
    Tests that stop_server properly handles a Docker API error.
    """
    manager = DockerManager(docker_url="unix://var/run/docker.sock")
    manager.client = AsyncMock()
    container_mock = AsyncMock()
    container_mock.stop.side_effect = aiodocker.exceptions.DockerError(
        status=500, data={"message": "Server error"}
    )
    manager.client.containers.get.return_value = container_mock

    result = await manager.stop_server("some_container")
    assert result is False
