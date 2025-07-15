# tests/conftest.py
import asyncio
import os
import subprocess
import time

import aiodocker
import pytest
import pytest_asyncio
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config():
    """
    Loads test environment configuration from 'tests/.env', dynamically
    builds the DOCKER_HOST connection string, and returns the config.
    """
    print("--- Loading Test Environment Configuration ---")
    load_dotenv(dotenv_path="tests/.env")
    host_ip = os.environ.get("DOCKER_HOST_IP")
    conn_type = os.environ.get("DOCKER_CONNECTION_TYPE", "").lower()
    conn_port = os.environ.get("DOCKER_CONNECTION_PORT")
    ssh_user = os.environ.get("DOCKER_SSH_USER")
    if conn_type == "tcp":
        os.environ["DOCKER_HOST"] = f"tcp://{host_ip}:{conn_port}"
    elif conn_type == "ssh" and ssh_user:
        os.environ["DOCKER_HOST"] = f"ssh://{ssh_user}@{host_ip}:{conn_port}"
    return os.environ


@pytest.fixture(scope="session")
def docker_compose_project_name():
    """Generates a unique project name for docker-compose."""
    return f"netherbridge_test_{int(time.time())}"


@pytest_asyncio.fixture(scope="function")
def event_loop():
    """Create an instance of the default event loop for each test function."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def docker_client_fixture(env_config):
    """Provides an async Docker client for each test function."""
    client = aiodocker.Docker()
    yield client
    await client.close()


@pytest.fixture(scope="session")
def docker_compose_fixture(request, docker_compose_project_name, env_config):
    """Manages the lifecycle of Docker containers for integration tests."""
    if os.environ.get("CI_MODE"):
        print("CI_MODE detected. Skipping Docker Compose management.")
        yield
        return

    compose_file = "tests/docker-compose.tests.yml"
    env_file = "tests/.env"

    static_container_names = [
        "nether-bridge",
        "mc-bedrock",
        "mc-java",
        "nb-tester",
    ]
    for name in static_container_names:
        print(f"Attempting to remove stale container: {name}")
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)

    subprocess.run(
        [
            "docker-compose",
            "-f",
            compose_file,
            "-p",
            docker_compose_project_name,
            "down",
            "-v",
        ],
        check=False,
    )
    subprocess.run(
        [
            "docker-compose",
            "-f",
            compose_file,
            "--env-file",
            env_file,
            "-p",
            docker_compose_project_name,
            "up",
            "--build",
            "-d",
        ],
        check=True,
    )
    yield
    if request.session.testsfailed > 0:
        print("--- DUMPING CONTAINER LOGS ON FAILURE ---")
        subprocess.run(
            [
                "docker-compose",
                "-f",
                compose_file,
                "-p",
                docker_compose_project_name,
                "logs",
                "--no-color",
            ],
            check=False,
        )

    subprocess.run(
        [
            "docker-compose",
            "-f",
            compose_file,
            "-p",
            docker_compose_project_name,
            "down",
            "-v",
        ],
        check=False,
    )
