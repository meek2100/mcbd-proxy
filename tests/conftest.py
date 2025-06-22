# tests/conftest.py
import os
from pathlib import Path

import docker
import pytest
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config():
    """
    Loads configuration from 'tests/.env' and environment variables.
    This fixture determines if tests are running against a local or remote daemon.
    """
    print("\n--- Loading Test Environment Configuration ---")

    # Load .env file if it exists
    env_file_path = Path(__file__).parent / ".env"
    if env_file_path.is_file():
        print(f"Found environment file at: {env_file_path}")
        load_dotenv(dotenv_path=env_file_path)
    else:
        print("No 'tests/.env' file found. Using environment variables from shell.")

    config = {
        "DOCKER_HOST": os.environ.get("DOCKER_HOST"),
        "DOCKER_HOST_IP": os.environ.get("DOCKER_HOST_IP"),
        "CI_MODE": os.environ.get("CI_MODE"),
    }

    if config["CI_MODE"]:
        print("\n==================================================")
        print(" Test Environment Profile: CI Mode")
        print("==================================================")
    elif config["DOCKER_HOST"]:
        print("\n==================================================")
        print(
            " Test Environment Profile: Remote Mode (connecting to "
            f"{config['DOCKER_HOST']})"
        )
        print("==================================================")
    else:
        print("\n==================================================")
        print(" Test Environment Profile: Local Mode (Docker Desktop)")
        print("==================================================")

    return config


@pytest.fixture(scope="session")
def docker_client_fixture(env_config):
    """
    Provides a Docker client instance configured from the environment.
    This fixture assumes that the Docker environment is already running.
    """
    try:
        # docker.from_env() correctly handles DOCKER_HOST variable if set,
        # otherwise it uses defaults. This covers all environments.
        client = docker.from_env()

        # Verify the connection to the daemon is working.
        client.ping()
        print("\nSuccessfully connected to Docker daemon.")
        return client
    except docker.errors.DockerException as e:
        pytest.fail(
            (
                f"Could not connect to Docker daemon. "
                f"Please ensure your environment is running before executing tests. "
                f"Error: {e}"
            )
        )
