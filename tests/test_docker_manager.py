# tests/test_docker_manager.py

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager


@pytest.fixture
def mock_aiodocker_client():
    """Provides a mocked aiodocker.Docker client instance."""
    client = AsyncMock(spec=aiodocker.Docker)
    client.containers = AsyncMock()  # Mock the containers attribute
    return client


@pytest.fixture
def docker_manager_instance(mock_aiodocker_client):
    """
    Provides a DockerManager instance with a mocked aiodocker client.
    Initializes DockerManager with a dummy URL, as it expects one.
    """
    manager = DockerManager(docker_url="unix://var/run/docker.sock")
    manager.client = mock_aiodocker_client
    return manager


@pytest.fixture
def mock_container_object():
    """Provides a reusable AsyncMock for an aiodocker container object."""
    return AsyncMock(spec=aiodocker.containers.DockerContainer)


@pytest.fixture
def mock_server_config():
    """Provides a mock ServerConfig object for testing."""
    config = MagicMock(spec=ServerConfig)
    config.name = "test-mc-server"
    config.container_name = "test-mc-container"
    config.internal_port = 25565
    config.server_type = "java"  # Default to Java for readiness checks
    config.idle_timeout_seconds = None
    return config


@pytest.fixture
def mock_proxy_settings():
    """Provides a mock ProxySettings object for testing."""
    settings = MagicMock(spec=ProxySettings)
    settings.server_ready_max_wait_time_seconds = 1  # Short timeout for tests
    settings.query_timeout_seconds = 0.5  # Short timeout for queries
    settings.docker_url = "unix://var/run/docker.sock"
    return settings


# --- Tests for is_container_running ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_container_running_exists_and_running(
    docker_manager_instance, mock_aiodocker_client, mock_container_object
):
    """Tests that is_container_running returns True for a running container."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.show.return_value = {"State": {"Running": True}}

    result = await docker_manager_instance.is_container_running("test-container")
    assert result is True
    mock_aiodocker_client.containers.get.assert_awaited_once_with("test-container")
    mock_container_object.show.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_container_running_exists_and_not_running(
    docker_manager_instance, mock_aiodocker_client, mock_container_object
):
    """Tests that is_container_running returns False for a stopped container."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.show.return_value = {"State": {"Running": False}}

    result = await docker_manager_instance.is_container_running("test-container")
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_container_running_not_found(
    docker_manager_instance, mock_aiodocker_client
):
    """Tests that is_container_running returns False for a non-existent container."""
    mock_aiodocker_client.containers.get.side_effect = aiodocker.exceptions.DockerError(
        status=404, data={"message": "No such container"}
    )

    result = await docker_manager_instance.is_container_running("not-found-container")
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_container_running_api_error(
    docker_manager_instance, mock_aiodocker_client
):
    """
    Tests that is_container_running raises for other Docker API errors.
    """
    mock_aiodocker_client.containers.get.side_effect = aiodocker.exceptions.DockerError(
        status=500, data={"message": "Server error"}
    )
    with pytest.raises(aiodocker.exceptions.DockerError):
        await docker_manager_instance.is_container_running("some-container")


# --- Tests for wait_for_server_query_ready ---


@pytest.mark.unit
@pytest.mark.asyncio
@patch("docker_manager.JavaServer.async_lookup")
async def test_wait_for_server_query_ready_success_java(
    mock_async_lookup,
    docker_manager_instance,
    mock_server_config,
    mock_proxy_settings,
):
    """Tests that the readiness probe succeeds for a Java server."""
    mock_server = AsyncMock()
    mock_server.async_status.return_value = MagicMock()  # Mock the status object
    mock_async_lookup.return_value = mock_server

    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config,
        mock_proxy_settings.server_ready_max_wait_time_seconds,
        mock_proxy_settings.query_timeout_seconds,
    )
    assert result is True
    mock_async_lookup.assert_awaited_once()
    mock_server.async_status.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@patch("docker_manager.BedrockServer.async_lookup")
async def test_wait_for_server_query_ready_success_bedrock(
    mock_async_lookup,
    docker_manager_instance,
    mock_server_config,
    mock_proxy_settings,
):
    """Tests that the readiness probe succeeds for a Bedrock server."""
    mock_server_config.server_type = "bedrock"  # Change to bedrock
    mock_server = AsyncMock()
    mock_server.async_status.return_value = MagicMock()
    mock_async_lookup.return_value = mock_server

    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config,
        mock_proxy_settings.server_ready_max_wait_time_seconds,
        mock_proxy_settings.query_timeout_seconds,
    )
    assert result is True
    mock_async_lookup.assert_awaited_once()
    mock_server.async_status.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@patch("docker_manager.JavaServer.async_lookup", side_effect=Exception("Timeout"))
async def test_wait_for_server_query_ready_timeout(
    mock_async_lookup,
    docker_manager_instance,
    mock_server_config,
    mock_proxy_settings,
):
    """
    Tests that the readiness probe times out if the server never responds.
    """
    # Ensure time.time() progresses enough to trigger a timeout
    with patch("time.time", side_effect=[0, 0.6, 1.1, 1.6, 2.1]):
        result = await docker_manager_instance.wait_for_server_query_ready(
            mock_server_config,
            mock_proxy_settings.server_ready_max_wait_time_seconds,  # 1 second
            mock_proxy_settings.query_timeout_seconds,  # 0.5 second
        )
        assert result is False
        assert mock_async_lookup.await_count > 1  # Should retry at least once


@pytest.mark.unit
@pytest.mark.asyncio
@patch("asyncio.sleep", new=AsyncMock())  # Mock asyncio.sleep
async def test_wait_for_server_query_ready_unsupported_type(
    docker_manager_instance, mock_server_config, mock_proxy_settings
):
    """
    Tests that an unsupported server type is handled gracefully.
    """
    mock_server_config.server_type = "unsupported"
    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config,
        mock_proxy_settings.server_ready_max_wait_time_seconds,
        mock_proxy_settings.query_timeout_seconds,
    )
    assert result is False
    # Ensure it sleeps for the fallback duration
    asyncio.sleep.assert_awaited_with(5)


# --- Tests for start_server ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_success(
    docker_manager_instance,
    mock_aiodocker_client,
    mock_container_object,
    mock_server_config,
    mock_proxy_settings,
):
    """Tests successful server start and readiness check."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.start.return_value = None  # Simulate successful start

    # Mock the wait_for_server_query_ready method as it's tested separately
    with patch.object(
        docker_manager_instance,
        "wait_for_server_query_ready",
        new=AsyncMock(return_value=True),
    ) as mock_wait_ready:
        result = await docker_manager_instance.start_server(
            mock_server_config, mock_proxy_settings
        )
        assert result is True
        mock_aiodocker_client.containers.get.assert_awaited_once_with(
            mock_server_config.container_name
        )
        mock_container_object.start.assert_awaited_once()
        mock_wait_ready.assert_awaited_once_with(
            mock_server_config,
            mock_proxy_settings.server_ready_max_wait_time_seconds,
            mock_proxy_settings.query_timeout_seconds,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_container_not_found(
    docker_manager_instance,
    mock_aiodocker_client,
    mock_server_config,
    mock_proxy_settings,
):
    """Tests start_server when container is not found."""
    mock_aiodocker_client.containers.get.side_effect = aiodocker.exceptions.DockerError(
        status=404, data={"message": "No such container"}
    )
    result = await docker_manager_instance.start_server(
        mock_server_config, mock_proxy_settings
    )
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_docker_api_error_on_start(
    docker_manager_instance,
    mock_aiodocker_client,
    mock_container_object,
    mock_server_config,
    mock_proxy_settings,
):
    """Tests Docker API error during container start."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.start.side_effect = aiodocker.exceptions.DockerError(
        status=500, data={"message": "Server error"}
    )
    result = await docker_manager_instance.start_server(
        mock_server_config, mock_proxy_settings
    )
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_readiness_check_fails(
    docker_manager_instance,
    mock_aiodocker_client,
    mock_container_object,
    mock_server_config,
    mock_proxy_settings,
):
    """
    Tests that start_server returns False if readiness check fails.
    """
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.start.return_value = None

    with patch.object(
        docker_manager_instance,
        "wait_for_server_query_ready",
        new=AsyncMock(return_value=False),
    ) as mock_wait_ready:
        result = await docker_manager_instance.start_server(
            mock_server_config, mock_proxy_settings
        )
        assert result is False
        mock_wait_ready.assert_awaited_once()


# --- Tests for stop_server ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_success(
    docker_manager_instance, mock_aiodocker_client, mock_container_object
):
    """Tests successful server stop."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.stop.return_value = None  # Simulate successful stop
    result = await docker_manager_instance.stop_server("test-container")
    assert result is True
    mock_aiodocker_client.containers.get.assert_awaited_once_with("test-container")
    mock_container_object.stop.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_container_not_found(
    docker_manager_instance, mock_aiodocker_client
):
    """Tests stop_server when container is not found."""
    mock_aiodocker_client.containers.get.side_effect = aiodocker.exceptions.DockerError(
        status=404, data={"message": "No such container"}
    )
    result = await docker_manager_instance.stop_server("not-found-container")
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_api_error(
    docker_manager_instance, mock_aiodocker_client, mock_container_object
):
    """Tests Docker API error during container stop."""
    mock_aiodocker_client.containers.get.return_value = mock_container_object
    mock_container_object.stop.side_effect = aiodocker.exceptions.DockerError(
        status=500, data={"message": "Server error"}
    )
    result = await docker_manager_instance.stop_server("some-container")
    assert result is False
