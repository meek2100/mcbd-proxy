import socket
import time
import sys
import struct # Import struct for 4-byte integer packing

# Bedrock proxy port as defined in docker-compose.yml
BEDROCK_PROXY_PORT = 19133
JAVA_PROXY_PORT = 25565
TARGET_HOST = "127.0.0.1"

# Minecraft Bedrock OPEN_CONNECTION_REQUEST_1 packet example
BEDROCK_CONNECTION_PACKET = (
    b"\x05" # Packet ID for OPEN_CONNECTION_REQUEST_1
    b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" # Magic
    b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
)

# Helper to encode VarInt (length of string, etc.) for Java protocol
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
    # Server Address as VarInt String
    server_address_bytes = host.encode('utf-8')
    server_address_varint_string = encode_varint(len(server_address_bytes)) + server_address_bytes

    # Port as unsigned short (2 bytes)
    port_bytes = port.to_bytes(2, byteorder='big') # 25565 is 0x6379

    # Handshake Packet (ID 0x00)
    # Protocol version -1 (0xFFFFFFFF) is encoded as a 4-byte signed integer, not a VarInt
    protocol_version_bytes = struct.pack('>i', -1) # >i means signed integer, big-endian

    handshake_payload = (
        protocol_version_bytes + # Correctly encoded -1 protocol version
        server_address_varint_string +
        port_bytes +
        encode_varint(1) # Next State: Status (1)
    )
    handshake_packet = encode_varint(len(handshake_payload)) + b'\x00' + handshake_payload

    # Status Request Packet (ID 0x00 in Status state)
    status_request_packet_payload = b'' # Empty payload for status request
    status_request_packet = encode_varint(len(status_request_packet_payload)) + b'\x00' + status_request_packet_payload
    
    return handshake_packet, status_request_packet


def send_bedrock_packet():
    print(f"Attempting to send Bedrock packet to {TARGET_HOST}:{BEDROCK_PROXY_PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
    try:
        sock.sendto(BEDROCK_CONNECTION_PACKET, (TARGET_HOST, BEDROCK_PROXY_PORT))
        print("Bedrock connection packet sent.")
    except Exception as e:
        print(f"Error sending Bedrock packet: {e}")
    finally:
        sock.close()

def send_java_packet():
    print(f"Attempting to send Java packet to {TARGET_HOST}:{JAVA_PROXY_PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # TCP
    try:
        sock.settimeout(5) # Shorter timeout for direct send
        sock.connect((TARGET_HOST, JAVA_PROXY_PORT))
        
        handshake_packet, status_request_packet = get_java_handshake_and_status_request_packets(TARGET_HOST, JAVA_PROXY_PORT)

        sock.sendall(handshake_packet)
        sock.sendall(status_request_packet) # Send status request after handshake
        
        # Optional: Try to receive a response to keep connection open longer
        # For a status request, the server should send a JSON response
        # data = sock.recv(4096)
        # print(f"Received Java response: {data[:50]}...")

        print("Java handshake and status request packets sent.")
    except ConnectionRefusedError:
        print(f"Connection to {TARGET_HOST}:{JAVA_PROXY_PORT} refused. Proxy/server not listening or port blocked.")
    except socket.timeout:
        print(f"Connection to {TARGET_HOST}:{JAVA_PROXY_PORT} timed out.")
    except Exception as e:
        print(f"Error sending Java packet: {e}")
    finally:
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