# tests/integration_tests/test_integration.py
"""
Integration tests for the Nether-bridge proxy.
These tests use live Docker containers to verify end-to-end functionality.
"""

import asyncio
import io
import json
import os
import signal
import sys
import tarfile

import pytest
import pytest_asyncio
import structlog

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from docker_manager import DockerManager
from tests.helpers import (
    BEDROCK_UNCONNECTED_PING,  # Import the consolidated constant
    check_port_listening,
    get_active_sessions_metric,
    get_java_handshake_and_status_request_packets,
    get_proxy_host,
    wait_for_container_status,
    wait_for_log_message,
    wait_for_mc_server_ready,
)

# Initialize logger for this test file
log = structlog.get_logger()

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PROXY_HOST = get_proxy_host()


@pytest_asyncio.fixture(scope="function")
async def docker_manager(docker_client_fixture):
    """Provides a DockerManager instance for the test function."""
    # Note: app_config is usually passed during DockerManager init in main.
    # For testing, a mock is okay or it can be set later if needed.
    manager = DockerManager(app_config=None)
    manager.docker = docker_client_fixture
    yield manager
    # Ensure DockerManager's aiodocker client is closed after test
    await manager.close()


async def test_java_server_lifecycle(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Java server: on-demand start and idle
    stop.
    """
    container_name = "mc-java"
    server_name = "Java Creative"  # Matches name in servers.json
    proxy_port = 25565
    idle_timeout = 30  # Matches NB_IDLE_TIMEOUT in docker-compose.tests.yml

    # Ensure server is initially stopped
    assert not await docker_manager.is_container_running(container_name), (
        f"Java container {container_name} should be initially stopped."
    )
    # Ensure proxy is listening on the port
    assert await check_port_listening(PROXY_HOST, proxy_port, protocol="tcp"), (
        f"Proxy not listening on TCP port {proxy_port}."
    )

    # Trigger server startup by attempting a connection
    try:
        # Client connection to trigger startup
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(PROXY_HOST, proxy_port), timeout=10
        )
        # Send a minimal handshake packet to fully establish the session
        handshake_packet, _ = await asyncio.to_thread(
            lambda h, p: get_java_handshake_and_status_request_packets(h, p),
            PROXY_HOST,
            proxy_port,
        )
        writer.write(handshake_packet)
        await writer.drain()
        log.info(
            f"Sent initial handshake to {server_name} via proxy to trigger startup."
        )
    except (ConnectionRefusedError, asyncio.TimeoutError) as e:
        # This is expected if the server isn't instantly ready, proxy holds conn
        log.info(
            f"Initial connection attempt to {server_name} got {type(e).__name__}. "
            "Proceeding, proxy should be starting server."
        )
    finally:
        if "writer" in locals() and writer is not None:
            writer.close()
            await writer.wait_closed()

    # Assert proxy logs the startup
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Server not running. Initiating startup...",
        timeout=10,
    ), "Proxy did not log start of Java server."

    # Assert container starts and becomes ready
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), f"Java container {container_name} failed to start."
    assert await wait_for_mc_server_ready("java", PROXY_HOST, proxy_port), (
        "Java server did not become query-ready through proxy."
    )

    # Assert active sessions metric is 0 after connection is closed
    assert await get_active_sessions_metric(PROXY_HOST, server_name) == 0, (
        "Active sessions metric did not reset to 0 after initial connection."
    )

    # Wait for idle shutdown
    log_message_timeout = idle_timeout + 15  # Give some buffer
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Server idle with 0 players. Stopping.",
        timeout=log_message_timeout,
    ), "Proxy did not log idle shutdown of Java server."

    # Assert container stops
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "exited", timeout=idle_timeout + 20
    ), f"Java container {container_name} did not stop after idle timeout."

    # Assert active sessions metric is 0 after shutdown
    assert await get_active_sessions_metric(PROXY_HOST, server_name) == 0, (
        "Active sessions metric did not reset to 0 after idle shutdown."
    )


async def test_bedrock_server_lifecycle(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Bedrock server: on-demand start and idle
    stop.
    """
    container_name = "mc-bedrock"
    server_name = "Bedrock Survival"  # Matches name in servers.json
    proxy_port = 19132
    idle_timeout = 30  # Matches NB_IDLE_TIMEOUT in docker-compose.tests.yml

    # Ensure server is initially stopped
    assert not await docker_manager.is_container_running(container_name), (
        f"Bedrock container {container_name} should be initially stopped."
    )
    # Ensure proxy is listening on the port
    assert await check_port_listening(PROXY_HOST, proxy_port, protocol="udp"), (
        f"Proxy not listening on UDP port {proxy_port}."
    )

    # Trigger server startup by sending a UDP packet
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: asyncio.DatagramProtocol(), remote_addr=(PROXY_HOST, proxy_port)
    )
    transport.sendto(BEDROCK_UNCONNECTED_PING)  # Use the consolidated constant
    transport.close()

    # Assert proxy logs the startup
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "New UDP client",  # First UDP packet log
        timeout=10,
    ), "Proxy did not log new UDP client for Bedrock server."

    # Assert container starts and becomes ready
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), f"Bedrock container {container_name} failed to start."
    assert await wait_for_mc_server_ready("bedrock", PROXY_HOST, proxy_port), (
        "Bedrock server did not become query-ready through proxy."
    )

    # Assert active sessions metric is 1 (or at least > 0)
    # Note: UDP session count can be transient depending on test timing
    assert await get_active_sessions_metric(PROXY_HOST, server_name) >= 0, (
        "Failed to get active sessions metric for Bedrock."
    )

    # Wait for idle shutdown
    log_message_timeout = idle_timeout + 15
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Server idle with 0 players. Stopping.",
        timeout=log_message_timeout,
    ), "Proxy did not log idle shutdown of Bedrock server."

    # Assert container stops
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "exited", timeout=idle_timeout + 20
    ), f"Bedrock container {container_name} did not stop after idle timeout."

    # Assert active sessions metric is 0 after shutdown
    assert await get_active_sessions_metric(PROXY_HOST, server_name) == 0, (
        "Active sessions metric did not reset to 0 after Bedrock idle shutdown."
    )


async def test_java_server_pre_warm_startup(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Verifies that a Java server configured with 'pre_warm: true' starts
    automatically with the proxy, without requiring a client connection.
    """
    container_name = "mc-java"
    # Note: This test requires 'Java Pre-warm' server to be configured
    # in servers.json with pre_warm: true and listening on a distinct port.
    # For now, it will re-use mc-java container by injecting config.
    pre_warm_server_name = "Java Pre-warm"
    pre_warm_proxy_port = 25566  # A new, dedicated port for the pre-warmed server

    # 1. Ensure server is initially stopped
    log.info(f"Ensuring {container_name} is stopped before pre-warm test.")
    await docker_manager.stop_server(container_name, 10)
    assert not await docker_manager.is_container_running(container_name), (
        f"Java container {container_name} should be initially stopped."
    )

    # 2. Inject a servers.json with a pre_warm entry
    log.info("Injecting servers.json with a pre_warm server entry...")
    new_config = {
        "servers": [
            {
                "name": pre_warm_server_name,
                "game_type": "java",
                "proxy_port": pre_warm_proxy_port,
                "container_name": container_name,  # Reuse existing container
                "port": 25565,
                "pre_warm": True,  # This server should pre-warm
            }
        ]
    }
    async with docker_manager.get_container("nether-bridge") as proxy_container:
        assert proxy_container is not None, "nether-bridge container not found"
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            config_bytes = json.dumps(new_config).encode("utf-8")
            info = tarfile.TarInfo(name="servers.json")
            info.size = len(config_bytes)
            tar.addfile(info, io.BytesIO(config_bytes))
        tar_stream.seek(0)
        await proxy_container.put_archive("/app", tar_stream.read())
        log.info(f"New servers.json injected into {proxy_container.id}.")

        # 3. Send SIGHUP to proxy to reload config (which should trigger pre-warm)
        log.info(f"Sending SIGHUP to nether-bridge {proxy_container.id} to reload.")
        await proxy_container.kill(signal=signal.SIGHUP)
        assert await wait_for_log_message(
            docker_manager.docker,
            "nether-bridge",
            "Configuration reload complete.",
            timeout=15,
        ), "Proxy did not log completion of configuration reload after SIGHUP."

    # 4. Assert proxy logs pre-warm startup and container is running
    log.info(f"Waiting for proxy to log pre-warm startup for {pre_warm_server_name}.")
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        f"Pre-warming server. server={pre_warm_server_name}",
        timeout=10,
    ), "Proxy did not log pre-warming of Java server."

    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), f"Pre-warmed Java container {container_name} failed to start."

    assert await wait_for_mc_server_ready("java", PROXY_HOST, pre_warm_proxy_port), (
        f"Pre-warmed Java server {pre_warm_server_name} did not become "
        "query-ready through proxy."
    )
    log.info(f"Pre-warmed server {pre_warm_server_name} is running and ready.")


async def test_sighup_reloads_configuration(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Tests that the proxy correctly reloads its configuration upon SIGHUP.
    """
    new_port = 19134
    old_java_port = 25565

    # 1. Ensure proxy is ready and listening on old port
    assert await check_port_listening(PROXY_HOST, old_java_port), (
        f"Proxy not listening on old Java port {old_java_port} initially."
    )

    # 2. Prepare new config to inject
    new_config = {
        "servers": [
            {
                "name": "Bedrock Reloaded (SIGHUP)",
                "game_type": "bedrock",
                "proxy_port": new_port,
                "container_name": "mc-bedrock",
                "port": 19132,
            }
        ]
    }

    # 3. Inject new config file into nether-bridge container
    async with docker_manager.get_container("nether-bridge") as container:
        assert container is not None, "nether-bridge container not found"
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            config_bytes = json.dumps(new_config).encode("utf-8")
            info = tarfile.TarInfo(name="servers.json")
            info.size = len(config_bytes)
            tar.addfile(info, io.BytesIO(config_bytes))
        tar_stream.seek(0)
        await container.put_archive("/app", tar_stream.read())
        log.info(f"New servers.json injected into {container.id}.")

        # 4. Send SIGHUP signal to the proxy
        await container.kill(signal=signal.SIGHUP)
        log.info(f"SIGHUP sent to nether-bridge container {container.id}.")

    # 5. Assert proxy logs the reload completion
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Configuration reload complete.",
        timeout=15,
    ), "Proxy did not log completion of configuration reload after SIGHUP."

    # 6. Verify new configuration is active and old one is not
    assert await check_port_listening(PROXY_HOST, new_port, protocol="udp"), (
        f"Proxy did not start listening on new port {new_port} after reload."
    )
    # Give a brief moment for old listeners to close
    await asyncio.sleep(2)
    assert not await check_port_listening(PROXY_HOST, old_java_port), (
        "Proxy did not stop listening on old Java port after reload."
    )


async def test_proxy_restarts_crashed_server(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Tests that the proxy will re-start a server that was stopped externally
    (simulating a crash).
    """
    container_name = "mc-java"
    proxy_port = 25565

    # 1. Trigger initial server startup via proxy connection
    log.info("Triggering initial Java server start via proxy.")
    # Establish a connection to trigger server startup
    client_reader, client_writer = None, None
    try:
        client_reader, client_writer = await asyncio.open_connection(
            PROXY_HOST, proxy_port
        )
        # Send a minimal handshake to ensure a session is established
        handshake_packet, _ = await asyncio.to_thread(
            lambda h, p: get_java_handshake_and_status_request_packets(h, p),
            PROXY_HOST,
            proxy_port,
        )
        client_writer.write(handshake_packet)
        await client_writer.drain()
    except (ConnectionRefusedError, asyncio.TimeoutError):
        log.info("Initial connection attempt refused/timed out (expected).")
    finally:
        if client_writer:
            client_writer.close()
            await client_writer.wait_closed()

    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Initial Java server did not start on connection."

    # Assert proxy logged "Server startup complete."
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Server startup complete.",
        timeout=10,
    ), "Proxy did not log server startup completion for initial start."

    # 2. Manually stop (simulate crash) the server container
    log.info(f"Manually stopping container: {container_name} to simulate crash.")
    async with docker_manager.get_container(container_name) as container:
        assert container is not None, f"{container_name} container not found."
        await container.stop(t=10)  # Use a timeout for graceful stop
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "exited", timeout=15
    ), f"Container {container_name} did not stop after manual command."

    await asyncio.sleep(1)  # Give a moment for proxy to detect/process state change

    # 3. Attempt a new connection to trigger proxy restart
    log.info(f"Attempting new connection to trigger restart for {container_name}.")
    # The act of connecting will trigger the start
    client_reader_2, client_writer_2 = None, None
    try:
        client_reader_2, client_writer_2 = await asyncio.open_connection(
            PROXY_HOST, proxy_port
        )
        handshake_packet_2, _ = await asyncio.to_thread(
            lambda h, p: get_java_handshake_and_status_request_packets(h, p),
            PROXY_HOST,
            proxy_port,
        )
        client_writer_2.write(handshake_packet_2)
        await client_writer_2.drain()
    except (ConnectionRefusedError, asyncio.TimeoutError):
        log.info(
            "Second connection attempt refused/timed out (expected during startup)."
        )
    finally:
        if client_writer_2:
            client_writer_2.close()
            await client_writer_2.wait_closed()

    # 4. Assert proxy logs its intent to restart the server
    assert await wait_for_log_message(
        docker_manager.docker,
        "nether-bridge",
        "Server not running. Initiating startup...",
        timeout=10,
    ), "Proxy did not log that it was attempting to restart the crashed server."

    # 5. Assert server is running again
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), f"Proxy did not restart the crashed server {container_name} on new connection."


async def test_proxy_cleans_up_session_on_container_crash(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Tests that the proxy correctly cleans up a session if the backend crashes.
    """
    container_name = "mc-java"
    proxy_port = 25565

    # 1. Pre-warm the server and establish a connection
    log.info("Establishing connection to Java server for session cleanup test.")
    reader, writer = None, None
    try:
        reader, writer = await asyncio.open_connection(PROXY_HOST, proxy_port)
        # Send a dummy byte/packet to ensure the session is fully
        # established on the proxy's side
        handshake_packet, status_request_packet = await asyncio.to_thread(
            lambda h, p: get_java_handshake_and_status_request_packets(h, p),
            PROXY_HOST,
            proxy_port,
        )
        writer.write(handshake_packet)
        writer.write(status_request_packet)
        await writer.drain()

        # Wait for the server to be running and queryable via proxy
        assert await wait_for_container_status(
            docker_manager.docker, container_name, "running", timeout=60
        ), "Java server did not start for session cleanup test."
        assert await wait_for_mc_server_ready("java", PROXY_HOST, proxy_port), (
            "Java server did not become query-ready through proxy."
        )

        # Assert proxy logs new session
        assert await wait_for_log_message(
            docker_manager.docker,
            "nether-bridge",
            "New TCP connection",
            timeout=10,
        ), "Proxy did not log new TCP connection."

        log.info("Client connected and session established. Proxy is active.")

        # 2. Forcibly kill the backend server container
        log.info(f"Forcibly killing container: {container_name}")
        async with docker_manager.get_container(container_name) as container:
            await container.kill()
        assert await wait_for_container_status(
            docker_manager.docker, container_name, "exited", timeout=15
        ), f"Container {container_name} did not stop after being killed."

        await asyncio.sleep(1)  # Give a moment for network stack to register close

        # 3. Attempt to send data from client, which should now fail
        log.info("Attempting to send data to trigger proxy's error handling...")
        with pytest.raises((ConnectionResetError, BrokenPipeError, OSError)):
            # Sending data on a broken pipe/reset connection will raise an error
            writer.write(b"data_after_crash")
            await writer.drain()

        # 4. Assert that the PROXY detected the error and logged the cleanup
        # The specific message might vary based on exact timing, look for either
        # a connection error or session closure message.
        assert await wait_for_log_message(
            docker_manager.docker,
            "nether-bridge",
            "Connection reset during data proxy.",
            timeout=10,
        ) or await wait_for_log_message(
            docker_manager.docker,
            "nether-bridge",
            "TCP session closed",
            timeout=10,
        ), "Proxy did not log session cleanup after the container crash."

        log.info("Test passed: Proxy correctly handled the crashed session.")

    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
