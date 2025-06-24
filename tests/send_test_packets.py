import argparse
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum


# --- Enums for Configuration ---
class TestMode(Enum):
    SPIKE = "spike"
    SUSTAINED = "sustained"


class ServerType(Enum):
    JAVA = "java"
    BEDROCK = "bedrock"


# --- Packet Definitions ---

# A standard Minecraft Bedrock Edition Unconnected Ping packet
BEDROCK_UNCONNECTED_PING = (
    b"\x01"  # Packet ID (Unconnected Ping)
    + b"\x00\x00\x00\x00\x00\x00\x00\x00"  # Nonce (can be anything)
    + b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
    + b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78"  # RakNet Magic
    + b"\x00\x00\x00\x00\x00\x00\x00\x00"  # Client GUID (can be anything)
)

# --- Helper functions ---


def get_proxy_host():
    """
    Determines the correct IP address or hostname for tests by checking
    the environment in a specific order of precedence.

    """
    # 1. Inside Docker ('CI_MODE'): Uses Docker's internal DNS.
    if os.environ.get("CI_MODE"):
        return "nether-bridge"

    # 2. Explicit 'PROXY_IP': An override for specific CI scenarios.
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]

    # 3. Remote Docker 'DOCKER_HOST_IP': For targeting a remote Docker daemon.
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]

    # 4. Fallback: Local Development (e.g., Docker Desktop).
    return "127.0.0.1"


def encode_varint(value):
    """Encodes an integer into the VarInt format used by Minecraft."""
    buf = b""
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
    server_address_bytes = host.encode("utf-8")
    handshake_payload = (
        encode_varint(754)
        + encode_varint(len(server_address_bytes))
        + server_address_bytes
        + port.to_bytes(2, byteorder="big")
        + encode_varint(1)
    )
    handshake_packet = (
        encode_varint(len(handshake_payload) + 1) + b"\x00" + handshake_payload
    )

    # Status Request Packet
    status_request_payload = b""
    status_request_packet = (
        encode_varint(len(status_request_payload) + 1)
        + b"\x00"
        + status_request_payload
    )

    return handshake_packet, status_request_packet


# --- Main Worker Function ---


def simulate_client(
    client_id: int,
    server_type: ServerType,
    host: str,
    port: int,
    mode: TestMode,
    duration: int,
    packet_interval: int,
):
    """
    Simulates a single client performing a test against the proxy.
    Returns "SUCCESS" or a "FAILURE: reason" string.
    """
    try:
        if server_type == ServerType.JAVA:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect((host, port))

            handshake, status_request = get_java_handshake_and_status_request_packets(
                host, port
            )
            sock.sendall(handshake)
            sock.sendall(status_request)

            if mode == TestMode.SPIKE:
                sock.recv(4096)
            elif mode == TestMode.SUSTAINED:
                end_time = time.time() + duration
                while time.time() < end_time:
                    sock.sendall(status_request)
                    sock.recv(4096)
                    time.sleep(packet_interval)

            sock.close()

        elif server_type == ServerType.BEDROCK:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(10)

            if mode == TestMode.SPIKE:
                sock.sendto(BEDROCK_UNCONNECTED_PING, (host, port))
                sock.recvfrom(4096)
            elif mode == TestMode.SUSTAINED:
                end_time = time.time() + duration
                while time.time() < end_time:
                    sock.sendto(BEDROCK_UNCONNECTED_PING, (host, port))
                    sock.recvfrom(4096)
                    time.sleep(packet_interval)

            sock.close()

        return "SUCCESS"
    except Exception as e:
        return f"FAILURE ({type(e).__name__})"


# --- Main Execution Block ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Load testing tool for Nether-bridge proxy."
    )
    parser.add_argument(
        "--mode",
        type=TestMode,
        choices=list(TestMode),
        default=TestMode.SPIKE,
        help="Test mode: 'spike' (connections) or 'sustained' (active sessions).",
    )
    parser.add_argument(
        "--server-type",
        type=ServerType,
        choices=list(ServerType),
        required=True,
        help="The type of Minecraft server to test ('java' or 'bedrock').",
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=50,
        help="Number of concurrent clients to simulate.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="For sustained mode, the duration of the test in seconds.",
    )
    parser.add_argument(
        "--packet-interval",
        type=int,
        default=5,
        help="For sustained mode, the interval between sending keep-alive packets.",
    )

    args = parser.parse_args()

    # --- Dynamically determine the target host ---
    target_host = get_proxy_host()
    port = 25565 if args.server_type == ServerType.JAVA else 19132

    print("--- Nether-bridge Load Test ---")
    print(f"Mode:               {args.mode.value}")
    print(f"Server Type:        {args.server_type.value}")
    print(f"Target:             {target_host}:{port}")
    print(f"Concurrent Clients: {args.clients}")
    if args.mode == TestMode.SUSTAINED:
        print(f"Test Duration:      {args.duration}s")
        print(f"Packet Interval:    {args.packet_interval}s")
    print("---------------------------------")

    start_time = time.time()

    tasks = []
    with ThreadPoolExecutor(max_workers=args.clients) as executor:
        for i in range(args.clients):
            task = executor.submit(
                simulate_client,
                i,
                args.server_type,
                target_host,
                port,
                args.mode,
                args.duration,
                args.packet_interval,
            )
            tasks.append(task)

        results = [t.result() for t in tasks]

    end_time = time.time()

    success_count = len([r for r in results if r == "SUCCESS"])
    failure_count = len(results) - success_count

    print("\n--- Load Test Summary ---")
    print(f"Total Time Elapsed: {end_time - start_time:.2f} seconds")
    print(f"Successful Clients: {success_count} / {args.clients}")
    print(f"Failed Clients:     {failure_count} / {args.clients}")
    print("-------------------------")

    if failure_count > 0:
        print("\nFailure Details:")
        failure_reasons = {}
        for r in results:
            if r != "SUCCESS":
                reason = r
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        for reason, count in failure_reasons.items():
            print(f"- {reason}: {count} times")
        sys.exit(1)

    sys.exit(0)
