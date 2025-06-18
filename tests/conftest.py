# tests/conftest.py
import pytest
import docker
import subprocess
import time
import os
from pathlib import Path
import sys
import shutil

# Try to load local environment specific IP and DOCKER_HOST for testing
_local_vm_host_ip = None
_local_docker_host_from_file = None

try:
    current_tests_dir = str(Path(__file__).parent)
    if current_tests_dir not in sys.path:
        sys.path.insert(0, current_tests_dir)

    from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP

    _local_vm_host_ip = LOCAL_VM_HOST_IP
    os.environ["VM_HOST_IP"] = LOCAL_VM_HOST_IP
    print(
        f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}" # E501: Line split
    )

    from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST_VALUE

    _local_docker_host_from_file = LOCAL_DOCKER_HOST_VALUE
    os.environ["DOCKER_HOST"] = LOCAL_DOCKER_HOST_VALUE
    print(
        f"Using local DOCKER_HOST from local_env.py: " # E501: Line split
        f"{LOCAL_DOCKER_HOST_VALUE}"
    )

except ImportError:
    print(
        "local_env.py not found in tests/. Relying on environment or " # E501: Line split
        "default 127.0.0.1 for local/CI."
    )
finally:
    if "current_tests_dir" in locals() and current_tests_dir in sys.path:
        sys.path.remove(current_tests_dir)


def pytest_addoption(parser):
    """Add a command line option to specify the docker-compose file."""
    parser.addoption(
        "--compose-file",
        action="store",
        default="tests/docker-compose.tests.yml", # E501: Line split
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
    # In CI, the workflow file handles setup/teardown. This fixture should be a no-op.
    if os.environ.get("CI_MODE"):
        print(
            "CI_MODE detected. Skipping Docker Compose management from conftest." # E501: Line split
        )
        yield
        return

    compose_file_to_use_abs = Path(
        pytestconfig.rootdir
    ) / pytestconfig.getoption("--compose-file")

    temp_compose_file_dir = None
    temp_compose_file_path_abs = None

    env_vars = os.environ.copy()
    if _local_docker_host_from_file:
        env_vars["DOCKER_HOST"] = _local_docker_host_from_file
        print(
            f"Passing DOCKER_HOST={env_vars['DOCKER_HOST']} to subprocess " # E501: Line split
            "commands."
        )
    elif "DOCKER_HOST" in env_vars:
        print(
            "DOCKER_HOST is already set in environment for subprocesses: "
            f"{env_vars['DOCKER_HOST']}"
        )
    else:
        print(
            "DOCKER_HOST not set by local_env.py or host environment. " # E501: Line split
            "Subprocesses will use default Docker context."
        )

    print(
        f"\nStarting Docker Compose project '{docker_compose_project_name}' " # E501: Line split
        f"from {compose_file_to_use_abs}..."
    )

    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        hardcoded_names_to_remove = [
            "nether-bridge",
            "mc-bedrock",
            "mc-java",
            "nb-tester",
        ]
        list_cmd = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "name=netherbridge_test_",
        ]
        for name in hardcoded_names_to_remove:
            list_cmd.extend(["--filter", f"name={name}"])

        result = subprocess.run(
            list_cmd,
            capture_output=True,
            encoding="utf-8",
            check=False,
            env=env_vars,
        )
        stale_ids = result.stdout.strip().splitlines()
        if stale_ids:
            print(
                f"Found stale containers: {', '.join(stale_ids)}. " # E501: Line split
                "Forcibly removing..."
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
        up_command = [
            "docker",
            "compose",
            "-p",
            docker_compose_project_name,
            "-f",
            str(compose_file_to_use_abs),
            "up",
            "--build",
            "--force-recreate",
            "--remove-orphans",
            "-d",
        ]
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
                    container.attrs.get("State", {})
                    .get("Health", {})
                    .get("Status")
                )
                if health_status == "healthy":
                    print("Nether-bridge is healthy.")
                    break
                time.sleep(2)
            else:
                health_log = (
                    container.attrs.get("State", {})
                    .get("Health", {})
                    .get("Log")
                )
                last_log = health_log[-1] if health_log else "No health log."
                raise Exception(
                    "Timeout waiting for nether-bridge container to become " # E501: Line split
                    f"healthy. Last status: {health_status}. Last log: {last_log}"
                )
        finally:
            client.close()

    except subprocess.CalledProcessError as e:
        print(f"Error during Docker Compose setup: {e.stderr}")
        print(
            f"\n--- Logs for project '{docker_compose_project_name}' " # E501: Line split
            "(during setup failure) ---"
        )
        try:
            logs_cmd = [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_abs),
                "logs",
            ]
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs: {log_e}")
        print(
            "Attempting forceful teardown after setup failure for " # E501: Line split
            f"'{docker_compose_project_name}'..."
        )
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    docker_compose_project_name,
                    "-f",
                    str(compose_file_to_use_abs),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise
    except Exception as e:
        print(
            "An unexpected error occurred during Docker Compose setup: " # E501: Line split
            f"{e}"
        )
        print(
            "Attempting forceful teardown after unexpected setup error for " # E501: Line split
            f"'{docker_compose_project_name}'..."
        )
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    docker_compose_project_name,
                    "-f",
                    str(compose_file_to_use_abs),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise

    # Yield control to the tests
    yield request.param if hasattr(request, "param") else None

    # Teardown: Capture logs on test failure
    if request.session.testsfailed > 0:
        print(
            "--- DUMPING ALL CONTAINER LOGS DUE TO TEST FAILURE in project " # E501: Line split
            f"'{docker_compose_project_name}' ---"
        )
        try:
            logs_cmd = [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_abs),
                "logs",
            ]
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs during test teardown: {log_e}")
        print("--- END LOG DUMP ---")

    print(
        f"\nTests finished. Tearing down Docker Compose project "
        f"'{docker_compose_project_name}'..."
    )
    try:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_abs),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=pytestconfig.rootdir,
        )
        print(
            f"Docker Compose project '{docker_compose_project_name}' " # E501: Line split
            "stopped and removed."
        )
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
    except Exception as e:
        print(
            "An unexpected error occurred during Docker Compose teardown: " # E501: Line split
            f"{e}"
        )
    finally:
        if temp_compose_file_path_abs and temp_compose_file_path_abs.exists():
            try:
                os.remove(str(temp_compose_file_path_abs))
            except OSError as e:
                print(
                    "Warning: Could not remove temporary compose file " # E501: Line split
                    f"{temp_compose_file_path_abs}: {e}"
                )

        if temp_compose_file_dir and temp_compose_file_dir.exists():
            try:
                shutil.rmtree(temp_compose_file_dir, ignore_errors=True)
            except OSError as e:
                print(
                    "Warning: Could not remove temporary directory " # E501: Line split
                    f"{temp_compose_file_dir}: {e}"
                )


@pytest.fixture(scope="session")
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = None
    try:
        if _local_docker_host_from_file:
            client = docker.DockerClient(base_url=_local_docker_host_from_file)
            print(
                "\nAttempting to connect Docker client to remote: " # E501: Line split
                f"{_local_docker_host_from_file}"
            )
        else:
            client = docker.from_env()
            print(
                "\nAttempting to connect Docker client using default " # E501: Line split
                "(local or CI) environment variables."
            )

        client.ping()
        print(
            "Successfully connected to Docker daemon for Docker client fixture." # E501: Line split
        )
    except docker.errors.DockerException as e:
        pytest.fail(
            "Could not connect to Docker daemon for client fixture. " # E501: Line split
            "Ensure Docker is running and DOCKER_HOST is correctly set. " # E501: Line split
            f"Error: {e}"
        )
    except Exception as e:
        pytest.fail(
            "An unexpected error occurred while setting up Docker client " # E501: Line split
            f"fixture: {e}"
        )

    yield client

    if client:
        client.close()