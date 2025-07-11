# tests/load_tester.py

import argparse
import os
import random
import socket
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import docker

# Add project root to the path to allow module imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.docker_utils import ensure_container_stopped
from tests.helpers import (
    BEDROCK_UNCONNECTED_PING,
    get_java_handshake_and_status_request_packets,
    get_proxy_host,
)


# --- Enums for Configuration ---
class ServerType(Enum):
    JAVA = "java"
    BEDROCK = "bedrock"


def simulate_single_client(
    server_type: ServerType,
    host: str,
    port: int,
    test_timeout: int,
    chaos_percent: int,
):
    """
    Simulates a single client connecting to the proxy.

    If chaos mode is enabled, a percentage of clients will misbehave by
    connecting and then abruptly disconnecting to test the proxy's error
    handling and session cleanup.
    """
    start_time = time.time()

    # --- Chaos Mode Logic ---
    if chaos_percent > 0 and random.randint(1, 100) <= chaos_percent:
        try:
            if server_type == ServerType.JAVA:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((host, port))
                time.sleep(random.uniform(0.5, 2.0))
                sock.close()
            else:  # Bedrock
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(BEDROCK_UNCONNECTED_PING, (host, port))
                time.sleep(random.uniform(0.5, 2.0))
                sock.close()
            return "CHAOS_ABORT"
        except Exception as e:
            return f"FAILURE: Chaos client failed - {type(e).__name__}"

    # --- Regular "Patient" Client Logic ---
    if server_type == ServerType.JAVA:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(test_timeout)
            sock.connect((host, port))
            sock.settimeout(5)  # Shorter timeout for subsequent pings

            while time.time() - start_time < test_timeout:
                try:
                    (
                        handshake,
                        status_req,
                    ) = get_java_handshake_and_status_request_packets(host, port)
                    sock.sendall(handshake)
                    sock.sendall(status_req)
                    sock.recv(4096)
                    return "SUCCESS"
                except (socket.timeout, ConnectionResetError):
                    time.sleep(2)
                    continue
        except Exception as e:
            return f"FAILURE: Could not connect or stay connected - {type(e).__name__}"
        finally:
            if "sock" in locals():
                sock.close()

    elif server_type == ServerType.BEDROCK:
        while time.time() - start_time < test_timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(3)
                sock.sendto(BEDROCK_UNCONNECTED_PING, (host, port))
                sock.recvfrom(4096)
                sock.close()
                return "SUCCESS"
            except socket.timeout:
                sock.close()
                time.sleep(2)
            except Exception as e:
                return f"FAILURE: Bedrock client error - {type(e).__name__}"

    return f"FAILURE: Client timed out after {test_timeout} seconds."


def dump_debug_info(container_names_to_log):
    """
    Connects to the Docker daemon to retrieve and print status and logs for
    specified containers, providing a complete snapshot of the environment.
    """
    print("\n" + "=" * 25 + " FINAL DIAGNOSTIC INFO " + "=" * 25)
    client = None
    try:
        client = docker.from_env()
        all_test_containers = ["nether-bridge", "mc-bedrock", "mc-java", "nb-tester"]

        print("\n--- STATUS OF ALL TEST CONTAINERS ---")
        for container in client.containers.list(all=True):
            if any(name in container.name for name in all_test_containers):
                status = f"Status: {container.status}"
                print(f"  - {container.name:<20} | {status}")

        for name in container_names_to_log:
            print(f"\n--- LOGS FOR CONTAINER: {name} ---")
            try:
                container = client.containers.get(name)
                logs = container.logs().decode("utf-8", errors="ignore").strip()
                print(logs if logs else "[No logs found for this container]")
            except docker.errors.NotFound:
                print(f"[Container '{name}' was not found]")
            except Exception as e:
                print(f"[Could not retrieve logs for '{name}': {e}]")

    except Exception as e:
        print(f"\nCRITICAL: Could not gather debug info. Docker client failed: {e}")
    finally:
        if client:
            client.close()
        print("\n" + "=" * 27 + " END OF DEBUG INFO " + "=" * 27)


# --- Main Execution Block ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Load testing tool for Nether-bridge proxy."
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
        default=25,
        help="Number of concurrent clients to simulate.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="Max time (seconds) for the entire test, including server boot.",
    )
    parser.add_argument(
        "--chaos",
        type=int,
        default=0,
        choices=range(0, 101),
        metavar="[0-100]",
        help="Percentage of clients that will connect and abruptly disconnect.",
    )

    args = parser.parse_args()

    target_host = get_proxy_host()
    port = 25565 if args.server_type == ServerType.JAVA else 19132
    target_container = (
        "mc-java" if args.server_type == ServerType.JAVA else "mc-bedrock"
    )
    containers_to_log = ["nether-bridge", target_container]
    exit_code = 1

    try:
        print("--- Nether-bridge True Cold-Start Load Test ---")
        print(f"Server Type:        {args.server_type.value}")
        print(f"Target Container:   {target_container}")
        print(f"Target Endpoint:    {target_host}:{port}")
        print(f"Concurrent Clients: {args.clients}")
        print(f"Client Timeout:     {args.timeout}s")
        print(f"Chaos Percentage:   {args.chaos}%")
        print("---------------------------------------------")

        ensure_container_stopped(target_container)

        print(f"\n--- Starting test with {args.clients} clients... ---")
        start_time = time.time()

        tasks = []
        with ThreadPoolExecutor(max_workers=args.clients) as executor:
            for i in range(args.clients):
                task = executor.submit(
                    simulate_single_client,
                    args.server_type,
                    target_host,
                    port,
                    args.timeout,
                    args.chaos,
                )
                tasks.append(task)
            results = [t.result() for t in tasks]

        end_time = time.time()

        success_count = results.count("SUCCESS")
        chaos_count = results.count("CHAOS_ABORT")
        failure_count = len(results) - success_count - chaos_count

        print("\n--- Load Test Summary ---")
        print(f"Total Time Elapsed: {end_time - start_time:.2f} seconds")
        print(f"Successful Clients: {success_count} / {args.clients}")
        print(f"Chaos Clients:      {chaos_count} / {args.clients}")
        print(f"Failed Clients:     {failure_count} / {args.clients}")
        print("-------------------------")

        if failure_count > 0:
            print("\nFailure Details:")
            failure_reasons = {}
            for r in results:
                if r.startswith("FAILURE"):
                    failure_reasons[r] = failure_reasons.get(r, 0) + 1
            for reason, count in failure_reasons.items():
                print(f"- {reason}: {count} occurrences")
            exit_code = 1
        else:
            print("\n--- Load Test Passed ---")
            exit_code = 0

    except Exception as e:
        print("\n" + "=" * 20 + " UNHANDLED EXCEPTION DURING TEST " + "=" * 20)
        print(f"An unexpected error occurred: {type(e).__name__} - {e}")
        traceback.print_exc()
        exit_code = 1

    finally:
        print("\n--- DUMPING DIAGNOSTIC INFO ---")
        dump_debug_info(containers_to_log)
        sys.exit(exit_code)
