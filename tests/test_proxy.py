# tests/test_proxy.py

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Adjust sys.path to ensure modules can be found when tests are run from the root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the new application modules
from config import ProxySettings, ServerConfig
from metrics import (  # Import metrics for mocking
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
)
from proxy import NetherBridgeProxy, UdpProxyProtocol  # Import UdpProxyProtocol


@pytest.fixture
def default_proxy_settings():
    """Provides a default ProxySettings object for testing."""
    settings_dict = {
        "idle_timeout_seconds": 0.2,  # Short timeout for faster tests
        "player_check_interval_seconds": 0.1,  # Frequent checks for tests
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
        "docker_url": "unix://var/run/docker.sock",  # Add docker_url
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
            idle_timeout_seconds=None,
        ),
        ServerConfig(
            name="Java Test",
            server_type="java",
            listen_port=25565,
            container_name="test-mc-java",
            internal_port=25565,
            idle_timeout_seconds=None,
        ),
    ]


@pytest.fixture
def mock_docker_manager():
    """Provides an AsyncMock for DockerManager."""
    # DockerManager is not imported explicitly here to avoid F401 lint error
    # as it's only used as a type for the mock.
    return AsyncMock()


@pytest.fixture
def mock_metrics():
    """Provides AsyncMocks for Prometheus metrics."""
    return {
        "active_sessions": AsyncMock(spec=ACTIVE_SESSIONS),
        "running_servers": AsyncMock(spec=RUNNING_SERVERS),
        "bytes_transferred": AsyncMock(spec=BYTES_TRANSFERRED),
        "server_startup_duration": AsyncMock(spec=SERVER_STARTUP_DURATION),
    }


@pytest.fixture
def proxy_instance(
    default_proxy_settings, mock_servers_config, mock_docker_manager, mock_metrics
):
    """
    Provides a NetherBridgeProxy instance for testing.
    All async dependencies are mocked.
    """
    # Create asyncio.Event mocks
    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    # Create a dummy config_path
    mock_config_path = MagicMock(spec=Path)
    mock_config_path.return_value.mkdir = MagicMock()
    mock_config_path.__truediv__.return_value = MagicMock(spec=Path)
    # Ensure the mocked path object behaves like a Path object for operations
    mock_config_path.exists.return_value = False
    mock_config_path.is_dir.return_value = True

    proxy = NetherBridgeProxy(
        settings=default_proxy_settings,
        servers_list=mock_servers_config,
        docker_manager=mock_docker_manager,
        shutdown_event=shutdown_event,
        reload_event=reload_event,
        active_sessions_metric=mock_metrics["active_sessions"],
        running_servers_metric=mock_metrics["running_servers"],
        bytes_transferred_metric=mock_metrics["bytes_transferred"],
        server_startup_duration_metric=mock_metrics["server_startup_duration"],
        config_path=mock_config_path,
    )
    return proxy


# --- Test Cases for NetherBridgeProxy Logic ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_proxy_initialization(
    proxy_instance, default_proxy_settings, mock_servers_config
):
    """Tests that the proxy initializes its state correctly."""
    assert proxy_instance.settings == default_proxy_settings
    assert proxy_instance.servers_list == mock_servers_config
    assert proxy_instance.docker_manager is not None
    # Verify initial server states are 'stopped' and sessions are 0
    for srv_cfg in mock_servers_config:
        assert (
            proxy_instance.server_states[srv_cfg.container_name]["status"] == "stopped"
        )
        assert proxy_instance.server_states[srv_cfg.container_name]["sessions"] == 0
        assert isinstance(
            proxy_instance.server_locks[srv_cfg.container_name], asyncio.Lock
        )
    assert isinstance(proxy_instance.session_lock, asyncio.Lock)
    assert not proxy_instance.active_sessions
    assert not proxy_instance.tcp_listeners
    assert not proxy_instance.udp_listeners


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_activity_stops_idle_server(
    proxy_instance, mock_docker_manager, mock_metrics, mock_servers_config
):
    """
    Tests that the asynchronous monitor task correctly identifies an idle server
    and calls the stop method.
    """
    # Use the first server config for this test
    idle_server_config = mock_servers_config[0]
    container_name = idle_server_config.container_name

    # Set initial state for the server to be running and idle
    proxy_instance.server_states[container_name]["status"] = "running"
    proxy_instance.server_states[container_name]["last_activity"] = (
        time.time() - proxy_instance.settings.idle_timeout_seconds - 1
    )
    proxy_instance.server_states[container_name]["sessions"] = 0

    # Mock DockerManager's behavior
    mock_docker_manager.is_container_running.return_value = True
    mock_docker_manager.stop_server.return_value = True

    # Use a mock for the shutdown event to control the monitor loop
    proxy_instance.shutdown_event = asyncio.Event()

    # Run _monitor_activity as a task and then cancel it after one iteration
    # Patch asyncio.sleep to allow the loop to progress
    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        monitor_task = asyncio.create_task(proxy_instance._monitor_activity())

        # Allow the monitor to run one cycle, then trigger shutdown
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass  # Expected way to exit the while loop for testing purposes

    # Assert that DockerManager.stop_server was called
    mock_docker_manager.stop_server.assert_awaited_once_with(container_name)
    assert proxy_instance.server_states[container_name]["status"] == "stopped"
    mock_metrics["running_servers"].dec.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_activity_updates_state_for_externally_stopped_server(
    proxy_instance, mock_docker_manager, mock_metrics, mock_servers_config
):
    """
    Tests that the monitor task correctly updates the proxy's internal state
    if a server container is stopped externally (outside the proxy's control).
    """
    server_config = mock_servers_config[0]
    container_name = server_config.container_name

    # Set initial state to running, but Docker will report it as stopped
    proxy_instance.server_states[container_name]["status"] = "running"
    proxy_instance.server_states[container_name]["sessions"] = 0

    # DockerManager reports the container as NOT running
    mock_docker_manager.is_container_running.return_value = False

    proxy_instance.shutdown_event = asyncio.Event()

    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        monitor_task = asyncio.create_task(proxy_instance._monitor_activity())
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    mock_docker_manager.is_container_running.assert_awaited_once_with(container_name)
    assert proxy_instance.server_states[container_name]["status"] == "stopped"
    mock_metrics["running_servers"].dec.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_all_servers_stopped_on_startup(
    proxy_instance, mock_docker_manager, mock_servers_config
):
    """
    Tests that _ensure_all_servers_stopped_on_startup correctly
    stops all managed servers if they are found running.
    """
    # Mock one server as initially running, another as stopped
    mock_docker_manager.is_container_running.side_effect = [
        True,  # For 'test-mc-bedrock'
        False,  # For 'test-mc-java'
        False,  # For the loop inside (mc-bedrock after stop)
    ]
    mock_docker_manager.wait_for_server_query_ready.return_value = True
    mock_docker_manager.stop_server.return_value = True

    # Temporarily set the server state to running for the one we expect to stop
    proxy_instance.server_states["test-mc-bedrock"]["status"] = "running"
    proxy_instance.running_servers_metric.inc()  # Simulate increment

    with patch("asyncio.sleep", new=AsyncMock()):  # Mock sleep calls
        await proxy_instance._ensure_all_servers_stopped_on_startup()

    # Verify stop_server was called for the running server
    mock_docker_manager.stop_server.assert_awaited_once_with("test-mc-bedrock")
    # Verify is_container_running was called for both, and then again after stop
    assert mock_docker_manager.is_container_running.call_count == 3
    # Verify the running_servers metric was decremented
    proxy_instance.running_servers_metric.dec.assert_called_once()
    assert proxy_instance.server_states["test-mc-bedrock"]["status"] == "stopped"
    assert proxy_instance.server_states["test-mc-java"]["status"] == "stopped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_connection_new_session_server_stopped(
    proxy_instance, mock_docker_manager, mock_servers_config, mock_metrics
):
    """
    Tests handling of a new TCP connection when the target server is initially
    stopped and needs to be started.
    """
    java_server_config = next(s for s in mock_servers_config if s.server_type == "java")
    container_name = java_server_config.container_name
    client_addr = ("127.0.0.1", 12345)

    # Simulate server being stopped
    proxy_instance.server_states[container_name]["status"] = "stopped"

    # Mock DockerManager behavior
    mock_docker_manager.is_container_running.return_value = False
    mock_docker_manager.start_server.return_value = True  # Server starts successfully

    # Mock asyncio.open_connection for the backend server
    mock_reader = AsyncMock()
    mock_writer = AsyncMock()
    mock_writer.get_extra_info.return_value = client_addr  # Mock client peername
    # Mock the server-side reader/writer for the proxy_data tasks
    mock_backend_reader = AsyncMock()
    mock_backend_writer = AsyncMock()

    with (
        patch(
            "asyncio.open_connection",
            new=AsyncMock(return_value=(mock_backend_reader, mock_backend_writer)),
        ) as mock_open_connection,
        patch.object(proxy_instance, "_proxy_data", new=AsyncMock()) as mock_proxy_data,
        patch(
            "time.time", return_value=100.0
        ) as _mock_time,  # Use _mock_time to suppress F841
    ):
        await proxy_instance._handle_tcp_connection(
            mock_reader, mock_writer, java_server_config
        )

        mock_docker_manager.start_server.assert_awaited_once_with(
            java_server_config, proxy_instance.settings
        )
        mock_open_connection.assert_awaited_once_with(
            container_name, java_server_config.internal_port
        )

        # Verify session was created
        session_key = (client_addr, java_server_config.listen_port, "tcp")
        async with proxy_instance.session_lock:
            assert session_key in proxy_instance.active_sessions
            session_info = proxy_instance.active_sessions[session_key]
            assert session_info["client_addr"] == client_addr
            assert session_info["protocol"] == "tcp"
            assert session_info["target_container"] == container_name
            assert session_info["last_packet_time"] == 100.0

        # Verify proxy_data tasks were created
        assert mock_proxy_data.await_count == 2
        mock_metrics["active_sessions"].labels.return_value.inc.assert_called_once()
        mock_metrics["running_servers"].inc.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_connection_server_startup_fails(
    proxy_instance, mock_docker_manager, mock_servers_config, mock_metrics
):
    """
    Tests handling of a new TCP connection when the target server fails to start.
    """
    java_server_config = next(s for s in mock_servers_config if s.server_type == "java")
    container_name = java_server_config.container_name
    client_addr = ("127.0.0.1", 12345)

    proxy_instance.server_states[container_name]["status"] = "stopped"

    # Server fails to start
    mock_docker_manager.is_container_running.return_value = False
    mock_docker_manager.start_server.return_value = False

    mock_reader = AsyncMock()
    mock_writer = AsyncMock()
    mock_writer.get_extra_info.return_value = client_addr

    with patch.object(proxy_instance, "_proxy_data", new=AsyncMock()):
        await proxy_instance._handle_tcp_connection(
            mock_reader, mock_writer, java_server_config
        )

        mock_docker_manager.start_server.assert_awaited_once_with(
            java_server_config, proxy_instance.settings
        )
        # Verify client writer was closed
        mock_writer.close.assert_awaited_once()
        mock_writer.wait_closed.assert_awaited_once()

        # Verify no session was created
        session_key = (client_addr, java_server_config.listen_port, "tcp")
        async with proxy_instance.session_lock:
            assert session_key not in proxy_instance.active_sessions
        mock_metrics["active_sessions"].labels.return_value.inc.assert_not_called()
        mock_metrics["running_servers"].inc.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_udp_datagram_new_session_server_stopped(
    proxy_instance, mock_docker_manager, mock_servers_config, mock_metrics
):
    """
    Tests handling of a new UDP datagram when the target server is initially
    stopped and needs to be started.
    """
    bedrock_server_config = next(
        s for s in mock_servers_config if s.server_type == "bedrock"
    )
    container_name = bedrock_server_config.container_name
    client_addr = ("127.0.0.1", 54321)
    test_data = b"test_udp_packet"

    # Simulate server being stopped
    proxy_instance.server_states[container_name]["status"] = "stopped"

    # Mock DockerManager behavior
    mock_docker_manager.is_container_running.return_value = False
    mock_docker_manager.start_server.return_value = True  # Server starts successfully

    # Mock create_datagram_endpoint
    mock_transport = AsyncMock()
    mock_protocol = MagicMock(spec=UdpProxyProtocol)
    mock_create_datagram_endpoint_return_value = (mock_transport, mock_protocol)

    with (
        patch(
            "asyncio.get_running_loop().create_datagram_endpoint",
            new=AsyncMock(return_value=mock_create_datagram_endpoint_return_value),
        ) as mock_create_endpoint,
        patch("time.time", return_value=100.0) as _mock_time,
    ):
        await proxy_instance._handle_udp_datagram(
            test_data, client_addr, bedrock_server_config
        )

        mock_docker_manager.start_server.assert_awaited_once_with(
            bedrock_server_config, proxy_instance.settings
        )
        mock_create_endpoint.assert_awaited_once_with(
            mock_protocol.__class__,  # Expected to be UdpProxyProtocol factory
            local_addr=("0.0.0.0", 0),
        )

        # Verify session was created
        session_key = (client_addr[0], bedrock_server_config.listen_port, "udp")
        async with proxy_instance.session_lock:
            assert session_key in proxy_instance.active_sessions
            session_info = proxy_instance.active_sessions[session_key]
            assert session_info["client_addr"] == client_addr[0]
            assert session_info["protocol"] == "udp"
            assert session_info["target_container"] == container_name
            assert session_info["last_packet_time"] == 100.0
            assert session_info["transport"] == mock_transport

        # Verify packet was forwarded
        mock_transport.sendto.assert_called_once_with(
            test_data, (container_name, bedrock_server_config.internal_port)
        )
        mock_metrics["active_sessions"].labels.return_value.inc.assert_called_once()
        mock_metrics["running_servers"].inc.assert_called_once()
        mock_metrics[
            "bytes_transferred"
        ].labels.return_value.inc.assert_called_once_with(len(test_data))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_udp_datagram_server_already_running(
    proxy_instance, mock_docker_manager, mock_servers_config, mock_metrics
):
    """
    Tests handling of a new UDP datagram when the target server is already running.
    """
    bedrock_server_config = next(
        s for s in mock_servers_config if s.server_type == "bedrock"
    )
    container_name = bedrock_server_config.container_name
    client_addr = ("127.0.0.1", 54321)
    test_data = b"test_udp_packet_running"

    # Simulate server being running
    proxy_instance.server_states[container_name]["status"] = "running"
    proxy_instance.running_servers_metric.inc()  # Manually set for test

    # Mock create_datagram_endpoint and session
    mock_transport = AsyncMock()
    mock_protocol = MagicMock(spec=UdpProxyProtocol)

    session_key = (client_addr[0], bedrock_server_config.listen_port, "udp")
    async with proxy_instance.session_lock:
        proxy_instance.active_sessions[session_key] = {
            "client_addr": client_addr[0],
            "listen_port": bedrock_server_config.listen_port,
            "protocol": "udp",
            "target_container": container_name,
            "last_packet_time": time.time(),
            "transport": mock_transport,
            "protocol_instance": mock_protocol,
        }
        proxy_instance.server_states[container_name]["sessions"] = 1
        proxy_instance.active_sessions_metric.labels(
            server_name=bedrock_server_config.name
        ).inc()

    with (
        patch.object(proxy_instance, "_cleanup_udp_session", new=AsyncMock()),
        patch("time.time", return_value=100.0) as _mock_time,
    ):
        await proxy_instance._handle_udp_datagram(
            test_data, client_addr, bedrock_server_config
        )

        mock_docker_manager.start_server.assert_not_awaited()
        # Verify packet was forwarded
        mock_transport.sendto.assert_called_once_with(
            test_data, (container_name, bedrock_server_config.internal_port)
        )
        mock_metrics[
            "bytes_transferred"
        ].labels.return_value.inc.assert_called_once_with(len(test_data))
        # No new session created, so increment not called
        mock_metrics["active_sessions"].labels.return_value.inc.assert_not_called()
        assert proxy_instance.server_states[container_name]["last_activity"] == 100.0
        proxy_instance._cleanup_udp_session.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reload_configuration(
    proxy_instance, mock_docker_manager, mock_servers_config, default_proxy_settings
):
    """
    Tests that _reload_configuration correctly handles closing listeners,
    shuts down sessions, and restarts with new configuration.
    """
    # Set up some initial state
    proxy_instance.tcp_listeners = [AsyncMock(), AsyncMock()]
    proxy_instance.udp_listeners = [AsyncMock(), AsyncMock()]
    proxy_instance.active_sessions = {("client1", 1234, "tcp"): {"some_info": True}}

    # Define new settings and servers
    new_settings = default_proxy_settings
    new_settings.idle_timeout_seconds = 999
    new_servers = [
        ServerConfig(
            name="New Bedrock",
            server_type="bedrock",
            listen_port=19133,
            container_name="new-mc-bedrock",
            internal_port=19133,
        )
    ]

    # Mock internal methods that are called during reload
    with (
        patch.object(
            proxy_instance, "_close_listeners", new=AsyncMock()
        ) as mock_close_listeners,
        patch.object(
            proxy_instance, "_shutdown_all_sessions", new=AsyncMock()
        ) as mock_shutdown_sessions,
        patch.object(
            proxy_instance, "_start_listeners", new=AsyncMock()
        ) as mock_start_listeners,
    ):
        await proxy_instance._reload_configuration(new_settings, new_servers)

        mock_close_listeners.assert_awaited_once()
        mock_shutdown_sessions.assert_awaited_once()

        # Verify settings and server list are updated
        assert proxy_instance.settings == new_settings
        assert proxy_instance.servers_list == new_servers
        assert proxy_instance.servers_config_map[19133].name == "New Bedrock"

        # Verify new server state is initialized
        assert "new-mc-bedrock" in proxy_instance.server_states
        assert proxy_instance.server_states["new-mc-bedrock"]["status"] == "stopped"

        mock_start_listeners.assert_awaited_once()
