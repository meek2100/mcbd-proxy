import pytest
import time
import socket
import docker
from mcstatus import BedrockServer, JavaServer

# Assuming Nether-bridge and Minecraft servers are defined in docker-compose.yml
# and listening on their respective ports.
# The 'docker_compose_up' fixture from conftest.py will automatically manage the stack.

# IMPORTANT: Set this to your Debian VM's Host IP address (e.g., 192.168.1.176)
# This is where your Windows machine connects to the published ports.
VM_HOST_IP = "192.168.1.176" # <--- IMPORTANT: UPDATE THIS TO YOUR ACTUAL VM'S IP

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

# Helper function to wait for a port on the host to be open/listening
def wait_for_host_port_open(host, port, protocol="udp", timeout=60, interval=1):
    start_time = time.time()
    print(f"Waiting for host port {host}:{port}/{protocol} to be open (max {timeout}s)...")
    while time.time() - start_time < timeout:
        if protocol == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(interval)
                sock.sendto(b"test", (host, port))
                print(f"  UDP port {host}:{port} seems responsive.")
                return True
            except socket.error as e:
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
            pass
        time.sleep(interval)
    print(f"[{server_type}@{host}:{port}] Timeout waiting for server to be ready.")
    return False

# Helper to encode VarInt for Java protocol
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

# Java handshake + status request packets
def get_java_handshake_and_status_request_packets(host, port):
    server_address_bytes = host.encode('utf-8')
    server_address_varint_string = encode_varint(len(server_address_bytes)) + server_address_bytes
    port_bytes = port.to_bytes(2, byteorder='big')
    protocol_version_bytes = (-1).to_bytes(4, byteorder='big', signed=True) # Java protocol version -1 as 4-byte signed int

    handshake_payload = (
        protocol_version_bytes +
        server_address_varint_string +
        port_bytes +
        encode_varint(1) # Next State: Status (1)
    )
    handshake_packet = encode_varint(len(handshake_payload)) + b'\x00' + handshake_payload

    status_request_packet_payload = b''
    status_request_packet = encode_varint(len(status_request_packet_payload)) + b'\x00' + status_request_packet_payload
    
    return handshake_packet, status_request_packet


# --- Integration Test Cases ---

@pytest.mark.integration
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture, docker_compose_project_name):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    bedrock_proxy_port = 19132 # As defined in docker-compose.yml
    mc_bedrock_service_name = "mc-bedrock"
    
    # Expected container name from docker-compose.yml
    expected_mc_bedrock_container_name = mc_bedrock_service_name

    # 1. Wait for nether-bridge proxy to fully initialize and stop the MC servers
    print(f"\nWaiting for {mc_bedrock_service_name} to become stopped by proxy...")
    assert wait_for_container_status(
        docker_client_fixture,
        expected_mc_bedrock_container_name,
        ["exited", "stopped", "created"], # Target states after proxy stops it
        timeout=240, # 4 minutes to account for full MC server startup + proxy stop
        interval=5 # Check less frequently
    ), f"{mc_bedrock_service_name} did not become stopped after proxy startup within timeout."
    
    initial_status = get_container_status(docker_client_fixture, expected_mc_bedrock_container_name)
    assert initial_status in ["exited", "created", "stopped"], \
        f"Bedrock server should be initially stopped or not running, but is: {initial_status} (Container: {expected_mc_bedrock_container_name})"
    print(f"\nInitial status of {expected_mc_bedrock_container_name}: {initial_status}")

    # 2. Simulate a client connection to the proxy's Bedrock port
    # Ensure nether-bridge proxy UDP port is open on the host
    print(f"Ensuring nether-bridge proxy UDP port {bedrock_proxy_port} is open on host {VM_HOST_IP}...")
    assert wait_for_host_port_open(VM_HOST_IP, bedrock_proxy_port, "udp", timeout=60, interval=1), \
        f"Nether-bridge UDP port {bedrock_proxy_port} not open on host {VM_HOST_IP} after 60s."

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"Simulating connection to nether-bridge on port {bedrock_proxy_port} on host {VM_HOST_IP}...")
        
        # Minecraft Bedrock OPEN_CONNECTION_REQUEST_1 packet example
        bedrock_connection_packet = (
            b"\x05" + # Packet ID for OPEN_CONNECTION_REQUEST_1
            b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" + # Magic
            b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
        )
        client_socket.sendto(bedrock_connection_packet, (VM_HOST_IP, bedrock_proxy_port))
        print("Bedrock connection packet sent.")

        # 3. Wait for the Minecraft server container to start and become running
        assert wait_for_container_status(
            docker_client_fixture,
            expected_mc_bedrock_container_name,
            ["running"], # Target state is 'running'
            timeout=180, # Increased to 180 seconds for Bedrock server startup
            interval=2
        ), f"Bedrock server '{expected_mc_bedrock_container_name}' did not start after {180}s."

        current_status = get_container_status(docker_client_fixture, expected_mc_bedrock_container_name)
        assert current_status == "running", f"Bedrock server '{expected_mc_bedrock_container_name}' is not running, but is: {current_status}"
        print(f"Server '{expected_mc_bedrock_container_name}' is now running.")

        # 4. Verify server is query-ready through the proxy
        assert wait_for_mc_server_ready(
            {'host': VM_HOST_IP, 'port': bedrock_proxy_port, 'type': 'bedrock'},
            timeout=60,
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
    java_proxy_port = 25565 # As defined in docker-compose.yml (using 25565 for testing)
    mc_java_service_name = "mc-java"
    
    # Expected container name from docker-compose.yml
    expected_mc_java_container_name = mc_java_service_name

    # 1. Wait for nether-bridge proxy to fully initialize and stop the MC servers
    print(f"\nWaiting for {mc_java_service_name} to become stopped by proxy...")
    assert wait_for_container_status(
        docker_client_fixture,
        expected_mc_java_container_name,
        ["exited", "stopped", "created"], # Target states after proxy stops it
        timeout=240, # Allow ample time for Java server to start and proxy to stop it
        interval=5
    ), f"{mc_java_service_name} did not become stopped after proxy startup within timeout."
    
    initial_status = get_container_status(docker_client_fixture, expected_mc_java_container_name)
    assert initial_status in ["exited", "created", "stopped"], \
        f"Java server should be initially stopped or not running, but is: {initial_status} (Container: {expected_mc_java_container_name})"
    print(f"\nInitial status of {expected_mc_java_container_name}: {initial_status}")

    # 2. Simulate a client connection to the proxy's Java port
    # Ensure nether-bridge proxy TCP port is open on the host
    print(f"Ensuring nether-bridge proxy TCP port {java_proxy_port} is open on host {VM_HOST_IP}...")
    assert wait_for_host_port_open(VM_HOST_IP, java_proxy_port, "tcp", timeout=120, interval=2), \
        f"Nether-bridge TCP port {java_proxy_port} not open on host {VM_HOST_IP} after 120s."

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(f"Simulating connection to nether-bridge on port {java_proxy_port} on host {VM_HOST_IP}...")
        client_socket.connect((VM_HOST_IP, java_proxy_port))
        print(f"Successfully connected to {VM_HOST_IP}:{java_proxy_port}.")
        
        # Send a more robust Java handshake + status request for better triggering
        handshake_packet, status_request_packet = get_java_handshake_and_status_request_packets(VM_HOST_IP, java_proxy_port)

        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        # IMPORTANT: Try to receive a response to keep connection active and verify proxy's behavior
        # This will block until data is received or timeout occurs.
        response = client_socket.recv(4096) 
        print(f"Received response from proxy ({len(response)} bytes): {response[:50]}...") # Log response
        time.sleep(2) # Give proxy additional time to process and for server to potentially start

        # 3. Wait for the Minecraft server container to start and become running
        assert wait_for_container_status(
            docker_client_fixture,
            expected_mc_java_container_name,
            ["running"], # Target state is 'running'
            timeout=180, # Increased to 180 seconds for Java server startup
            interval=2
        ), f"Java server '{expected_mc_java_container_name}' did not start after {180}s."

        current_status = get_container_status(docker_client_fixture, expected_mc_java_container_name)
        assert current_status == "running", f"Java server '{expected_mc_java_container_name}' is not running, but is: {current_status}"
        print(f"Server '{expected_mc_java_container_name}' is now running.")

        # 4. Verify server is query-ready through the proxy
        assert wait_for_mc_server_ready(
            {'host': VM_HOST_IP, 'port': java_proxy_port, 'type': 'java'},
            timeout=60,
            interval=2
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()