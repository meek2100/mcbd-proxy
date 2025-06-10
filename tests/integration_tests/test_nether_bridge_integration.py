import pytest
import time
import socket
import docker
from mcstatus import BedrockServer, JavaServer

# Helper function to check container status via Docker API
def get_container_status(docker_client_fixture, container_name):
    try:
        container = docker_client_fixture.containers.get(container_name)
        return container.status
    except docker.errors.NotFound:
        return "not_found"
    except Exception as e:
        pytest.fail(f"Failed to get status for container {container_name}: {e}")

# Helper function to wait for a server to be query-ready via mcstatus
def wait_for_mc_server_ready(server_config, timeout=60, interval=1):
    host, port = server_config['host'], server_config['port']
    server_type = server_config['type']
    start_time = time.time()
    print(f"\nWaiting for {server_type} server at {host}:{port} to be ready...")

    while time.time() - start_time < timeout:
        try:
            status = None
            if server_type == 'bedrock':
                server = BedrockServer.lookup(f"{host}:{port}", timeout=interval)
                status = server.status()
            elif server_type == 'java':
                server = JavaServer.lookup(f"{host}:{port}", timeout=interval)
                status = server.status()

            if status:
                print(f"[{server_type}@{host}:{port}] Server responded! Latency: {status.latency:.2f}ms. Online players: {status.players.online}")
                return True
        except Exception as e:
            # print(f"[{server_type}@{host}:{port}] Query failed: {e}. Retrying...") # Can be noisy, uncomment for verbose debug
            pass
        time.sleep(interval)
    print(f"[{server_type}@{host}:{port}] Timeout waiting for server to be ready.")
    return False

# --- Integration Test Cases ---

@pytest.mark.integration
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture, docker_compose_project_name):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    bedrock_proxy_port = 19133 # As defined in docker-compose.yml
    service_name = "mc-bedrock"
    
    # 1. Ensure server is initially stopped by the proxy on startup
    print(f"\nWaiting for proxy to initialize and ensure {service_name} is stopped...")
    time.sleep(15) # Allow proxy to start and issue initial stop commands

    # Robustly find the actual container name using Docker client's list method
    actual_bedrock_container_name = None
    all_containers = docker_client_fixture.containers.list(all=True) # Get all containers, including stopped ones
    for container in all_containers:
        # Docker Compose V2 naming convention: <project_name>-<service_name>-<instance_number>
        if container.name.startswith(f"{docker_compose_project_name}-{service_name}"):
            actual_bedrock_container_name = container.name
            break

    assert actual_bedrock_container_name is not None, \
        f"Could not find Docker container for service '{service_name}' in project '{docker_compose_project_name}'."
    print(f"Found actual container name for {service_name}: {actual_bedrock_container_name}")

    initial_status = get_container_status(docker_client_fixture, actual_bedrock_container_name)
    assert initial_status in ["exited", "created", "stopped"], \
        f"Bedrock server should be initially stopped or not running, but is: {initial_status} (Container: {actual_bedrock_container_name})"
    print(f"\nInitial status of {actual_bedrock_container_name}: {initial_status}")

    # 2. Simulate a client connection to the proxy's Bedrock port
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Send a basic Minecraft Bedrock "Unconnected Ping" packet
        ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        print(f"Simulating connection to nether-bridge on port {bedrock_proxy_port}...")
        client_socket.sendto(ping_packet, ("127.0.0.1", bedrock_proxy_port))
        print("Dummy packet sent.")

        # 3. Wait for the Minecraft server container to start and become running
        max_wait_time = 30 # seconds
        check_interval = 2 # seconds
        start_time = time.time()
        server_started = False
        while time.time() - start_time < max_wait_time:
            current_status = get_container_status(docker_client_fixture, bedrock_server_full_container_name)
            print(f"Current status of {bedrock_server_full_container_name}: {current_status}")
            if current_status == "running":
                server_started = True
                break
            time.sleep(check_interval)

        assert server_started, f"Bedrock server '{bedrock_server_full_container_name}' did not start after {max_wait_time}s."
        print(f"Server '{bedrock_server_full_container_name}' is now running.")

        # 4. (Optional but recommended) Verify server is query-ready through the proxy
        # This checks Nether-bridge's query logic and full connectivity
        assert wait_for_mc_server_ready(
            {'host': '127.0.0.1', 'port': bedrock_proxy_port, 'type': 'bedrock'},
            timeout=30, interval=2
        ), "Bedrock server did not become query-ready through proxy."

    finally:
        client_socket.close()

# You can add more integration tests here:
# - test_java_server_starts_on_connection
# - test_server_shuts_down_on_idle (more complex, involves waiting, or mocking time)
# - test_packet_forwarding (basic data transfer)
# - test_multiple_servers_concurrently