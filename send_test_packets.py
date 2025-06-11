import socket
import time
import sys

# Bedrock proxy port as defined in docker-compose.yml
BEDROCK_PROXY_PORT = 19133
JAVA_PROXY_PORT = 25565
TARGET_HOST = "127.0.0.1"

# Minecraft Bedrock OPEN_CONNECTION_REQUEST_1 packet example
# This is the most reliable packet to trigger a Bedrock server startup via proxy
BEDROCK_CONNECTION_PACKET = (
    b"\x05" # Packet ID for OPEN_CONNECTION_REQUEST_1
    b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78" # Magic
    b"\xbe\x01" # Protocol Version (e.g., 671 for 1.21.84.1, in little-endian 0x01be)
)

# Basic Java handshake packet (minimal to trigger proxy/TCP listener)
JAVA_HANDSHAKE_PACKET = b"\x0f\x00\xfb\xff\xff\xff\xff\x05\x09127.0.0.1\x39\x90\x01"


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
        sock.sendall(JAVA_HANDSHAKE_PACKET)
        print("Java connection packet sent.")
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