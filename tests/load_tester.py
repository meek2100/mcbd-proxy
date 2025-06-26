import argparse
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

# Add project root to the path to allow module imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcstatus import BedrockServer, JavaServer

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


# --- Helper Functions ---


def simulate_single_client(
    server_type: ServerType,
    host: str,
    port: int,
    mode: TestMode,
    duration: int,
    packet_interval: int,
):
    """
    Simulates a single client performing a test against the proxy.
    This is the core logic that will be run in parallel by the thread pool.
    Returns "SUCCESS" or a "FAILURE: reason" string.
    """
    try:
        if server_type == ServerType.JAVA:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)  # Increased timeout for initial connection
            sock.connect((host, port))

            handshake, status_request = get_java_handshake_and_status_request_packets(
                host, port
            )
            sock.sendall(handshake)
            sock.sendall(status_request)

            if mode == TestMode.SPIKE:
                sock.recv(4096)  # Wait for the status response
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
        # Provide more context in failure messages for easier debugging in CI
        return f"FAILURE: {type(e).__name__} - {e}"


def pre_warm_and_wait(
    server_type: ServerType, host: str, port: int, timeout: int = 120
):
    """
    Handles the critical pre-warming sequence: triggers the server to start
    and then waits for it to become responsive to status queries. This prevents
    the main load test from failing due to server startup time.
    """
    # --- FIX: Reformatted line to be under 88 characters ---
    print(
        "--- Sending a pre-warming client to start the "
        f"{server_type.value} server... ---"
    )
    # We run a single client simulation. We don't care about the result, only that it
    # sends a packet to the proxy to trigger the server startup logic.
    simulate_single_client(server_type, host, port, TestMode.SPIKE, 0, 0)

    # --- FIX: Reformatted line to be under 88 characters ---
    print(
        f"--- Waiting for server at {host}:{port} to become ready "
        f"(max {timeout}s)... ---"
    )
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            status = None
            if server_type == ServerType.JAVA:
                server = JavaServer.lookup(f"{host}:{port}", timeout=2)
                status = server.status()
            elif server_type == ServerType.BEDROCK:
                server = BedrockServer.lookup(f"{host}:{port}", timeout=2)
                status = server.status()

            if status:
                # --- FIX: Reformatted line to be under 88 characters ---
                print(
                    f"--- Server is ready (latency: {status.latency:.2f}ms). "
                    "Proceeding with load test. ---"
                )
                return True
        except Exception:
            time.sleep(2)  # Wait a moment before retrying
            continue

    print("--- Timeout waiting for server to become ready. Aborting. ---")
    return False


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

    # In spike mode, we must always pre-warm the server first. Otherwise, the test
    # is measuring server start time, not proxy performance.
    if args.mode == TestMode.SPIKE:
        if not pre_warm_and_wait(args.server_type, target_host, port):
            sys.exit(1)  # Exit if the server fails to become ready.

    # --- Execute Main Load Test ---
    print(f"--- Starting main load test with {args.clients} clients... ---")
    tasks = []
    with ThreadPoolExecutor(max_workers=args.clients) as executor:
        for i in range(args.clients):
            task = executor.submit(
                simulate_single_client,
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

    print("\n--- Load Test Passed ---")
    sys.exit(0)
