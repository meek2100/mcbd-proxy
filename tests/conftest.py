import pytest
import docker
import subprocess
import time
import os
import json

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
def docker_services(docker_compose_project_name, pytestconfig):
    """
    Starts Docker Compose services, waits for them to be ready, and yields
    a helper object to get service container names.
    """
    compose_file_name = pytestconfig.getoption("compose_file")
    compose_file_path = str(pytestconfig.rootdir / compose_file_name)
    
    command = [
        'docker', 'compose', 
        '-p', docker_compose_project_name, 
        '-f', compose_file_path, 
        'up', '-d', '--build'
    ]

    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        subprocess.run(command, check=True)
        print(f"Docker Compose project '{docker_compose_project_name}' started.")
    except subprocess.CalledProcessError as e:
        print(f"Error starting Docker Compose services.")
        raise

    # --- New Helper Class to find container names ---
    class ServiceManager:
        def __init__(self, project_name):
            self.project_name = project_name
            self.client = docker.from_env()

        def get_container_name(self, service_name):
            """Finds the full container name for a given service."""
            # List all containers for this compose project
            filters = {'label': f'com.docker.compose.project={self.project_name}'}
            containers = self.client.containers.list(filters=filters)
            for container in containers:
                service_label = container.labels.get('com.docker.compose.service')
                if service_label == service_name:
                    return container.name
            raise RuntimeError(f"Could not find container for service '{service_name}' in project '{self.project_name}'")

    yield ServiceManager(docker_compose_project_name)

    # --- Teardown ---
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