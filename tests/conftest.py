# tests/conftest.py
import pytest
import docker
import subprocess
import time
import os
from pathlib import Path
import sys
import re
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
    This fixture relies on the 'docker compose' CLI directly.
    """
    original_compose_file_path_abs = Path(
        pytestconfig.rootdir
    ) / pytestconfig.getoption("--compose-file")

    # This will be the path to the compose file to use in subprocess calls.
    # It might be the original file, or a temporary one.
    compose_file_to_use_abs = original_compose_file_path_abs
    compose_file_to_use_rel_to_root = pytestconfig.getoption(
        "--compose-file"
    )  # Default relative path

    temp_file_for_cleanup = None  # To track the temp file path for cleanup

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
        f"{original_compose_file_path_abs}..."
    )

    # Aggressive Pre-cleanup (unchanged from previous version)
    # ... (omitted for brevity) ...
    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        list_cmd_prefix = ["docker", "ps", "-aq", "--filter", "name=netherbridge_test_"]
        result_prefix = subprocess.run(
            list_cmd_prefix,
            capture_output=True,
            encoding="utf-8",
            check=False,
            env=env_vars,
        )
        prefix_ids = result_prefix.stdout.strip().splitlines()
        if prefix_ids:
            print(
                f"Found stale containers by prefix: {', '.join(prefix_ids)}. "
                "Forcibly removing..."
            )
            subprocess.run(
                ["docker", "rm", "-f"] + prefix_ids,
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
            )

        hardcoded_names_to_remove = [
            "nether-bridge",
            "mc-bedrock",
            "mc-java",
            "tests-tester-1",
        ]
        for name in hardcoded_names_to_remove:
            list_cmd_hardcoded = ["docker", "ps", "-aq", "--filter", f"name={name}"]
            result_hardcoded = subprocess.run(
                list_cmd_hardcoded,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
            )
            hardcoded_ids = result_hardcoded.stdout.strip().splitlines()
            if hardcoded_ids:
                print(
                    f"Found stale container(s) with name '{name}': "
                    f"{', '.join(hardcoded_ids)}. Forcibly removing..."
                )
                subprocess.run(
                    ["docker", "rm", "-f"] + hardcoded_ids,
                    check=False,
                    capture_output=True,
                    encoding="utf-8",
                    env=env_vars,
                )

    except Exception as e:
        print(f"Warning during aggressive pre-cleanup: {e}")

    try:
        # --- Dynamic modification of docker-compose.tests.yml for remote build contexts and volumes ---
        if _local_docker_host_from_file:  # Only if a remote Docker host is specified
            print(
                "Detected remote Docker host. Temporarily modifying docker-compose.tests.yml for remote build contexts and volumes."
            )
            original_content = original_compose_file_path_abs.read_text(
                encoding="utf-8"
            )

            modified_content = original_content

            # 1. Change build contexts to '.' for ALL services with a build context
            # This ensures the build context is the current directory (project root) when invoked.
            modified_content = re.sub(
                r"(\s*build:\s*\n\s*context:)\s*\.\./?",  # matches "context: ../" or "context: ./"
                r"\1 ./",  # changes it to "context: ./"
                modified_content,
            )

            # 2. Comment out the host bind mount for 'tester' service
            # This avoids sending a Windows path to the Linux daemon for volumes.
            volume_pattern = (
                r"(\s*tester:\s*\n(?:.*\n)*?\s*volumes:\s*\n)(\s*-\s*\.?\./:/app\s*)"
            )
            modified_content = re.sub(
                volume_pattern,
                r"\1#\2 # Commented out for remote Docker testing to avoid invalid Windows path spec",
                modified_content,
                flags=re.DOTALL,
            )

            if (
                original_content == modified_content
                and "volumes:" in original_content
                and "tester:" in original_content
            ):
                print(
                    "Warning: No matching bind mount volume entry found for 'tester' to comment out. Ensure 'tester' service has '- ../:/app' or '- .:/app' volume entry."
                )

            # Write to a temporary file directly in the project root.
            # This makes the compose file and the Dockerfile share the same root context.
            temp_compose_file_name = f"docker-compose.tests.temp_{int(time.time())}.yml"
            temp_compose_file_path_abs = pytestconfig.rootdir / temp_compose_file_name
            temp_compose_file_path_abs.write_text(modified_content, encoding="utf-8")
            print(f"Using temporary docker-compose file: {temp_compose_file_path_abs}")

            compose_file_to_use_abs = temp_compose_file_path_abs  # Update absolute path
            compose_file_to_use_rel_to_root = Path(
                temp_compose_file_name
            )  # Update relative path
            temp_file_for_cleanup = (
                temp_compose_file_path_abs  # Mark temp file for cleanup
            )

        print("Checking if test images need to be built...")
        build_command = [
            "docker",
            "compose",
            "-p",
            docker_compose_project_name,
            "-f",
            str(compose_file_to_use_rel_to_root),  # Use relative path here
            "build",
            "nether-bridge",
            "tester",
        ]

        # All docker compose commands will be executed with CWD as the project root.
        # This is crucial for Docker to correctly package the build context.
        print(f"Running build command from CWD: {pytestconfig.rootdir}")
        build_result = subprocess.run(
            build_command,
            cwd=pytestconfig.rootdir,  # IMPORTANT: Set CWD to project root
            capture_output=True,
            encoding="utf-8",
            check=False,
            env=env_vars,
        )
        if build_result.returncode != 0:
            print(f"Error during image build:\n{build_result.stderr}")
            if (
                "no such file or directory" in build_result.stderr.lower()
                and "dockerfile" in build_result.stderr.lower()
            ):
                print("Hint: Check Dockerfile is present in the project root.")
            raise subprocess.CalledProcessError(
                build_result.returncode,
                build_result.args,
                output=build_result.stdout,
                stderr=build_result.stderr,
            )
        print("Nether-bridge and Tester images built successfully (or up-to-date).")

        create_command = [
            "docker",
            "compose",
            "-p",
            docker_compose_project_name,
            "-f",
            str(compose_file_to_use_rel_to_root),  # Use relative path
            "create",
        ]
        print(f"Running command: {' '.join(create_command)}")
        subprocess.run(
            create_command,
            cwd=pytestconfig.rootdir,  # Set CWD to project root
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
        )
        print("All test containers created successfully.")

        start_command = [
            "docker",
            "compose",
            "-p",
            docker_compose_project_name,
            "-f",
            str(compose_file_to_use_rel_to_root),  # Use relative path
            "start",
            "nether-bridge",
            "tester",
        ]
        print(f"Running command: {' '.join(start_command)}")
        subprocess.run(
            start_command,
            cwd=pytestconfig.rootdir,  # Set CWD to project root
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
        )
        print("Nether-bridge and Tester containers started.")

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
            logs_cmd = [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_rel_to_root),  # Use relative path
                "logs",
            ]
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,  # CWD for logs command
            )
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs: {log_e}")
        print(
            f"\nAttempting forceful teardown after setup failure for '{docker_compose_project_name}'..."
        )
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    docker_compose_project_name,
                    "-f",
                    str(compose_file_to_use_rel_to_root),  # Use relative path
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
                cwd=pytestconfig.rootdir,  # CWD for teardown
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        print(
            f"\nAttempting forceful teardown after unexpected setup error for '{docker_compose_project_name}'..."
        )
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    docker_compose_project_name,
                    "-f",
                    str(compose_file_to_use_rel_to_root),  # Use relative path
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=env_vars,
                cwd=pytestconfig.rootdir,  # CWD for teardown
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise

    # Yield control to the tests
    yield

    # Teardown: Capture logs on test failure
    if request.session.testsfailed > 0:
        print(
            f"\n--- DUMPING ALL CONTAINER LOGS DUE TO TEST FAILURE in project '{docker_compose_project_name}' ---"
        )
        try:
            logs_cmd = [
                "docker",
                "compose",
                "-p",
                docker_compose_project_name,
                "-f",
                str(compose_file_to_use_rel_to_root),  # Use relative path
                "logs",
            ]
            logs = subprocess.run(
                logs_cmd,
                capture_output=True,
                encoding="utf-8",
                check=False,
                env=env_vars,
                cwd=pytestconfig.rootdir,  # CWD for logs
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
                str(compose_file_to_use_rel_to_root),  # Use relative path
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env_vars,
            cwd=pytestconfig.rootdir,  # CWD for teardown
        )
        print(
            f"Docker Compose project '{docker_compose_project_name}' "
            "stopped and removed."
        )
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose teardown: {e}")
    finally:
        # Clean up the temporary file
        if temp_file_for_cleanup and temp_file_for_cleanup.exists():
            temp_file_for_cleanup.unlink()  # Delete the file
            print(f"Cleaned up temporary compose file: {temp_file_for_cleanup}")


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
            "An unexpected error occurred while setting up Docker client fixture: "
            f"{e}"
        )

    yield client

    if client:
        client.close()
