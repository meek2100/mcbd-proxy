import socket
import time
import sys
import struct
import docker

# Bedrock proxy port as defined in docker-compose.yml
BEDROCK_PROXY_PORT = 19133
JAVA_PROXY_PORT = 25566 # Using 25566 for current testing (as per servers.json)
TARGET_HOST = "192.168.1.176" # <--- IMPORTANT: This should be your VM's IP address

# Minecraft Bedrock OPEN_CONNECTION_REQUEST_1 packet example
BEDROCK_CONNECTION_PACKET = (
    b"\x05" # Packet ID for OPEN_CONNECTION_REQUEST_1
    b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" # Magic
    b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
)

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
    protocol_version_bytes = struct.pack('>i', -1)

    handshake_payload = (
        protocol_version_bytes +
        server_address_varint_string +
        port_bytes +
        encode_varint(1)
    )
    handshake_packet = encode_varint(len(handshake_payload)) + b'\x00' + handshake_payload

    status_request_packet_payload = b''
    status_request_packet = encode_varint(len(status_request_packet_payload)) + b'\x00' + status_request_packet_payload
    
    return handshake_packet, status_request_packet

def get_container_ip(container_name, network_name):
    """Gets the internal IP of a Docker container within a specific network."""
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        networks = container.attrs['NetworkSettings']['Networks']
        if network_name in networks:
            return networks[network_name]['IPAddress']
        print(f"Container '{container_name}' not found on network '{network_name}'.")
        return None
    except docker.errors.NotFound:
        print(f"Container '{container_name}' not found.")
        return None
    except Exception as e:
        print(f"Error getting IP for container '{container_name}': {e}")
        return None

def send_bedrock_packet():
    # Note: Bedrock still targets 127.0.0.1 for its published port
    print(f"Attempting to send Bedrock packet to 127.0.0.1:{BEDROCK_PROXY_PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
    try:
        sock.sendto(BEDROCK_CONNECTION_PACKET, ("127.0.0.1", BEDROCK_PROXY_PORT))
        print("Bedrock connection packet sent.")
        time.sleep(0.1) # Give proxy a moment to process (UDP is fire-and-forget)
    except Exception as e:
        print(f"Error sending Bedrock packet: {e}")
    finally:
        sock.close()

def send_java_packet():
    print(f"Attempting to send Java packet...")
    
    # Use the TARGET_HOST defined globally (which should be your VM's IP)
    java_target_host = TARGET_HOST 
    java_internal_port = JAVA_PROXY_PORT # Use the updated Java port

    print(f"Sending Java packet to proxy at: {java_target_host}:{java_internal_port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # TCP
    try:
        sock.settimeout(5) # Shorter timeout for direct send
        sock.connect((java_target_host, java_internal_port))
        print(f"Successfully connected to {java_target_host}:{java_internal_port}.")
        
        handshake_packet, status_request_packet = get_java_handshake_and_status_request_packets(java_target_host, java_internal_port)

        sock.sendall(handshake_packet)
        sock.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")
        
        # IMPORTANT: Try to receive a response to keep connection active and verify proxy's behavior
        # This will block until data is received or timeout occurs.
        response = sock.recv(4096) 
        print(f"Received response from proxy ({len(response)} bytes): {response[:50]}...") # Log response
        time.sleep(2) # Give proxy additional time to process and for server to potentially start

    except ConnectionRefusedError:
        print(f"Connection to {java_target_host}:{java_internal_port} refused. Proxy not listening or port blocked.")
    except socket.timeout:
        print(f"Connection to {java_target_host}:{java_internal_port} timed out during connect or recv.")
    except Exception as e:
        print(f"Error sending Java packet to {java_target_host}:{java_internal_port}: {e}")
    finally:
        if sock:
            sock.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1].lower() == "bedrock":
            send_bedrock_packet()
        elif sys.argv[1].lower() == "java":
            send_java_packet()
        else:
            print("Usage: python send_test_packets.py [bedrock|java]")
    else:
        print("Usage: python send_test_packets.py [bedrock|java]")