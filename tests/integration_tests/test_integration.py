# tests/integration_tests/test_integration.py
"""
Integration tests for the Nether-bridge proxy.
These tests use live Docker containers to verify end-to-end functionality.
"""

import asyncio
import os
import sys

import pytest
import pytest_asyncio
from mcstatus import BedrockServer, JavaServer

# Correctly add the project root to the path for reliable imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config import load_app_config
from docker_manager import DockerManager
from tests.helpers import check_port_listening, wait_for_container_status

# Mark all tests in this module as asyncio and integration
pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(scope="module")
async def app_config():
    """Provides the application configuration for integration tests."""
    return load_app_config()


@pytest_asyncio.fixture(scope="module")
async def docker_manager(app_config):
    """Provides an async DockerManager instance for the integration test module."""
    manager = DockerManager(app_config)
    yield manager
    await manager.close()


async def test_java_server_lifecycle(
    docker_manager: DockerManager, app_config, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Java server: on-demand start and idle stop.
    """
    server_config = next(s for s in app_config.game_servers if s.name == "mc-java")
    container_name = server_config.container_name

    # 1. Initial State: Ensure container is stopped
    assert not await docker_manager.is_container_running(container_name), (
        "Java container should be initially stopped."
    )

    # 2. Trigger Server Start: A client connection starts the server
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", server_config.proxy_port),
            timeout=10,
        )
        writer.close()
        await writer.wait_closed()
    except (ConnectionRefusedError, asyncio.TimeoutError):
        pass  # Expected as the server is spinning up

    # 3. Verify Server Started
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Java container failed to start."

    assert await check_port_listening("127.0.0.1", server_config.proxy_port), (
        "Proxy port for Java server is not listening."
    )

    # 4. Verify Server is Queryable
    server = JavaServer.lookup(f"127.0.0.1:{server_config.port}")
    status = await server.async_status()
    assert status.version.name, "Could not query the started Java server."
    print(f"Java server started with {status.players.online} players.")

    # 5. Verify Auto-Stop
    print("Waiting for Java server to auto-stop due to idle timeout...")
    idle_plus_buffer = server_config.stop_after_idle + 45  # Add more buffer
    await asyncio.sleep(idle_plus_buffer)

    assert not await docker_manager.is_container_running(container_name), (
        "Java container did not stop after the idle timeout."
    )
    print("Java server stopped successfully.")


async def test_bedrock_server_lifecycle(
    docker_manager: DockerManager, app_config, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Bedrock server: on-demand start and idle stop.
    """
    server_config = next(s for s in app_config.game_servers if s.name == "mc-bedrock")
    container_name = server_config.container_name

    # 1. Initial State: Ensure container is stopped
    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container should be initially stopped."
    )

    # 2. Trigger Server Start: A client ping starts the server
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: asyncio.DatagramProtocol(),
        remote_addr=("127.0.0.1", server_config.proxy_port),
    )
    transport.sendto(b"\x01\x00\x00\x00\x00\x01\x23\x45\x67\x89\xab\xcd\xef")
    transport.close()

    # 3. Verify Server Started
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Bedrock container failed to start."

    # 4. Verify Server is Queryable
    server = await BedrockServer.async_lookup(f"127.0.0.1:{server_config.port}")
    status = await server.async_status()
    assert status.version.name, "Could not query the started Bedrock server."
    print(f"Bedrock server started with {status.players.online} players.")

    # 5. Verify Auto-Stop
    print("Waiting for Bedrock server to auto-stop due to idle timeout...")
    idle_plus_buffer = server_config.stop_after_idle + 45  # Add more buffer
    await asyncio.sleep(idle_plus_buffer)

    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container did not stop after the idle timeout."
    )
    print("Bedrock server stopped successfully.")
