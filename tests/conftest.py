# tests/conftest.py
import os
import subprocess
import sys
import time
from pathlib import Path

import docker
import pytest

# --- NEW: Profile Handling Logic ---
# Read the test mode from an environment variable. Default to 'local'.
TEST_MODE = os.environ.get("TEST_MODE", "local")
print(f"Running in TEST_MODE: '{TEST_MODE}'")

_local_vm_host_ip = None
_local_docker_host_from_file = None

# Conditionally load local_env.py only in 'remote' mode
if TEST_MODE == "remote":
    try:
        # Temporarily add the tests directory to the path to find local_env
        current_tests_dir = str(Path(__file__).parent)
        if current_tests_dir not in sys.path:
            sys.path.insert(0, current_tests_dir)

        from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST_VALUE
        from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP

        _local_vm_host_ip = LOCAL_VM_HOST_IP
        os.environ["VM_HOST_IP"] = LOCAL_VM_HOST_IP
        print(f"Using remote VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")

        _local_docker_host_from_file = LOCAL_DOCKER_HOST_VALUE
        print(f"Using remote DOCKER_HOST from local_env.py: {LOCAL_DOCKER_HOST_VALUE}")

    except ImportError:
        pytest.fail(
            "TEST_MODE is 'remote' but 'tests/local_env.py' could not be found or is incomplete."
        )
    finally:
        # Clean up the path modification
        if "current_tests_dir" in locals() and current_tests_dir in sys.path:
            sys.path.remove(current_tests_dir)
# --- END: Profile Handling Logic ---


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
def docker_compose_up(docker_compose_project_name, pytestconfig, request):
    """
    Starts Docker Compose services before tests and tears them down afterwards.
    If running in CI_MODE, this fixture does nothing, as the CI workflow
    is responsible for service lifecycle management.
    """
    if os.environ.get("CI_MODE"):
        print("CI_MODE detected. Skipping Docker Compose management from conftest.")
        yield
        return

    # --- UPDATED: Dynamically select compose files based on TEST_MODE ---
    base_compose_file = Path(pytestconfig.rootdir) / pytestconfig.getoption(
        "--compose-file"
    )
    compose_files_to_use = [base_compose_file]

    if TEST_MODE == "local":
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
    # --- END: Dynamic file selection ---

    env_vars = os.environ.copy()
    if _local_docker_host_from_file:
        env_vars["DOCKER_HOST"] = _local_docker_host_from_file
        print(f"Passing DOCKER_HOST={env_vars['DOCKER_HOST']} to subprocess commands.")
    elif "DOCKER_HOST" in env_vars:
        print(
            "DOCKER_HOST is already set in environment for subprocesses: "
            f"{env_vars['DOCKER_HOST']}"
        )
    else:
        print(
            "DOCKER_HOST not set by local_env.py or host environment. "
            "Subprocesses will use default Docker context."
        )

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
            + ["up", "--build", "--force-recreate", "--remove-orphans", "-d"]
        )
        subprocess.run(
            up_command,
            cwd=pytestconfig.rootdir,
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
            f"\n--- Logs for project '{docker_compose_project_name}' (during setup failure) ---"
        )
        try:
            logs_cmd = (
                ["docker", "compose", "-p", docker_compose_project_name]
                + compose_file_args
                + ["logs"]
            )
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print(f"\n{logs.stdout}\n{logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs during setup failure: {log_e}")

        down_cmd_on_fail = (
            ["docker", "compose", "-p", docker_compose_project_name]
            + compose_file_args
            + ["down", "-v", "--remove-orphans"]
        )
        subprocess.run(
            down_cmd_on_fail,
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=pytestconfig.rootdir,
        )
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        raise

    # Yield control to the tests
    yield

    # Teardown: Capture logs on test failure
    if request.session.testsfailed > 0:
        print(
            f"\n--- DUMPING ALL CONTAINER LOGS DUE TO TEST FAILURE in project '{docker_compose_project_name}' ---"
        )
        try:
            logs_cmd_on_fail = (
                ["docker", "compose", "-p", docker_compose_project_name]
                + compose_file_args
                + ["logs", "--no-color"]
            )
            logs_result = subprocess.run(
                logs_cmd_on_fail,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print(f"\n{logs_result.stdout}\n{logs_result.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs during test teardown: {log_e}")

    print(
        f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'..."
    )
    down_cmd = (
        ["docker", "compose", "-p", docker_compose_project_name]
        + compose_file_args
        + ["down", "-v", "--remove-orphans"]
    )
    subprocess.run(
        down_cmd,
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=env_vars,
        cwd=pytestconfig.rootdir,
    )
    print("Teardown complete.")


@pytest.fixture(scope="session")
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = None
    try:
        if _local_docker_host_from_file:
            client = docker.DockerClient(base_url=_local_docker_host_from_file)
            print(
                "\nAttempting to connect Docker client to remote: "
                f"{_local_docker_host_from_file}"
            )
        else:
            client = docker.from_env()
            print(
                "\nAttempting to connect Docker client using default (local or CI) "
                "environment variables."
            )

        client.ping()
        print("Successfully connected to Docker daemon for Docker client fixture.")
    except docker.errors.DockerException as e:
        pytest.fail(
            "Could not connect to Docker daemon for client fixture. "
            f"Ensure Docker is running and DOCKER_HOST is correctly set. Error: {e}"
        )
    except Exception as e:
        pytest.fail(
            f"An unexpected error occurred while setting up Docker client fixture: {e}"
        )

    yield client

    if client:
        client.close()
