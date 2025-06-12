import pytest
import time
import socket
import docker
from mcstatus import BedrockServer, JavaServer

# --- Test File Configuration ---
VM_HOST_IP = "127.0.0.1" 

# --- Helper Functions (No changes needed here) ---
def get_container_status(docker_client_fixture, container_name):
    try:
        container = docker_client_fixture.containers.get(container_name)
        return container.status
    except docker.errors.NotFound:
        return "not_found"
    except Exception as e:
        pytest.fail(f"Failed to get status for container {container_name}: {e}")

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
                print(f"[{server_type}@{host}:{port}] Server responded! Latency: {status.latency:.2f}ms.")
                return True
        except Exception as e:
            pass
        time.sleep(interval)
    print(f"[{server_type}@{host}:{port}] Timeout waiting for server to be ready.")
    return False

def get_java_handshake_and_status_request_packets(host, port):
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
    
    server_address_bytes = host.encode('utf-8')
    handshake_payload = (
        encode_varint(754) +
        encode_varint(len(server_address_bytes)) + server_address_bytes +
        port.to_bytes(2, byteorder='big') +
        encode_varint(1)
    )
    handshake_packet = encode_varint(len(handshake_payload) + 1) + b'\x00' + handshake_payload
    status_request_packet = encode_varint(1) + b'\x00'
    return handshake_packet, status_request_packet

def wait_for_proxy_to_be_ready(docker_client_fixture, proxy_container_name, timeout=60):
    print(f"\nWaiting for proxy container '{proxy_container_name}' to be ready...")
    proxy_container = docker_client_fixture.containers.get(proxy_container_name)
    
    full_log = proxy_container.logs().decode('utf-8')
    if "Starting main proxy packet forwarding loop" in full_log:
        print("Proxy is already ready (found message in existing logs).")
        return True

    start_time = time.time()
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
def test_bedrock_server_starts_on_connection(docker_services, docker_client_fixture):
    bedrock_proxy_port = 19132
    mc_bedrock_container_name = docker_services.get_container_name("mc-bedrock")
    proxy_container_name = docker_services.get_container_name("nether-bridge")

    assert wait_for_proxy_to_be_ready(docker_client_fixture, proxy_container_name, timeout=300)
    
    initial_status = get_container_status(docker_client_fixture, mc_bedrock_container_name)
    assert initial_status == "exited"
    print(f"\nInitial status of {mc_bedrock_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"Simulating connection to proxy on port {bedrock_proxy_port}...")
        unconnected_ping_packet = (
            b'\x01' + b'\x00\x00\x00\x00\x00\x00\x00\x00' +
            b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78' +
            b'\x00\x00\x00\x00\x00\x00\x00\x00'
        )
        client_socket.sendto(unconnected_ping_packet, (VM_HOST_IP, bedrock_proxy_port))
        print("Bedrock 'Unconnected Ping' packet sent.")

        assert wait_for_container_status(docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=180)
        assert wait_for_mc_server_ready({'host': VM_HOST_IP, 'port': bedrock_proxy_port, 'type': 'bedrock'}, timeout=60)
    finally:
        client_socket.close()

@pytest.mark.integration
def test_java_server_starts_on_connection(docker_services, docker_client_fixture):
    java_proxy_port = 25565
    mc_java_container_name = docker_services.get_container_name("mc-java")
    proxy_container_name = docker_services.get_container_name("nether-bridge")

    assert wait_for_proxy_to_be_ready(docker_client_fixture, proxy_container_name, timeout=300)
        
    initial_status = get_container_status(docker_client_fixture, mc_java_container_name)
    assert initial_status == "exited"
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect((VM_HOST_IP, java_proxy_port))
        handshake, status_request = get_java_handshake_and_status_request_packets(VM_HOST_IP, java_proxy_port)
        client_socket.sendall(handshake)
        client_socket.sendall(status_request)
        print("Java handshake and status request packets sent.")

        assert wait_for_container_status(docker_client_fixture, mc_java_container_name, ["running"], timeout=180)
        assert wait_for_mc_server_ready({'host': VM_HOST_IP, 'port': java_proxy_port, 'type': 'java'}, timeout=120)
    finally:
        client_socket.close()

@pytest.mark.integration
def test_server_shuts_down_on_idle(docker_services, docker_client_fixture):
    bedrock_proxy_port = 19132
    mc_bedrock_container_name = docker_services.get_container_name("mc-bedrock")
    proxy_container_name = docker_services.get_container_name("nether-bridge")
    
    idle_timeout = 10 
    check_interval = 5
    
    assert wait_for_proxy_to_be_ready(docker_client_fixture, proxy_container_name, timeout=300)

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"\nTriggering server '{mc_bedrock_container_name}' to start...")
        unconnected_ping_packet = (
            b'\x01' + b'\x00\x00\x00\x00\x00\x00\x00\x00' +
            b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78' +
            b'\x00\x00\x00\x00\x00\x00\x00\x00'
        )
        client_socket.sendto(unconnected_ping_packet, (VM_HOST_IP, bedrock_proxy_port))
    finally:
        client_socket.close()
        print("Client socket closed, session terminated.")

    assert wait_for_container_status(docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=180)
    print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")

    wait_duration = idle_timeout + (2 * check_interval) + 5
    print(f"Server is running. Waiting {wait_duration}s for it to be shut down due to inactivity...")
    
    assert wait_for_container_status(docker_client_fixture, mc_bedrock_container_name, ["exited"], timeout=wait_duration)
    print("Server successfully shut down due to idle timeout.")