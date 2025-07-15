# tests/integration_tests/test_integration.py
"""
Integration tests for the Nether-bridge proxy.
These tests use live Docker containers to verify end-to-end functionality.
"""

import asyncio
import json
import os
import signal
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
    server_config = next(s for s in app_config.game_servers if s.game_type == "java")
    container_name = server_config.container_name

    # 1. Initial State: Ensure container is stopped
    assert not await docker_manager.is_container_running(container_name), (
        "Java container should be initially stopped."
    )

    # 2. Trigger Server Start
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", server_config.proxy_port),
            timeout=10,
        )
        writer.close()
        await writer.wait_closed()
    except (ConnectionRefusedError, asyncio.TimeoutError):
        pass  # Expected as the server spins up

    # 3. Verify Server Started and Queryable
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Java container failed to start."
    server = await JavaServer.async_lookup(f"127.0.0.1:{server_config.port}")
    await server.async_status()

    # 4. Verify Auto-Stop after idle
    idle_plus_buffer = app_config.idle_timeout + 15
    await asyncio.sleep(idle_plus_buffer)
    assert not await docker_manager.is_container_running(container_name), (
        "Java container did not stop after the idle timeout."
    )


async def test_bedrock_server_lifecycle(
    docker_manager: DockerManager, app_config, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Bedrock server: on-demand start and idle stop.
    """
    server_config = next(s for s in app_config.game_servers if s.game_type == "bedrock")
    container_name = server_config.container_name

    # 1. Initial State: Ensure container is stopped
    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container should be initially stopped."
    )

    # 2. Trigger Server Start with a ping
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: asyncio.DatagramProtocol(),
        remote_addr=("127.0.0.1", server_config.proxy_port),
    )
    ping_data = b"\x01\x00\x00\x00\x00\x01\x23\x45\x67\x89\xab\xcd\xef"
    transport.sendto(ping_data)
    transport.close()

    # 3. Verify Server Started and Queryable
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Bedrock container failed to start."
    server = await BedrockServer.async_lookup(f"127.0.0.1:{server_config.port}")
    await server.async_status()

    # 4. Verify Auto-Stop after idle
    idle_plus_buffer = app_config.idle_timeout + 15
    await asyncio.sleep(idle_plus_buffer)
    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container did not stop after the idle timeout."
    )


async def test_sighup_reloads_configuration(
    docker_manager: DockerManager, app_config, docker_compose_fixture
):
    """
    Tests that the proxy correctly reloads its configuration upon SIGHUP.
    """
    new_port = 19134  # A port not initially in use
    new_config = {
        "servers": [
            {
                "name": "Bedrock Reloaded",
                "game_type": "bedrock",
                "proxy_port": new_port,
                "container_name": "mc-bedrock",
                "port": 19132,
            }
        ]
    }

    # 1. Write the new config file inside the container
    async with docker_manager.get_container("nether-bridge") as container:
        assert container is not None, "nether-bridge container not found"
        await container.put_archive(
            "/app/",
            data=json.dumps(new_config).encode("utf-8"),
            filename="servers.json",
        )

        # 2. Send SIGHUP signal
        await container.kill(signal=signal.SIGHUP)

    # 3. Give proxy a moment to reload
    await asyncio.sleep(5)

    # 4. Verify the new port is listening and an old one is not
    assert await check_port_listening("127.0.0.1", new_port, protocol="udp"), (
        f"Proxy did not start listening on new port {new_port} after reload."
    )
    assert not await check_port_listening("127.0.0.1", 25565), (
        "Proxy did not stop listening on old port 25565 after reload."
    )


async def test_proxy_restarts_crashed_server(
    docker_manager: DockerManager, app_config, docker_compose_fixture
):
    """
    Tests that the proxy will re-start a server that was stopped externally.
    """
    server_config = next(s for s in app_config.game_servers if s.game_type == "java")
    container_name = server_config.container_name

    # 1. Trigger server start and wait for it to be running
    await check_port_listening("127.0.0.1", server_config.proxy_port)
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running"
    ), "Server did not start on first connection."

    # 2. Manually stop the container to simulate a crash
    await docker_manager.stop_server(container_name, 10)
    assert not await docker_manager.is_container_running(container_name), (
        "Container did not stop after manual command."
    )

    # 3. Attempt a new connection and verify it restarts
    await check_port_listening("127.0.0.1", server_config.proxy_port)
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running"
    ), "Proxy did not restart the 'crashed' server on new connection."
