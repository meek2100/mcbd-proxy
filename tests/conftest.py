import os
from pathlib import Path

import docker
import pytest
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config(pytestconfig):
    """
    Loads test environment configuration from 'tests/.env', dynamically builds the
    DOCKER_HOST connection string, and returns the configuration as a dict.
    This fixture is the single source of truth for test configuration.
    """
    print("--- Loading Test Environment Configuration ---")
    config = {}

    root_path = Path(str(pytestconfig.rootdir))
    env_file_path = root_path / "tests" / ".env"

    if env_file_path.is_file():
        print(f"Found environment file at: {env_file_path}")
        load_dotenv(dotenv_path=env_file_path, override=True)

    # Restore the original logic to build the DOCKER_HOST URL
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
            print(f"Dynamically constructed DOCKER_HOST: {docker_host_url}")
            os.environ["DOCKER_HOST"] = docker_host_url

    # Populate the config dictionary for other fixtures
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


@pytest.fixture(scope="session")
def docker_client_fixture(env_config):
    """
    Provides a Docker client instance configured to connect to the correct
    Docker daemon (local, remote, or CI).
    """
    try:
        client = docker.from_env()
        client.ping()
        print("Successfully connected to Docker daemon.")
        yield client
    except docker.errors.DockerException as e:
        pytest.fail(f"Could not connect to Docker daemon. Is it running? Error: {e}")
    finally:
        if "client" in locals() and client:
            client.close()
