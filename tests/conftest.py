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
    print(f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")

    from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST_VALUE

    _local_docker_host_from_file = LOCAL_DOCKER_HOST_VALUE
    os.environ["DOCKER_HOST"] = LOCAL_DOCKER_HOST_VALUE
    print(f"Using local DOCKER_HOST from local_env.py: {LOCAL_DOCKER_HOST_VALUE}")

except ImportError:
    print(
        "local_env.py not found in tests/. Relying on environment or default 127.0.0.1 for local/CI."
    )
finally:
    if "current_tests_dir" in locals() and current_tests_dir in sys.path:
        sys.path.remove(current_tests_dir)


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
    # In CI, the workflow file handles setup/teardown. This fixture should be a no-op.
    if os.environ.get("CI_MODE"):
        print("CI_MODE detected. Skipping Docker Compose management from conftest.")
        yield
        return

    compose_file_to_use_abs = Path(pytestconfig.rootdir) / pytestconfig.getoption(
        "--compose-file"
    )

    temp_compose_file_dir = None
    temp_compose_file_path_abs = None

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

    print(
        f"\nStarting Docker Compose project '{docker_compose_project_name}' from "
        f"{compose_file_to_use_abs}..."
    )

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
        print("Docker Compose environment is up.")

        print("Waiting for nether-bridge container to become healthy...")
        client = docker.from_env(environment=env_vars)
        try:
            container = client.containers.get(
                f"{docker_compose_project_name}-nether-bridge-1"
            )
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
        print(
            f"Error during Docker Compose setup:\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}"
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
                "--no-color",
            ]
            logs_result = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,
            )
            print(
                f"\n--- Logs from failed setup ---\n{logs_result.stdout}\n{logs_result.stderr}"
            )
        except Exception as log_e:
            print(f"Could not retrieve logs during setup failure: {log_e}")
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_abs),
                "down",
                "-v",
                "--remove-orphans",
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=pytestconfig.rootdir,
        )
        raise

    yield

    if request.session.testsfailed > 0:
        print(f"\n--- DUMPING LOGS DUE TO TEST FAILURE ---")
        try:
            logs_cmd = [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_abs),
                "logs",
                "--no-color",
            ]
            logs_result = subprocess.run(
                logs_cmd,
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
                "-v",
                "--remove-orphans",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=pytestconfig.rootdir,
        )
        print(
            f"Docker Compose project '{docker_compose_project_name}' stopped and removed."
        )
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
    finally:
        if temp_compose_file_path_abs and temp_compose_file_path_abs.exists():
            try:
                os.remove(str(temp_compose_file_path_abs))
                print(
                    f"Cleaned up temporary compose file: {temp_compose_file_path_abs}"
                )
            except OSError as e:
                print(
                    f"Warning: Could not remove temporary compose file {temp_compose_file_path_abs}: {e}"
                )
        if temp_compose_file_dir and temp_compose_file_dir.exists():
            try:
                shutil.rmtree(temp_compose_file_dir, ignore_errors=True)
                print(
                    f"Cleaned up temporary compose directory: {temp_compose_file_dir}"
                )
            except OSError as e:
                print(
                    f"Warning: Could not remove temporary directory {temp_compose_file_dir}: {e}"
                )


@pytest.fixture(scope="session")
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = None
    try:
        if _local_docker_host_from_file:
            client = docker.DockerClient(base_url=_local_docker_host_from_file)
            print(
                f"\nConnecting Docker client to remote: {_local_docker_host_from_file}"
            )
        else:
            client = docker.from_env()
            print("\nConnecting Docker client using default environment.")

        client.ping()
        print("Successfully connected to Docker daemon.")
    except Exception as e:
        pytest.fail(
            f"Could not connect to Docker daemon for client fixture. Error: {e}"
        )

    yield client

    if client:
        client.close()
