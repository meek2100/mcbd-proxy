# tests/conftest.py
import os
import subprocess
import time
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

    # Convert pytest's path object to a standard pathlib.Path object
    root_path = Path(str(pytestconfig.rootdir))
    env_file_path = root_path / "tests" / ".env"

    if env_file_path.is_file():
        print(f"Found environment file at: {env_file_path}")
        load_dotenv(dotenv_path=env_file_path)
    else:
        print("No 'tests/.env' file found. Using environment variables from CI/shell.")

    # Read connection variables from the environment
    host_ip = os.environ.get("DOCKER_HOST_IP")
    conn_type = os.environ.get("DOCKER_CONNECTION_TYPE", "").lower()
    conn_port = os.environ.get("DOCKER_CONNECTION_PORT")
    ssh_user = os.environ.get("DOCKER_SSH_USER")

    docker_host_url = None

    # Dynamically build the DOCKER_HOST URL if components are present
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

    if host_ip:
        os.environ["DOCKER_HOST_IP"] = host_ip

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
    """Generates a unique project name for docker-compose to isolate test runs."""
    return f"netherbridge_test_{int(time.time())}"


@pytest.fixture(scope="session")
def docker_compose_up(docker_compose_project_name, pytestconfig, request, env_config):
    """
    Starts Docker Compose services before tests and tears them down afterwards.
    """
    if os.environ.get("CI_MODE"):
        print("CI_MODE detected. Skipping Docker Compose management from conftest.")
        yield
        return

    # Convert pytest's path to a standard pathlib.Path object for consistency
    root_path = Path(str(pytestconfig.rootdir))
    base_compose_file = root_path / pytestconfig.getoption("--compose-file")

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

    # Create the list of file arguments for all docker-compose commands
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
        env_vars["DOCKER_HOST"] = env_config["DOCKER_HOST"]
        print(f"Passing DOCKER_HOST={env_vars['DOCKER_HOST']} to subprocess commands.")

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}'...")
    print(f"Using compose files: {[str(f) for f in compose_files_to_use]}")

    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        hardcoded_names_to_remove = [
            "nether-bridge",
            "mc-bedrock",
            "mc-java",
            "nb-tester",
        ]
        list_cmd = ["docker", "ps", "-aq", "--filter", "name=netherbridge_test_"]
        for name in hardcoded_names_to_remove:
            list_cmd.extend(["--filter", f"name={name}"])

        result = subprocess.run(
            list_cmd, capture_output=True, encoding="utf-8", check=False, env=env_vars
        )
        stale_ids = result.stdout.strip().splitlines()
        if stale_ids:
            print(
                f"Found stale containers: {', '.join(stale_ids)}. Forcibly removing..."
            )
            subprocess.run(
                ["docker", "rm", "-f"] + stale_ids,
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
            )
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
        client = docker.from_env(environment=env_vars)
        try:
            container = client.containers.get("nether-bridge")
            timeout = 120
            start_time = time.time()
            while time.time() - start_time < timeout:
                container.reload()
                health_status = (
                    container.attrs.get("State", {}).get("Health", {}).get("Status")
                )
                if health_status == "healthy":
                    print("Nether-bridge is healthy.")
                    break
                time.sleep(2)
            else:
                health_log = (
                    container.attrs.get("State", {}).get("Health", {}).get("Log")
                )
                last_log = health_log[-1] if health_log else "No health log."
                raise Exception(
                    "Timeout waiting for nether-bridge container to become healthy. "
                    f"Last status: {health_status}. Last log: {last_log}"
                )
        finally:
            client.close()

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

    yield

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


@pytest.fixture(scope="session")
def docker_client_fixture(env_config):
    """Provides a Docker client instance for integration tests."""
    client = None
    docker_host_url = env_config.get("DOCKER_HOST")
    try:
        if docker_host_url:
            client = docker.DockerClient(base_url=docker_host_url)
            print(f"\nConnecting Docker client to remote: {docker_host_url}")
        else:
            client = docker.from_env()
            print("\nConnecting Docker client using default (local or CI) environment.")

        client.ping()
        print("Successfully connected to Docker daemon for Docker client fixture.")
    except docker.errors.DockerException as e:
        pytest.fail(
            f"Could not connect to Docker daemon for client fixture. Error: {e}"
        )

    yield client

    if client:
        client.close()
