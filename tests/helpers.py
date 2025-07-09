"""
Shared helper functions for unit, integration, and load tests.
"""

import os
import re
import time

import docker
import pytest
import requests
from mcstatus import BedrockServer, JavaServer  # <-- ADD THIS IMPORT

# --- ADD THESE CONSTANTS ---
# Constants for test server addresses and ports, now centralized.
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host():
    """
    Determines the correct IP address or hostname for integration tests
    by checking the environment in a specific order of precedence.
    """
    if os.environ.get("CI_MODE"):
        return "nether-bridge"
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]
    return "127.0.0.1"


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


def get_active_sessions_metric(proxy_host: str, server_name: str) -> int:
    """
    Queries the proxy's /metrics endpoint and returns the number of active sessions.
    """
    try:
        metrics_url = f"http://{proxy_host}:8000/metrics"
        response = requests.get(metrics_url, timeout=5)
        response.raise_for_status()

        # Use a more robust regex that can find integer or float values.
        pattern = re.compile(
            rf'netherbridge_active_sessions{{server_name="{server_name}"}}\s+([0-9\.]+)'
        )
        match = pattern.search(response.text)

        if match:
            # --- FIX: Convert the value to a float first, then to an int ---
            value_str = match.group(1)
            return int(float(value_str))

        # If the metric is not found, it means there are 0 sessions for that server.
        return 0
    except (requests.RequestException, ValueError) as e:
        pytest.fail(f"Could not retrieve or parse active sessions metric: {e}")


# --- ADD ALL THE HELPERS FROM test_integration.py BELOW ---


def get_container_status(docker_client_fixture, container_name):
    """
    Retrieves the current status of a Docker container.
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
