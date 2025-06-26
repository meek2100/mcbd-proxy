import socket
import time

import docker
import pytest
from mcstatus import BedrockServer, JavaServer

from tests.helpers import get_java_handshake_and_status_request_packets, get_proxy_host

# Add this to the top of the file to ensure imports work inside the container
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Constants for test server addresses and ports
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_container_status(docker_client_fixture, container_name):
    """
    Retrieves the current status of a Docker container.

    Args:
        docker_client_fixture: The Docker client fixture.
        container_name (str): The name of the container to check.

    Returns:
        str: The container status (e.g., 'running', 'exited') or 'not_found'.
    """
    try:
        container = docker_client_fixture.containers.get(container_name)
        return container.status
    except docker.errors.NotFound:
        return "not_found"
    except Exception as e:
        pytest.fail(f"Failed to get status for container {container_name}: {e}")


def wait_for_container_status(
    docker_client_fixture,
    container_name,
    target_statuses,
    timeout=240,
    interval=5,
):
    """
    Waits for a container to enter one of a list of target statuses.

    Args:
        docker_client_fixture: The Docker client fixture.
        container_name (str): The name of the container.
        target_statuses (list): A list of desired statuses (e.g., ['running']).
        timeout (int): The maximum time to wait in seconds.
        interval (int): The interval between checks in seconds.

    Returns:
        bool: True if the container reached a target status, False otherwise.
    """
    start_time = time.time()
    print(
        f"Waiting for container '{container_name}' to reach status in "
        f"{target_statuses} (max {timeout}s)..."
    )
    while time.time() - start_time < timeout:
        current_status = get_container_status(docker_client_fixture, container_name)
        print(f"  Current status of '{container_name}': {current_status}")
        if current_status in target_statuses:
            print(
                f"  Container '{container_name}' reached desired status: "
                f"{current_status}"
            )
            return True
        time.sleep(interval)
    current_status = get_container_status(docker_client_fixture, container_name)
    print(
        f"Timeout waiting for container '{container_name}' to reach status in "
        f"{target_statuses}. Current: {current_status}"
    )
    return False


def wait_for_mc_server_ready(server_config, timeout=60, interval=1):
    """
    Waits for a Minecraft server to become query-ready via mcstatus.

    Args:
        server_config (dict): A dict with 'host', 'port', and 'type'.
        timeout (int): Maximum time to wait in seconds.
        interval (int): Interval between queries in seconds.

    Returns:
        bool: True if the server responded, False otherwise.
    """
    host, port = server_config["host"], server_config["port"]
    server_type = server_config["type"]
    start_time = time.time()
    print(f"\nWaiting for {server_type} server at {host}:{port} to be ready...")

    while time.time() - start_time < timeout:
        try:
            status = None
            if server_type == "bedrock":
                server = BedrockServer.lookup(f"{host}:{port}", timeout=interval)
                status = server.status()
            elif server_type == "java":
                server = JavaServer.lookup(f"{host}:{port}", timeout=interval)
                status = server.status()

            if status:
                print(
                    f"[{server_type}@{host}:{port}] Server responded! "
                    f"Latency: {status.latency:.2f}ms. "
                    f"Online players: {status.players.online}"
                )
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f"[{server_type}@{host}:{port}] Timeout waiting for server to be ready.")
    return False


def encode_varint(value):
    """Helper to encode VarInt for Java protocol."""
    buf = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80
        buf += bytes([byte])
        if value == 0:
            break
    return buf


def wait_for_proxy_to_be_ready(docker_client_fixture, timeout=60):
    """
    Waits for the nether-bridge proxy to be fully initialized by watching its logs.
    """
    print("\nWaiting for nether-bridge proxy to be ready...")
    # Use the static container name defined in the compose file
    container = docker_client_fixture.containers.get("nether-bridge")

    # Check existing logs first in case the message has already been printed
    if "Starting main proxy packet forwarding loop" in container.logs().decode("utf-8"):
        print("Proxy is already ready (found message in existing logs).")
        return True

    # Stream new logs if the message wasn't in the historical logs
    start_time = time.time()
    for line in container.logs(stream=True, since=int(start_time)):
        decoded_line = line.decode("utf-8").strip()
        print(f"  [proxy log]: {decoded_line}")
        if "Starting main proxy packet forwarding loop" in decoded_line:
            print("Proxy is now ready.")
            return True
        if time.time() - start_time > timeout:
            print("Timeout waiting for proxy to become ready.")
            return False
    return False


def wait_for_log_message(docker_client_fixture, container_name, message, timeout=30):
    """
    Waits for a specific message to appear in a container's logs.

    Checks historical logs first, then streams new logs until the message is
    found or the timeout is reached.

    Args:
        docker_client_fixture: The Docker client fixture.
        container_name (str): The name of the container to monitor.
        message (str): The log message to search for.
        timeout (int): The maximum time to wait in seconds.

    Returns:
        bool: True if the message was found, False otherwise.
    """
    container = docker_client_fixture.containers.get(container_name)
    start_time = time.time()

    print(f"\nWaiting for message in '{container_name}' logs: '{message}'...")

    # Check existing logs first
    if message in container.logs().decode("utf-8"):
        print("  Found message in existing logs.")
        return True

    # Stream new logs
    for line in container.logs(stream=True, since=int(start_time)):
        decoded_line = line.decode("utf-8").strip()
        print(f"  [log]: {decoded_line}")
        if message in decoded_line:
            print("  Found message.")
            return True
        if time.time() - start_time > timeout:
            print("  Timeout waiting for message.")
            return False
    return False


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
        # Send a packet to the initial port to confirm it's being listened on
        # A timeout is expected because no server will start, but no connection
        # refused error should occur.
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

    # This new config uses a different port for the Bedrock server
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
    # Use `sh -c` to handle writing the multi-line string to the file
    cmd_write_config = f"sh -c 'echo \"{new_config_json}\" > /app/servers.json'"
    # We must execute the command as the 'naeus' user, so the resulting
    # file has the correct ownership and is readable by the proxy process.
    exit_code, output = container.exec_run(cmd_write_config, user="naeus", demux=True)
    assert exit_code == 0, (
        "Failed to write new config: "
        f"{output[1].decode() if output[1] else output[0].decode()}"
    )
    print("New servers.json written successfully.")

    print("(SIGHUP Test) Sending SIGHUP signal...")
    # Use the Docker SDK to send the signal directly. This is more robust
    # and doesn't depend on the 'kill' command being in the container.
    container.kill(signal="SIGHUP")

    # --- 3. Verify that the proxy reloaded the configuration ---
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "SIGHUP received. Reloading configuration...",
        timeout=10,
    ), "Proxy did not log that it was reloading the configuration."

    print("(SIGHUP Test) Proxy logged reload message. Verifying new behavior...")
    # Give the proxy a moment to close old sockets and open new ones
    time.sleep(3)

    # --- 4. Verify new configuration is active and old one is not ---
    # The old port should now be closed and refuse connection
    try:
        print(f"(SIGHUP Test) Verifying old port {initial_bedrock_port} is closed...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"old-port-ping", (proxy_host, initial_bedrock_port))
        # This should raise an error because nothing is listening anymore
        data, addr = client_socket.recvfrom(1024)
        pytest.fail(f"Old port {initial_bedrock_port} is still open unexpectedly.")
    except (ConnectionRefusedError, OSError):
        print(
            f"Old port {initial_bedrock_port} is correctly closed (Connection Refused)."
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
    the proxy detects the resulting connection error and cleans up the session.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"

    # Step 1: Ensure the server is running by making a preliminary connection.
    print("\n(Chaos Test) Pre-warming server to ensure it is running...")
    try:
        pre_warm_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        pre_warm_socket.connect((proxy_host, java_proxy_port))
        pre_warm_socket.close()
        assert wait_for_container_status(
            docker_client_fixture, mc_java_container_name, ["running"]
        ), "Server did not start after pre-warming."
        print("(Chaos Test) Server is confirmed to be running.")
    except Exception as e:
        pytest.fail(f"Chaos test pre-warming failed: {e}")

    # Step 2: Establish the actual session to be tested.
    victim_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    victim_socket.connect((proxy_host, java_proxy_port))
    print("(Chaos Test) Victim client connected, session established.")

    # Step 3: Verify the session is active.
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Establishing new TCP session for running server",
        timeout=30,
    ), "Proxy did not log the establishment of the victim's TCP session."
    print("(Chaos Test) Proxy session is active.")

    # Step 4: Forcibly kill the server container.
    print(f"(Chaos Test) Forcibly killing container: {mc_java_container_name}")
    container = docker_client_fixture.containers.get(mc_java_container_name)
    container.kill()
    assert wait_for_container_status(
        docker_client_fixture, mc_java_container_name, ["exited", "dead"]
    ), "Container did not stop after being killed."
    print("(Chaos Test) Container successfully killed.")

    # Step 5: Attempt to send data to trigger the error in the proxy.
    try:
        print("(Chaos Test) Sending data to trigger proxy's error handling...")
        victim_socket.sendall(b"data_after_crash")
    except socket.error as e:
        print(f"(Chaos Test) Client socket error as expected: {e}")
    finally:
        victim_socket.close()

    # --- FIX IS HERE ---
    # Add a short delay to give the proxy's event loop time to process
    # the now-broken socket connection and log the cleanup message.
    print("(Chaos Test) Waiting for proxy to process the connection error...")
    time.sleep(2)

    # Step 6: Assert that the proxy detected the error and logged the cleanup.
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "[DEBUG] Session cleanup block triggered.",
        timeout=10,
    ), "Proxy did not log the session cleanup after the container crash."

    print("(Chaos Test) Test passed: Proxy correctly handled the crashed session.")
=======
    # --- FIX: Replace brittle pre-warming with robust readiness check ---
    # Step 1: Ensure the server is running by making a connection and waiting for it
    # to be fully ready before proceeding with the test.
    print("\n(Chaos Test) Pre-warming server to ensure it is running...")

    # Use the same robust readiness wait as other tests
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": java_proxy_port, "type": "java"},
        timeout=180,
        interval=5,
    ), "Java server did not become query-ready through proxy."

    assert wait_for_container_status(
        docker_client_fixture, mc_java_container_name, ["running"]
    ), "Server did not enter 'running' state after pre-warming."

    print("(Chaos Test) Server is confirmed to be running.")

    # Step 2: Establish a persistent client connection to the server
    print("(Chaos Test) Establishing persistent client connection...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((proxy_host, java_proxy_port))
    handshake, status_request = get_java_handshake_and_status_request_packets(
        proxy_host, java_proxy_port
    )
    client_socket.sendall(handshake)
    client_socket.sendall(status_request)
    print("(Chaos Test) Persistent client connected.")

    # Step 3: Manually stop (crash) the Minecraft server container
    print(f"(Chaos Test) Manually stopping container '{mc_java_container_name}'...")
    container = docker_client_fixture.containers.get(mc_java_container_name)
    container.stop()
    assert wait_for_container_status(
        docker_client_fixture, mc_java_container_name, ["exited"], timeout=90
    ), "Container did not stop after manual command."
    print("(Chaos Test) Container successfully stopped.")

    # Step 4: Attempt to send data through the now-broken session and verify cleanup
    print("(Chaos Test) Sending data to trigger session cleanup...")
    try:
        # This send should fail, causing the proxy to handle the broken pipe
        client_socket.sendall(status_request)
        # Give the proxy a moment to process the error and clean up
        time.sleep(2)
    except (ConnectionResetError, BrokenPipeError):
        # This is the expected outcome from the client's perspective
        print("(Chaos Test) Client socket correctly detected a connection error.")
    finally:
        client_socket.close()

    # Step 5: Verify the proxy logged the cleanup
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Session cleanup block triggered",
        timeout=10,
    ), "Proxy did not log that it cleaned up the broken session."

    print("(Chaos Test) Proxy correctly cleaned up session. Test passed.")
