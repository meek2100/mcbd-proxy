# tests/conftest.py
import asyncio
import os
import subprocess
import time
from pathlib import Path

import aiodocker
import pytest
import pytest_asyncio
from dotenv import load_dotenv


@pytest.fixture(scope="session")
def env_config(pytestconfig):
    """
    Loads test environment configuration from 'tests/.env', dynamically
    builds the DOCKER_HOST connection string, and returns the config.
    """
    config = {}

    # Use pytestconfig.rootdir to find the .env file reliably
    root_path = Path(str(pytestconfig.rootdir))
    env_file_path = root_path / "tests" / ".env"

    if env_file_path.is_file():
        print(f"--- Loading Test Environment Configuration from: {env_file_path} ---")
        load_dotenv(dotenv_path=env_file_path)
    else:
        print(
            "--- No 'tests/.env' file found. Using environment variables from CI/shell. ---"
        )

    host_ip = os.environ.get("DOCKER_HOST_IP")
    conn_type = os.environ.get("DOCKER_CONNECTION_TYPE", "").lower()
    conn_port = os.environ.get("DOCKER_CONNECTION_PORT")
    ssh_user = os.environ.get("DOCKER_SSH_USER")

    # Dynamically build the DOCKER_HOST URL if components are present
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
            # Set DOCKER_HOST in os.environ for aiodocker and subprocess calls
            os.environ["DOCKER_HOST"] = docker_host_url
            print(f"Dynamically constructed DOCKER_HOST: {docker_host_url}")

    # Populate the config dictionary for other fixtures
    config["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
    config["DOCKER_HOST_IP"] = os.environ.get("DOCKER_HOST_IP")

    # Determine and print a clear summary of the detected mode
    mode_description = ""
    if os.environ.get("CI_MODE"):
        mode_description = "CI Mode (driven by GitHub Actions workflow)"
    elif config.get("DOCKER_HOST"):
        mode_description = f"Remote Mode (connecting to {config['DOCKER_HOST']})"
    else:
        mode_description = "Local Mode (using default Docker context)"

    print("\n" + "=" * 50)
    print(f" Test Environment Profile: {mode_description}")
    print("=" * 50)

    return config


def pytest_addoption(parser):
    """Add a command line option to specify the docker-compose file."""
    parser.addoption(
        "--compose-file",
        action="store",
        default="tests/docker-compose.tests.yml",
        help="Specify the docker-compose file to use for tests.",
    )


@pytest.fixture(scope="session")
def docker_compose_project_name():
    """Generates a unique project name for docker compose."""
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
    client = None
    docker_host_url = env_config.get("DOCKER_HOST")
    try:
        # aiodocker will pick up DOCKER_HOST env var automatically
        client = aiodocker.Docker()
        if docker_host_url:
            print(f"\nConnecting Docker client to remote: {docker_host_url}")
        else:
            print("\nConnecting Docker client using default (local or CI) environment.")

        # Ping equivalent: try to list containers (a simple Docker API call)
        # This will raise an exception if connection fails.
        await client.containers.list(limit=1)
        print("Successfully connected to Docker daemon for Docker client fixture.")
    except aiodocker.exceptions.DockerError as e:
        pytest.fail(
            f"Could not connect to Docker daemon for client fixture. Error: {e}"
        )

    yield client
    if client:
        await client.close()


@pytest.fixture(scope="function")  # Changed to function scope for isolation
async def docker_compose_fixture(
    request, docker_compose_project_name, env_config, pytestconfig
):
    """
    Manages the lifecycle of Docker containers for integration tests.
    """
    if os.environ.get("CI_MODE"):
        print("CI_MODE detected. Skipping Docker Compose management from conftest.")
        yield
        return

    # Use pytestconfig to get the rootdir and construct paths
    root_path = Path(str(pytestconfig.rootdir))
    compose_file_option = pytestconfig.getoption("--compose-file")
    base_compose_file = root_path / compose_file_option

    compose_files_to_use = [base_compose_file]

    # Automatically apply local override if not in remote mode
    is_remote_mode = bool(env_config.get("DOCKER_HOST"))
    if not is_remote_mode:
        local_override_file = (
            base_compose_file.parent / "docker-compose.tests.local.yml"
        )
        if local_override_file.exists():
            compose_files_to_use.append(local_override_file)
            print(f"Applying local override: {local_override_file}")

    # Create the list of file arguments for all docker compose commands
    compose_file_args = []
    for f in compose_files_to_use:
        compose_file_args.extend(["-f", str(f)])

    # Create the --env-file argument if the file exists
    env_file_path = root_path / "tests" / ".env"
    env_file_args = []
    if env_file_path.is_file():
        env_file_args = ["--env-file", str(env_file_path)]
        print(f"Telling Docker Compose to use env file: {env_file_path}")

    # Set up environment for subprocess calls
    env_vars = os.environ.copy()
    if is_remote_mode:
        # DOCKER_HOST is already in os.environ from env_config fixture
        print(f"Passing DOCKER_HOST={env_vars.get('DOCKER_HOST')} to subprocess.")

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}'...")
    print(f"Using compose files: {[str(f) for f in compose_files_to_use]}")

    # Preemptively remove any containers from previous, failed test runs
    # This uses `docker compose rm` with the specific project name
    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        cleanup_command = (
            ["docker", "compose", "-p", docker_compose_project_name]
            + compose_file_args
            + env_file_args
            + ["rm", "-f", "-s", "-v"]  # -s stops, -v removes volumes
        )
        subprocess.run(
            cleanup_command,
            cwd=root_path,
            check=False,  # Don't raise error if no containers to remove
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
        )
        print("Pre-cleanup command executed.")
    except Exception as e:
        print(f"Warning during aggressive pre-cleanup: {e}")

    try:
        print("Bringing up test environment with build step...")
        up_command = (
            ["docker", "compose", "-p", docker_compose_project_name]
            + compose_file_args
            + env_file_args
            + ["up", "--build", "--force-recreate", "--remove-orphans", "-d"]
        )
        subprocess.run(
            up_command,
            cwd=root_path,
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
        )
        print("Docker Compose 'up' command completed.")

        print("Waiting for nether-bridge container to become healthy...")
        # Use a new aiodocker client here as docker_client_fixture is
        # function-scoped and not available at fixture setup time.
        # This client needs to be explicitly closed.
        temp_docker_client = None
        try:
            temp_docker_client = aiodocker.Docker()
            nether_bridge_container = await asyncio.wait_for(
                temp_docker_client.containers.get("nether-bridge"), timeout=10
            )

            timeout = 120
            start_time = time.time()
            while time.time() - start_time < timeout:
                container_info = await nether_bridge_container.show()
                health_status = (
                    container_info.get("State", {}).get("Health", {}).get("Status")
                )
                if health_status == "healthy":
                    print("Nether-bridge is healthy.")
                    break
                await asyncio.sleep(2)
            else:
                health_log = (
                    container_info.get("State", {}).get("Health", {}).get("Log")
                )
                last_log = health_log[-1] if health_log else "No health log."
                raise Exception(
                    "Timeout waiting for nether-bridge container to become healthy. "
                    f"Last status: {health_status}. Last log: {last_log}"
                )
        finally:
            if temp_docker_client:
                await temp_docker_client.close()

    except subprocess.CalledProcessError as e:
        print(f"Error during Docker Compose setup: {e.stderr}")
        print(
            f"\n--- Logs for project '{docker_compose_project_name}' "
            f"(during setup failure) ---"
        )
        try:
            logs_cmd = (
                ["docker", "compose", "-p", docker_compose_project_name]
                + compose_file_args
                + env_file_args
                + ["logs"]
            )
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=root_path,
            )
            print(f"\n{logs.stdout}\n{logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs during setup failure: {log_e}")

        # Ensure teardown even on setup failure
        down_cmd_on_fail = (
            ["docker", "compose", "-p", docker_compose_project_name]
            + compose_file_args
            + env_file_args
            + ["down", "-v", "--remove-orphans"]
        )
        subprocess.run(
            down_cmd_on_fail,
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=root_path,
        )
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        raise

    yield  # Yield control to the tests

    # Teardown: runs after all tests using this fixture complete
    if request.session.testsfailed > 0:
        print(
            (
                f"\n--- DUMPING ALL CONTAINER LOGS DUE TO TEST FAILURE in project "
                f"'{docker_compose_project_name}' ---"
            )
        )
        try:
            logs_cmd_on_fail = (
                ["docker", "compose", "-p", docker_compose_project_name]
                + compose_file_args
                + env_file_args
                + ["logs", "--no-color"]
            )
            logs_result = subprocess.run(
                logs_cmd_on_fail,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=root_path,
            )
            print(f"\n{logs_result.stdout}\n{logs_result.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs during test teardown: {log_e}")

    print(
        (
            f"\nTests finished. Tearing down Docker Compose project "
            f"'{docker_compose_project_name}'..."
        )
    )
    down_cmd = (
        ["docker", "compose", "-p", docker_compose_project_name]
        + compose_file_args
        + env_file_args
        + ["down", "-v", "--remove-orphans"]
    )
    subprocess.run(
        down_cmd,
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=env_vars,
        cwd=root_path,
    )
    print("Teardown complete.")
