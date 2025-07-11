import os
import select
import signal
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Adjust sys.path to ensure modules can be found when tests are run from the root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the new application modules
from config import ProxySettings, ServerConfig
from proxy import NetherBridgeProxy


@pytest.fixture
def default_proxy_settings():
    """Provides a default ProxySettings object for testing."""
    # This dictionary is formatted to be under 88 characters per line.
    settings_dict = {
        "idle_timeout_seconds": 0.2,
        "player_check_interval_seconds": 0.1,
        "query_timeout_seconds": 0.1,
        "server_ready_max_wait_time_seconds": 0.5,
        "initial_boot_ready_max_wait_time_seconds": 0.5,
        "server_startup_delay_seconds": 0,
        "initial_server_query_delay_seconds": 0,
        "log_level": "DEBUG",
        "log_formatter": "console",
        "healthcheck_stale_threshold_seconds": 0.5,
        "proxy_heartbeat_interval_seconds": 0.1,
        "tcp_listen_backlog": 128,
        "max_concurrent_sessions": -1,
        "prometheus_enabled": False,
        "prometheus_port": 8000,
    }
    return ProxySettings(**settings_dict)


@pytest.fixture
def mock_servers_config():
    """Provides a list of mock ServerConfig objects for testing."""
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
def proxy_instance(default_proxy_settings, mock_servers_config):
    """
    Provides a NetherBridgeProxy instance with a mocked DockerManager.
    This is the primary fixture for testing the proxy's internal logic.
    """
    with patch("proxy.DockerManager") as MockDockerManager:
        mock_docker_manager_instance = MockDockerManager.return_value
        proxy = NetherBridgeProxy(default_proxy_settings, mock_servers_config)
        proxy.docker_manager = mock_docker_manager_instance
        yield proxy


# --- Test Cases for NetherBridgeProxy Logic ---


@pytest.mark.unit
def test_proxy_initialization(
    proxy_instance, default_proxy_settings, mock_servers_config
):
    """Tests that the proxy initializes its state correctly."""
    assert proxy_instance.settings == default_proxy_settings
    assert proxy_instance.servers_list == mock_servers_config
    assert proxy_instance.docker_manager is not None
    assert "test-mc-bedrock" in proxy_instance.server_states
    assert not proxy_instance.server_states["test-mc-bedrock"]["running"]


@pytest.mark.unit
@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGHUP is not available on Windows"
)
def test_signal_handler_sighup(proxy_instance):
    """Tests that a SIGHUP signal correctly flags the proxy for a reload."""
    assert not proxy_instance._reload_requested
    proxy_instance.signal_handler(signal.SIGHUP, None)
    assert proxy_instance._reload_requested is True
    assert not proxy_instance._shutdown_requested


@pytest.mark.unit
def test_signal_handler_sigint(proxy_instance):
    """Tests that a SIGINT signal correctly flags the proxy for shutdown."""
    assert not proxy_instance._shutdown_requested
    proxy_instance.signal_handler(signal.SIGINT, None)
    assert proxy_instance._shutdown_requested is True
    assert not proxy_instance._reload_requested


@pytest.mark.unit
def test_start_minecraft_server_wrapper_success(proxy_instance, mock_servers_config):
    """
    Tests the proxy's start method. It should delegate to the DockerManager
    and update its internal state on success.
    """
    server_config = mock_servers_config[0]
    container_name = server_config.container_name

    proxy_instance.docker_manager.is_container_running.return_value = False
    proxy_instance.docker_manager.start_server.return_value = True

    proxy_instance._start_minecraft_server(server_config)

    proxy_instance.docker_manager.is_container_running.assert_called_once_with(
        container_name
    )
    proxy_instance.docker_manager.start_server.assert_called_once_with(
        server_config, proxy_instance.settings
    )
    assert proxy_instance.server_states[container_name]["running"] is True


@pytest.mark.unit
def test_start_minecraft_server_wrapper_already_running(
    proxy_instance, mock_servers_config
):
    """Tests that if the server is already running, the start logic is skipped."""
    server_config = mock_servers_config[0]
    proxy_instance.docker_manager.is_container_running.return_value = True

    proxy_instance._start_minecraft_server(server_config)

    proxy_instance.docker_manager.start_server.assert_not_called()
    assert proxy_instance.server_states[server_config.container_name]["running"] is True


@pytest.mark.unit
def test_stop_minecraft_server_wrapper(proxy_instance):
    """

    Tests that the proxy's stop method correctly calls the docker_manager
    and updates its state.
    """
    container_name = "test-mc-bedrock"
    proxy_instance.server_states[container_name]["running"] = True
    proxy_instance.docker_manager.stop_server.return_value = True

    proxy_instance._stop_minecraft_server(container_name)

    proxy_instance.docker_manager.stop_server.assert_called_once_with(container_name)
    assert not proxy_instance.server_states[container_name]["running"]


@pytest.mark.unit
# Correct the patch target to look for the Event class inside the proxy module
@patch("proxy.Event.wait")
def test_monitor_servers_activity_stops_idle_server(
    mock_event_wait, proxy_instance, mock_servers_config
):
    """
    Tests that the monitor thread correctly identifies an idle server
    and calls the stop method.
    """
    # This side effect will allow the loop to run once, then raise an
    # error on the second call to Event.wait() to exit the test.
    mock_event_wait.side_effect = [None, InterruptedError("Stop loop")]

    idle_server_config = mock_servers_config[0]
    container_name = idle_server_config.container_name

    # Set up the state for an idle server that is currently running
    proxy_instance.server_states[container_name]["running"] = True
    proxy_instance.server_states[container_name]["last_activity"] = time.time() - 1000

    # Mock the stop method on the instance to verify it gets called
    with patch.object(proxy_instance, "_stop_minecraft_server") as mock_stop_method:
        try:
            proxy_instance._monitor_servers_activity()
        except InterruptedError:
            pass  # Expected way to exit the while loop for testing purposes

    # Assert that the logic correctly identified and tried to stop the idle server.
    mock_stop_method.assert_called_once_with(container_name)


@pytest.mark.unit
@patch("proxy.select.select")
@patch("proxy.time.sleep")
def test_run_proxy_loop_handles_select_error(mock_sleep, mock_select, proxy_instance):
    """
    Tests that the main proxy loop gracefully handles a select.error,
    logs it, and continues, preventing a crash.
    """

    # We define a side effect function for the select mock.
    def select_side_effect(*args, **kwargs):
        # The FIRST time this is called, we set the flag to stop the loop
        # on the NEXT iteration. Then we raise the error to be tested.
        proxy_instance._shutdown_requested = True
        raise select.error

    mock_select.side_effect = select_side_effect

    # The _run_proxy_loop method requires the main module for reloads, so we mock it.
    mock_main_module = MagicMock()

    # The loop will run once, hit the error, call sleep, continue,
    # and then exit because the shutdown flag is now set.
    proxy_instance._run_proxy_loop(mock_main_module)

    # Assert that the loop caught the error and slept for 1 second.
    mock_sleep.assert_called_once_with(1)
