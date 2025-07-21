# tests/helpers.py
"""
Shared helper functions for unit, integration, and load tests.
"""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import aiodocker
import pytest
import requests
import structlog
from aiodocker.exceptions import DockerError

# --- Constants ---
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565
PROMETHEUS_PORT = 8000

log = structlog.get_logger()

# The raw packet for a Bedrock Edition "Unconnected Ping".
BEDROCK_UNCONNECTED_PING = (
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
    b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
)


def add_project_root_to_path():
    """Adds the project root to the Python path for module resolution."""
    project_root = Path(__file__).parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


add_project_root_to_path()
# Now that path is set, we can import from mcstatus
from mcstatus import BedrockServer, JavaServer  # noqa: E402


def get_proxy_host():
    """
    Determines the correct IP address or hostname for the proxy based on the
    test environment.
    """
    if os.environ.get("CI_MODE"):
        return "nether-bridge"
    if os.environ.get("DOCKER_HOST_IP"):
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
        b"\x00"  # Protocol version (0 for status)
        + encode_varint(754)  # Minecraft version
        + encode_varint(len(server_address_bytes))
        + server_address_bytes
        + port.to_bytes(2, byteorder="big")
        + encode_varint(1)  # Next state: status
    )
    handshake_packet = encode_varint(len(handshake_payload)) + handshake_payload

    status_request_payload = b"\x00"  # Empty payload for status request
    status_request_packet = (
        encode_varint(len(status_request_payload)) + status_request_payload
    )

    return handshake_packet, status_request_packet


async def get_active_sessions_metric(proxy_host: str, server_name: str) -> int:
    """
    Queries the proxy's /metrics endpoint and returns the number of active
    sessions for a specific server.
    """
    try:
        metrics_url = f"http://{proxy_host}:{PROMETHEUS_PORT}/metrics"
        response = await asyncio.to_thread(requests.get, metrics_url, timeout=5)
        response.raise_for_status()

        pattern = re.compile(
            rf'netherbridge_active_connections{{server="{server_name}"}}\s+([0-9\.]+)'
        )
        match = pattern.search(response.text)

        if match:
            value_str = match.group(1)
            return int(float(value_str))
        return 0
    except (requests.RequestException, ValueError) as e:
        pytest.fail(f"Could not retrieve or parse active sessions metric: {e}")


async def wait_for_container_status(
    docker_client: aiodocker.Docker,
    container_name: str,
    target_status: str,
    timeout: int = 120,
) -> bool:
    """Waits for a container to enter a target status."""
    start_time = time.time()
    log.info(
        f"Waiting for '{container_name}' to be '{target_status}'...",
        timeout=timeout,
    )
    while time.time() - start_time < timeout:
        try:
            container = await docker_client.containers.get(container_name)
            info = await container.show()
            current_status = info["State"]["Status"]
            log.debug(f"Status of '{container_name}': {current_status}")
            if current_status == target_status:
                log.info(
                    f"Container '{container_name}' reached '{target_status}'.",
                )
                return True
        except DockerError as e:
            if e.status == 404 and target_status == "exited":
                log.info(f"Container '{container_name}' not found, assuming exited.")
                return True
        await asyncio.sleep(2)
    log.error(f"Timeout waiting for '{container_name}' to be '{target_status}'.")
    return False


async def wait_for_mc_server_ready(
    server_type: str,
    host: str,
    port: int,
    timeout: int = 120,
    interval: int = 2,
    initial_delay: int = 5,
):
    """
    Waits for a Minecraft server to become query-ready via mcstatus,
    with an initial delay to allow the server to initialize.
    """
    start_time = time.time()
    log.info(
        f"Waiting for {server_type} server at {host}:{port} to be ready...",
    )

    if initial_delay > 0:
        log.debug("Initial delay before polling...", delay=initial_delay)
        await asyncio.sleep(initial_delay)

    while time.time() - start_time < timeout:
        try:
            lookup_str = f"{host}:{port}"
            if server_type == "java":
                server = await JavaServer.async_lookup(lookup_str, timeout=interval)
            else:
                server = await asyncio.to_thread(
                    BedrockServer.lookup, lookup_str, timeout=interval
                )
            status = await server.async_status()
            log.info(
                f"Server at {host}:{port} is ready!",
                latency=f"{status.latency:.2f}ms",
            )
            return True
        except Exception:
            log.debug(f"Server at {host}:{port} not ready, retrying...")
        await asyncio.sleep(interval)

    log.error(f"Timeout waiting for {host}:{port} to be ready.")
    return False


async def wait_for_log_message(
    docker_client: aiodocker.Docker,
    container_name: str,
    message: str,
    timeout: int = 30,
) -> bool:
    """Waits for a specific message to appear in a container's logs."""
    log.info(f"Waiting for message in '{container_name}': '{message}'...")
    try:
        container = await docker_client.containers.get(container_name)
        async for line in container.log(
            stdout=True, stderr=True, follow=True, since=int(time.time())
        ):
            if message in line:
                log.info(f"Found message in '{container_name}'.")
                return True
            if time.time() - timeout > 0:
                log.error(f"Timeout waiting for message in '{container_name}'.")
                return False
    except asyncio.TimeoutError:
        log.error(f"Timeout waiting for message in '{container_name}'.")
    except Exception as e:
        log.error(f"Error reading logs for '{container_name}': {e}")
    return False


async def check_port_listening(
    host: str, port: int, protocol: str = "tcp", timeout: int = 5
) -> bool:
    """
    Asynchronously checks if a specified TCP or UDP port is actively listening.
    """
    try:
        if protocol == "tcp":
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
        else:  # udp
            loop = asyncio.get_running_loop()
            transport, _ = await asyncio.wait_for(
                loop.create_datagram_endpoint(
                    lambda: asyncio.DatagramProtocol(), remote_addr=(host, port)
                ),
                timeout=timeout,
            )
            transport.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False
