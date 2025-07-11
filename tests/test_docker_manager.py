# tests/test_docker_manager.py
from unittest.mock import MagicMock, patch

import docker
import pytest
from docker.errors import APIError, NotFound

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager


# New fixture: Patch docker.from_env globally for all tests in this module
@pytest.fixture(autouse=True)  # autouse=True means this fixture runs for all tests
def mock_docker_from_env():
    """Patches docker.from_env to return a mock DockerClient instance."""
    with patch("docker.from_env", autospec=True) as mock_from_env:
        # Create a mock DockerClient that will be returned by docker.from_env()
        mock_client = MagicMock(spec=docker.DockerClient)
        mock_client.ping.return_value = True  # Default ping to pass
        mock_from_env.return_value = mock_client
        yield mock_from_env  # Yield the patch object itself


@pytest.fixture
def docker_manager_instance():
    """Fixture for DockerManager instance with mocked Docker client."""
    manager = DockerManager()
    manager.connect()  # This will now use the patched docker.from_env
    yield manager


@pytest.fixture
def mock_docker_client(mock_docker_from_env):
    """Fixture for the mocked Docker client instance."""
    return mock_docker_from_env.return_value


@pytest.fixture
def mock_container():
    """Fixture for a mocked Docker container object."""
    return MagicMock(spec=docker.models.containers.Container)


@pytest.fixture
def mock_server_config():
    """Fixture for a mocked ServerConfig."""
    return MagicMock(spec=ServerConfig)


@pytest.fixture
def mock_settings():
    """Fixture for a mocked ProxySettings."""
    settings = MagicMock(spec=ProxySettings)
    settings.docker_network_gateway_ip = "172.0.0.1"
    settings.server_ready_max_wait_time_seconds = 5
    settings.query_timeout_seconds = 1
    settings.player_check_interval_seconds = 10
    settings.idle_timeout_seconds = 300
    return settings


@pytest.fixture
def mock_java_server_lookup():
    """Mocks mcstatus.JavaServer.lookup for mcstatus calls."""
    with patch("mcstatus.JavaServer.lookup") as mock_lookup:
        yield mock_lookup


@pytest.fixture
def mock_bedrock_server_lookup():
    """Mocks mcstatus.BedrockServer.lookup for mcstatus calls."""
    with patch("mcstatus.BedrockServer.lookup") as mock_lookup:
        yield mock_lookup


# Patch asyncio.to_thread globally for all async tests in this file
@pytest.fixture(autouse=True)
def mock_asyncio_to_thread():
    """Mocks asyncio.to_thread to simply run the function directly for testing."""
    with patch(
        "asyncio.to_thread",
        side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
    ) as mock_to_thread:
        yield mock_to_thread


@pytest.mark.unit
def test_connect_to_docker_success(docker_manager_instance, mock_docker_client):
    """Tests successful connection to the Docker daemon."""
    # connect is called in fixture, so just assert calls
    mock_docker_client.ping.assert_called_once()
    assert docker_manager_instance.client == mock_docker_client


@pytest.mark.unit
def test_connect_to_docker_failure(docker_manager_instance, mock_docker_client, capsys):
    """Tests connection failure to the Docker daemon."""
    # Set side_effect directly on the mock_docker_client.ping from the fixture
    mock_docker_client.ping.side_effect = APIError("Docker connect error")
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        docker_manager_instance.connect()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    outerr = capsys.readouterr()
    assert "FATAL: Could not connect to Docker daemon" in outerr.err


@pytest.mark.unit
def test_is_container_running_exists_and_running(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests checking a container that exists and is running."""
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    result = docker_manager_instance.is_container_running("test-container")
    assert result is True


@pytest.mark.unit
def test_is_container_running_exists_and_stopped(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests checking a container that exists but is stopped."""
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    result = docker_manager_instance.is_container_running("test-container")
    assert result is False


@pytest.mark.unit
def test_is_container_running_not_found(docker_manager_instance, mock_docker_client):
    """Tests checking a container that does not exist."""
    mock_docker_client.containers.get.side_effect = NotFound("Not found")
    result = docker_manager_instance.is_container_running("not-found-container")
    assert result is False


@pytest.mark.unit
def test_is_container_running_api_error(
    docker_manager_instance, mock_docker_client, caplog
):
    """Tests handling a Docker API error when checking container status."""
    mock_docker_client.containers.get.side_effect = APIError("Docker daemon error")
    result = docker_manager_instance.is_container_running("any-container")
    assert result is False
    assert "API error checking container status." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio  # Mark as async test
async def test_stop_server_api_error(  # Make test function async
    docker_manager_instance, mock_docker_client, mock_container, caplog
):
    """Tests the handling of a Docker API error during server stop."""
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    mock_container.stop.side_effect = APIError("API error on stop")

    # Await the async method call
    result = await docker_manager_instance.stop_server("any-container")
    assert result is False
    assert "Docker API error during stop." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_server_query_ready_timeout(
    mock_java_server_lookup,
    mock_bedrock_server_lookup,
    docker_manager_instance,
    mock_server_config,
    mock_settings,
    caplog,
):
    """
    Tests that the readiness probe correctly times out and returns False
    if the target server never responds.
    """
    mock_bedrock_server_lookup.return_value.status.side_effect = Exception(
        "Query failed"
    )
    mock_java_server_lookup.return_value.status.side_effect = Exception("Query failed")

    mock_server_config.server_type = "bedrock"
    mock_server_config.container_name = "test-bedrock"
    mock_server_config.internal_port = 19132

    mock_settings.server_ready_max_wait_time_seconds = 0.2
    mock_settings.query_timeout_seconds = 0.1

    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config,
        mock_settings,
        polling_interval_seconds=0.01,
    )
    assert result is False
    assert "Server query readiness timed out" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_server_query_ready_java_success(
    docker_manager_instance,
    mock_server_config,
    mock_settings,
    mock_java_server_lookup,
    caplog,
):
    """Tests that the readiness probe correctly identifies a ready Java server."""
    mock_server_config.server_type = "java"
    mock_server_config.container_name = "test-java-server"
    mock_server_config.internal_port = 25565

    mock_status_result = MagicMock()
    mock_status_result.latency = 50
    mock_java_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config, mock_settings
    )
    assert result is True
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_server_query_ready_bedrock_success(
    docker_manager_instance,
    mock_server_config,
    mock_settings,
    mock_bedrock_server_lookup,
    caplog,
):
    """Tests that the readiness probe correctly identifies a ready Bedrock server."""
    mock_server_config.server_type = "bedrock"
    mock_server_config.container_name = "test-bedrock-server"
    mock_server_config.internal_port = 19132

    mock_status_result = MagicMock()
    mock_status_result.latency = 70
    mock_bedrock_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config, mock_settings
    )
    assert result is True
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_success(
    docker_manager_instance,
    mock_docker_client,
    mock_server_config,
    mock_settings,
    mock_java_server_lookup,  # Assuming Java server is used for readiness
    mock_container,
    caplog,
):
    """
    Tests successful server startup, including container creation/start,
    when container initially not found.
    """
    mock_server_config.container_name = "test-server"
    mock_server_config.server_type = "java"
    mock_server_config.docker_image = "test-image"
    mock_server_config.environment_vars = {"EULA": "TRUE"}
    mock_server_config.port_bindings = {"25565/tcp": 25565}
    mock_server_config.docker_network = "test-network"
    mock_server_config.internal_port = 25565

    # Mock containers.get to raise NotFound initially
    mock_docker_client.containers.get.side_effect = NotFound("Container not found")

    # Mock containers.run to return the mock_container and its start method
    mock_docker_client.containers.run.return_value = mock_container
    mock_container.status = "running"  # Set status as if it started immediately
    mock_container.start.return_value = None  # This is for explicit start() call

    # Mock server readiness check to pass
    mock_status_result = MagicMock()
    mock_status_result.latency = 50
    mock_java_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.start_server(
        mock_server_config, mock_settings
    )

    assert result is True
    mock_docker_client.containers.get.assert_called_once_with(
        mock_server_config.container_name
    )
    mock_docker_client.containers.run.assert_called_once_with(
        mock_server_config.docker_image,
        name=mock_server_config.container_name,
        environment=mock_server_config.environment_vars,
        ports=mock_server_config.port_bindings,
        detach=True,
        auto_remove=True,
        network=mock_server_config.docker_network,
    )
    # The container.start() method is called if the container exists but is not running.
    # In this specific test, we mock NotFound from get() leading to run().
    # The `run()` method implicitly starts the container, so explicit `start()`
    # on the container object might not be called separately in this path.
    # Assert `start()` only if you mock a path where it would be explicitly called.
    mock_container.start.assert_not_called()
    # Should not be called if containers.run handles it.

    assert "Attempting to start server" in caplog.text
    assert "Container not found, creating new one." in caplog.text
    assert "Container created and started." in caplog.text
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_existing_stopped(
    docker_manager_instance,
    mock_docker_client,
    mock_server_config,
    mock_settings,
    mock_java_server_lookup,  # Or bedrock if type is bedrock
    mock_container,
    caplog,
):
    """
    Tests successful server startup when container exists but is stopped.
    """
    mock_server_config.container_name = "existing-stopped-server"
    mock_server_config.server_type = "java"
    mock_server_config.internal_port = 25565

    # Mock get to return an existing, but stopped container
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"  # Important: status is 'exited' for stopped
    mock_container.start.return_value = None  # Mock the start method of the container

    # Mock server readiness check to pass
    mock_status_result = MagicMock()
    mock_status_result.latency = 50
    mock_java_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.start_server(
        mock_server_config, mock_settings
    )

    assert result is True
    mock_docker_client.containers.get.assert_called_once_with(
        mock_server_config.container_name
    )
    mock_docker_client.containers.run.assert_not_called()  # Should not call run
    mock_container.start.assert_called_once()  # Should call start on existing container

    assert "Attempting to start server" in caplog.text
    assert "Container found but not running. Attempting to start it." in caplog.text
    assert "Container started." in caplog.text
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_server_already_running(
    docker_manager_instance,
    mock_docker_client,
    mock_server_config,
    mock_settings,
    mock_java_server_lookup,  # Or bedrock if type is bedrock
    mock_container,
    caplog,
):
    """
    Tests server startup when container is already running.
    Should just check readiness.
    """
    mock_server_config.container_name = "already-running-server"
    mock_server_config.server_type = "java"
    mock_server_config.internal_port = 25565

    # Mock get to return an existing, running container
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"

    # Mock server readiness check to pass (even if already running, still probes)
    mock_status_result = MagicMock()
    mock_status_result.latency = 50
    mock_java_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.start_server(
        mock_server_config, mock_settings
    )

    assert result is True
    mock_docker_client.containers.get.assert_called_once_with(
        mock_server_config.container_name
    )
    mock_docker_client.containers.run.assert_not_called()
    mock_container.start.assert_not_called()  # Should not call start
    assert "Server already running." in caplog.text
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_success_already_stopped(
    docker_manager_instance, mock_docker_client, mock_container, caplog
):
    """Tests stopping a server that is already stopped."""
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    result = await docker_manager_instance.stop_server("test-container")
    assert result is True
    mock_container.stop.assert_not_called()
    assert "Server not running, no need to stop." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_success_running(
    docker_manager_instance, mock_docker_client, mock_container, caplog
):
    """Tests successful stopping of a running server."""
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    mock_container.stop.return_value = None
    result = await docker_manager_instance.stop_server("test-container")
    assert result is True
    mock_container.stop.assert_called_once()
    assert "Container stopped." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_server_not_found(
    docker_manager_instance, mock_docker_client, caplog
):
    """Tests stopping a server that does not exist."""
    mock_docker_client.containers.get.side_effect = NotFound("Not found")
    result = await docker_manager_instance.stop_server("not-found-container")
    assert result is True
    assert "Container not found, assuming already stopped." in caplog.text
