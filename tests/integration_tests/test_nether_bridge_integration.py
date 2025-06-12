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
    handshake_payload = (
        encode_varint(754) +
        encode_varint(len(server_address_bytes)) + server_address_bytes +
        port.to_bytes(2, byteorder='big') +
        encode_varint(1)
    )
    handshake_packet = encode_varint(len(handshake_payload) + 1) + b'\x00' + handshake_payload

    status_request_packet_payload = b''
    status_request_packet = encode_varint(len(status_request_packet_payload) + 1) + b'\x00' + status_request_packet_payload
    
    return handshake_packet, status_request_packet

# --- NEW HELPER FUNCTION ---
def wait_for_proxy_to_be_ready(docker_client_fixture, timeout=60):
    """Waits for the nether-bridge proxy to be fully initialized by watching its logs."""
    print("\nWaiting for nether-bridge proxy to be ready...")
    start_time = time.time()
    proxy_container = docker_client_fixture.containers.get("nether-bridge")
    
    # Check logs since the container started to avoid missing the message
    logs = proxy_container.logs(since=int(start_time - 10)).decode('utf-8')
    if "Starting main proxy packet forwarding loop" in logs:
        print("Proxy is ready (found message in recent logs).")
        return True

    # If not found, stream new logs
    for line in proxy_container.logs(stream=True, since=int(start_time)):
        decoded_line = line.decode('utf-8').strip()
        print(f"  [proxy log]: {decoded_line}")
        if "Starting main proxy packet forwarding loop" in decoded_line:
            print("Proxy is now ready.")
            return True
        if time.time() - start_time > timeout:
            print("Timeout waiting for proxy to become ready.")
            return False
    return False

# --- Integration Test Cases ---

@pytest.mark.integration
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture, docker_compose_project_name):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    bedrock_proxy_port = 19132
    mc_bedrock_container_name = "mc-bedrock"

    # 1. Wait for the proxy to be fully ready (after it has stopped all servers)
    assert wait_for_proxy_to_be_ready(docker_client_fixture, timeout=300), \
        "Proxy did not become ready within the timeout period."

    # 2. Confirm the server is initially stopped
    initial_status = get_container_status(docker_client_fixture, mc_bedrock_container_name)
    assert initial_status in ["exited", "created", "stopped"], f"Bedrock server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_bedrock_container_name}: {initial_status}")

    # 3. Simulate a client connection
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"Simulating connection to nether-bridge on port {bedrock_proxy_port} on host {VM_HOST_IP}...")
        unconnected_ping_packet = (
            b'\x01' + b'\x00\x00\x00\x00\x00\x00\x00\x00' +
            b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78' +
            b'\x00\x00\x00\x00\x00\x00\x00\x00'
        )
        client_socket.sendto(unconnected_ping_packet, (VM_HOST_IP, bedrock_proxy_port))
        print("Bedrock 'Unconnected Ping' packet sent.")

        # 4. Wait for the container to start
        assert wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            ["running"],
            timeout=180,
            interval=2
        ), f"Bedrock server '{mc_bedrock_container_name}' did not start after 180s."
        print(f"Server '{mc_bedrock_container_name}' is now running.")

        # 5. Verify server is query-ready
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
    java_proxy_port = 25565
    mc_java_container_name = "mc-java"

    # 1. Wait for the proxy to be fully ready
    assert wait_for_proxy_to_be_ready(docker_client_fixture, timeout=300), \
        "Proxy did not become ready within the timeout period."
        
    # 2. Confirm the server is initially stopped
    initial_status = get_container_status(docker_client_fixture, mc_java_container_name)
    assert initial_status in ["exited", "created", "stopped"], f"Java server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    # 3. Simulate a client connection
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(f"Simulating connection to nether-bridge on port {java_proxy_port} on host {VM_HOST_IP}...")
        client_socket.connect((VM_HOST_IP, java_proxy_port))
        print(f"Successfully connected to {VM_HOST_IP}:{java_proxy_port}.")
        
        handshake_packet, status_request_packet = get_java_handshake_and_status_request_packets(VM_HOST_IP, java_proxy_port)
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        # 4. Wait for the container to start
        assert wait_for_container_status(
            docker_client_fixture,
            mc_java_container_name,
            ["running"],
            timeout=180,
            interval=2
        ), f"Java server '{mc_java_container_name}' did not start after 180s."
        print(f"Server '{mc_java_container_name}' is now running.")

        # 5. Verify server is query-ready
        assert wait_for_mc_server_ready(
            {'host': VM_HOST_IP, 'port': java_proxy_port, 'type': 'java'},
            timeout=60,
            interval=2
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()

@pytest.mark.integration
def test_server_shuts_down_on_idle(docker_compose_up, docker_client_fixture, docker_compose_project_name):
    """
    Tests that a running server is automatically stopped by the proxy after a
    period of inactivity.
    """
    bedrock_proxy_port = 19132
    mc_bedrock_container_name = "mc-bedrock"
    
    # Using short timeouts for this specific test by passing them as env vars
    # to the proxy. This overrides the settings.json for this test run.
    # Note: This requires the docker_compose_up fixture to be function-scoped.
    # We will adjust conftest.py for this.
    
    # 1. Wait for the proxy to be ready
    assert wait_for_proxy_to_be_ready(docker_client_fixture, timeout=300), \
        "Proxy did not become ready within the timeout period."

    # 2. Start the Bedrock server by sending a packet
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"\nTriggering server '{mc_bedrock_container_name}' to start...")
        unconnected_ping_packet = (
            b'\x01' + b'\x00\x00\x00\x00\x00\x00\x00\x00' +
            b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78' +
            b'\x00\x00\x00\x00\x00\x00\x00\x00'
        )
        client_socket.sendto(unconnected_ping_packet, (VM_HOST_IP, bedrock_proxy_port))
        
        # 3. Confirm the server starts and becomes running
        assert wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            ["running"],
            timeout=180,
            interval=2
        ), "Bedrock server did not start after being triggered."
        print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")

        # 4. Wait for a duration longer than the idle_timeout + check_interval
        # idle_timeout_seconds is 10s and player_check_interval_seconds is 5s in settings.json
        idle_wait_time = 18 
        print(f"Server is running. Waiting {idle_wait_time}s for it to be shut down due to inactivity...")
        
        # 5. Assert that the server is stopped by the proxy
        assert wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            ["exited"],
            timeout=idle_wait_time,
            interval=2
        ), f"Server was not stopped after {idle_wait_time}s of inactivity."
        print("Server successfully shut down due to idle timeout.")

    finally:
        client_socket.close()