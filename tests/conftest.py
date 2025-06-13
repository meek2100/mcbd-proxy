# tests/conftest.py
import pytest
import docker
import subprocess
import time
import os
from pathlib import Path
import sys

# Try to load local environment specific IP and DOCKER_HOST for testing
_local_vm_host_ip = None
_local_docker_host_from_file = None # Renamed to avoid confusion with os.environ.get

try:
    current_tests_dir = str(Path(__file__).parent)
    if current_tests_dir not in sys.path:
        sys.path.insert(0, current_tests_dir)
    
    from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP
    _local_vm_host_ip = LOCAL_VM_HOST_IP
    os.environ['VM_HOST_IP'] = LOCAL_VM_HOST_IP # Set for test_integration.py to use
    print(f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")

    from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST_VALUE
    _local_docker_host_from_file = LOCAL_DOCKER_HOST_VALUE # Store for explicit use in fixture
    os.environ['DOCKER_HOST'] = LOCAL_DOCKER_HOST_VALUE # Also set in os.environ for subprocess
    print(f"Using local DOCKER_HOST from local_env.py: {LOCAL_DOCKER_HOST_VALUE}")

except ImportError:
    print("local_env.py not found in tests/. Relying on environment or default 127.0.0.1 for CI/CD.")
finally:
    if 'current_tests_dir' in locals() and current_tests_dir in sys.path:
        sys.path.remove(current_tests_dir)


def pytest_addoption(parser):
    """Add a command line option to specify the docker-compose file."""
    parser.addoption(
        "--compose-file", action="store", default="tests/docker-compose.tests.yml",
        help="Specify the docker-compose file to use for tests."
    )

@pytest.fixture(scope='session')
def docker_compose_project_name():
    """Generates a unique project name for docker-compose to isolate test runs."""
    return f"netherbridge_test_{int(time.time())}"

@pytest.fixture(scope='session')
def docker_compose_up(docker_compose_project_name, pytestconfig):
    """
    Starts Docker Compose services before tests and tears them down afterwards.
    This fixture relies on the 'docker compose' CLI directly.
    """
    compose_file_path = str(pytestconfig.rootdir / 'tests' / 'docker-compose.tests.yml')
    
    env_vars = os.environ.copy()
    if _local_docker_host_from_file:
        env_vars['DOCKER_HOST'] = _local_docker_host_from_file
        print(f"Passing DOCKER_HOST={env_vars['DOCKER_HOST']} to subprocess commands.")
    elif 'DOCKER_HOST' in env_vars:
        print(f"DOCKER_HOST is already set in environment for subprocesses: {env_vars['DOCKER_HOST']}")
    else:
        print("DOCKER_HOST not set by local_env.py or host environment. Subprocesses will use default Docker context.")

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")

    # Aggressive Pre-cleanup for ANY previous test containers
    print("Performing aggressive pre-cleanup of any stale test containers...")
    try:
        # Remove any containers from previous test runs based on the project name prefix
        list_cmd_prefix = ['docker', 'ps', '-aq', '--filter', 'name=netherbridge_test_']
        result_prefix = subprocess.run(list_cmd_prefix, capture_output=True, text=True, check=False, env=env_vars)
        prefix_ids = result_prefix.stdout.strip().splitlines()
        if prefix_ids:
            print(f"Found stale containers by prefix: {', '.join(prefix_ids)}. Forcibly removing...")
            subprocess.run(['docker', 'rm', '-f'] + prefix_ids, check=False, capture_output=True, text=True, env=env_vars)
        
        # Also specifically remove containers by their hardcoded names, in case they were left orphaned
        hardcoded_names_to_remove = ["nether-bridge", "mc-bedrock", "mc-java"]
        for name in hardcoded_names_to_remove:
            list_cmd_hardcoded = ['docker', 'ps', '-aq', '--filter', f'name={name}']
            result_hardcoded = subprocess.run(list_cmd_hardcoded, capture_output=True, text=True, check=False, env=env_vars)
            hardcoded_ids = result_hardcoded.stdout.strip().splitlines()
            if hardcoded_ids:
                print(f"Found stale container(s) with name '{name}': {', '.join(hardcoded_ids)}. Forcibly removing...")
                subprocess.run(['docker', 'rm', '-f'] + hardcoded_ids, check=False, capture_output=True, text=True, env=env_vars)

    except Exception as e:
        print(f"Warning during aggressive pre-cleanup: {e}")

    try:
        # Build the nether-bridge image first (explicitly for the test build context)
        build_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'build', 'nether-bridge']
        print(f"Running command: {' '.join(build_command)}")
        subprocess.run(build_command, check=True, capture_output=True, text=True, env=env_vars)
        print("Nether-bridge image built successfully for testing.")

        # 1. Create all services, but do not start them.
        # This ensures mc-java and mc-bedrock exist in a 'created' state.
        create_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'create']
        print(f"Running command: {' '.join(create_command)}")
        subprocess.run(create_command, check=True, capture_output=True, text=True, env=env_vars)
        print("All test containers created successfully.")

        # 2. Start only the nether-bridge service.
        start_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'start', 'nether-bridge']
        print(f"Running command: {' '.join(start_command)}")
        subprocess.run(start_command, check=True, capture_output=True, text=True, env=env_vars)
        print("Nether-bridge container started.")

        # 3. Manually wait for the nether-bridge container to become healthy.
        print("Waiting for nether-bridge container to become healthy...")
        client = docker.from_env(environment=env_vars)
        try:
            container = client.containers.get('nether-bridge')
            timeout = 120  # 2 minutes
            start_time = time.time()
            while time.time() - start_time < timeout:
                container.reload()
                health_status = container.attrs.get('State', {}).get('Health', {}).get('Status')
                if health_status == 'healthy':
                    print("Nether-bridge is healthy.")
                    break
                time.sleep(2)
            else: # This 'else' belongs to the 'while' loop
                health_log = container.attrs.get('State', {}).get('Health', {}).get('Log')
                last_log = health_log[-1] if health_log else "No health log."
                raise Exception(f"Timeout waiting for nether-bridge container to become healthy. Last status: {health_status}. Last log: {last_log}")
        finally:
            client.close()
        
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services: {e.stderr}")
        print(f"\n--- Logs for project '{docker_compose_project_name}' (if available) ---")
        try:
            logs_cmd = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'logs']
            logs = subprocess.run(logs_cmd, capture_output=True, text=True, check=False, env=env_vars)
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs: {log_e}")
        print(f"\nAttempting forceful teardown after setup failure for '{docker_compose_project_name}'...")
        try:
            subprocess.run(
                ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
                check=False,
                capture_output=True,
                text=True,
                env=env_vars
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        print(f"\nAttempting forceful teardown after unexpected setup error for '{docker_compose_project_name}'...")
        try:
            subprocess.run(
                ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
                check=False,
                capture_output=True,
                text=True,
                env=env_vars
            )
            print("Forceful teardown initiated.")
        except Exception as teardown_e:
            print(f"Error during forceful teardown: {teardown_e}")
        raise

    yield # Yield control to tests

    # The existing teardown logic is already correct.
    # `down --volumes` will remove the named volumes we created.
    print(f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'...")
    try:
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
            check=True,
            capture_output=True,
            text=True,
            env=env_vars
        )
        print(f"Docker Compose project '{docker_compose_project_name}' stopped and removed.")
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose teardown: {e}")


@pytest.fixture(scope='session')
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = None
    try:
        # If _local_docker_host_from_file is set, it means local_env.py was found,
        # so explicitly connect to the remote DOCKER_HOST.
        if _local_docker_host_from_file:
            client = docker.DockerClient(base_url=_local_docker_host_from_file)
            print(f"\nAttempting to connect Docker client to remote: {_local_docker_host_from_file}")
        else:
            # Otherwise, rely on docker.from_env() which will pick up local named pipes/sockets
            # or DOCKER_HOST if set in the CI environment.
            client = docker.from_env()
            print("\nAttempting to connect Docker client using default (local or CI) environment variables.")
        
        client.ping()
        print("Successfully connected to Docker daemon for Docker client fixture.")
    except docker.errors.DockerException as e:
        pytest.fail(f"Could not connect to Docker daemon for client fixture. Ensure Docker is running and DOCKER_HOST is correctly set. Error: {e}")
    except Exception as e:
        pytest.fail(f"An unexpected error occurred while setting up Docker client fixture: {e}")
    
    yield client
    
    if client:
        client.close()