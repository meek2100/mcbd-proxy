# tests/integration_tests/test_integration.py

import asyncio
import socket

import aiodocker
import pytest

from tests.helpers import (
    BEDROCK_PROXY_PORT,
    JAVA_PROXY_PORT,
    get_container_status,
    get_java_handshake_and_status_request_packets,
    get_proxy_host,
    wait_for_container_status,
    wait_for_log_message,
    wait_for_mc_server_ready,
    wait_for_proxy_to_be_ready,
)


# --- Integration Test Cases ---
@pytest.mark.integration
@pytest.mark.asyncio
async def test_bedrock_server_starts_on_connection(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
    nether_bridge_container_name = "nether-bridge"

    # Wait for the proxy to report itself as ready via logs
    assert await wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = await get_container_status(
        docker_client_fixture, mc_bedrock_container_name
    )
    assert initial_status in [
        False,
        "not_found",
    ], f"Bedrock server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_bedrock_container_name}: {initial_status}")

    # Simulate a Bedrock client connection to trigger server start
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(
            f"Simulating connection to {nether_bridge_container_name} on port "
            f"{bedrock_proxy_port} on host {proxy_host}..."
        )
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
        print("Bedrock 'Unconnected Ping' packet sent.")

        assert await wait_for_log_message(
            docker_client_fixture,
            nether_bridge_container_name,
            "First UDP packet received for stopped server. Starting...",
            timeout=10,
        ), (
            "Proxy did not log that it was starting the Bedrock server."  #
        )

        # Wait for the server container to become "running"
        assert await wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            [True],  # True indicates 'running'
            timeout=180,
            interval=2,
        ), f"Bedrock server '{mc_bedrock_container_name}' did not start after 180s."

        # Wait for the Minecraft server to be ready for queries via the proxy
        assert await wait_for_mc_server_ready(
            {
                "host": proxy_host,
                "port": bedrock_proxy_port,
                "type": "bedrock",
            },
            timeout=180,
            interval=5,
        ), "Bedrock server did not become query-ready through proxy."
    finally:
        # client_socket may not be defined if an exception occurred earlier
        if "client_socket" in locals() and client_socket.fileno() != -1:
            client_socket.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_java_server_starts_on_connection(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Test that the mc-java server starts when a connection attempt is made
    to the nether-bridge proxy on its Java port.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"
    nether_bridge_container_name = "nether-bridge"

    assert await wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = await get_container_status(
        docker_client_fixture, mc_java_container_name
    )
    assert initial_status in [
        False,
        "not_found",
    ], f"Java server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    # Simulate a Java client connection to trigger server start
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(
            f"Simulating connection to {nether_bridge_container_name} on port "
            f"{java_proxy_port} on host {proxy_host}..."
        )
        client_socket.connect((proxy_host, java_proxy_port))
        print(f"Successfully connected to {proxy_host}:{java_proxy_port}.")

        assert await wait_for_log_message(
            docker_client_fixture,
            nether_bridge_container_name,
            "First TCP connection for stopped server. Starting...",
            timeout=10,
        ), "Proxy did not log that it was starting the Java server."

        # Send initial handshake and status request packets
        handshake_packet, status_request_packet = (
            get_java_handshake_and_status_request_packets(proxy_host, java_proxy_port)
        )
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        # Wait for the server container to become "running"
        assert await wait_for_container_status(
            docker_client_fixture,
            mc_java_container_name,
            [True],  # True indicates 'running'
            timeout=180,
            interval=2,
        ), f"Java server '{mc_java_container_name}' did not start after 180s."

        # Wait for the Minecraft server to be ready for queries via the proxy
        assert await wait_for_mc_server_ready(
            {"host": proxy_host, "port": java_proxy_port, "type": "java"},
            timeout=180,
            interval=5,
        ), "Java server did not become query-ready through proxy."

    finally:
        # client_socket may not be defined or closed yet
        if "client_socket" in locals() and client_socket.fileno() != -1:
            client_socket.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_shuts_down_on_idle(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Tests that a running server is automatically stopped by the proxy after a
    period of inactivity.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
    nether_bridge_container_name = "nether-bridge"

    # Match docker-compose.tests.yml: NB_IDLE_TIMEOUT=30, NB_PLAYER_CHECK_INTERVAL=5
    idle_timeout = 30
    check_interval = 5

    assert await wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    # Trigger the server to start by simulating a connection
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"\nTriggering server '{mc_bedrock_container_name}' to start...")
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        if "client_socket" in locals() and client_socket.fileno() != -1:
            client_socket.close()
        print("Client socket closed, session terminated.")

    # Wait for the server to be running and query-ready
    assert await wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        [True],
        timeout=180,
    ), "Bedrock server did not start after being triggered."
    print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")
    assert await wait_for_mc_server_ready(
        {"host": proxy_host, "port": bedrock_proxy_port, "type": "bedrock"},
        timeout=180,
    ), "Bedrock server did not become query-ready after starting."
    print(f"Server '{mc_bedrock_container_name}' confirmed to be query-ready.")

    # Wait for idle shutdown message in proxy logs
    wait_duration = idle_timeout + (2 * check_interval) + 5
    print(f"Server is running. Waiting up to {wait_duration}s for idle shutdown...")

    assert await wait_for_log_message(
        docker_client_fixture,
        nether_bridge_container_name,
        "Server idle with 0 sessions. Initiating shutdown.",
        timeout=wait_duration,
    ), "Proxy did not log that it was shutting down an idle server."

    # Verify the container actually stopped
    assert await wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        [False],  # False indicates 'stopped' or 'exited'
        timeout=60,
    ), f"Bedrock server '{mc_bedrock_container_name}' did not stop after idle timeout."

    print("Proxy correctly initiated idle shutdown and server stopped. Test passed.")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_restarts_crashed_server_on_new_connection(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Tests that the proxy will re-start a server that has been stopped
    or has crashed.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
    nether_bridge_container_name = "nether-bridge"
    unconnected_ping_packet = (
        b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
        b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
    )

    # --- 1. Start the server and confirm it is running ---
    print(
        (
            f"\n(Crash Test) Triggering initial server start for "
            f"'{mc_bedrock_container_name}'..."
        )
    )
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        if "client_socket" in locals() and client_socket.fileno() != -1:
            client_socket.close()

    assert await wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, [True], timeout=60
    ), "Container did not start on first connection."
    print("(Crash Test) Initial server start successful.")

    # --- 2. Manually stop the container to simulate a crash ---
    print(f"(Crash Test) Manually stopping container '{mc_bedrock_container_name}'...")
    container = await docker_client_fixture.containers.get(mc_bedrock_container_name)
    await container.stop()
    assert await wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, [False]
    ), "Container did not stop after manual command."
    print("(Crash Test) Container successfully stopped.")

    await asyncio.sleep(2)  # Give a moment for Docker state to settle

    # --- 3. Attempt a new connection to the 'crashed' server ---
    print("\n(Crash Test) Attempting new connection to trigger restart...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        if "client_socket" in locals() and client_socket.fileno() != -1:
            client_socket.close()

    # --- 4. Verify that the proxy detects this and tries to start it again ---
    assert await wait_for_log_message(
        docker_client_fixture,
        nether_bridge_container_name,
        "First UDP packet for stopped server. Starting...",
        timeout=10,
    ), (
        "Proxy did not log that it was attempting to restart the server."  #
    )

    print("(Crash Test) Proxy correctly logged its intent to restart. Test passed.")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_configuration_reload_on_sighup(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Tests that the proxy correctly reloads its server configuration upon
    receiving a SIGHUP signal, without requiring a restart.
    """
    proxy_host = get_proxy_host()
    initial_bedrock_port = 19132
    reloaded_bedrock_port = 19134
    nether_bridge_container_name = "nether-bridge"

    assert await wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    # --- 1. Verify initial configuration is active ---
    print("\n(SIGHUP Test) Verifying initial server configuration...")
    client_socket_initial = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket_initial.settimeout(2)
        client_socket_initial.sendto(
            b"initial-ping", (proxy_host, initial_bedrock_port)
        )
        # Attempt to receive to confirm port is open. Actual data doesn't matter.
        client_socket_initial.recvfrom(1024)
        print(f"Initial check on port {initial_bedrock_port} is OK.")
    except socket.timeout:
        pytest.fail(f"Initial port {initial_bedrock_port} did not respond.")
    except ConnectionRefusedError:
        pytest.fail(f"Initial port {initial_bedrock_port} refused connection.")
    finally:
        if "client_socket_initial" in locals() and client_socket_initial.fileno() != -1:
            client_socket_initial.close()

    # --- 2. Create new config and send SIGHUP ---
    print(
        f"(SIGHUP Test) Writing new configuration inside "
        f"{nether_bridge_container_name}..."
    )  #
    container = await docker_client_fixture.containers.get(nether_bridge_container_name)

    new_config_json = """
{
  "servers": [
    {
      "name": "Bedrock RELOADED",
      "server_type": "bedrock",
      "listen_port": 19134,
      "container_name": "mc-bedrock",
      "internal_port": 19132
    }
  ]
}
"""
    # Use `exec_run` to write the file, ensuring correct permissions
    # The `user="naeus"` is important as the app runs as this user.
    # The path is /app/config/servers.json based on main.py.
    cmd_write_config = (
        f"mkdir -p /app/config && printf '%s' '{new_config_json}' "
        f"> /app/config/servers.json"
    )
    # Ensure correct ownership
    cmd_chown_config = "chown naeus:nogroup /app/config/servers.json"

    # Execute command as root first to create file, then chown
    exec_result_mkdir_printf = await container.exec_run(cmd_write_config, user="root")
    assert exec_result_mkdir_printf.exit_code == 0, (
        f"Failed to write new config: {exec_result_mkdir_printf.output.decode()}"
    )

    exec_result_chown = await container.exec_run(cmd_chown_config, user="root")
    assert exec_result_chown.exit_code == 0, (
        f"Failed to chown config: {exec_result_chown.output.decode()}"
    )

    print("New servers.json written and permissions set successfully.")

    print("(SIGHUP Test) Sending SIGHUP signal...")
    # Send SIGHUP to the main process inside the container.
    # The default entrypoint for `nether-bridge` is `entrypoint.sh` which then
    # `exec gosu naeus python main.py`. `gosu` handles signals correctly.
    await container.kill(signal="SIGHUP")

    assert await wait_for_log_message(
        docker_client_fixture,
        nether_bridge_container_name,
        "Configuration reload complete.",
        timeout=15,
    ), "Proxy did not log that it completed the configuration reload."

    print("(SIGHUP Test) Proxy logged reload completion. Verifying new behavior...")

    # --- 4. Verify new configuration is active and old one is not ---
    # Check that the old port is no longer listening
    client_socket_old_port = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket_old_port.settimeout(2)
        client_socket_old_port.sendto(
            b"old-port-ping", (proxy_host, initial_bedrock_port)
        )
        data, addr = client_socket_old_port.recvfrom(1024)
        pytest.fail(f"Old port {initial_bedrock_port} is still open unexpectedly.")
    except (ConnectionRefusedError, socket.timeout, OSError):
        print(f"Old port {initial_bedrock_port} is correctly closed/refused.")
    finally:
        if (
            "client_socket_old_port" in locals()
            and client_socket_old_port.fileno() != -1
        ):
            client_socket_old_port.close()

    # Check that the new port is now listening
    client_socket_new_port = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket_new_port.settimeout(2)
        client_socket_new_port.sendto(
            b"new-port-ping", (proxy_host, reloaded_bedrock_port)
        )
        data, addr = client_socket_new_port.recvfrom(1024)
        print(f"New port {reloaded_bedrock_port} is correctly open and responsive.")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        pytest.fail(f"New port {reloaded_bedrock_port} was not opened: {e}.")
    finally:
        if (
            "client_socket_new_port" in locals()
            and client_socket_new_port.fileno() != -1
        ):
            client_socket_new_port.close()

    print("(SIGHUP Test) Test passed: Proxy correctly reloaded its configuration.")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_cleans_up_session_on_container_crash(
    docker_compose_up, docker_client_fixture: aiodocker.Docker
):
    """
    Tests that if a server container crashes during an active session,
    the proxy detects the resulting connection error and cleans up the session.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"
    nether_bridge_container_name = "nether-bridge"

    assert await wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready before chaos test."
    )

    # Step 1: Pre-warm the server to ensure it is running and fully ready.
    print("\n(Chaos Test) Pre-warming server to ensure it is running...")
    pre_warm_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        pre_warm_socket.connect((proxy_host, java_proxy_port))
        (
            handshake,
            status_request,
        ) = get_java_handshake_and_status_request_packets(proxy_host, java_proxy_port)
        pre_warm_socket.sendall(handshake)
        pre_warm_socket.sendall(status_request)
        # FIX: Increase timeout to allow for slow server startup in CI.
        assert await wait_for_mc_server_ready(
            {"host": proxy_host, "port": java_proxy_port, "type": "java"},
            timeout=240,
        ), "Server did not become query-ready during pre-warming."
        print("(Chaos Test) Server is confirmed to be running and ready.")
        await asyncio.sleep(2)
    except Exception as e:
        pytest.fail(f"Chaos test pre-warming failed: {e}")
    finally:
        if "pre_warm_socket" in locals() and pre_warm_socket.fileno() != -1:
            pre_warm_socket.close()

    # Step 2: Establish the actual "victim" session to be tested.
    victim_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        victim_socket.connect((proxy_host, java_proxy_port))
        print("(Chaos Test) Victim client connected, session established.")

        assert await wait_for_log_message(
            docker_client_fixture,
            nether_bridge_container_name,
            "Establishing new TCP session for running server",
            timeout=30,
        ), "Proxy did not log the establishment of the victim's TCP session."
        print("(Chaos Test) Proxy session is active.")

        # Step 4: Forcibly kill the server container.
        print(f"(Chaos Test) Forcibly killing container: {mc_java_container_name}")
        container = await docker_client_fixture.containers.get(mc_java_container_name)
        await container.kill()
        assert await wait_for_container_status(
            docker_client_fixture,
            mc_java_container_name,
            [False],  # Expect False (stopped)
        ), "Container did not stop after being killed."
        print("(Chaos Test) Container successfully killed.")

        await asyncio.sleep(1)  # Give a moment for the proxy to react

        # Step 5: Attempt to send data. This should trigger an error in proxy.
        try:
            print("(Chaos Test) Sending data to trigger proxy's error handling...")
            victim_socket.sendall(b"data_after_crash")
        except socket.error as e:
            print(f"(Chaos Test) Client socket error as expected: {e}")

        # Step 6: Assert that the PROXY detected the error and logged the cleanup.
        assert await wait_for_log_message(
            docker_client_fixture,
            nether_bridge_container_name,
            "Connection reset during data forwarding. Client disconnected.",
            timeout=10,
        ), "Proxy did not log the session cleanup after the container crash."

        print("(Chaos Test) Test passed: Proxy correctly handled the crashed session.")

    finally:
        if "victim_socket" in locals() and victim_socket.fileno() != -1:
            victim_socket.close()
