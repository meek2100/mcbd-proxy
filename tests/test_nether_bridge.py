import json
import os
import select
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
    _load_servers_from_env,
    _load_servers_from_json,
    _load_settings_from_json,
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
        log_formatter="console",
        healthcheck_stale_threshold_seconds=0.5,
        proxy_heartbeat_interval_seconds=0.1,
        tcp_listen_backlog=128,
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


def test_is_container_running_unexpected_error(
    nether_bridge_instance, mock_docker_client
):
    """Test handling of a generic exception."""
    mock_docker_client.containers.get.side_effect = Exception("Unexpected error")
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


def test_load_config_invalid_env_var_falls_back():
    with patch.dict(os.environ, {"NB_IDLE_TIMEOUT": "not-a-number"}):
        settings, _ = load_application_config()
        assert settings.idle_timeout_seconds == DEFAULT_SETTINGS["idle_timeout_seconds"]


@patch("nether_bridge.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat, mock_servers_config):
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
    with patch(
        "nether_bridge.load_application_config",
        return_value=(
            MagicMock(healthcheck_stale_threshold_seconds=60),
            mock_servers_config,
        ),
    ):
        mock_heartbeat.is_file.return_value = True
        mock_time.return_value = 1000
        mock_heartbeat.read_text.return_value = "900"
        with pytest.raises(SystemExit) as e:
            perform_health_check()
        assert e.value.code == 1


@patch("nether_bridge.sys.argv", ["nether_bridge.py", "--healthcheck"])
@patch("nether_bridge.perform_health_check")
def test_main_runs_health_check(mock_perform_health):
    with pytest.raises(SystemExit):
        main()
    mock_perform_health.assert_called_once()


@patch("nether_bridge.sys.argv", ["nether_bridge.py"])
@patch("nether_bridge.load_application_config")
@patch("nether_bridge.NetherBridgeProxy")
@patch("signal.signal")
def test_main_execution_flow(mock_signal, mock_proxy_class, mock_load_config):
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


# --- New tests for increased coverage ---


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGHUP is not available on Windows"
)
def test_signal_handler_sighup(nether_bridge_instance):
    """Test the SIGHUP path of the signal handler."""
    nether_bridge_instance.signal_handler(signal.SIGHUP, None)
    assert nether_bridge_instance._reload_requested is True
    assert nether_bridge_instance._shutdown_requested is False


def test_connect_to_docker_failure(nether_bridge_instance):
    """Test that sys.exit is called if Docker connection fails."""
    with patch("docker.from_env", side_effect=Exception("Docker connect error")):
        with pytest.raises(SystemExit) as e:
            nether_bridge_instance._connect_to_docker()
        assert e.value.code == 1


def test_stop_minecraft_server_unexpected_error(
    nether_bridge_instance, mock_docker_client
):
    """Test handling of a generic exception during server stop."""
    mock_docker_client.containers.get.side_effect = Exception("Unexpected stop error")
    result = nether_bridge_instance._stop_minecraft_server("any-container")
    assert result is False


@patch("json.load", side_effect=json.JSONDecodeError("JSON error", "", 0))
@patch("pathlib.Path.is_file", return_value=True)
@patch("builtins.open")
def test_load_settings_json_decode_error(mock_open, mock_is_file, mock_json_load):
    """Test that a JSON decode error in settings file is handled."""
    result = _load_settings_from_json(MagicMock())
    assert result == {}


@patch("json.load", side_effect=json.JSONDecodeError("JSON error", "", 0))
@patch("pathlib.Path.is_file", return_value=True)
@patch("builtins.open")
def test_load_servers_json_decode_error(mock_open, mock_is_file, mock_json_load):
    """Test that a JSON decode error in servers file is handled."""
    result = _load_servers_from_json(MagicMock())
    assert result == []


def test_load_servers_from_env_incomplete_definition():
    """Test skipping an incomplete server definition from environment variables."""
    with patch.dict(
        os.environ, {"NB_1_LISTEN_PORT": "12345"}
    ):  # Missing other required vars
        result = _load_servers_from_env()
        assert result == []


def test_load_servers_from_env_invalid_server_type():
    """Test skipping a server definition with an invalid server_type."""
    with patch.dict(
        os.environ,
        {
            "NB_1_LISTEN_PORT": "12345",
            "NB_1_CONTAINER_NAME": "test-cont",
            "NB_1_INTERNAL_PORT": "12345",
            "NB_1_SERVER_TYPE": "invalid-type",
        },
    ):
        result = _load_servers_from_env()
        assert result == []


@patch("select.select")
@patch("time.sleep")
def test_run_proxy_loop_select_error(mock_sleep, mock_select, nether_bridge_instance):
    """Test that a select.error is caught and the loop continues."""

    def select_side_effect(*args, **kwargs):
        nether_bridge_instance._shutdown_requested = True
        raise select.error

    mock_select.side_effect = select_side_effect

    nether_bridge_instance._run_proxy_loop()
    mock_sleep.assert_called_once_with(1)


@patch("nether_bridge.NetherBridgeProxy.run")
def test_main_prometheus_startup_error(mock_proxy_run):
    """Test that failure to start Prometheus server is handled in main()."""
    with patch(
        "prometheus_client.start_http_server", side_effect=Exception("Port in use")
    ):
        with patch("nether_bridge.sys.argv", ["nether_bridge.py"]):
            with patch(
                "nether_bridge.load_application_config",
                return_value=(MagicMock(log_level="INFO"), [MagicMock()]),
            ):
                with patch("signal.signal"):
                    # We call main, which will try to start prometheus, fail, log, and
                    # continue.
                    main()
    # The main assertion is that the code doesn't crash and still proceeds to create
    # and run the proxy.
    mock_proxy_run.assert_called_once()


@patch(
    "nether_bridge.NetherBridgeProxy._run_proxy_loop"
)  # Prevent the loop from running
@patch("pathlib.Path.exists", return_value=True)
@patch("pathlib.Path.unlink")
def test_main_cannot_remove_stale_heartbeat(mock_unlink, mock_exists, mock_proxy_run):
    """Test that an OSError when removing stale heartbeat is handled."""
    mock_unlink.side_effect = OSError("Permission denied")
    with patch("nether_bridge.sys.argv", ["nether_bridge.py"]):
        with patch(
            "nether_bridge.load_application_config",
            return_value=(MagicMock(log_level="INFO"), [MagicMock()]),
        ):
            # We only need to patch the methods inside run() that would block
            # or have side effects
            with patch("nether_bridge.NetherBridgeProxy._connect_to_docker"):
                with patch(
                    "nether_bridge.NetherBridgeProxy._ensure_all_servers_stopped_on_startup"
                ):
                    with patch(
                        "nether_bridge.NetherBridgeProxy._create_listening_socket"
                    ):
                        with patch("threading.Thread"):
                            with patch("signal.signal"):
                                with patch("prometheus_client.start_http_server"):
                                    main()
    mock_exists.assert_called_once()
    mock_unlink.assert_called_once()
    # Verify the app continued past the error to the run stage
    mock_proxy_run.assert_called_once()
