# tests/helpers.py
import asyncio
import os

import docker
import pytest
from mcstatus import BedrockServer, JavaServer

# --- Constants ---
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host():
    """Determines the correct IP address or hostname for the proxy."""
    return os.environ.get("DOCKER_HOST_IP", "127.0.0.1")


def get_java_handshake_and_status_request_packets(host, port):
    """Constructs the two packets needed to request a status from a Java server."""

    # This remains a synchronous utility function
    def encode_varint(value):
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

    server_address = host.encode("utf-8")
    handshake_payload = (
        b"\x00"  # Handshake packet ID
        + encode_varint(754)  # Protocol version
        + encode_varint(len(server_address))
        + server_address
        + port.to_bytes(2, "big")
        + b"\x01"  # Next state: status
    )
    handshake_packet = encode_varint(len(handshake_payload)) + handshake_payload
    status_request_packet = encode_varint(1) + b"\x00"  # Status request packet

    return handshake_packet, status_request_packet


async def get_container_status(docker_client, container_name):
    """Asynchronously retrieves the current status of a Docker container."""
    try:
        container = await asyncio.to_thread(
            docker_client.containers.get, container_name
        )
        return container.status
    except docker.errors.NotFound:
        return "not_found"


async def wait_for_container_status(
    docker_client, container_name, target_status, timeout=120
):
    """Waits for a container to enter a target status."""
    for _ in range(timeout):
        status = await get_container_status(docker_client, container_name)
        if status == target_status:
            return True
        await asyncio.sleep(1)
    return False


async def wait_for_mc_server_ready(server_info, timeout=120):
    """Waits for a Minecraft server to become queryable."""
    host = server_info["host"]
    port = server_info["port"]
    server_type = server_info["type"]
    ServerClass = JavaServer if server_type == "java" else BedrockServer

    for _ in range(timeout):
        try:
            lookup = await asyncio.to_thread(ServerClass.lookup, f"{host}:{port}")
            await lookup.async_status()
            return True
        except Exception:
            await asyncio.sleep(1)
    return False


async def wait_for_log_message(docker_client, container_name, message, timeout=30):
    """Waits for a specific message to appear in a container's logs."""
    try:
        container = await asyncio.to_thread(
            docker_client.containers.get, container_name
        )
        for _ in range(timeout):
            logs = await asyncio.to_thread(container.logs, tail=50)
            if message in logs.decode("utf-8"):
                return True
            await asyncio.sleep(1)
    except docker.errors.NotFound:
        pytest.fail(f"Container '{container_name}' not found while waiting for logs.")
    return False


async def wait_for_proxy_to_be_ready(docker_client_fixture, timeout=60):
    """
    Waits for the nether-bridge proxy to be fully initialized by watching its logs.
    """
    return await wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Proxy server listening on",
        timeout,
    )
