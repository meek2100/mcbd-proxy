import os
import sys
import time
from unittest.mock import MagicMock, patch

import docker
import pytest

# Adjusting sys.path to allow importing nether_bridge.py from the parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from nether_bridge import (  # noqa: E402
    NetherBridgeProxy,
    ProxySettings,
    ServerConfig,
)

# --- Fixtures for common test setup ---


@pytest.fixture
def mock_docker_client():
    """Mocks the docker.client.DockerClient object."""
    mock_client = MagicMock()
    # Default behavior for get() is to return a mock container that is 'running'
    mock_container_obj = MagicMock(status="running")
    mock_client.containers.get.return_value = mock_container_obj
    yield mock_client


@pytest.fixture
def default_proxy_settings():
    """Provides default ProxySettings for testing."""
    return ProxySettings(
        idle_timeout_seconds=0.2,  # Reduced for faster tests
        player_check_interval_seconds=0.1,  # Reduced for faster tests
        query_timeout_seconds=0.1,
        server_ready_max_wait_time_seconds=0.5,
        initial_boot_ready_max_wait_time_seconds=0.5,
        server_startup_delay_seconds=0,
        initial_server_query_delay_seconds=0,
        log_level="DEBUG",
        healthcheck_stale_threshold_seconds=0.5,
        proxy_heartbeat_interval_seconds=0.1,
    )


@pytest.fixture
def mock_servers_config():
    """Provides a list of mock ServerConfig objects."""
    return [
        ServerConfig(
            name="Bedrock Test",
            server_type="bedrock",
            listen_port=19132,
            container_name="test-mc-bedrock",
            internal_port=19132,
        ),
        ServerConfig(
            name="Java Test",
            server_type="java",
            listen_port=25565,
            container_name="test-mc-java",
            internal_port=25565,
        ),
    ]


@pytest.fixture
def nether_bridge_instance(
    default_proxy_settings, mock_servers_config, mock_docker_client
):
    """Provides a NetherBridgeProxy instance with mocked Docker client."""
    proxy = NetherBridgeProxy(default_proxy_settings, mock_servers_config)
    proxy.docker_client = mock_docker_client  # Inject the mocked client
    return proxy


@pytest.fixture
def mock_container():
    """Provides a reusable MagicMock for a Docker container."""
    return MagicMock()


# --- Test Cases ---


def test_proxy_initialization(
    default_proxy_settings, mock_servers_config, nether_bridge_instance
):
    """Test if the proxy initializes correctly."""
    assert nether_bridge_instance.settings == default_proxy_settings
    assert nether_bridge_instance.servers_list == mock_servers_config
    assert len(nether_bridge_instance.servers_config_map) == len(mock_servers_config)
    assert "test-mc-bedrock" in nether_bridge_instance.server_states
    assert nether_bridge_instance.server_states["test-mc-bedrock"]["running"] is False


# Test cases for _is_container_running
def test_is_container_running_exists_and_running(
    nether_bridge_instance, mock_docker_client, mock_container
):
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container
    assert nether_bridge_instance._is_container_running("test-mc-bedrock") is True


def test_is_container_running_exists_and_stopped(
    nether_bridge_instance, mock_docker_client, mock_container
):
    mock_container.status = "exited"
    mock_docker_client.containers.get.return_value = mock_container
    assert nether_bridge_instance._is_container_running("test-mc-bedrock") is False


def test_is_container_running_not_found(nether_bridge_instance, mock_docker_client):
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
        "No such container"
    )
    assert (
        nether_bridge_instance._is_container_running("non-existent-container") is False
    )


# Test cases for _start_minecraft_server
@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_success(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    nether_bridge_instance.server_states[container_name]["running"] = False

    result = nether_bridge_instance._start_minecraft_server(container_name)

    assert result is True
    mock_container.start.assert_called_once()
    mock_wait_ready.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is True


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_already_running(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    nether_bridge_instance.server_states[container_name]["running"] = True

    result = nether_bridge_instance._start_minecraft_server(container_name)

    assert result is True
    mock_container.start.assert_not_called()
    mock_wait_ready.assert_not_called()
    assert nether_bridge_instance.server_states[container_name]["running"] is True


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_docker_api_error(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    mock_container.start.side_effect = docker.errors.APIError(
        "Docker API error", response=None, explanation="Test error"
    )
    nether_bridge_instance.server_states[container_name]["running"] = False

    result = nether_bridge_instance._start_minecraft_server(container_name)

    assert result is False
    mock_container.start.assert_called_once()
    mock_wait_ready.assert_not_called()
    assert nether_bridge_instance.server_states[container_name]["running"] is False


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=False,
)
def test_start_minecraft_server_readiness_timeout(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    nether_bridge_instance.server_states[container_name]["running"] = False

    result = nether_bridge_instance._start_minecraft_server(container_name)

    assert result is True
    mock_container.start.assert_called_once()
    mock_wait_ready.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is True


# Test cases for _stop_minecraft_server
def test_stop_minecraft_server_success(
    nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    nether_bridge_instance.server_states[container_name]["running"] = True

    result = nether_bridge_instance._stop_minecraft_server(container_name)

    assert result is True
    mock_container.stop.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is False


def test_stop_minecraft_server_already_stopped(
    nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "exited"
    nether_bridge_instance.server_states[container_name]["running"] = False

    result = nether_bridge_instance._stop_minecraft_server(container_name)

    assert result is True
    mock_container.stop.assert_not_called()
    assert nether_bridge_instance.server_states[container_name]["running"] is False


def test_stop_minecraft_server_docker_api_error(
    nether_bridge_instance, mock_docker_client, mock_container
):
    container_name = "test-mc-bedrock"
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.status = "running"
    mock_container.stop.side_effect = docker.errors.APIError(
        "Docker API error", response=None, explanation="Test error"
    )
    nether_bridge_instance.server_states[container_name]["running"] = True

    result = nether_bridge_instance._stop_minecraft_server(container_name)

    assert result is False
    mock_container.stop.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is True


def test_stop_minecraft_server_not_found_on_stop(
    nether_bridge_instance, mock_docker_client
):
    container_name = "non-existent-container"
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
        "No such container"
    )
    nether_bridge_instance.server_states[container_name] = {
        "running": True,
        "last_activity": time.time(),
    }

    result = nether_bridge_instance._stop_minecraft_server(container_name)

    assert result is True
    assert nether_bridge_instance.server_states[container_name]["running"] is False


# Test cases for _wait_for_server_query_ready
@patch("nether_bridge.BedrockServer.lookup")
@patch("nether_bridge.JavaServer.lookup")
def test_wait_for_server_query_ready_bedrock_success(
    mock_java_lookup,
    mock_bedrock_lookup,
    nether_bridge_instance,
    mock_servers_config,
):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == "bedrock")
    mock_server_instance = MagicMock()
    mock_server_instance.status.return_value = MagicMock(
        latency=50, players=MagicMock(online=0)
    )
    mock_bedrock_lookup.return_value = mock_server_instance

    result = nether_bridge_instance._wait_for_server_query_ready(
        bedrock_config,
        nether_bridge_instance.settings.server_ready_max_wait_time_seconds,
        nether_bridge_instance.settings.query_timeout_seconds,
    )

    assert result is True
    mock_bedrock_lookup.assert_called_once_with(
        f"{bedrock_config.container_name}:{bedrock_config.internal_port}",
        timeout=nether_bridge_instance.settings.query_timeout_seconds,
    )
    mock_java_lookup.assert_not_called()
    mock_server_instance.status.assert_called_once()


@patch("nether_bridge.BedrockServer.lookup")
@patch("nether_bridge.JavaServer.lookup")
def test_wait_for_server_query_ready_java_success(
    mock_java_lookup,
    mock_bedrock_lookup,
    nether_bridge_instance,
    mock_servers_config,
):
    java_config = next(s for s in mock_servers_config if s.server_type == "java")
    mock_server_instance = MagicMock()
    mock_server_instance.status.return_value = MagicMock(
        latency=50, players=MagicMock(online=0)
    )
    mock_java_lookup.return_value = mock_server_instance

    result = nether_bridge_instance._wait_for_server_query_ready(
        java_config,
        nether_bridge_instance.settings.server_ready_max_wait_time_seconds,
        nether_bridge_instance.settings.query_timeout_seconds,
    )

    assert result is True
    mock_java_lookup.assert_called_once_with(
        f"{java_config.container_name}:{java_config.internal_port}",
        timeout=nether_bridge_instance.settings.query_timeout_seconds,
    )
    mock_bedrock_lookup.assert_not_called()
    mock_server_instance.status.assert_called_once()


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.BedrockServer.lookup", side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_bedrock_timeout(
    mock_bedrock_lookup,
    mock_sleep,
    nether_bridge_instance,
    mock_servers_config,
):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == "bedrock")
    nether_bridge_instance.settings.server_ready_max_wait_time_seconds = 0.2
    nether_bridge_instance.settings.query_timeout_seconds = 0.1

    result = nether_bridge_instance._wait_for_server_query_ready(
        bedrock_config,
        nether_bridge_instance.settings.server_ready_max_wait_time_seconds,
        nether_bridge_instance.settings.query_timeout_seconds,
    )

    assert result is False
    assert mock_bedrock_lookup.call_count >= 2
    mock_sleep.assert_called()


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.JavaServer.lookup", side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_java_timeout(
    mock_java_lookup, mock_sleep, nether_bridge_instance, mock_servers_config
):
    java_config = next(s for s in mock_servers_config if s.server_type == "java")
    nether_bridge_instance.settings.server_ready_max_wait_time_seconds = 0.2
    nether_bridge_instance.settings.query_timeout_seconds = 0.1

    result = nether_bridge_instance._wait_for_server_query_ready(
        java_config,
        nether_bridge_instance.settings.server_ready_max_wait_time_seconds,
        nether_bridge_instance.settings.query_timeout_seconds,
    )

    assert result is False
    assert mock_java_lookup.call_count >= 2
    mock_sleep.assert_called()


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.NetherBridgeProxy._stop_minecraft_server")
def test_monitor_servers_activity_stops_idle_server(
    mock_stop_minecraft_server,
    mock_sleep,
    nether_bridge_instance,
    mock_servers_config,
):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == "bedrock")
    java_config = next(s for s in mock_servers_config if s.server_type == "java")

    nether_bridge_instance.server_states[bedrock_config.container_name]["running"] = (
        True
    )
    nether_bridge_instance.server_states[bedrock_config.container_name][
        "last_activity"
    ] = time.time() - nether_bridge_instance.settings.idle_timeout_seconds - 1

    nether_bridge_instance.server_states[java_config.container_name]["running"] = True
    nether_bridge_instance.server_states[java_config.container_name][
        "last_activity"
    ] = time.time()

    mock_sleep.side_effect = [None, Exception("Stop loop")]

    try:
        nether_bridge_instance._monitor_servers_activity()
    except Exception as e:
        assert str(e) == "Stop loop"

    mock_stop_minecraft_server.assert_called_once_with(bedrock_config.container_name)


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.NetherBridgeProxy._stop_minecraft_server")
def test_monitor_servers_activity_resets_active_server_timer(
    mock_stop_minecraft_server,
    mock_sleep,
    nether_bridge_instance,
    mock_servers_config,
):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == "bedrock")

    nether_bridge_instance.server_states[bedrock_config.container_name]["running"] = (
        True
    )
    original_last_activity = time.time() - (
        nether_bridge_instance.settings.idle_timeout_seconds / 2
    )
    nether_bridge_instance.server_states[bedrock_config.container_name][
        "last_activity"
    ] = original_last_activity

    dummy_session_key = (("127.0.0.1", 12345), 19132, "udp")
    nether_bridge_instance.active_sessions[dummy_session_key] = {
        "client_socket": MagicMock(),
        "server_socket": MagicMock(),
        "target_container": bedrock_config.container_name,
        "last_packet_time": time.time(),
        "listen_port": bedrock_config.listen_port,
        "protocol": "udp",
    }

    mock_sleep.side_effect = [None, Exception("Stop loop")]

    try:
        nether_bridge_instance._monitor_servers_activity()
    except Exception as e:
        assert str(e) == "Stop loop"

    mock_stop_minecraft_server.assert_not_called()
    assert (
        nether_bridge_instance.server_states[bedrock_config.container_name][
            "last_activity"
        ]
        == original_last_activity
    )


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.NetherBridgeProxy._stop_minecraft_server")
@patch("nether_bridge.BedrockServer.lookup", side_effect=Exception("Query failed"))
def test_monitor_servers_activity_handles_query_failure(
    mock_bedrock_lookup,
    mock_stop_minecraft_server,
    mock_sleep,
    nether_bridge_instance,
    mock_servers_config,
):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == "bedrock")

    nether_bridge_instance.server_states[bedrock_config.container_name]["running"] = (
        True
    )
    nether_bridge_instance.server_states[bedrock_config.container_name][
        "last_activity"
    ] = time.time() - nether_bridge_instance.settings.idle_timeout_seconds - 1

    mock_sleep.side_effect = [None, Exception("Stop loop")]

    try:
        nether_bridge_instance._monitor_servers_activity()
    except Exception as e:
        assert str(e) == "Stop loop"

    mock_stop_minecraft_server.assert_called_once_with(bedrock_config.container_name)
    mock_bedrock_lookup.assert_not_called()
