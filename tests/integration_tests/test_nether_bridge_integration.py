import os
import socket
import sys
import time

import docker
import pytest
from mcstatus import BedrockServer, JavaServer

# Add this to the top of the file to ensure imports work inside the container
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Constants for test server addresses and ports
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host():
    """
    Determines the correct IP address or hostname to target for integration tests.
    """
    # When running inside a Docker container (CI or local simulation),
    # use the service name, which Docker's internal DNS will resolve.
    if os.environ.get("CI_MODE"):
        return "nether-bridge"

    # When running tests against a remote Docker host from the local machine.
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]

    # Fallback for running tests against local Docker Desktop from the local machine.
    return "127.0.0.1"


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


def get_java_handshake_and_status_request_packets(host, port):
    """Constructs the two packets needed to request a status from a Java server."""
    server_address_bytes = host.encode("utf-8")
    handshake_payload = (
        encode_varint(754)
        + encode_varint(len(server_address_bytes))
        + server_address_bytes
        + port.to_bytes(2, byteorder="big")
        + encode_varint(1)
    )
    handshake_packet = (
        encode_varint(len(handshake_payload) + 1) + b"\x00" + handshake_payload
    )

    status_request_payload = b""
    status_request_packet = (
        encode_varint(len(status_request_payload) + 1)
        + b"\x00"
        + status_request_payload
    )

    return handshake_packet, status_request_packet


def wait_for_proxy_to_be_ready(docker_client_fixture, timeout=60):
    """
    Waits for the nether-bridge proxy to be fully initialized by watching its logs.
    """
    print("\nWaiting for nether-bridge proxy to be ready...")
    container = docker_client_fixture.containers.get("nether-bridge")

    if "Starting main proxy packet forwarding loop" in container.logs().decode("utf-8"):
        print("Proxy is already ready (found message in existing logs).")
        return True

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

    if message in container.logs().decode("utf-8"):
        print("  Found message in existing logs.")
        return True

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
def test_bedrock_server_starts_on_connection(docker_client_fixture):
    """
    Test that the mc-bedrock server starts when a connection attempt is made.
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
    assert initial_status in ["exited", "created"], (
        f"Bedrock server should be stopped, but is: {initial_status}"
    )
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

    finally:
        client_socket.close()

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
        {"host": proxy_host, "port": bedrock_proxy_port, "type": "bedrock"},
        timeout=180,
        interval=5,
    ), "Bedrock server did not become query-ready through proxy."


@pytest.mark.integration
def test_java_server_starts_on_connection(docker_client_fixture):
    """
    Test that the mc-java server starts when a connection attempt is made.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = get_container_status(docker_client_fixture, mc_java_container_name)
    assert initial_status in ["exited", "created"], (
        f"Java server should be stopped, but is: {initial_status}"
    )
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(
            f"Simulating connection to nether-bridge on port {java_proxy_port} "
            f"on host {proxy_host}..."
        )
        client_socket.connect((proxy_host, java_proxy_port))
        print(f"Successfully connected to {proxy_host}:{java_proxy_port}.")

        handshake_packet, status_request_packet = (
            get_java_handshake_and_status_request_packets(proxy_host, java_proxy_port)
        )
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")
    finally:
        client_socket.close()

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


@pytest.mark.integration
def test_server_shuts_down_on_idle(docker_client_fixture):
    """
    Tests that a running server is automatically stopped by the proxy after a period
    of inactivity.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"

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
        interval=2,
    ), "Bedrock server did not start after being triggered."
    print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")

    wait_duration = idle_timeout + (2 * check_interval) + 5
    print(f"Server is running. Waiting up to {wait_duration}s for idle shutdown...")

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Server idle with 0 sessions. Initiating shutdown.",
        timeout=wait_duration,
    ), "Proxy did not log that it was shutting down an idle server."

    assert wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        ["exited"],
        timeout=15,
        interval=2,
    ), "Bedrock container did not stop after proxy initiated shutdown."


@pytest.mark.integration
def test_proxy_restarts_crashed_server_on_new_connection(docker_client_fixture):
    """
    Tests that the proxy can detect a crashed/stopped server and restart it.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    print(f"\n(Crash Test) Triggering server '{mc_bedrock_container_name}' to start...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=180
    ), "Server did not start initially."
    print("(Crash Test) Server is running.")

    print(
        f"\n(Crash Test) Simulating crash of container '{mc_bedrock_container_name}'..."
    )
    container = docker_client_fixture.containers.get(mc_bedrock_container_name)
    container.stop(timeout=10)
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["exited"], timeout=30
    ), "Container did not stop after manual command."
    print("(Crash Test) Container successfully stopped.")

    time.sleep(2)

    print("\n(Crash Test) Attempting new connection to trigger restart...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First packet received for stopped server. Starting...",
        timeout=15,
    ), "Proxy did not log a restart attempt for the crashed server."

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=180
    ), "Crashed server did not restart successfully."

    print("\n(Crash Test) Successfully verified proxy can recover from a crash.")
