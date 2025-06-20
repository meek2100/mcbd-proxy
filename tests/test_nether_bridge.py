import os
import signal
import sys
import time
from unittest.mock import MagicMock, patch

import docker
import pytest

# Adjusting sys.path to allow importing nether_bridge.py from the parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Now we can also import the new main function
from nether_bridge import (  # noqa: E402
    DEFAULT_SETTINGS,
    NetherBridgeProxy,
    ProxySettings,
    ServerConfig,
    load_application_config,
    main,
    perform_health_check,
)

# --- Fixtures for common test setup ---


@pytest.fixture
def mock_docker_client():
    """Mocks the docker.client.DockerClient object."""
    mock_client = MagicMock()
    mock_container_obj = MagicMock(status="running")
    mock_client.containers.get.return_value = mock_container_obj
    yield mock_client


@pytest.fixture
def default_proxy_settings():
    """Provides default ProxySettings for testing."""
    return ProxySettings(
        idle_timeout_seconds=0.2,
        player_check_interval_seconds=0.1,
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
    proxy.docker_client = mock_docker_client
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


def test_is_container_running_api_error(nether_bridge_instance, mock_docker_client):
    mock_docker_client.containers.get.side_effect = docker.errors.APIError(
        "Docker daemon error"
    )
    assert nether_bridge_instance._is_container_running("any-container") is False


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_success(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    with patch.object(
        nether_bridge_instance, "_is_container_running", return_value=False
    ):
        mock_docker_client.containers.get.return_value = mock_container
        result = nether_bridge_instance._start_minecraft_server("test-mc-bedrock")
        assert result is True
        mock_container.start.assert_called_once()
        mock_wait_ready.assert_called_once()


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_already_running(
    mock_wait_ready, nether_bridge_instance, mock_container
):
    with patch.object(
        nether_bridge_instance, "_is_container_running", return_value=True
    ):
        result = nether_bridge_instance._start_minecraft_server("test-mc-bedrock")
        assert result is True
        mock_container.start.assert_not_called()
        mock_wait_ready.assert_not_called()


@patch(
    "nether_bridge.NetherBridgeProxy._wait_for_server_query_ready",
    return_value=True,
)
def test_start_minecraft_server_docker_api_error(
    mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container
):
    with patch.object(
        nether_bridge_instance, "_is_container_running", return_value=False
    ):
        mock_docker_client.containers.get.return_value = mock_container
        mock_container.start.side_effect = docker.errors.APIError("API error")
        result = nether_bridge_instance._start_minecraft_server("test-mc-bedrock")
        assert result is False
        mock_wait_ready.assert_not_called()


@patch("nether_bridge.BedrockServer.lookup")
@patch("nether_bridge.JavaServer.lookup")
def test_wait_for_server_query_ready_bedrock_success(
    mock_java_lookup, mock_bedrock_lookup, nether_bridge_instance, mock_servers_config
):
    config = next(s for s in mock_servers_config if s.server_type == "bedrock")
    mock_bedrock_lookup.return_value.status.return_value = MagicMock(latency=50)
    result = nether_bridge_instance._wait_for_server_query_ready(config, 0.1, 0.1)
    assert result is True


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.BedrockServer.lookup", side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_timeout(
    mock_lookup, mock_sleep, nether_bridge_instance, mock_servers_config
):
    config = mock_servers_config[0]
    result = nether_bridge_instance._wait_for_server_query_ready(config, 0.2, 0.1)
    assert result is False


@patch("nether_bridge.time.sleep")
@patch("nether_bridge.NetherBridgeProxy._stop_minecraft_server")
def test_monitor_servers_activity_stops_idle_server(
    mock_stop, mock_sleep, nether_bridge_instance, mock_servers_config
):
    config = mock_servers_config[0]
    nether_bridge_instance.server_states[config.container_name]["running"] = True
    nether_bridge_instance.server_states[config.container_name]["last_activity"] = (
        time.time() - 1000
    )
    mock_sleep.side_effect = [None, Exception("Stop loop")]
    try:
        nether_bridge_instance._monitor_servers_activity()
    except Exception as e:
        assert str(e) == "Stop loop"
    mock_stop.assert_called_once_with(config.container_name)


# --- Coverage Tests for main() and healthcheck() ---


def test_load_config_invalid_env_var_falls_back():
    """Test that a non-integer env var falls back to the default setting."""
    with patch.dict(os.environ, {"NB_IDLE_TIMEOUT": "not-a-number"}):
        settings, _ = load_application_config()
        assert settings.idle_timeout_seconds == DEFAULT_SETTINGS["idle_timeout_seconds"]


@patch("nether_bridge.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat, mock_servers_config):
    """Test health check exits if the heartbeat file is not found."""
    with patch(
        "nether_bridge.load_application_config",
        return_value=(MagicMock(), mock_servers_config),
    ):
        mock_heartbeat.is_file.return_value = False
        with pytest.raises(SystemExit) as e:
            perform_health_check()
        assert e.value.code == 1


@patch("nether_bridge.HEARTBEAT_FILE")
@patch("time.time")
def test_health_check_fails_if_heartbeat_is_stale(
    mock_time, mock_heartbeat, mock_servers_config
):
    """Test health check exits if the heartbeat is too old."""
    with patch(
        "nether_bridge.load_application_config",
        return_value=(
            MagicMock(healthcheck_stale_threshold_seconds=60),
            mock_servers_config,
        ),
    ):
        mock_heartbeat.is_file.return_value = True
        mock_time.return_value = 1000
        mock_heartbeat.read_text.return_value = "900"  # 100 seconds old
        with pytest.raises(SystemExit) as e:
            perform_health_check()
        assert e.value.code == 1


@patch("nether_bridge.sys.argv", ["nether_bridge.py", "--healthcheck"])
@patch("nether_bridge.perform_health_check")
def test_main_runs_health_check(mock_perform_health):
    """Test that main() calls the health check when --healthcheck is passed."""
    with pytest.raises(SystemExit):
        main()
    mock_perform_health.assert_called_once()


@patch("nether_bridge.sys.argv", ["nether_bridge.py"])
@patch("nether_bridge.load_application_config")
@patch("nether_bridge.NetherBridgeProxy")
@patch("signal.signal")
def test_main_execution_flow(mock_signal, mock_proxy_class, mock_load_config):
    """Test the main application startup flow."""
    mock_settings = MagicMock(log_level="INFO")
    mock_servers = [MagicMock()]
    mock_load_config.return_value = (mock_settings, mock_servers)
    mock_proxy_instance = MagicMock()
    mock_proxy_class.return_value = mock_proxy_instance

    main()

    mock_load_config.assert_called_once()
    mock_proxy_class.assert_called_once_with(mock_settings, mock_servers)
    mock_signal.assert_any_call(signal.SIGINT, mock_proxy_instance.signal_handler)
    mock_signal.assert_any_call(signal.SIGTERM, mock_proxy_instance.signal_handler)
    mock_proxy_instance.run.assert_called_once()
