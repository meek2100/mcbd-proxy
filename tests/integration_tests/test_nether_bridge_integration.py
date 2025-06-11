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

# Helper function to wait for a specific container status
def wait_for_container_status(docker_client_fixture, container_name, target_statuses, timeout=240, interval=5): # Default timeout for *any* container status change
    start_time = time.time()
    print(f"Waiting for container '{container_name}' to reach status in {target_statuses} (max {timeout}s)...")
    while time.time() - start_time < timeout:
        current_status = get_container_status(docker_client_fixture, container_name)
        print(f"  Current status of '{container_name}': {current_status}")
        if current_status in target_statuses:
            print(f"  Container '{container_name}' reached desired status: {current_status}")
            return True
        time.sleep(interval)
    print(f"Timeout waiting for container '{container_name}' to reach status in {target_statuses}. Current: {current_status}")
    return False

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
    mc_bedrock_service_name = "mc-bedrock"
    
    # Expected container name from docker-compose.yml
    expected_mc_bedrock_container_name = mc_bedrock_service_name

    # Initialize the variable before its first use
    found_bedrock_container_name = expected_mc_bedrock_container_name

    # 1. Wait for nether-bridge proxy to fully initialize and stop the MC servers
    print(f"\nWaiting for {mc_bedrock_service_name} to become stopped by proxy...")
    assert wait_for_container_status(
        docker_client_fixture,
        expected_mc_bedrock_container_name,
        ["exited", "stopped", "created"], # Target states after proxy stops it
        timeout=240, # 4 minutes to account for full MC server startup + proxy stop
        interval=5 # Check less frequently
    ), f"{mc_bedrock_service_name} did not become stopped after proxy startup within timeout."
    
    initial_status = get_container_status(docker_client_fixture, found_bedrock_container_name)
    assert initial_status in ["exited", "created", "stopped"], \
        f"Bedrock server should be initially stopped or not running, but is: {initial_status} (Container: {found_bedrock_container_name})"
    print(f"\nInitial status of {found_bedrock_container_name}: {initial_status}")

    # 2. Simulate a client connection to the proxy's Bedrock port
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"Simulating connection to nether-bridge on port {bedrock_proxy_port}...")
        
        # Minecraft Bedrock OPEN_CONNECTION_REQUEST_1 packet example
        # This is the most reliable packet to trigger a Bedrock server startup via proxy
        bedrock_connection_packet = (
            b"\x05" # Packet ID for OPEN_CONNECTION_REQUEST_1
            b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" # Magic
            b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
        )
        client_socket.sendto(bedrock_connection_packet, ("127.0.0.1", bedrock_proxy_port))
        print("Bedrock connection packet sent.")

        # 3. Wait for the Minecraft server container to start and become running
        # Use the robust wait_for_container_status helper for this too
        assert wait_for_container_status(
            docker_client_fixture,
            found_bedrock_container_name,
            ["running"], # Target state is 'running'
            timeout=120, # Increased to 120 seconds for Bedrock server startup
            interval=2
        ), f"Bedrock server '{found_bedrock_container_name}' did not start after {120}s."

        # Assert that it is indeed running after the wait
        current_status = get_container_status(docker_client_fixture, found_bedrock_container_name)
        assert current_status == "running", f"Bedrock server '{found_bedrock_container_name}' is not running, but is: {current_status}"
        print(f"Server '{found_bedrock_container_name}' is now running.")

        # 4. (Optional but recommended) Verify server is query-ready through the proxy
        assert wait_for_mc_server_ready(
            {'host': '127.0.0.1', 'port': bedrock_proxy_port, 'type': 'bedrock'},
            timeout=60, # Keep this reasonable for query readiness
            interval=2
        ), "Bedrock server did not become query-ready through proxy."

    finally:
        client_socket.close()

@pytest.mark.integration
def test_java_server_starts_on_connection(docker_compose_up, docker_client_fixture, docker_compose_project_name):
    """
    Test that the mc-java server starts when a connection attempt is made
    to the nether-bridge proxy on its Java port.
    """
    java_proxy_port = 25565 # As defined in docker-compose.yml
    mc_java_service_name = "mc-java"
    
    # Expected container name from docker-compose.yml
    expected_mc_java_container_name = mc_java_service_name

    # Initialize the variable before its first use
    found_java_container_name = expected_mc_java_container_name

    # 1. Wait for nether-bridge proxy to fully initialize and stop the MC servers
    print(f"\nWaiting for {mc_java_service_name} to become stopped by proxy...")
    assert wait_for_container_status(
        docker_client_fixture,
        expected_mc_java_container_name,
        ["exited", "stopped", "created"], # Target states after proxy stops it
        timeout=240, # Allow ample time for Java server to start and proxy to stop it
        interval=5
    ), f"{mc_java_service_name} did not become stopped after proxy startup within timeout."
    
    initial_status = get_container_status(docker_client_fixture, found_java_container_name)
    assert initial_status in ["exited", "created", "stopped"], \
        f"Java server should be initially stopped or not running, but is: {initial_status} (Container: {found_java_container_name})"
    print(f"\nInitial status of {found_java_container_name}: {initial_status}")

    # 2. Simulate a client connection to the proxy's Java port
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # Java uses TCP
    client_socket.settimeout(10) # Set a timeout for connect/recv
    try:
        print(f"Simulating connection to nether-bridge on port {java_proxy_port} for Java server...")
        client_socket.connect(("127.0.0.1", java_proxy_port))
        
        # Send a basic Java handshake packet. This is the simplest possible
        # packet to initiate a connection for Java clients.
        java_handshake_packet = b"\x0f\x00\xfb\xff\xff\xff\xff\x05\x09127.0.0.1\x39\x90\x01"
        client_socket.sendall(java_handshake_packet)
        print("Java connection packet sent.")

        # 3. Wait for the Minecraft server container to start and become running
        # Use the robust wait_for_container_status helper for this too
        assert wait_for_container_status(
            docker_client_fixture,
            found_java_container_name,
            ["running"], # Target state is 'running'
            timeout=120, # Increased to 120 seconds for Java server startup
            interval=2
        ), f"Java server '{found_java_container_name}' did not start after {120}s." # Update message for clarity

        # Assert that it is indeed running after the wait
        current_status = get_container_status(docker_client_fixture, found_java_container_name)
        assert current_status == "running", f"Java server '{found_java_container_name}' is not running, but is: {current_status}"
        print(f"Server '{found_java_container_name}' is now running.")

        # 4. (Optional but recommended) Verify server is query-ready through the proxy
        assert wait_for_mc_server_ready(
            {'host': '127.0.0.1', 'port': java_proxy_port, 'type': 'java'},
            timeout=60, # Keep this reasonable for query readiness
            interval=2
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()