import pytest
import docker
import subprocess
import time
import os

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
    compose_file_path = str(pytestconfig.rootdir / 'docker-compose.yml')
    # Use subprocess to call 'docker compose' CLI
    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        # Use docker compose up -d to start services in detached mode
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'up', '-d'],
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' started.")
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services: {e.stderr}")
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
        print(f"Error stopping Docker Compose services: {e.stderr}")
        # Don't re-raise, try to clean up as much as possible even if stopping fails

@pytest.fixture(scope='session')
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = docker.from_env()
    yield client
    client.close()