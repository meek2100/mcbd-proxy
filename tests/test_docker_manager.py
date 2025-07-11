# tests/test_docker_manager.py
from unittest.mock import MagicMock, patch

import docker
import pytest
from docker.errors import APIError, NotFound

from config import ProxySettings, ServerConfig

# Removed 'BedrockServer' and 'JavaServer' imports as they are not
# directly used; patching happens via string paths.
# import mcstatus # Not needed if only patching sub-attributes.
from docker_manager import DockerManager


@pytest.fixture
def docker_manager_instance():
    """Fixture for DockerManager instance."""
    manager = DockerManager()
    # Mock the client connection as we don't want to hit real Docker in unit tests
    with patch.object(manager, "client"):
        manager.connect()
        yield manager


@pytest.fixture
def mock_docker_client(docker_manager_instance):
    """Fixture for a mocked Docker client."""
    return docker_manager_instance.client


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
    # Ensure these attributes return values when accessed, as required by tests
    settings.docker_network_gateway_ip = "172.0.0.1"
    settings.server_ready_max_wait_time_seconds = 5
    settings.query_timeout_seconds = 1
    settings.player_check_interval_seconds = 10
    settings.idle_timeout_seconds = 300
    # Add other settings attributes if needed by tests from config.py
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


@pytest.mark.unit
def test_connect_to_docker_success(docker_manager_instance, mock_docker_client):
    """Tests successful connection to the Docker daemon."""
    docker_manager_instance.connect()
    mock_docker_client.ping.assert_called_once()
    assert docker_manager_instance.client == mock_docker_client


@pytest.mark.unit
def test_connect_to_docker_failure(docker_manager_instance, mock_docker_client, capsys):
    """Tests connection failure to the Docker daemon."""
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
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    result = docker_manager_instance.is_container_running("test-container")
    assert result is True


@pytest.mark.unit
def test_is_container_running_exists_and_stopped(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests checking a container that exists but is stopped."""
    mock_container.status = "exited"
    mock_docker_client.containers.get.return_value = mock_container
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
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.stop.side_effect = APIError("API error on stop")

    # Await the async method call
    result = await docker_manager_instance.stop_server("any-container")
    assert result is False
    assert "Docker API error during stop." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)  # Patch to execute sync for tests
async def test_wait_for_server_query_ready_timeout(
    mock_to_thread,  # Added to mock asyncio.to_thread
    mock_java_server_lookup,  # Renamed mock_lookup to be specific
    mock_bedrock_server_lookup,  # New mock for bedrock lookup
    docker_manager_instance,
    mock_server_config,  # Now passed via fixture
    mock_settings,  # Now passed via fixture
    caplog,
):
    """
    Tests that the readiness probe correctly times out and returns False
    if the target server never responds.
    """
    # Configure mocks for server lookup to always fail
    mock_bedrock_server_lookup.return_value.status.side_effect = Exception(
        "Query failed"
    )
    mock_java_server_lookup.return_value.status.side_effect = Exception("Query failed")

    mock_server_config.server_type = "bedrock"  # Set type for the test
    mock_server_config.container_name = "test-bedrock"
    mock_server_config.internal_port = 19132

    # Override settings for this specific test to make timeout short
    mock_settings.server_ready_max_wait_time_seconds = 0.2
    mock_settings.query_timeout_seconds = 0.1

    # `asyncio.sleep` is implicitly patched by pytest-asyncio in `auto` mode
    result = await docker_manager_instance.wait_for_server_query_ready(
        mock_server_config,
        mock_settings,
        polling_interval_seconds=0.01,  # Keep polling fast for test
    )
    assert result is False
    assert "Server query readiness timed out" in caplog.text
    # Assert that asyncio.sleep was called multiple times, implicitly by
    # the polling loop structure and mocked `to_thread`.
    # Specific sleep call assertions can be tricky with patched `to_thread`
    # if `mcstatus` calls `sleep` internally. Focus on overall outcome.


@pytest.mark.unit
@pytest.mark.asyncio
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_wait_for_server_query_ready_java_success(
    mock_to_thread,
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

    # Mock the status() method of JavaServer.lookup to return a successful status
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
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_wait_for_server_query_ready_bedrock_success(
    mock_to_thread,
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

    # Mock the status() method of BedrockServer.lookup to return a successful status
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
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_start_server_success(
    mock_to_thread,
    docker_manager_instance,
    mock_docker_client,
    mock_server_config,
    mock_settings,
    mock_java_server_lookup,
    mock_container,
    caplog,
):
    """Tests successful server startup."""
    mock_server_config.container_name = "test-server"
    mock_server_config.server_type = "java"
    # These attributes are not used in the current docker_manager.start_server
    # if the container already exists and only `start` is called.
    # If container needs to be *run* (created), then these would be used.
    # For now, let's keep them here for completeness if that logic changes.
    mock_server_config.docker_image = "test-image"
    mock_server_config.environment_vars = {"EULA": "TRUE"}
    mock_server_config.port_bindings = {"25565/tcp": 25565}
    mock_server_config.docker_network = "test-network"
    mock_server_config.internal_port = 25565

    # Mock get to raise NotFound initially, then return a stopped container
    mock_docker_client.containers.get.side_effect = [
        NotFound("Container not found"),
        mock_container,  # Return container mock for the subsequent get call
    ]
    mock_container.status = "stopped"
    mock_container.start.return_value = None

    # Mock server readiness check to pass
    mock_status_result = MagicMock()
    mock_status_result.latency = 50
    mock_java_server_lookup.return_value.status.return_value = mock_status_result

    result = await docker_manager_instance.start_server(
        mock_server_config, mock_settings
    )

    assert result is True
    mock_docker_client.containers.get.assert_called()  # Should be called twice
    mock_container.start.assert_called_once()
    assert "Attempting to start server" in caplog.text
    # "Container not found, creating new one." log removed from docker_manager.py
    # if not explicitly creating the container in start_server path,
    # as per latest docker_manager.py, it only calls get/start for existing containers.
    # Re-check docker_manager.py for log messages and update test.
    # Based on latest docker_manager.py, the "Container not found, creating new one."
    # log is still there if NotFound is caught from .get() at line 70
    assert "Container not found, creating new one." in caplog.text
    assert "Container started." in caplog.text
    assert "Server is query ready" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_stop_server_success_already_stopped(
    mock_to_thread,
    docker_manager_instance,
    mock_docker_client,
    mock_container,
    caplog,
):
    """Tests stopping a server that is already stopped."""
    mock_container.status = "exited"
    mock_docker_client.containers.get.return_value = mock_container
    result = await docker_manager_instance.stop_server("test-container")
    assert result is True
    mock_container.stop.assert_not_called()
    assert "Server not running, no need to stop." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_stop_server_success_running(
    mock_to_thread,
    docker_manager_instance,
    mock_docker_client,
    mock_container,
    caplog,
):
    """Tests successful stopping of a running server."""
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.stop.return_value = None
    result = await docker_manager_instance.stop_server("test-container")
    assert result is True
    mock_container.stop.assert_called_once()
    assert "Server stopped successfully." in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@patch(
    "asyncio.to_thread",
    side_effect=lambda func, *args, **kwargs: func(*args, **kwargs),
)
async def test_stop_server_not_found(
    mock_to_thread, docker_manager_instance, mock_docker_client, caplog
):
    """Tests stopping a server that does not exist."""
    mock_docker_client.containers.get.side_effect = NotFound("Not found")
    result = await docker_manager_instance.stop_server("not-found-container")
    # Returns True because the desired state (stopped) is met, even if not found
    assert result is True
    assert "Container not found, assuming already stopped." in caplog.text
