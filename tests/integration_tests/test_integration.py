import asyncio

import pytest
from mcstatus import BedrockServer, JavaServer

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_java_server_starts_on_connection(docker_compose_up):
    """
    Tests that a Java server container starts when a client probes its status.
    """
    server = JavaServer.lookup("localhost:25565")

    try:
        # First attempt is expected to time out while the server starts
        await server.async_status()
    except (asyncio.TimeoutError, ConnectionRefusedError):
        # Wait for the server to spin up
        await asyncio.sleep(15)

    # Second attempt should connect successfully
    status = await server.async_status()
    assert status.version.name is not None
    assert status.players.online >= 0


@pytest.mark.asyncio
async def test_bedrock_server_starts_on_connection(docker_compose_up):
    """
    Tests that a Bedrock server container starts when a client probes its status.
    """
    server = BedrockServer.lookup("localhost:19132")

    try:
        # First attempt is expected to time out
        await server.async_status()
    except asyncio.TimeoutError:
        # Wait for the server to spin up
        await asyncio.sleep(15)

    # Second attempt should connect successfully
    status = await server.async_status()
    assert status.version.brand == "BEDROCK"
    assert status.players.online >= 0
