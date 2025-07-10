import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import ProxySettings, ServerConfig
from proxy import NetherBridgeProxy


@pytest.fixture
def default_proxy_settings():
    """Provides a default ProxySettings object for testing."""
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
        max_concurrent_sessions=-1,
        prometheus_enabled=False,
        prometheus_port=8000,
    )


@pytest.fixture
def mock_servers_config():
    """Provides a list of mock ServerConfig objects for testing."""
    return [
        ServerConfig(
            name="TestServer",
            server_type="java",
            listen_port=25565,
            container_name="test-mc-java",
            internal_port=25565,
        )
    ]


@pytest.fixture
def proxy_instance(default_proxy_settings, mock_servers_config):
    """
    Provides a NetherBridgeProxy instance with a mocked DockerManager.
    """
    with patch("proxy.DockerManager") as MockDockerManager:
        proxy = NetherBridgeProxy(default_proxy_settings, mock_servers_config)
        proxy.docker_manager = MockDockerManager.return_value
        yield proxy


@pytest.mark.unit
def test_proxy_initialization(proxy_instance):
    """Tests that the proxy initializes its state correctly."""
    assert "test-mc-java" in proxy_instance.server_states
    assert proxy_instance.server_states["test-mc-java"]["status"] == "stopped"


@pytest.mark.unit
@patch("proxy.Thread")
def test_handle_new_tcp_connection_starts_stopped_server(mock_thread, proxy_instance):
    """
    Tests that a new TCP connection to a stopped server correctly queues the
    connection and starts the server startup task in a background thread.
    """
    mock_socket, mock_conn, mock_addr = MagicMock(), MagicMock(), ("127.0.0.1", 12345)
    mock_socket.accept.return_value = (mock_conn, mock_addr)
    mock_socket.getsockname.return_value = ("0.0.0.0", 25565)
    proxy_instance.server_states["test-mc-java"]["status"] = "stopped"

    proxy_instance._handle_new_connection(mock_socket)

    assert proxy_instance.server_states["test-mc-java"]["status"] == "starting"
    # FIX: Use the correct key for the pending sockets dictionary
    assert (
        mock_conn in proxy_instance.server_states["test-mc-java"]["pending_tcp_sockets"]
    )
    mock_thread.assert_called_once()


@pytest.mark.unit
def test_handle_new_tcp_connection_queues_for_starting_server(proxy_instance):
    """
    Tests that a new TCP connection to an already-starting server is simply
    queued without starting another background thread.
    """
    mock_socket, mock_conn, mock_addr = MagicMock(), MagicMock(), ("127.0.0.1", 12345)
    mock_socket.accept.return_value = (mock_conn, mock_addr)
    mock_socket.getsockname.return_value = ("0.0.0.0", 25565)
    proxy_instance.server_states["test-mc-java"]["status"] = "starting"

    with (
        patch.object(proxy_instance, "_establish_tcp_session") as mock_establish,
        patch("proxy.Thread") as mock_thread,
    ):
        proxy_instance._handle_new_connection(mock_socket)

        # FIX: Use the correct key for the pending sockets dictionary
        assert (
            mock_conn
            in proxy_instance.server_states["test-mc-java"]["pending_tcp_sockets"]
        )
        mock_establish.assert_not_called()
        mock_thread.assert_not_called()


@pytest.mark.unit
def test_start_server_task_processes_pending_connections(
    proxy_instance, mock_servers_config
):
    """
    Tests that the background startup task correctly processes all queued
    connections after the server has successfully started.
    """
    server_config = mock_servers_config[0]
    container_name = server_config.container_name

    mock_conn, mock_addr = MagicMock(), ("127.0.0.1", 12345)
    mock_conn.getpeername.return_value = mock_addr
    # FIX: Use the correct key when setting up the mock pending connection
    proxy_instance.server_states[container_name]["pending_tcp_sockets"] = {
        mock_conn: b"buffered_data"
    }
    proxy_instance.docker_manager.start_server.return_value = True

    with patch.object(
        proxy_instance, "_establish_tcp_session"
    ) as mock_establish_session:
        proxy_instance._start_minecraft_server_task(server_config)

        assert proxy_instance.server_states[container_name]["status"] == "running"
        mock_establish_session.assert_called_once_with(
            mock_conn, mock_addr, server_config, b"buffered_data"
        )
        assert not proxy_instance.server_states[container_name]["pending_tcp_sockets"]
