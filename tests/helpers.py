# tests/helpers.py
"""
Asynchronous helper functions for testing.
"""

import asyncio

import aiodocker
import structlog
from mcstatus import BedrockServer, JavaServer

log = structlog.get_logger()


async def wait_for_container_status(
    docker_client: aiodocker.Docker,
    container_name: str,
    expected_status: str,
    timeout=60,
):
    """Asynchronously waits for a container to reach the expected status."""
    log.info(
        "Waiting for container status",
        container=container_name,
        expected=expected_status,
        timeout=timeout,
    )
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            container = await docker_client.containers.get(container_name)
            container_info = await container.show()
            current_status = container_info.get("State", {}).get("Status", "unknown")
            if current_status == expected_status:
                log.info(
                    "Container reached expected status",
                    container=container_name,
                    status=current_status,
                )
                return True
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                log.error("Docker error checking status", exc_info=True)
        except Exception:
            log.error("Unexpected error checking status", exc_info=True)

        await asyncio.sleep(1)

    log.error(
        "Timeout waiting for container status",
        container=container_name,
        expected=expected_status,
    )
    return False


async def wait_for_mc_server_ready(
    server_type: str, host: str, port: int, timeout=120, initial_delay=5
):
    """Asynchronously waits for a Minecraft server to become queryable."""
    log.info(
        "Waiting for Minecraft server to be ready",
        server=server_type,
        host=host,
        port=port,
    )
    await asyncio.sleep(initial_delay)
    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            if server_type == "java":
                server = await JavaServer.async_lookup(host, port)
                status = await server.async_status()
            elif server_type == "bedrock":
                server = await BedrockServer.async_lookup(host, port)
                status = await server.async_status()
            else:
                log.error("Unknown server type", server_type=server_type)
                return False

            log.info(
                "Server is ready!",
                server=server_type,
                players=status.players.online,
            )
            return True
        except Exception as e:
            log.debug("Server not ready yet, retrying...", error=str(e))
            await asyncio.sleep(5)

    log.error("Timeout waiting for server", server=server_type)
    return False


async def check_port_listening(host: str, port: int, timeout=1):
    """Asynchronously checks if a port is actively listening for TCP connections."""
    try:
        # The timeout applies to each phase of the connection establishment
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False
