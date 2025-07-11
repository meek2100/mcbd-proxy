# tests/helpers.py

import asyncio
import time

import aiodocker
import pytest
from mcstatus import BedrockServer, JavaServer

BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host(env_config):
    """Retrieves the Docker host IP for proxy connections."""
    return env_config.get("DOCKER_HOST_IP", "127.0.0.1")


async def get_container_status(docker_client: aiodocker.Docker, container_name: str):
    """Gets the Docker container's current status (e.g., 'running', 'exited')."""
    try:
        container = await docker_client.containers.get(container_name)
        data = await container.show()
        return data["State"]["Status"]
    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            return "not_found"
        pytest.fail(f"Docker error getting status for {container_name}: {e.message}")


async def wait_for_log_message(
    docker_client: aiodocker.Docker,
    container_name: str,
    message: str,
    timeout: int = 60,
) -> bool:
    """Waits for a specific log message to appear in a container's logs."""
    try:
        container = await docker_client.containers.get(container_name)
        async for line in container.log(stdout=True, stderr=True, follow=True):
            if message in line:
                return True
            await asyncio.sleep(0.1)  # Yield control to allow other tasks to run
            timeout -= 0.1
            if timeout <= 0:
                print(f"Timeout waiting for message: '{message}' in {container_name}")
                return False
    except aiodocker.exceptions.DockerError as e:
        pytest.fail(
            f"Container '{container_name}' not found while waiting for logs: "
            f"{e.message}"
        )
    return False


async def wait_for_mc_server_ready(server_info: dict, timeout: int = 120) -> bool:
    """Waits for a Minecraft server to become query-ready."""
    host, port, server_type = (
        server_info["host"],
        server_info["port"],
        server_info["type"],
    )
    ServerClass = JavaServer if server_type == "java" else BedrockServer

    for _ in range(timeout):
        try:
            # Short timeout for each individual query attempt
            server = await ServerClass.async_lookup(f"{host}:{port}", timeout=1)
            await server.async_status()
            return True
        except Exception:
            await asyncio.sleep(1)  # Wait before retrying
    return False


async def wait_for_proxy_to_be_healthy(
    docker_client: aiodocker.Docker, timeout: int = 120
) -> bool:
    """
    Waits for the 'nether-bridge' container to report a 'healthy' status
    from its Docker healthcheck.
    """
    container_name = "nether-bridge"
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            container = await docker_client.containers.get(container_name)
            data = await container.show()
            health_status = data["State"].get("Health", {}).get("Status")
            if health_status == "healthy":
                return True
            await asyncio.sleep(5)  # Wait before checking again
        except aiodocker.exceptions.DockerError as e:
            if e.status == 404:
                pytest.fail(f"Nether-bridge container not found: {e.message}")
            else:
                pytest.fail(f"Docker error checking health: {e.message}")
        except Exception as e:
            pytest.fail(f"Unexpected error checking health: {e}")
    return False


async def wait_for_proxy_to_be_ready(docker_client: aiodocker.Docker) -> bool:
    """
    Placeholder for backward compatibility.
    Prefer `wait_for_proxy_to_healthy` for accurate health checks.
    """
    return await wait_for_proxy_to_be_healthy(docker_client)
