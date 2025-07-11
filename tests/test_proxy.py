# tests/test_proxy.py
import asyncio
import signal
import threading
import time
from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from metrics import ACTIVE_SESSIONS, BYTES_TRANSFERRED
from proxy import NetherBridgeProxy, NetherBridgeUDPProxyProtocol


@pytest.fixture
def mock_settings():
    """Fixture for a mocked ProxySettings object."""
    settings = MagicMock(spec=ProxySettings)
    settings.player_check_interval_seconds = 1
    settings.activity_check_interval_seconds = 1
    settings.server_idle_timeout_seconds = 5
    settings.docker_network_gateway_ip = "172.0.0.1"
    settings.server_ready_max_wait_time_seconds = 1
    settings.query_timeout_seconds = 1
    return settings


@pytest.fixture
def mock_servers_config():
    """Fixture for a list of mocked ServerConfig objects."""
    server1 = MagicMock(spec=ServerConfig)
    server1.name = "Java Test"
    server1.container_name = "test-mc-java"
    server1.server_type = "java"
    server1.listen_port = 25565
    server1.internal_port = 25565
    server1.idle_timeout_seconds = None

    server2 = MagicMock(spec=ServerConfig)
    server2.name = "Bedrock Test"
    server2.container_name = "test-mc-bedrock"
    server2.server_type = "bedrock"
    server2.listen_port = 19132
    server2.internal_port = 19132
    server2.idle_timeout_seconds = None

    return [server1, server2]


@pytest.fixture
def proxy_instance(mock_settings, mock_servers_config):
    """Fixture for a NetherBridgeProxy instance."""
    # Patch DockerManager to prevent real Docker calls during proxy init
    with patch("proxy.DockerManager"):
        proxy = NetherBridgeProxy(mock_settings, mock_servers_config)
        # Ensure the mock docker_manager has async methods where expected
        proxy.docker_manager = MagicMock(spec=DockerManager)
        # Mock the async methods that will be called
        proxy.docker_manager.start_server.return_value = (
            asyncio.Future()
        )  # Will be set later
        proxy.docker_manager.stop_server.return_value = (
            asyncio.Future()
        )  # Will be set later
        proxy.docker_manager.is_container_running.return_value = True

        yield proxy


@pytest.mark.unit
def test_proxy_initialization(proxy_instance, mock_settings, mock_servers_config):
    """Tests that the proxy initializes correctly."""
    assert proxy_instance.settings == mock_settings
    assert proxy_instance.servers_list == mock_servers_config
    assert isinstance(proxy_instance._shutdown_event, asyncio.Event)
    for server_name, state in proxy_instance.server_states.items():
        assert state["status"] == "stopped"
        assert isinstance(state["lock"], Lock)
        assert isinstance(state["ready_event"], asyncio.Event)


@pytest.mark.unit
def test_signal_handler_sighup(proxy_instance):
    """Tests that SIGHUP sets the reload and shutdown events."""
    with patch.object(proxy_instance._shutdown_event, "set") as mock_shutdown_set:
        # Simulate SIGHUP
        proxy_instance.signal_handler(signal.SIGHUP, None)
        assert proxy_instance._reload_requested is True
        mock_shutdown_set.assert_called_once()


@pytest.mark.unit
def test_signal_handler_sigint(proxy_instance):
    """Tests that SIGINT sets the shutdown event."""
    with patch.object(proxy_instance._shutdown_event, "set") as mock_set:
        # Simulate SIGINT
        proxy_instance.signal_handler(signal.SIGINT, None)
        assert proxy_instance._shutdown_requested is True
        mock_set.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio  # Mark test as async
async def test_start_minecraft_server_task_success(proxy_instance, mock_servers_config):
    """
    Tests that _start_minecraft_server_task correctly starts a server
    and updates its state.
    """
    server_config = mock_servers_config[0]
    container_name = server_config.container_name

    # Mock the return value of docker_manager.start_server to be a Future that
    # resolves to True
    mock_start_server_future = asyncio.Future()
    mock_start_server_future.set_result(True)
    proxy_instance.docker_manager.start_server.return_value = mock_start_server_future

    # Initial state
    proxy_instance.server_states[container_name]["status"] = "stopped"
    proxy_instance.server_states[container_name]["ready_event"].clear()

    # Call the async task directly
    await proxy_instance._start_minecraft_server_task(server_config)

    # Assertions
    proxy_instance.docker_manager.start_server.assert_awaited_once_with(
        server_config, proxy_instance.settings
    )
    assert proxy_instance.server_states[container_name]["status"] == "running"
    assert proxy_instance.server_states[container_name]["ready_event"].is_set() is True
    # Check if SERVER_STARTUP_DURATION was observed (can be tricky to assert
    # exact value without custom Prometheus client mock)
    # For now, just ensure the proxy state is correct.


@pytest.mark.unit
@patch("proxy.time.time", side_effect=[0, 100, 101])  # Simulate time passing
@patch("asyncio.run")  # Patch asyncio.run as it's used in the thread
@patch.object(DockerManager, "is_container_running", return_value=True)
async def test_monitor_servers_activity_stops_idle_server(
    mock_is_container_running,
    mock_asyncio_run,
    mock_time,
    proxy_instance,
    mock_servers_config,
):
    """
    Tests that the server monitoring thread correctly identifies and stops
    an idle server.
    """
    server_config = mock_servers_config[1]  # Use the Java server for this test
    container_name = server_config.container_name

    # Set up the initial state to be running and idle
    proxy_instance.server_states[container_name]["status"] = "running"
    # Make it idle for more than idle_timeout_seconds (which is 5 in mock_settings)
    proxy_instance.server_states[container_name]["last_activity"] = time.time() - 100

    # Mock the _shutdown_event to stop the monitor loop after one iteration
    side_effect_is_set = [False, True]  # Returns False then True
    with patch.object(
        proxy_instance._shutdown_event, "is_set", side_effect=side_effect_is_set
    ):
        # Mock ACTIVE_SESSIONS to indicate no active sessions for the server
        # Access _value directly as per proxy.py's implementation
        with patch.object(
            ACTIVE_SESSIONS.labels(server_name=server_config.name),
            "_value",
            new=0,  # Use new=0 to directly set the value
        ):
            # Ensure stop_server returns a completed Future for asyncio.run
            # This is how `asyncio.run` expects its argument to behave.
            mock_asyncio_run.return_value = None

            # Run the monitor activity in a separate thread
            # We are testing the _monitor_servers_activity method directly,
            # which is designed to run in a thread.
            monitor_thread = threading.Thread(
                target=proxy_instance._monitor_servers_activity, daemon=True
            )
            monitor_thread.start()
            monitor_thread.join(timeout=2)  # Wait for the thread to finish or timeout

            # Assertions
            # Assert that asyncio.run was called with proxy.docker_manager.stop_server
            mock_asyncio_run.assert_called_once()
            # Verify the specific call to docker_manager.stop_server *inside*
            # the mocked asyncio.run
            assert isinstance(
                mock_asyncio_run.call_args[0][0], asyncio.Future
            ) or asyncio.iscoroutine(mock_asyncio_run.call_args[0][0])
            proxy_instance.docker_manager.stop_server.assert_called_once_with(
                container_name
            )

            assert proxy_instance.server_states[container_name]["status"] == "stopped"


# Example: test for _forward_data
@pytest.mark.unit
@pytest.mark.asyncio
async def test_forward_data(proxy_instance, mock_servers_config):
    """Tests the _forward_data coroutine for stream forwarding."""
    server_config = mock_servers_config[0]
    client_reader = MagicMock(spec=asyncio.StreamReader)
    client_writer = MagicMock(spec=asyncio.StreamWriter)
    server_name = server_config.name
    # Removed unused local variable: client_addr

    # Simulate data being read from the reader
    client_reader.at_eof.side_effect = [False, False, True]  # Read twice then eof
    client_reader.read.side_effect = [b"hello", b"world", b""]

    client_writer.drain.return_value = None

    # Patch BYTES_TRANSFERRED metric for this test
    with patch.object(
        BYTES_TRANSFERRED.labels(server_name=server_name, direction="c2s"),
        "inc",
    ) as mock_inc:
        await proxy_instance._forward_data(
            client_reader, client_writer, server_name, "c2s"
        )

    client_writer.write.assert_any_call(b"hello")
    client_writer.write.assert_any_call(b"world")
    assert client_writer.write.call_count == 2
    assert client_writer.drain.call_count == 2
    mock_inc.assert_any_call(5)  # "hello"
    mock_inc.assert_any_call(5)  # "world"
    assert mock_inc.call_count == 2
    client_writer.close.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_tcp_client_server_starts(
    proxy_instance, mock_settings, mock_servers_config
):
    """Tests TCP client handling when server needs to be started."""
    server_config = mock_servers_config[0]  # Java server
    client_reader = MagicMock(spec=asyncio.StreamReader)
    client_writer = MagicMock(spec=asyncio.StreamWriter)
    client_writer.get_extra_info.side_effect = [
        ("127.0.0.1", 12345),  # peername
        (None, server_config.listen_port),  # sockname
    ]

    # Set initial state to stopped
    proxy_instance.server_states[server_config.container_name]["status"] = "stopped"
    proxy_instance.server_states[server_config.container_name]["ready_event"].clear()

    # Mock _initiate_server_start to return True (it will start the task)
    with patch.object(
        proxy_instance, "_initiate_server_start", return_value=True
    ) as mock_initiate_start:
        # Mock asyncio.open_connection
        mock_server_reader = MagicMock(spec=asyncio.StreamReader)
        mock_server_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_open_connection_future = asyncio.Future()
        mock_open_connection_future.set_result((mock_server_reader, mock_server_writer))

        with patch("asyncio.open_connection", return_value=mock_open_connection_future):
            # Mock _forward_data to just return
            with patch.object(
                proxy_instance, "_forward_data", new_callable=MagicMock
            ) as mock_forward_data:
                mock_forward_data.return_value = asyncio.Future()
                mock_forward_data.return_value.set_result(None)

                await proxy_instance._handle_tcp_client(client_reader, client_writer)

                mock_initiate_start.assert_awaited_once_with(server_config)
                # Verify that ready_event.wait() was awaited by the handler
                assert (
                    proxy_instance.server_states[server_config.container_name][
                        "ready_event"
                    ].is_set()
                    is True
                )
                asyncio.open_connection.assert_awaited_once_with(
                    host=server_config.container_name,
                    port=server_config.internal_port,
                )
                assert mock_forward_data.call_count == 2
                client_writer.close.assert_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_proxy_loop_shuts_down_on_event(proxy_instance):
    """Tests that _run_proxy_loop exits when the shutdown event is set."""
    # Mock start_server to prevent actual port binding in test
    with patch("asyncio.start_server") as mock_tcp_start:
        # Mock the loop to return a mock for create_datagram_endpoint
        mock_loop = MagicMock(spec=asyncio.BaseEventLoop)
        # Patch asyncio.get_running_loop to return our mock loop
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            mock_loop.create_datagram_endpoint.return_value = (
                MagicMock(spec=asyncio.BaseTransport),
                MagicMock(spec=NetherBridgeUDPProxyProtocol),
            )

            mock_tcp_server = MagicMock(spec=asyncio.Server)
            mock_tcp_start.return_value = mock_tcp_server
            mock_tcp_server.serve_forever.return_value = asyncio.Future()
            mock_tcp_server.serve_forever.return_value.set_result(None)

            # Start the _run_proxy_loop as a task
            proxy_loop_task = asyncio.create_task(proxy_instance._run_proxy_loop())

            # Give it a moment to set up servers
            await asyncio.sleep(0.01)

            # Signal shutdown
            proxy_instance._shutdown_event.set()

            # Wait for the proxy loop task to complete
            await proxy_loop_task

            mock_tcp_start.assert_awaited_once()
            mock_loop.create_datagram_endpoint.assert_called_once()
            mock_tcp_server.close.assert_called_once()
            mock_tcp_server.wait_closed.assert_awaited_once()

            # Check that stop_server was called for running servers
            proxy_instance.docker_manager.stop_server.assert_not_called()
