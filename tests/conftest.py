import pytest
import docker
import subprocess
import time
import os
import json
from pathlib import Path
import sys

# Add this block at the top of conftest.py
# Try to load local environment specific IP and DOCKER_HOST for testing
_local_docker_host = None
try:
    current_tests_dir = str(Path(__file__).parent)
    if current_tests_dir not in sys.path:
        sys.path.insert(0, current_tests_dir)
    
    from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP
    os.environ['VM_HOST_IP'] = LOCAL_VM_HOST_IP
    print(f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")

    # New: Handle DOCKER_HOST from local_env.py
    from local_env import DOCKER_HOST as LOCAL_DOCKER_HOST
    _local_docker_host = LOCAL_DOCKER_HOST # Store it for later use in subprocess.run
    print(f"Using local DOCKER_HOST from local_env.py: {LOCAL_DOCKER_HOST}")

except ImportError:
    print("local_env.py not found in tests/. Relying on environment or default 127.0.0.1 for CI/CD.")
finally:
    if current_tests_dir in sys.path:
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
    
    # Create a copy of the current environment to pass to subprocesses
    env_vars = os.environ.copy()
    # Explicitly set DOCKER_HOST for subprocesses if it was loaded from local_env.py
    if _local_docker_host:
        env_vars['DOCKER_HOST'] = _local_docker_host
        print(f"Passing DOCKER_HOST={env_vars['DOCKER_HOST']} to subprocess commands.")
    elif 'DOCKER_HOST' in env_vars:
        print(f"DOCKER_HOST is already set in environment: {env_vars['DOCKER_HOST']}")
    else:
        print("DOCKER_HOST not set in local_env.py or environment. Subprocesses will use default Docker context.")


    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        # Build the nether-bridge image first (explicitly for the test build context)
        build_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'build', 'nether-bridge']
        print(f"Running command: {' '.join(build_command)}")
        subprocess.run(build_command, check=True, capture_output=True, text=True, env=env_vars) # <--- ADDED env=env_vars
        print("Nether-bridge image built successfully for testing.")

        # Use docker compose up -d to start services in detached mode
        up_command = [
            'docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path,
            'up', '-d', '--wait', '--wait-timeout', '240'
        ]
        print(f"Running command: {' '.join(up_command)}")
        subprocess.run(
            up_command,
            check=True,
            capture_output=True,
            text=True,
            env=env_vars # <--- ADDED env=env_vars
        )
        print(f"Docker Compose project '{docker_compose_project_name}' started and healthy.")
        time.sleep(5) # Give a small buffer for proxy to fully initialize after healthchecks
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services: {e.stderr}")
        print(f"\n--- Logs for project '{docker_compose_project_name}' (if available) ---")
        try:
            logs_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'logs']
            logs = subprocess.run(logs_command, capture_output=True, text=True, check=False, env=env_vars) # <--- ADDED env=env_vars
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs: {log_e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        raise

    yield # Yield control to tests

    print(f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'...")
    try:
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
            check=True,
            capture_output=True,
            text=True,
            env=env_vars # <--- ADDED env=env_vars
        )
        print(f"Docker Compose project '{docker_compose_project_name}' stopped and removed.")
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")