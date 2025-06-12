import pytest
import docker
import subprocess
import time
import os

def pytest_addoption(parser):
    """Add a command line option to specify the docker-compose file."""
    parser.addoption(
        "--compose-file", action="store", default="docker-compose.yml",
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
    """
    # Use the compose file specified on the command line, or the default
    compose_file_name = pytestconfig.getoption("compose_file")
    compose_file_path = str(pytestconfig.rootdir / compose_file_name)
    
    # Add --build flag to ensure the image is built from source in CI
    command = [
        'docker', 'compose', 
        '-p', docker_compose_project_name, 
        '-f', compose_file_path, 
        'up', '-d', '--build'
    ]

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' started.")
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services: {e.stderr}")
        raise

    yield

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

@pytest.fixture(scope='session')
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = docker.from_env()
    yield client
    client.close()