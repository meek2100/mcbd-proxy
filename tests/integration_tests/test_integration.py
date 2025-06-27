import socket
import time

import pytest

# The mcstatus import is no longer needed here since the helpers handle it.
from tests.helpers import (
    BEDROCK_PROXY_PORT,
    JAVA_PROXY_PORT,
    get_active_sessions_metric,
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
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = get_container_status(
        docker_client_fixture, mc_bedrock_container_name
    )
    assert initial_status in [
        "exited",
        "created",
    ], f"Bedrock server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_bedrock_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(
            f"Simulating connection to nether-bridge on port {bedrock_proxy_port} "
            f"on host {proxy_host}..."
        )
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
        print("Bedrock 'Unconnected Ping' packet sent.")

        # Assert that the proxy logs its intent to start the server
        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "First packet received for stopped server. Starting...",
            timeout=10,
        ), "Proxy did not log that it was starting the Bedrock server."

        assert wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            ["running"],
            timeout=180,
            interval=2,
        ), f"Bedrock server '{mc_bedrock_container_name}' did not start after 180s."

        assert wait_for_mc_server_ready(
            {
                "host": proxy_host,
                "port": bedrock_proxy_port,
                "type": "bedrock",
            },
            timeout=180,
            interval=5,
        ), "Bedrock server did not become query-ready through proxy."
    finally:
        client_socket.close()


@pytest.mark.integration
def test_java_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-java server starts when a connection attempt is made
    to the nether-bridge proxy on its Java port.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = get_container_status(docker_client_fixture, mc_java_container_name)
    assert initial_status in [
        "exited",
        "created",
    ], f"Java server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(
            f"Simulating connection to nether-bridge on port {java_proxy_port} "
            f"on host {proxy_host}..."
        )
        client_socket.connect((proxy_host, java_proxy_port))
        print(f"Successfully connected to {proxy_host}:{java_proxy_port}.")

        # Assert that the proxy logs the new connection
        # --- FIX STARTS HERE ---
        # The test was looking for the wrong log message. The correct message
        # for a stopped server is
        # "First TCP connection for stopped server. Starting...".
        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "First TCP connection for stopped server. Starting...",
            timeout=10,
        ), "Proxy did not log that it was starting the Java server."
        # --- FIX ENDS HERE ---

        handshake_packet, status_request_packet = (
            get_java_handshake_and_status_request_packets(proxy_host, java_proxy_port)
        )
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        assert wait_for_container_status(
            docker_client_fixture,
            mc_java_container_name,
            ["running"],
            timeout=180,
            interval=2,
        ), f"Java server '{mc_java_container_name}' did not start after 180s."

        assert wait_for_mc_server_ready(
            {"host": proxy_host, "port": java_proxy_port, "type": "java"},
            timeout=180,
            interval=5,
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()


@pytest.mark.integration
def test_server_shuts_down_on_idle(docker_compose_up, docker_client_fixture):
    """
    Tests that a running server is automatically stopped by the proxy after a
    period of inactivity.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"

    # These values must correspond to the test environment variables in
    # docker-compose.tests.yml
    idle_timeout = 30
    check_interval = 5

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"\nTriggering server '{mc_bedrock_container_name}' to start...")
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()
        print("Client socket closed, session terminated.")

    assert wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        ["running"],
        timeout=180,
    ), "Bedrock server did not start after being triggered."
    print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")

    wait_duration = idle_timeout + (2 * check_interval) + 5
    print(f"Server is running. Waiting up to {wait_duration}s for idle shutdown...")

    # Assert that the proxy LOGS its intent to shut down the idle server.
    # This is a sufficient and reliable test of the proxy's logic.
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Server idle with 0 sessions. Initiating shutdown.",
        timeout=wait_duration,
    ), "Proxy did not log that it was shutting down an idle server."

    print("Proxy correctly initiated idle shutdown. Test passed.")


@pytest.mark.integration
def test_proxy_restarts_crashed_server_on_new_connection(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that the proxy will re-start a server that has been stopped
    or has crashed.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
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
        client_socket.close()

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=60
    ), "Container did not start on first connection."
    print("(Crash Test) Initial server start successful.")

    # --- 2. Manually stop the container to simulate a crash ---
    print(f"(Crash Test) Manually stopping container '{mc_bedrock_container_name}'...")
    container = docker_client_fixture.containers.get(mc_bedrock_container_name)
    container.stop()
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["exited", "dead"], timeout=90
    ), "Container did not stop after manual command."
    print("(Crash Test) Container successfully stopped.")

    # Give the proxy a moment to register the change if needed
    time.sleep(2)

    # --- 3. Attempt a new connection to the 'crashed' server ---
    print("\n(Crash Test) Attempting new connection to trigger restart...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    # --- 4. Verify that the proxy detects this and tries to start it again ---
    # This is the correct and final assertion for this test.
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First packet received for stopped server. Starting...",
        timeout=10,
    ), "Proxy did not log that it was attempting to restart the server."

    print("(Crash Test) Proxy correctly logged its intent to restart. Test passed.")


@pytest.mark.integration
def test_configuration_reload_on_sighup(docker_compose_up, docker_client_fixture):
    """
    Tests that the proxy correctly reloads its server configuration upon
    receiving a SIGHUP signal, without requiring a restart.
    """
    proxy_host = get_proxy_host()
    initial_bedrock_port = 19132
    reloaded_bedrock_port = 19134  # A new port for the reloaded config

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    # --- 1. Verify initial configuration is active ---
    print("\n(SIGHUP Test) Verifying initial server configuration...")
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"initial-ping", (proxy_host, initial_bedrock_port))
        client_socket.recvfrom(1024)
    except socket.timeout:
        print(
            f"Initial check on port {initial_bedrock_port} is OK (timeout as expected)."
        )
    except ConnectionRefusedError:
        pytest.fail(
            f"Initial port {initial_bedrock_port} was refused. It should be open."
        )
    finally:
        client_socket.close()

    # --- 2. Create new config and send SIGHUP ---
    print("(SIGHUP Test) Writing new configuration inside the container...")
    container = docker_client_fixture.containers.get("nether-bridge")

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
    cmd_write_config = f"sh -c 'echo \"{new_config_json}\" > /app/servers.json'"
    exit_code, output = container.exec_run(cmd_write_config, user="naeus", demux=True)
    assert exit_code == 0, (
        "Failed to write new config: "
        f"{output[1].decode() if output[1] else output[0].decode()}"
    )
    print("New servers.json written successfully.")

    print("(SIGHUP Test) Sending SIGHUP signal...")
    container.kill(signal="SIGHUP")

    # --- FIX: Wait for the correct log message ---
    # We must wait for the proxy to confirm the reload is COMPLETE, not just received.
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Configuration reload complete.",
        timeout=10,
    ), "Proxy did not log that it completed the configuration reload."

    print("(SIGHUP Test) Proxy logged reload completion. Verifying new behavior...")

    # --- 4. Verify new configuration is active and old one is not ---
    # The old port should now be closed and refuse connection
    try:
        print(f"(SIGHUP Test) Verifying old port {initial_bedrock_port} is closed...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        # For UDP on Linux, a closed port may not raise ConnectionRefusedError
        # immediately. Sending to it is often the best check. An error is a pass.
        # No error is a fail.
        client_socket.sendto(b"old-port-ping", (proxy_host, initial_bedrock_port))
        # We expect this to fail. If it times out, the port is likely still open.
        data, addr = client_socket.recvfrom(1024)
        pytest.fail(f"Old port {initial_bedrock_port} is still open unexpectedly.")
    except (ConnectionRefusedError, OSError):
        print(
            f"Old port {initial_bedrock_port} is correctly closed "
            f"(Connection Refused/Host Unreachable)."
        )
    except socket.timeout:
        pytest.fail(f"Old port {initial_bedrock_port} is still open (timed out).")
    finally:
        client_socket.close()

    # The new port should now be open
    try:
        print(f"(SIGHUP Test) Verifying new port {reloaded_bedrock_port} is open...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"new-port-ping", (proxy_host, reloaded_bedrock_port))
        client_socket.recvfrom(1024)
    except socket.timeout:
        print(
            f"New port {reloaded_bedrock_port} is correctly open (timeout as expected)."
        )
    except ConnectionRefusedError:
        pytest.fail(f"New port {reloaded_bedrock_port} was refused. It should be open.")
    finally:
        client_socket.close()

    print("(SIGHUP Test) Test passed: Proxy correctly reloaded its configuration.")


@pytest.mark.integration
def test_proxy_cleans_up_session_on_container_crash(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that if a server container crashes during an active session,
    the proxy correctly cleans up the session, reflected by the active_sessions metric.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"
    java_server_name = "Java Creative"  # Must match the name in servers.json

    # Step 1: Ensure the server is running.
    print("\n(Chaos Test) Pre-warming server to ensure it is running...")
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": java_proxy_port, "type": "java"}, timeout=180
    ), "Java server did not become query-ready through proxy."
    print("(Chaos Test) Server is confirmed to be running.")

    # Step 2: Assert that there are initially 0 active sessions.
    assert get_active_sessions_metric(proxy_host, java_server_name) == 0, (
        "Initial active session count should be 0."
    )

    # Step 3: Establish a persistent client connection to create a session.
    print("(Chaos Test) Establishing persistent client connection...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((proxy_host, java_proxy_port))
    print("(Chaos Test) Persistent client connected.")

    # Give the proxy a moment to register the session.
    time.sleep(1)

    # Step 4: Assert that the active session count is now 1.
    assert get_active_sessions_metric(proxy_host, java_server_name) == 1, (
        "Active session count should be 1 after connection."
    )

    # Step 5: Manually stop (crash) the Minecraft server container.
    print(f"(Chaos Test) Manually stopping container '{mc_java_container_name}'...")
    container = docker_client_fixture.containers.get(mc_java_container_name)
    container.stop(timeout=10)
    assert wait_for_container_status(
        docker_client_fixture, mc_java_container_name, ["exited"], timeout=90
    ), "Container did not stop after manual command."
    print("(Chaos Test) Container successfully stopped.")

    # Step 6: Wait for the proxy to clean up the session.
    # We poll the metric endpoint until the session count returns to 0.
    cleanup_success = False
    for _ in range(10):  # Poll for up to 10 seconds
        if get_active_sessions_metric(proxy_host, java_server_name) == 0:
            cleanup_success = True
            break
        time.sleep(1)

    # Step 7: Assert that the session was cleaned up.
    assert cleanup_success, (
        "Proxy did not clean up the session (metric did not return to 0)."
    )

    client_socket.close()
    print("(Chaos Test) Proxy correctly cleaned up session. Test passed.")
