import pytest
import docker
import subprocess
import time
import os
import json
from pathlib import Path
import sys

# Add this block at the top of conftest.py
# Try to load local environment specific IP for testing
try:
    # Temporarily add tests/ to sys.path to import local_env
    # Only if not already there, to avoid duplicates.
    current_tests_dir = str(Path(__file__).parent)
    if current_tests_dir not in sys.path:
        sys.path.insert(0, current_tests_dir)
    
    from local_env import VM_HOST_IP as LOCAL_VM_HOST_IP
    os.environ['VM_HOST_IP'] = LOCAL_VM_HOST_IP
    print(f"Using local VM_HOST_IP from local_env.py: {LOCAL_VM_HOST_IP}")
except ImportError:
    print("local_env.py not found in tests/. Relying on environment or default 127.0.0.1 for CI/CD.")
    # If not found, rely on GitHub Actions to set VM_HOST_IP or use 127.0.0.1 default in test_integration.py
finally:
    # Clean up sys.path if added
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
    # Use a unique name to prevent conflicts if tests are run concurrently or repeatedly
    return f"netherbridge_test_{int(time.time())}"

@pytest.fixture(scope='session')
def docker_compose_up(docker_compose_project_name, pytestconfig):
    """
    Starts Docker Compose services before tests and tears them down afterwards.
    This fixture relies on the 'docker compose' CLI directly.
    """
    # Corrected path to the test-specific docker-compose file
    compose_file_path = str(pytestconfig.rootdir / 'tests' / 'docker-compose.tests.yml')
    
    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        # Build the nether-bridge image first (explicitly for the test build context)
        build_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'build', 'nether-bridge']
        print(f"Running command: {' '.join(build_command)}")
        subprocess.run(build_command, check=True, capture_output=True, text=True)
        print("Nether-bridge image built successfully for testing.")

        # Use docker compose up -d to start services in detached mode
        # Adding --wait and --wait-timeout for robustness
        up_command = [
            'docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path,
            'up', '-d', '--wait', '--wait-timeout', '240' # Increased timeout for services to become healthy
        ]
        print(f"Running command: {' '.join(up_command)}")
        subprocess.run(
            up_command,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' started and healthy.")
        # Give a small buffer for proxy to fully initialize after healthchecks
        time.sleep(5)
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services: {e.stderr}")
        # Capture and print logs for debugging if startup fails
        print(f"\n--- Logs for project '{docker_compose_project_name}' (if available) ---")
        try:
            logs_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'logs']
            logs = subprocess.run(logs_command, capture_output=True, text=True, check=False)
            print(logs.stdout)
            if logs.stderr:
                print(f"Stderr logs: {logs.stderr}")
        except Exception as log_e:
            print(f"Could not retrieve logs: {log_e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred during Docker Compose setup: {e}")
        raise

    # Yield control to tests
    yield

    # Teardown: Stop and remove services automatically after tests complete.
    print(f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'...")
    try:
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' stopped and removed.")
    except subprocess.CalledProcessError as e:
        print(f"Error tearing down Docker Compose services: {e.stderr}")
        # Don't re-raise, try to clean up as much as possible even if stopping fails

@pytest.fixture(scope='session')
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = docker.from_env()
    yield client
    client.close()