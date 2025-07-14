import asyncio

import pytest
from mcstatus import BedrockServer, JavaServer

from tests.helpers import get_proxy_host

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

# --- Constants ---
JAVA_PORT = 25565
BEDROCK_PORT = 19132


@pytest.mark.asyncio
async def test_java_server_starts_on_connection(docker_client_fixture):
    """
    Tests that a Java server container starts when a client probes its status.
    """
    proxy_host = get_proxy_host()
    server = JavaServer.lookup(f"{proxy_host}:{JAVA_PORT}")

    # First attempt should trigger the server start and is expected to time out
    try:
        await server.async_status(timeout=5)
    except (asyncio.TimeoutError, ConnectionRefusedError):
        print(
            "Initial connection timed out as expected. Waiting for server to start..."
        )
        await asyncio.sleep(20)  # Generous wait time for the server to spin up

    # Second attempt should succeed
    status = await server.async_status(timeout=15)
    assert status.version.name is not None, "Server should have a version name."
    assert status.players.online >= 0, "Player count should be a number."


@pytest.mark.asyncio
async def test_bedrock_server_starts_on_connection(docker_client_fixture):
    """
    Tests that a Bedrock server container starts when a client probes its status.
    """
    proxy_host = get_proxy_host()
    server = BedrockServer.lookup(f"{proxy_host}:{BEDROCK_PORT}")

    # First attempt is expected to time out
    try:
        await server.async_status(timeout=5)
    except asyncio.TimeoutError:
        print(
            "Initial connection timed out as expected. Waiting for server to start..."
        )
        await asyncio.sleep(20)

    # Second attempt should succeed
    status = await server.async_status(timeout=15)
    assert status.version.brand == "BEDROCK", "Server should be Bedrock."
    assert status.players.online >= 0, "Player count should be a number."


@pytest.mark.asyncio
async def test_server_shuts_down_on_idle(docker_client_fixture):
    """
    Tests that a running server is automatically stopped by the proxy after a
    period of inactivity. (Based on NB_IDLE_TIMEOUT=30 in compose file).
    """
    proxy_host = get_proxy_host()
    server = JavaServer.lookup(f"{proxy_host}:{JAVA_PORT}")
    container_name = "mc-java"

    # 1. Start the server by probing it
    try:
        await server.async_status(timeout=5)
    except (asyncio.TimeoutError, ConnectionRefusedError):
        await asyncio.sleep(15)  # Wait for it to start

    # Confirm it's running
    status = await server.async_status(timeout=15)
    assert status.version.name is not None, "Server did not start correctly."
    print(f"\nServer '{container_name}' is running. Now waiting for idle shutdown...")

    # 2. Wait for a duration longer than the idle timeout
    await asyncio.sleep(45)  # NB_IDLE_TIMEOUT is 30s, this gives a buffer

    # 3. Verify the container has been stopped
    container = docker_client_fixture.containers.get(container_name)
    container.reload()
    assert container.status == "exited", "Proxy did not stop the idle server."
    print(f"Server '{container_name}' correctly stopped due to idle timeout.")


@pytest.mark.asyncio
async def test_proxy_restarts_crashed_server(docker_client_fixture):
    """
    Tests that the proxy will re-start a server that has been stopped
    or has crashed.
    """
    proxy_host = get_proxy_host()
    server = JavaServer.lookup(f"{proxy_host}:{JAVA_PORT}")
    container_name = "mc-java"

    # 1. Start the server
    try:
        await server.async_status(timeout=5)
    except (asyncio.TimeoutError, ConnectionRefusedError):
        await asyncio.sleep(15)
    await server.async_status(timeout=15)
    print(f"\nServer '{container_name}' started successfully.")

    # 2. Manually stop the container to simulate a crash
    container = docker_client_fixture.containers.get(container_name)
    container.stop(timeout=10)
    print(f"Manually stopped container '{container_name}' to simulate crash.")
    await asyncio.sleep(5)

    # 3. Attempt a new connection and verify it restarts
    try:
        await server.async_status(timeout=5)
    except (asyncio.TimeoutError, ConnectionRefusedError):
        print("Connection timed out as expected on restart. Waiting...")
        await asyncio.sleep(15)

    status = await server.async_status(timeout=15)
    assert status.version.name is not None, (
        "Server should have restarted and be available."
    )
    print("Proxy correctly restarted the crashed server on a new connection.")
