import asyncio
import os
import random
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config(pytestconfig):
    """
    Loads test environment configuration from 'tests/.env', dynamically builds the
    DOCKER_HOST connection string, and returns the configuration as a dict.
    This preserves the original, correct logic for multi-environment testing.
    """
    print("--- Loading Test Environment Configuration ---")
    config = {}

    root_path = Path(str(pytestconfig.rootdir))
    env_file_path = root_path / "tests" / ".env"

    if env_file_path.is_file():
        print(f"Found environment file at: {env_file_path}")
        load_dotenv(dotenv_path=env_file_path, override=True)

    host_ip = os.environ.get("DOCKER_HOST_IP")
    conn_type = os.environ.get("DOCKER_CONNECTION_TYPE", "").lower()
    conn_port = os.environ.get("DOCKER_CONNECTION_PORT")
    ssh_user = os.environ.get("DOCKER_SSH_USER")
    docker_host_url = None

    if host_ip and conn_type and conn_port:
        if conn_type == "tcp":
            docker_host_url = f"tcp://{host_ip}:{conn_port}"
        elif conn_type == "ssh":
            if ssh_user:
                docker_host_url = f"ssh://{ssh_user}@{host_ip}:{conn_port}"
            else:
                pytest.fail("DOCKER_SSH_USER must be set for SSH connections.")
        if docker_host_url:
            os.environ["DOCKER_HOST"] = docker_host_url

    if host_ip:
        os.environ["DOCKER_HOST_IP"] = host_ip

    config["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
    config["DOCKER_HOST_IP"] = os.environ.get("DOCKER_HOST_IP")

    mode_description = "Local Mode (using default Docker context)"
    if os.environ.get("CI_MODE"):
        mode_description = "CI Mode (driven by GitHub Actions workflow)"
    elif config.get("DOCKER_HOST"):
        mode_description = f"Remote Mode (connecting to {config['DOCKER_HOST']})"

    print("\n==================================================")
    print(f" Test Environment Profile: {mode_description}")
    print("==================================================")

    return config


@pytest_asyncio.fixture(scope="session")
async def docker_compose_session(pytestconfig):
    """
    Manages the Docker Compose test environment for the entire session.
    It performs pre-cleanup, starts services, and tears them down.
    """
    # --- Environment Loading ---
    env_path = Path(__file__).parent / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=True)
        print(f"Loaded environment variables from: {env_path}")

    project_name = f"netherbridge_test_{random.randint(1, 1000)}"
    compose_file = str(Path(__file__).parent / "docker-compose.tests.yml")

    # --- Restored: Aggressive Pre-Cleanup Logic ---
    print("\n--- Performing aggressive pre-cleanup of any stale test containers... ---")
    try:
        find_stale_cmd = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "name=netherbridge_test_",
        ]
        proc = await asyncio.create_subprocess_exec(
            *find_stale_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stale_ids = stdout.decode().strip().splitlines()

        if stale_ids:
            print(
                f"Found stale containers: {', '.join(stale_ids)}. Forcibly removing..."
            )
            rm_proc = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                *stale_ids,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await rm_proc.communicate()
        else:
            print("No stale containers found.")

    except Exception as e:
        print(f"Warning during pre-cleanup: {e}")

    # Docker Compose commands
    up_command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        compose_file,
        "up",
        "--build",
        "-d",
    ]
    down_command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        compose_file,
        "down",
        "-v",
    ]

    try:
        print("--- Bringing up test environment... ---")
        up_process = await asyncio.create_subprocess_exec(*up_command)
        await up_process.wait()
        if up_process.returncode != 0:
            pytest.fail("Docker Compose failed to start.", pytrace=False)

        await asyncio.sleep(10)  # Wait for services to stabilize
        yield

    finally:
        print("\n--- Tearing down Docker Compose environment... ---")
        down_process = await asyncio.create_subprocess_exec(*down_command)
        await down_process.wait()
        print("Teardown complete.")


@pytest.fixture(scope="session")
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    try:
        client = docker.from_env()
        client.ping()
        yield client
    except docker.errors.DockerException as e:
        pytest.fail(f"Could not connect to Docker daemon. Error: {e}")
    finally:
        if "client" in locals() and client:
            client.close()
