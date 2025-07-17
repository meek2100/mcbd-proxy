# tests/helpers.py
"""
Asynchronous helper functions for testing.
"""

import asyncio
import os
import re

import aiodocker
import requests  # Import requests for metrics querying
import structlog
from mcstatus import BedrockServer, JavaServer

log = structlog.get_logger()


def get_proxy_host() -> str:
    """
    Determines the correct IP address or hostname for the proxy.
    This restores the logic from the original test suite to support
    different testing scenarios (in-container, remote host, CI).
    """
    # In CI or when tests run inside a container, use the service name.
    if os.environ.get("CI_MODE") or os.environ.get("NB_TEST_MODE") == "container":
        return "nether-bridge"

    # For remote testing, an explicit IP can be provided.
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]

    # The original also correctly checked for DOCKER_HOST_IP for remote tests.
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]

    # Fallback for local runs on the host machine.
    return "127.0.0.1"


async def wait_for_container_status(
    docker_client: aiodocker.Docker,
    container_name: str,
    expected_status: str,
    timeout=60,
) -> bool:
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
) -> bool:
    """Asynchronously waits for a Minecraft server to become queryable."""
    log.info(
        "Waiting for Minecraft server to be ready",
        server=server_type,
        host=host,
        port=port,
    )
    # The initial_delay gives the server a moment to start listening
    # before mcstatus begins querying.
    await asyncio.sleep(initial_delay)
    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            if server_type == "java":
                server = await JavaServer.async_lookup(host, port)
            elif server_type == "bedrock":
                # BedrockServer.lookup is synchronous; run it in a thread
                server = await asyncio.to_thread(BedrockServer.lookup, host, port)
            else:
                log.error("Unknown server type", server_type=server_type)
                return False

            status = await server.async_status()
            log.info(
                "Server is ready!",
                server=server_type,
                players=status.players.online,
            )
            return True
        except Exception as e:
            log.debug("Server not ready yet, retrying...", error=str(e))
            await asyncio.sleep(5)  # Consistent retry interval
        # If the sleep gets cancelled, it's okay, let the loop continue
        # to re-evaluate the overall timeout.

    log.error("Timeout waiting for server", server=server_type)
    return False


async def check_port_listening(host: str, port: int, protocol="tcp", timeout=1) -> bool:
    """Asynchronously checks if a port is actively listening."""
    if protocol == "tcp":
        try:
            # Attempt to open a connection; if successful, port is listening
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            await writer.wait_closed()
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False
    else:  # udp
        loop = asyncio.get_running_loop()
        try:
            # For UDP, just try to create a datagram endpoint. If it succeeds,
            # it means the port is available to send/receive, implying a listener.
            # This is a less direct check than TCP.
            transport, _ = await loop.create_datagram_endpoint(
                lambda: asyncio.DatagramProtocol(), remote_addr=(host, port)
            )
            transport.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False


async def wait_for_log_message(
    docker_client: aiodocker.Docker,
    container_name: str,
    message: str,
    timeout: int = 30,
) -> bool:
    """
    Asynchronously waits for a specific message to appear in a container's logs.
    """
    log.info(
        "Waiting for log message",
        container=container_name,
        message=message,
        timeout=timeout,
    )
    start_time = asyncio.get_event_loop().time()
    try:
        container = await docker_client.containers.get(container_name)
    except aiodocker.exceptions.DockerError as e:
        log.error(
            "Could not get container for log check",
            container=container_name,
            error=str(e),
        )
        return False

    # Start streaming logs from 'since' a bit before current time
    # to catch logs that might have just occurred.
    # The `follow=True` makes it stream new logs.
    async for line in container.log(
        follow=True, since=int(start_time - 5), stdout=True, stderr=True
    ):
        decoded_line = line.strip()
        log.debug(f"[{container_name} log]: {decoded_line}")
        if message in decoded_line:
            log.info("Found log message.", container=container_name, message=message)
            return True
        if asyncio.get_event_loop().time() - start_time > timeout:
            log.warning(
                "Timeout waiting for log message.",
                container=container_name,
                message=message,
            )
            break
        # Briefly yield control to the event loop
        await asyncio.sleep(0.1)  # Prevent busy-waiting
    return False


async def get_active_sessions_metric(proxy_host: str, server_name: str) -> int:
    """
    Queries the proxy's /metrics endpoint and returns the number of active sessions.
    """
    metrics_url = f"http://{proxy_host}:8000/metrics"
    try:
        # Use asyncio-compatible HTTP client if possible, or run sync in thread
        # For simplicity in testing, requests can be run in a thread.
        # In a real app, aiohttp would be preferred.
        response = await asyncio.to_thread(requests.get, metrics_url, timeout=5)
        response.raise_for_status()

        # The new metric name is netherbridge_active_connections and label is 'server'
        pattern = re.compile(
            rf'netherbridge_active_connections{{server="{server_name}"}}\s+'
            r"([0-9\.]+)"
        )
        match = pattern.search(response.text)

        if match:
            value_str = match.group(1)
            # Metrics values can be float, convert to int for session count
            return int(float(value_str))
        log.warning("Active sessions metric not found.", server=server_name)
        return 0
    except requests.RequestException as e:
        log.error(
            f"Could not retrieve or parse active sessions metric: {e}", exc_info=True
        )
        return -1  # Indicate failure to retrieve
    except Exception as e:
        log.error(
            f"Unexpected error getting active sessions metric: {e}", exc_info=True
        )
        return -1
