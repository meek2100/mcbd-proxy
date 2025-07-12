# tests/helpers.py

import asyncio
import os
import re
import time
from typing import List, Literal, Union

import aiodocker
import pytest
import requests
from mcstatus import BedrockServer, JavaServer

# --- Constants ---
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565

# The raw packet for a Bedrock Edition "Unconnected Ping".
BEDROCK_UNCONNECTED_PING = (
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
    b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
)


def get_proxy_host():
    """
    Determines the correct IP address or hostname for the proxy.
    It checks the environment in a specific order of precedence to support
    different testing scenarios (in-container, remote host, CI).
    """
    # 1. Check if running inside the test environment's container network.
    if os.environ.get("NB_TEST_MODE") == "container":
        return "nether-bridge"  #

    # 2. Check for CI mode (also uses service name).
    if os.environ.get("CI_MODE"):
        return "nether-bridge"  #

    # 3. Check for an explicit IP for remote testing via pytest.
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]  #
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]  #

    # 4. Fallback for local `pytest` runs without special config.
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
        encode_varint(754)  # Protocol version (1.16.5, adjust as needed)
        + encode_varint(len(server_address_bytes))
        + server_address_bytes
        + port.to_bytes(2, byteorder="big")
        + encode_varint(1)  # Next state: status
    )
    handshake_packet = (
        encode_varint(len(handshake_payload)) + b"\x00" + handshake_payload
    )

    status_request_payload = b""
    status_request_packet = (
        encode_varint(len(status_request_payload)) + b"\x00" + status_request_payload
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

        pattern = re.compile(
            rf'netherbridge_active_sessions{{server_name="{server_name}"}}\s+([0-9\.]+)'
        )
        match = pattern.search(response.text)

        if match:
            value_str = match.group(1)
            return int(float(value_str))

        return 0
    except (requests.RequestException, ValueError) as e:
        pytest.fail(f"Could not retrieve or parse active sessions metric: {e}")


async def get_container_status(
    docker_client: aiodocker.Docker, container_name: str
) -> Union[bool, Literal["not_found"]]:
    """
    Retrieves the current running status of a Docker container.
    Returns True if running, False if stopped/exited, "not_found" if not found.
    """
    try:
        container = await docker_client.containers.get(container_name)
        info = await container.show()
        return info["State"]["Running"]
    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            return "not_found"
        pytest.fail(f"Docker error getting status for {container_name}: {e}")
    except Exception as e:
        pytest.fail(f"Failed to get status for container {container_name}: {e}")


async def wait_for_container_status(
    docker_client: aiodocker.Docker,
    container_name: str,
    target_status: Union[
        bool, Literal["not_found"], List[Union[bool, Literal["not_found"]]]
    ],
    timeout: int = 240,
    interval: int = 5,
) -> bool:
    """
    Waits for a container to enter one of a list of target statuses.
    `target_status` can be True (running), False (stopped), or "not_found".
    """
    if not isinstance(target_status, list):
        target_status = [target_status]

    start_time = time.time()
    print(
        f"Waiting for container '{container_name}' to reach status in "
        f"{target_status} (max {timeout}s)..."
    )
    while time.time() - start_time < timeout:
        current_status = await get_container_status(docker_client, container_name)
        print(f"  Current status of '{container_name}': {current_status}")
        if current_status in target_status:
            print(
                f"  Container '{container_name}' reached desired status: "
                f"{current_status}"
            )
            return True
        await asyncio.sleep(interval)
    current_status = await get_container_status(docker_client, container_name)
    print(
        f"Timeout waiting for container '{container_name}' to reach status in "
        f"{target_status}. Current: {current_status}"
    )
    return False


async def wait_for_mc_server_ready(
    server_config: dict, timeout: int = 60, interval: int = 1
) -> bool:
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
                server = await BedrockServer.async_lookup(
                    f"{host}:{port}", timeout=interval
                )
                status = await server.async_status()
            elif server_type == "java":
                server = await JavaServer.async_lookup(
                    f"{host}:{port}", timeout=interval
                )
                status = await server.async_status()

            if status:
                print(
                    f"[{server_type}@{host}:{port}] Server responded! "
                    f"Latency: {status.latency:.2f}ms. "
                    f"Online players: {status.players.online}"
                )
                return True
        except Exception:
            # Continue retrying on connection errors or timeouts
            pass
        await asyncio.sleep(interval)
    print(f"[{server_type}@{host}:{port}] Timeout waiting for server to be ready.")
    return False


async def wait_for_proxy_to_be_ready(
    docker_client: aiodocker.Docker, timeout: int = 60
) -> bool:
    """
    Waits for the nether-bridge proxy to be fully initialized by watching its logs.
    """
    print("\nWaiting for nether-bridge proxy to be ready...")
    container = await docker_client.containers.get("nether-bridge")

    start_time = time.time()
    # Use since=int(start_time) to only stream new logs after this point
    async for line_bytes in container.logs(
        stream=True, follow=True, since=int(start_time)
    ):
        decoded_line = line_bytes.decode("utf-8").strip()
        print(f"  [proxy log]: {decoded_line}")
        if "Starting Nether-bridge On-Demand Proxy Async" in decoded_line:
            print("Proxy is now ready.")
            return True
        if time.time() - start_time > timeout:
            print("Timeout waiting for proxy to become ready (log message).")
            return False
    return False


async def wait_for_log_message(
    docker_client: aiodocker.Docker,
    container_name: str,
    message: str,
    timeout: int = 30,
) -> bool:
    """
    Waits for a specific message to appear in a container's logs.
    """
    container = await docker_client.containers.get(container_name)
    start_time = time.time()

    print(f"\nWaiting for message in '{container_name}' logs: '{message}'...")

    # Check existing logs first in case the message already passed
    existing_logs = (await container.logs()).decode("utf-8")
    if message in existing_logs:
        print("  Found message in existing logs.")
        return True

    # Stream new logs
    async for line_bytes in container.logs(
        stream=True, follow=True, since=int(start_time)
    ):
        decoded_line = line_bytes.decode("utf-8").strip()
        print(f"  [log]: {decoded_line}")
        if message in decoded_line:
            print("  Found message.")
            return True
        if time.time() - start_time > timeout:
            print("  Timeout waiting for message.")
            return False
    return False
