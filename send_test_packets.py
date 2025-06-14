import socket
import time
import sys
import struct
import docker

# --- Configuration ---
# IMPORTANT: This must be the IP address of the machine running Docker (your Debian VM)
TARGET_HOST = "127.0.0.1" 
# Ports should match what is defined in your docker-compose.yml and servers.json
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565 # Using the standard port as per servers.json

# --- Packet Definitions ---

# A standard Minecraft Bedrock Edition Unconnected Ping packet
# This is a simple packet that should elicit a response from a running server.
BEDROCK_UNCONNECTED_PING = (
    b'\x01' +                # Packet ID (Unconnected Ping)
    b'\x00\x00\x00\x00\x00\x00\x00\x00' +  # Nonce (can be anything)
    b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78' + # RakNet Magic
    b'\x00\x00\x00\x00\x00\x00\x00\x00'   # Client GUID (can be anything)
)

# --- Helper functions for Java Protocol ---

def encode_varint(value):
    """Encodes an integer into the VarInt format used by Minecraft."""
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

def get_java_handshake_and_status_request_packets(host, port):
    """Constructs the two packets needed to request a status from a Java server."""
    # Handshake Packet
    server_address_bytes = host.encode('utf-8')
    handshake_payload = (
        encode_varint(754) +  # Protocol Version (e.g., 754 for 1.16.5, can be anything for status)
        encode_varint(len(server_address_bytes)) + server_address_bytes +
        port.to_bytes(2, byteorder='big') +
        encode_varint(1)  # Next State: 1 for Status
    )
    handshake_packet = encode_varint(len(handshake_payload) + 1) + b'\x00' + handshake_payload

    # Status Request Packet
    status_request_payload = b''
    status_request_packet = encode_varint(len(status_request_payload) + 1) + b'\x00' + status_request_payload
    
    return handshake_packet, status_request_packet

# --- Test Functions ---

def test_bedrock_server():
    """Sends a UDP packet to trigger the Bedrock server and listens for a response."""
    print(f"--- Testing Bedrock (UDP) -> {TARGET_HOST}:{BEDROCK_PROXY_PORT} ---")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5) # 5 second timeout to wait for a reply
    try:
        print("Sending Unconnected Ping packet to proxy...")
        sock.sendto(BEDROCK_UNCONNECTED_PING, (TARGET_HOST, BEDROCK_PROXY_PORT))
        print("Packet sent. Waiting for response...")
        
        # After the server starts, it should respond to our ping.
        data, addr = sock.recvfrom(4096)
        print(f"SUCCESS: Received {len(data)} bytes back from {addr}.")
        if b'MCPE' in data:
            print("Response contains 'MCPE', server is likely up and responding correctly.")
        else:
            print("Response received, but may not be a standard Minecraft pong packet.")

    except socket.timeout:
        print("FAIL: Did not receive a response within 5 seconds. The server may not have started or the proxy failed.")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        sock.close()
        print("--- Bedrock Test Finished ---\n")

def test_java_server():
    """Sends TCP packets to trigger the Java server and listens for a response."""
    print(f"--- Testing Java (TCP) -> {TARGET_HOST}:{JAVA_PROXY_PORT} ---")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10) # 10 second timeout
    try:
        print(f"Attempting to connect to proxy...")
        sock.connect((TARGET_HOST, JAVA_PROXY_PORT))
        print("Connection successful. Sending handshake and status request packets...")
        
        handshake, status_request = get_java_handshake_and_status_request_packets(TARGET_HOST, JAVA_PROXY_PORT)
        
        sock.sendall(handshake)
        sock.sendall(status_request)
        print("Packets sent. Waiting for response...")

        response = sock.recv(4096)
        print(f"SUCCESS: Received {len(response)} bytes back.")
        if b'{"version"' in response:
             print("Response appears to be valid JSON from a Minecraft server.")
        else:
            print("Response received, but may not be a standard Minecraft status response.")

    except ConnectionRefusedError:
        print(f"FAIL: Connection refused. The proxy is not listening on {TARGET_HOST}:{JAVA_PROXY_PORT}.")
    except socket.timeout:
        print(f"FAIL: Connection timed out. The server may not have started or the proxy failed.")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        sock.close()
        print("--- Java Test Finished ---\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python send_test_packets.py [bedrock|java|all]")
        sys.exit(1)

    test_type = sys.argv[1].lower()

    if test_type == "bedrock":
        test_bedrock_server()
    elif test_type == "java":
        test_java_server()
    elif test_type == "all":
        test_bedrock_server()
        print("Pausing for a moment between tests...")
        time.sleep(2)
        test_java_server()
    else:
        print(f"Error: Unknown test type '{test_type}'. Use 'bedrock', 'java', or 'all'.")