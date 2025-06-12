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
    Starts Docker Compose services in stages for a clean test environment.
    """
    compose_file_name = pytestconfig.getoption("compose_file")
    compose_file_path = str(pytestconfig.rootdir / compose_file_name)
    
    print(f"\nStarting Docker Compose project '{docker_compose_project_name}' from {compose_file_path}...")
    try:
        # Step 1: Start the main nether-bridge service
        print("Starting core 'nether-bridge' service...")
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'up', '-d', '--build', 'nether-bridge'],
            check=True
        )
        # Step 2: Create the server containers but do not start them
        print("Creating server containers in a stopped state...")
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'create', 'mc-bedrock', 'mc-java'],
            check=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' is set up.")
    except subprocess.CalledProcessError as e:
        print(f"Error setting up Docker Compose services.")
        raise

    class ServiceManager:
        # ... (rest of the class is unchanged)
        def __init__(self, project_name):
            self.project_name = project_name
            self.client = docker.from_env()

        def get_container_name(self, service_name):
            filters = {'label': f'com.docker.compose.project={self.project_name}'}
            containers = self.client.containers.list(all=True, filters=filters)
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