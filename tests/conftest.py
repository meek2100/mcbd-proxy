# tests/conftest.py
import pytest
import docker
import subprocess
import time
import os
from pathlib import Path
import sys

_local_vm_host_ip = None
_local_docker_host_from_file = None 
_local_docker_gid = None

try:
    current_tests_dir = str(Path(__file__).parent)
    if current_tests_dir not in sys.path:
        sys.path.insert(0, current_tests_dir)

    from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP
    _local_vm_host_ip = LOCAL_VM_HOST_IP
    os.environ['VM_HOST_IP'] = LOCAL_VM_HOST_IP 
    print(f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")

    from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST_VALUE
    _local_docker_host_from_file = LOCAL_DOCKER_HOST_VALUE
    print(f"Using local DOCKER_HOST from local_env.py: {LOCAL_DOCKER_HOST_VALUE}")

    from local_env import DOCKER_GID as LOCAL_DOCKER_GID
    _local_docker_gid = str(LOCAL_DOCKER_GID)
    print(f"Using local DOCKER_GID from local_env.py: {_local_docker_gid}")

except ImportError:
    print("local_env.py not found or incomplete. Relying on environment or defaults for CI/CD and local Docker Desktop.")
finally:
    if 'current_tests_dir' in locals() and current_tests_dir in sys.path:
        sys.path.remove(current_tests_dir)


def pytest_addoption(parser):
    parser.addoption(
        "--compose-file", action="store", default="tests/docker-compose.tests.yml",
        help="Specify the docker-compose file to use for tests."
    )

@pytest.fixture(scope='session')
def docker_compose_project_name():
    return f"netherbridge_test_{int(time.time())}"

@pytest.fixture(scope='session')
def docker_compose_up(docker_compose_project_name, pytestconfig):
    compose_file_path = str(pytestconfig.rootdir / 'tests' / 'docker-compose.tests.yml')

    subprocess_env = os.environ.copy()
    if _local_docker_host_from_file:
        subprocess_env['DOCKER_HOST'] = _local_docker_host_from_file
        print(f"Passing DOCKER_HOST={subprocess_env['DOCKER_HOST']} to subprocess commands.")

    if _local_docker_gid:
        subprocess_env['DOCKER_GID'] = _local_docker_gid
        print(f"Passing DOCKER_GID={subprocess_env['DOCKER_GID']} to subprocess commands.")

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")

    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        hardcoded_names_to_remove = ["nether-bridge", "mc-bedrock", "mc-java"]
        for name in hardcoded_names_to_remove:
            list_cmd_hardcoded = ['docker', 'ps', '-aq', '--filter', f'name={name}']
            result_hardcoded = subprocess.run(list_cmd_hardcoded, capture_output=True, text=True, check=False, env=subprocess_env)
            hardcoded_ids = result_hardcoded.stdout.strip().splitlines()
            if hardcoded_ids:
                print(f"Found stale container(s) with name '{name}': {', '.join(hardcoded_ids)}. Forcibly removing...")
                subprocess.run(['docker', 'rm', '-f'] + hardcoded_ids, check=False, capture_output=True, text=True, env=subprocess_env)
    except Exception as e:
        print(f"Warning during aggressive pre-cleanup: {e}")

    try:
        build_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'build', '--no-cache', 'nether-bridge']
        print(f"Running command: {' '.join(build_command)}")
        subprocess.run(build_command, check=True, capture_output=True, text=True, env=subprocess_env)
        print("Nether-bridge image built successfully for testing.")

        create_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'create']
        print(f"Running command: {' '.join(create_command)}")
        subprocess.run(create_command, check=True, capture_output=True, text=True, env=subprocess_env)
        print("All test containers created successfully.")

        start_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'start', 'nether-bridge']
        print(f"Running command: {' '.join(start_command)}")
        subprocess.run(start_command, check=True, capture_output=True, text=True, env=subprocess_env)
        print("Nether-bridge container started.")

        print("Waiting for nether-bridge container to become healthy...")
        client = docker.DockerClient(base_url=_local_docker_host_from_file) if _local_docker_host_from_file else docker.from_env()
        try:
            container = client.containers.get('nether-bridge')
            timeout = 120
            start_time = time.time()
            while time.time() - start_time < timeout:
                container.reload()
                health_status = container.attrs.get('State', {}).get('Health', {}).get('Status')
                if health_status == 'healthy':
                    print("Nether-bridge is healthy.")
                    break
                time.sleep(2)
            else:
                health_log = container.attrs.get('State', {}).get('Health', {}).get('Log', [])
                last_log = health_log[-1] if health_log else {"Output": "No health log found."}
                raise Exception(f"Timeout waiting for nether-bridge container to become healthy. Last status: {health_status}. Last log: {last_log}")
        finally:
            client.close()

    except subprocess.CalledProcessError as e:
        print(f"Error during Docker Compose setup: {e.stderr}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        raise

    yield

    print(f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'...")
    try:
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
            check=True, capture_output=True, text=True, env=subprocess_env
        )
        print(f"Docker Compose project '{docker_compose_project_name}' stopped and removed.")
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose teardown: {e}")


@pytest.fixture(scope='session')
def docker_client_fixture():
    if _local_docker_host_from_file:
        print(f"\n[Fixture] Creating Docker client for remote: {_local_docker_host_from_file}")
        client = docker.DockerClient(base_url=_local_docker_host_from_file)
    else:
        print("\n[Fixture] Creating Docker client using default (local or CI) environment.")
        client = docker.from_env()

    try:
        client.ping()
        print("[Fixture] Successfully connected to Docker daemon.")
    except Exception as e:
        pytest.fail(f"Could not connect Docker client fixture to Docker daemon. Error: {e}")

    yield client

    if client:
        client.close()