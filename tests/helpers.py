import asyncio

import aiodocker
import pytest
from mcstatus import BedrockServer, JavaServer

BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565


def get_proxy_host(env_config):
    return env_config.get("DOCKER_HOST_IP", "127.0.0.1")


async def get_container_status(docker_client: aiodocker.Docker, container_name: str):
    try:
        container = await docker_client.containers.get(container_name)
        data = await container.show()
        return data["State"]["Status"]
    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            return "not_found"
        pytest.fail(f"Docker error getting status for {container_name}: {e.message}")


async def wait_for_log_message(docker_client, container_name, message, timeout=60):
    try:
        container = await docker_client.containers.get(container_name)
        async for line in container.log(stdout=True, stderr=True, follow=True):
            if message in line:
                return True
            # Simple timeout check
            timeout -= 0.1
            if timeout <= 0:
                return False
            await asyncio.sleep(0.1)
    except aiodocker.exceptions.DockerError:
        pytest.fail(f"Container '{container_name}' not found while waiting for logs.")
    return False


async def wait_for_mc_server_ready(server_info, timeout=120):
    host, port, server_type = (
        server_info["host"],
        server_info["port"],
        server_info["type"],
    )
    ServerClass = JavaServer if server_type == "java" else BedrockServer

    for _ in range(timeout):
        try:
            server = await ServerClass.async_lookup(f"{host}:{port}", timeout=1)
            await server.async_status()
            return True
        except Exception:
            await asyncio.sleep(1)
    return False
