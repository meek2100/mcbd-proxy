import os
import sys
from unittest.mock import MagicMock, patch

import docker
import pytest

# Adjust sys.path to ensure modules can be found when tests are run from the root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the module being tested
from docker_manager import DockerManager


@pytest.fixture
def mock_docker_client():
    """Provides a mocked instance of the Docker API client."""
    return MagicMock(spec=docker.DockerClient)


@pytest.fixture
def docker_manager_instance(mock_docker_client):
    """Provides a DockerManager instance with a mocked Docker client."""
    manager = DockerManager()
    manager.client = mock_docker_client
    return manager


@pytest.fixture
def mock_container():
    """Provides a reusable MagicMock for a Docker container object."""
    return MagicMock(spec=docker.models.containers.Container)


@pytest.fixture
def mock_server_config():
    """Provides a mock server config object for testing isolated methods."""
    # This mock simulates the ServerConfig object that the method expects.
    config = MagicMock()
    config.container_name = "test-mc-bedrock"
    config.internal_port = 19132
    config.server_type = "bedrock"
    return config


# --- Test Cases for DockerManager Logic ---


@pytest.mark.unit
def test_connect_to_docker_failure():
    """Tests that sys.exit is called if the Docker connection fails."""
    manager = DockerManager()
    with patch("docker.from_env", side_effect=Exception("Docker connect error")):
        with pytest.raises(SystemExit) as e:
            manager.connect()
        assert e.value.code == 1


@pytest.mark.unit
def test_is_container_running_exists_and_running(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests status check for a running container."""
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    assert docker_manager_instance.is_container_running("test-container") is True


@pytest.mark.unit
def test_is_container_running_exists_and_stopped(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests status check for a stopped (exited) container."""
    mock_container.status = "exited"
    mock_docker_client.containers.get.return_value = mock_container
    assert docker_manager_instance.is_container_running("test-container") is False


@pytest.mark.unit
def test_is_container_running_not_found(docker_manager_instance, mock_docker_client):
    """Tests status check for a non-existent container."""
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
        "Container not found"
    )
    assert docker_manager_instance.is_container_running("not-found-container") is False


@pytest.mark.unit
def test_is_container_running_api_error(docker_manager_instance, mock_docker_client):
    """Tests the handling of a Docker API error during status check."""
    mock_docker_client.containers.get.side_effect = docker.errors.APIError(
        "Docker daemon error"
    )
    assert docker_manager_instance.is_container_running("any-container") is False


@pytest.mark.unit
def test_stop_server_api_error(
    docker_manager_instance, mock_docker_client, mock_container
):
    """Tests the handling of a Docker API error during server stop."""
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.stop.side_effect = docker.errors.APIError("API error on stop")

    result = docker_manager_instance.stop_server("any-container")
    assert result is False


@pytest.mark.unit
@patch("docker_manager.time.sleep")
@patch("docker_manager.BedrockServer.lookup", side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_timeout(
    mock_lookup, mock_sleep, docker_manager_instance, mock_server_config
):
    """
    Tests that the readiness probe correctly times out and returns False
    if the target server never responds.
    """
    result = docker_manager_instance.wait_for_server_query_ready(
        mock_server_config, max_wait_seconds=0.2, query_timeout_seconds=0.1
    )
    assert result is False
    # Verify that the logic attempted to query the server at least once.
    mock_lookup.assert_called()
