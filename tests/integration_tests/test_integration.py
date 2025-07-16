# tests/integration_tests/test_integration.py
"""
Integration tests for the Nether-bridge proxy.
These tests use live Docker containers to verify end-to-end functionality.
"""

import asyncio
import io
import json
import os
import signal
import sys
import tarfile

import pytest
import pytest_asyncio
from mcstatus import BedrockServer, JavaServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from docker_manager import DockerManager
from tests.helpers import check_port_listening, wait_for_container_status

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(scope="function")
async def docker_manager(docker_client_fixture):
    """Provides a DockerManager instance for the test function."""
    manager = DockerManager(app_config=None)
    manager.docker = docker_client_fixture
    yield manager


async def test_java_server_lifecycle(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Java server: on-demand start and idle stop.
    """
    container_name = "mc-java"
    proxy_port = 25565
    backend_port = 25565
    idle_timeout = 30

    assert not await docker_manager.is_container_running(container_name), (
        "Java container should be initially stopped."
    )

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", proxy_port), timeout=10
        )
        writer.close()
        await writer.wait_closed()
    except (ConnectionRefusedError, asyncio.TimeoutError):
        pass

    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Java container failed to start."
    server = await JavaServer.async_lookup(f"127.0.0.1:{backend_port}")
    await server.async_status()

    await asyncio.sleep(idle_timeout + 15)
    assert not await docker_manager.is_container_running(container_name), (
        "Java container did not stop after the idle timeout."
    )


async def test_bedrock_server_lifecycle(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Verifies the full lifecycle for a Bedrock server: on-demand start and idle stop.
    """
    container_name = "mc-bedrock"
    proxy_port = 19132
    backend_port = 19132
    idle_timeout = 30

    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container should be initially stopped."
    )

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: asyncio.DatagramProtocol(), remote_addr=("127.0.0.1", proxy_port)
    )
    transport.sendto(b"\x01\x00\x00\x00\x00\x01\x23\x45\x67\x89\xab\xcd\xef")
    transport.close()

    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Bedrock container failed to start."
    server = await BedrockServer.async_lookup(f"127.0.0.1:{backend_port}")
    await server.async_status()

    await asyncio.sleep(idle_timeout + 15)
    assert not await docker_manager.is_container_running(container_name), (
        "Bedrock container did not stop after the idle timeout."
    )


async def test_sighup_reloads_configuration(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Tests that the proxy correctly reloads its configuration upon SIGHUP.
    """
    new_port = 19134
    old_java_port = 25565
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

    async with docker_manager.get_container("nether-bridge") as container:
        assert container is not None, "nether-bridge container not found"

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name="servers.json")
            config_bytes = json.dumps(new_config).encode("utf-8")
            info.size = len(config_bytes)
            tar.addfile(info, io.BytesIO(config_bytes))
        tar_stream.seek(0)
        await container.put_archive("/app", tar_stream.read())

        await container.kill(signal=signal.SIGHUP)

    await asyncio.sleep(5)

    assert await check_port_listening("127.0.0.1", new_port, protocol="udp"), (
        f"Proxy did not start listening on new port {new_port} after reload."
    )
    assert not await check_port_listening("127.0.0.1", old_java_port), (
        "Proxy did not stop listening on old port after reload."
    )


async def test_proxy_restarts_crashed_server(
    docker_manager: DockerManager, docker_compose_fixture
):
    """
    Tests that the proxy will re-start a server that was stopped externally.
    """
    container_name = "mc-java"
    proxy_port = 25565

    await check_port_listening("127.0.0.1", proxy_port)
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running"
    ), "Server did not start on first connection."

    async with docker_manager.get_container(container_name) as container:
        assert container is not None, "Java container not found for stopping."
        await container.stop(t=10)
    assert not await docker_manager.is_container_running(container_name), (
        "Container did not stop after manual command."
    )

    await asyncio.sleep(1)

    await check_port_listening("127.0.0.1", proxy_port)
    assert await wait_for_container_status(
        docker_manager.docker, container_name, "running", timeout=120
    ), "Proxy did not restart the 'crashed' server on new connection."
