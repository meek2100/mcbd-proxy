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
def wait_for_container_status(docker_client_fixture, container_name, target_statuses, timeout=240, interval=5):
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

# NEW Helper: Wait for a port on the host to be open/listening
def wait_for_host_port_open(host, port, protocol="udp", timeout=60, interval=1):
    start_time = time.time()
    print(f"Waiting for host port {host}:{port}/{protocol} to be open (max {timeout}s)...")
    while time.time() - start_time < timeout:
        if protocol == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(interval)
                sock.sendto(b"test", (host, port)) # Send a small packet to probe
                print(f"  UDP port {host}:{port} seems responsive.")
                return True
            except socket.error as e:
                # print(f"  UDP port {host}:{port} not ready: {e}. Retrying...")
                pass
            finally:
                sock.close()
        elif protocol == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(interval)
                sock.connect((host, port))
                print(f"  TCP port {host}:{port} is open.")
                return True
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                # print(f"  TCP port {host}:{port} not ready: {e}. Retrying...")
                pass
            finally:
                sock.close()
        time.sleep(interval)
    print(f"Timeout waiting for host port {host}:{port}/{protocol} to open.")
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

    # 0. Wait for nether-bridge proxy's UDP port to be open on the host
    print(f"\nEnsuring nether-bridge proxy UDP port {bedrock_proxy_port} is open...")
    assert wait_for_host_port_open("127.0.0.1", bedrock_proxy_port, "udp", timeout=60, interval=1), \
        f"Nether-bridge UDP port {bedrock_proxy_port} not open on host after 60s."

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
        bedrock_connection_packet = (
            b"\x05" # Packet ID for OPEN_CONNECTION_REQUEST_1
            b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" # Magic
            b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
        )
        client_socket.sendto(bedrock_connection_packet, ("127.0.0.1", bedrock_proxy_port))
        print("Bedrock connection packet sent.")

        # 3. Wait for the Minecraft server container to start and become running
        assert wait_for_container_status(
            docker_client_fixture,
            found_bedrock_container_name,
            ["running"], # Target state is 'running'
            timeout=180, # Increased to 180 seconds for Bedrock server startup
            interval=2
        ), f"Bedrock server '{found_bedrock_container_name}' did not start after {180}s."

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

    # 0. Wait for nether-bridge proxy's TCP port to be open on the host
    print(f"\nEnsuring nether-bridge proxy TCP port {java_proxy_port} is open...")
    # INCREASED TIMEOUT for TCP port check
    assert wait_for_host_port_open("127.0.0.1", java_proxy_port, "tcp", timeout=120, interval=2), \
        f"Nether-bridge TCP port {java_proxy_port} not open on host after 120s."

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
        
        # Send a more robust Java handshake + status request for better triggering
        # Length of next packet (VarInt) + Packet ID (0x00) + Protocol Version (VarInt) + Server Address (VarInt String) + Port (Unsigned Short) + Next State (VarInt)
        # For a status request, Protocol Version is -1 (0xFFFFFFFF as VarInt)
        # Server Address is the host: "127.0.0.1"
        # Port is 25565
        # Next State is 1 (Status)

        # Helper to encode VarInt (length of string, etc.)
        def encode_varint(value):
            buf = b''
            while True:
                byte = value & 0x7F
                value >>= 7
                if value != 0:
                    byte |= 0x80
                buf += bytes([byte])
                if value == 0:
                    break
            return buf

        # Server Address as VarInt String
        server_address = "127.0.0.1".encode('utf-8')
        server_address_varint = encode_varint(len(server_address)) + server_address

        # Port as unsigned short (2 bytes)
        port_bytes = java_proxy_port.to_bytes(2, byteorder='big') # 25565 is 0x6379

        # Handshake Packet (ID 0x00)
        # Protocol version -1 (0xFFFFFFFF) is needed for status requests
        handshake_payload = (
            encode_varint(-1) + # Protocol Version (-1 for status)
            server_address_varint +
            port_bytes +
            encode_varint(1) # Next State: Status (1)
        )
        handshake_packet = encode_varint(len(handshake_payload)) + b'\x00' + handshake_payload

        # Status Request Packet (ID 0x00 in Status state)
        status_request_packet_payload = b'' # Empty payload for status request
        status_request_packet = encode_varint(len(status_request_packet_payload)) + b'\x00' + status_request_packet_payload

        # Send both packets back-to-back to simulate a client requesting status
        # This should be enough to trigger the proxy's session logic
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        # 3. Wait for the Minecraft server container to start and become running
        assert wait_for_container_status(
            docker_client_fixture,
            found_java_container_name,
            ["running"], # Target state is 'running'
            timeout=180, # Increased to 180 seconds for Java server startup
            interval=2
        ), f"Java server '{found_java_container_name}' did not start after {180}s."

        # Assert that it is indeed running after the wait
        current_status = get_container_status(docker_client_fixture, found_java_container_name)
        assert current_status == "running", f"Java server '{found_java_container_name}' is not running, but is: {current_status}"
        print(f"Server '{found_java_container_name}' is now running.")

        # 4. (Optional but recommended) Verify server is query-ready through the proxy
        # We can use mcstatus's JavaServer.lookup here
        assert wait_for_mc_server_ready(
            {'host': '127.0.0.1', 'port': java_proxy_port, 'type': 'java'},
            timeout=60, # Keep this reasonable for query readiness
            interval=2
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()