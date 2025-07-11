import asyncio
import os

import aiodocker
import pytest
from mcstatus import BedrockServer, JavaServer

BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host(env_config):
    """
    Determines the correct IP address or hostname for the proxy
    based on the test environment configuration.
    """
    # 1. Check if running inside the test environment's container network.
    #    This is typically set for CI and 'nb-tester' container.
    if (
        env_config.get("NB_TEST_MODE") == "container"
        or os.environ.get("CI_MODE") == "true"
    ):
        return "nether-bridge"

    # 2. Check for an explicit IP set for remote/local Docker host.
    #    PROXY_IP is used in GitHub Actions (pr-validation.yml).
    #    DOCKER_HOST_IP is set in local_env.py (used via conftest.py).
    if "PROXY_IP" in env_config:
        return env_config["PROXY_IP"]
    if "DOCKER_HOST_IP" in env_config:
        return env_config["DOCKER_HOST_IP"]

    # 3. Fallback for local `pytest` runs without special config.
    return "127.0.0.1"


async def get_container_status(docker_client: aiodocker.Docker, container_name: str):
    """
    Retrieves the current status of a Docker container asynchronously.
    """
    try:
        container = await docker_client.containers.get(container_name)
        data = await container.show()
        return data["State"]["Status"]
    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            return "not_found"
        pytest.fail(f"Docker error getting status for {container_name}: {e.message}")


async def wait_for_log_message(docker_client, container_name, message, timeout=60):
    """
    Waits for a specific message to appear in a container's logs.
    """
    try:
        container = await docker_client.containers.get(container_name)
        async for line in container.log(stdout=True, stderr=True, follow=True):
            if message in line.decode("utf-8"):
                return True
            # Simple timeout check (can be more sophisticated)
            timeout -= 0.1  # Reduce timeout for each small sleep
            if timeout <= 0:
                return False
            await asyncio.sleep(0.1)  # Give control back to the event loop
    except aiodocker.exceptions.DockerError:
        pytest.fail(f"Container '{container_name}' not found during log wait.")
    return False


async def wait_for_mc_server_ready(server_info, timeout=120):
    """
    Waits for a Minecraft server to become query-ready via mcstatus.
    """
    host, port, server_type = (
        server_info["host"],
        server_info["port"],
        server_info["type"],
    )
    ServerClass = JavaServer if server_type == "java" else BedrockServer

    for _ in range(timeout):
        try:
            # Use a short timeout for the lookup to quickly retry
            server = await ServerClass.async_lookup(f"{host}:{port}", timeout=1)
            await server.async_status()
            return True
        except Exception:
            await asyncio.sleep(1)  # Wait before retrying
    return False


async def wait_for_container_status(
    docker_client: aiodocker.Docker,
    container_name: str,
    target_status: str,
    timeout: int = 240,
) -> bool:
    """
    Waits for a container to reach a specific target status.
    """
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        current_status = await get_container_status(docker_client, container_name)
        if current_status == target_status:
            return True
        await asyncio.sleep(5)  # Poll every 5 seconds
    return False


async def wait_for_proxy_to_be_ready(docker_client, timeout=60):
    """
    Waits for the nether-bridge proxy to be fully initialized.
    """
    return await wait_for_log_message(
        docker_client,
        "nether-bridge",
        "Starting main proxy packet forwarding loop",
        timeout,
    )
