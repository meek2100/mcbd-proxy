import pytest
import docker
import subprocess
import time
import os
import json
from pathlib import Path

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
    Starts Docker Compose services in stages for a clean test environment,
    creates a dynamic servers.json, and yields a helper to manage services.
    """
    compose_file_name = pytestconfig.getoption("compose_file")
    compose_file_path = str(pytestconfig.rootdir / compose_file_name)
    
    class ServiceManager:
        def __init__(self, project_name):
            self.project_name = project_name
            self.client = docker.from_env()
            self._container_map = {}

        def get_container(self, service_name):
            if service_name in self._container_map:
                return self._container_map[service_name]
            
            filters = {'label': f'com.docker.compose.project={self.project_name}'}
            containers = self.client.containers.list(all=True, filters=filters)
            for container in containers:
                if container.labels.get('com.docker.compose.service') == service_name:
                    self._container_map[service_name] = container
                    return container
            raise RuntimeError(f"Could not find container for service '{service_name}' in project '{self.project_name}'")

        def get_container_name(self, service_name):
            return self.get_container(service_name).name

    # Step 1: Create all containers but do not start them.
    print("\nCreating Docker containers for test session...")
    create_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'create']
    try:
        subprocess.run(create_command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Error creating initial containers: {e.stderr}")
        raise

    service_manager = ServiceManager(docker_compose_project_name)
    
    # Step 2: Dynamically generate the servers.json file with the correct container names.
    servers_config = {
        "servers": [
            {
                "name": "Bedrock Test", "server_type": "bedrock", "listen_port": 19132,
                "container_name": service_manager.get_container_name("mc-bedrock"), "internal_port": 19132
            },
            {
                "name": "Java Test", "server_type": "java", "listen_port": 25565,
                "container_name": service_manager.get_container_name("mc-java"), "internal_port": 25565
            }
        ]
    }
    servers_json_path = Path(pytestconfig.rootdir) / "servers.tests.json"
    with open(servers_json_path, "w") as f:
        json.dump(servers_config, f, indent=2)
    print(f"Dynamically generated '{servers_json_path.name}' for test run.")
    
    # Step 3: Now, start the proxy container. It will use the new servers.tests.json
    print("Starting 'nether-bridge' service...")
    start_command = ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'up', '-d', '--build', 'nether-bridge']
    try:
        subprocess.run(start_command, check=True)
        print(f"Docker Compose project '{docker_compose_project_name}' is set up.")
    except subprocess.CalledProcessError:
        print("Error starting 'nether-bridge' service.")
        raise

    yield service_manager

    # --- Teardown ---
    print(f"\nTests finished. Tearing down Docker Compose project '{docker_compose_project_name}'...")
    try:
        subprocess.run(
            ['docker', 'compose', '-p', docker_compose_project_name, '-f', compose_file_path, 'down', '--volumes', '--remove-orphans'],
            check=True, capture_output=True, text=True
        )
        print(f"Docker Compose project '{docker_compose_project_name}' stopped and removed.")
        os.remove(servers_json_path)
        print(f"Removed temporary config file '{servers_json_path.name}'.")
    except Exception as e:
        print(f"Error during teardown: {e}")

@pytest.fixture(scope='session')
def docker_client_fixture():
    """Provides a Docker client instance for integration tests."""
    client = docker.from_env()
    yield client
    client.close()