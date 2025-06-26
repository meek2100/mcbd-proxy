import argparse
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from mcstatus import JavaServer

from tests.helpers import get_java_handshake_and_status_request_packets, get_proxy_host


# --- Enums for Configuration ---
class TestMode(Enum):
    SPIKE = "spike"
    SUSTAINED = "sustained"


class ServerType(Enum):
    JAVA = "java"
    BEDROCK = "bedrock"


# --- Packet Definitions ---
BEDROCK_UNCONNECTED_PING = (
    b"\x01"
    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
    + b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
    + b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78"
    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
)


# --- Helper functions ---
def wait_for_server_ready(host: str, port: int, timeout: int = 60):
    """Waits for the server to respond to a status query."""
    print(f"--- Waiting for server at {host}:{port} to become ready... ---")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            server = JavaServer.lookup(f"{host}:{port}", timeout=2)
            status = server.status()
            if status:
                print("--- Server is ready. Proceeding with load test. ---")
                return True
        except Exception:
            time.sleep(1)
            continue
    print("--- Timeout waiting for server to become ready. ---")
    return False


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

    if args.server_type == ServerType.JAVA and args.mode == TestMode.SPIKE:
        print("--- Sending a pre-warming client to start the server... ---")
        pre_warm_result = simulate_client(
            -1, args.server_type, target_host, port, TestMode.SPIKE, 0, 0
        )
        if pre_warm_result != "SUCCESS":
            print(f"Pre-warming client failed: {pre_warm_result}. Aborting test.")
            sys.exit(1)

        if not wait_for_server_ready(target_host, port):
            print("Server did not become ready. Aborting test.")
            sys.exit(1)

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
