import os
import subprocess
import time
from pathlib import Path

import aiodocker
import pytest
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config(pytestconfig):
    # This fixture remains largely the same
    root_path = Path(str(pytestconfig.rootdir))
    env_file = root_path / "tests" / ".env"
    if env_file.is_file():
        load_dotenv(dotenv_path=env_file)

    config = {
        "DOCKER_HOST": os.environ.get("DOCKER_HOST"),
        "DOCKER_HOST_IP": os.environ.get("DOCKER_HOST_IP", "127.0.0.1"),
    }
    return config


def pytest_addoption(parser):
    parser.addoption(
        "--compose-file",
        action="store",
        default="tests/docker-compose.tests.yml",
        help="Specify the docker-compose file to use for tests.",
    )


@pytest.fixture(scope="session")
def docker_compose_up(pytestconfig, env_config):
    if os.environ.get("CI_MODE"):
        print("CI_MODE: Skipping docker-compose management from conftest.")
        yield
        return

    # This synchronous setup fixture can remain, as it's for the
    # overall test session setup, not individual async test logic.
    compose_file = Path(str(pytestconfig.rootdir)) / pytestconfig.getoption(
        "--compose-file"
    )
    project_name = f"netherbridge_test_{int(time.time())}"

    up_command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(compose_file),
        "up",
        "--build",
        "-d",
    ]
    subprocess.run(up_command, check=True)

    # Wait for the proxy to be healthy before starting tests
    time.sleep(15)  # Simple wait, could be improved with a readiness check

    yield

    down_command = ["docker", "compose", "-p", project_name, "down", "-v"]
    subprocess.run(down_command, check=False)


@pytest.fixture(scope="session")
async def docker_client_fixture(env_config):
    """Provides an AIO-Docker client for async integration tests."""
    client = aiodocker.Docker(url=env_config["DOCKER_HOST"])
    yield client
    await client.close()
