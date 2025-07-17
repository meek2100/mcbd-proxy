# tests/load_tester.py
"""
An asynchronous load and chaos tester for the Nether-bridge proxy.
Simulates multiple concurrent clients connecting to a server, with different
behavioral modes to test proxy stability.
"""

import argparse
import asyncio
import logging
import random
import socket
import sys
import time
from enum import Enum

import aiodocker
import structlog
from aiodocker.exceptions import DockerError

# Import centralized helpers
from tests.helpers import (
    BEDROCK_UNCONNECTED_PING,
    add_project_root_to_path,  # Function to add project root to path
    get_java_handshake_and_status_request_packets,
    get_proxy_host,
)

# Call the path helper immediately
add_project_root_to_path()

# Use structlog for this script's logging
log = structlog.get_logger()

# --- Statistics Tracking ---
# These are global because they are incremented by multiple concurrent tasks
successful_connections = 0
failed_connections = 0
chaos_aborts = 0
active_sockets = []  # Track open sockets for cleanup


# --- Enums for Configuration ---
class ServerType(Enum):
    JAVA = "java"
    BEDROCK = "bedrock"


class UDPClientProtocol(asyncio.DatagramProtocol):
    """A simple protocol that signals when a UDP packet is received."""

    def __init__(self, future, client_id):
        self.transport = None
        self.future = future  # A future to signal completion
        self.client_id = client_id
        self.response_received = asyncio.Event()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # log.debug(f"Client {self.client_id}: UDP response received from {addr}.")
        self.response_received.set()  # Signal that a response was received

    def error_received(self, exc):
        log.error(f"Client {self.client_id}: UDP client error", exc=exc)
        if not self.future.done():
            self.future.set_exception(exc)

    def connection_lost(self, exc):
        if exc and not self.future.done():
            self.future.set_exception(exc)
        elif not self.future.done():
            self.future.set_result(True)


async def simulate_client(
    server_type: ServerType,
    host: str,
    port: int,
    client_id: int,
    test_duration: int,  # Duration for patient clients to stay connected
    chaos_percent: int,
    initial_delay: float = 0.5,  # Small initial connection delay
) -> str:
    """
    Simulates a single client connecting to the proxy for a sustained duration.
    If chaos mode is enabled, a percentage of clients will misbehave.
    """
    global successful_connections, failed_connections, chaos_aborts

    start_client_time = time.time()
    try:
        # --- Chaos Mode Logic ---
        if chaos_percent > 0 and random.randint(1, 100) <= chaos_percent:
            log.info(f"Client {client_id}: Chaos client initiating abrupt disconnect.")
            sock = None
            try:
                if server_type == ServerType.JAVA:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    # Use a short timeout for initial connect
                    sock.settimeout(initial_delay + 1)
                    sock.connect((host, port))
                else:  # Bedrock UDP
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(BEDROCK_UNCONNECTED_PING, (host, port))

                await asyncio.sleep(random.uniform(0.1, initial_delay))
                chaos_aborts += 1
                return "CHAOS_ABORT"
            except Exception as e:
                log.warning(f"Client {client_id}: Chaos client connect failed: {e}")
                # Even if chaos fails to connect, count it as chaos attempt
                chaos_aborts += 1
                return f"CHAOS_FAILURE: {type(e).__name__}"
            finally:
                if sock:
                    sock.close()

        # --- Regular "Patient" Client Logic (Sustained Load) ---
        log.debug(f"Client {client_id}: Starting patient connection.", type=server_type)
        end_time_for_client = start_client_time + test_duration

        if server_type == ServerType.JAVA:
            reader, writer = None, None
            try:
                # Initial connection with a reasonable timeout
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=10
                )
                # Add socket to global list for potential cleanup
                active_sockets.append(writer.transport.get_extra_info("socket"))

                handshake_packet, status_request_packet = (
                    get_java_handshake_and_status_request_packets(host, port)
                )

                while time.time() < end_time_for_client:
                    writer.write(handshake_packet)
                    writer.write(status_request_packet)
                    await writer.drain()

                    # Read response to ensure connection is live
                    try:
                        # Java server status response can be quite large
                        response_data = await asyncio.wait_for(
                            reader.read(8192), timeout=5
                        )
                        if not response_data:  # Server closed connection
                            log.warning(
                                f"Client {client_id}: "
                                f"Java server closed connection (EOF)."
                            )
                            break  # Exit loop if connection closed gracefully
                    except asyncio.TimeoutError:
                        log.warning(
                            f"Client {client_id}: Java read timeout.",
                            elapsed=f"{time.time() - start_client_time:.2f}s",
                        )
                        # If timeout, try sending again, connection might recover
                        continue
                    except ConnectionResetError:
                        log.warning(
                            f"Client {client_id}: Java server reset connection.",
                            elapsed=f"{time.time() - start_client_time:.2f}s",
                        )
                        break  # Exit loop if connection reset

                    await asyncio.sleep(1)  # Simulate some delay between pings

                successful_connections += 1
                return "SUCCESS"

            except ConnectionRefusedError:
                log.error(
                    f"Client {client_id}: Java connection refused.",
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return "FAILURE: Connection Refused"
            except asyncio.TimeoutError:
                log.error(
                    f"Client {client_id}: Java initial connection timeout ({10}s).",
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return "FAILURE: Initial Connect Timeout"
            except Exception as e:
                log.error(
                    f"Client {client_id}: Unhandled Java client error: {e}",
                    exc_info=True,
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return f"FAILURE: Unhandled Java Error ({type(e).__name__})"
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()
                    # Remove socket from active list if it was added
                    sock_obj = writer.transport.get_extra_info("socket")
                    if sock_obj and sock_obj in active_sockets:
                        active_sockets.remove(sock_obj)

        else:  # Bedrock (UDP)
            loop = asyncio.get_running_loop()
            transport_future = loop.create_future()  # For connection_lost signal
            transport, protocol = None, None
            try:
                # Create UDP endpoint for client
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: UDPClientProtocol(transport_future, client_id),
                    remote_addr=(host, port),
                )
                # Add socket to global list for potential cleanup
                active_sockets.append(transport._sock)

                while time.time() < end_time_for_client:
                    protocol.response_received.clear()  # Clear event for next response
                    transport.sendto(BEDROCK_UNCONNECTED_PING)

                    try:
                        # Wait for a response, with a timeout
                        await asyncio.wait_for(
                            protocol.response_received.wait(), timeout=5
                        )
                    except asyncio.TimeoutError:
                        log.warning(
                            f"Client {client_id}: Bedrock response timeout.",
                            elapsed=f"{time.time() - start_client_time:.2f}s",
                        )
                        # If timeout, try sending again, maybe packet loss
                        continue
                    except Exception as e:  # Catch other potential issues
                        log.warning(
                            f"Client {client_id}: Bedrock response error: {e}",
                            exc_info=True,
                        )
                        break  # Break loop if persistent error

                    await asyncio.sleep(1)  # Simulate some delay between pings

                successful_connections += 1
                return "SUCCESS"

            except asyncio.TimeoutError:
                log.error(
                    f"Client {client_id}: Bedrock initial connect timeout ({10}s).",
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return "FAILURE: Initial Connect Timeout"
            except ConnectionRefusedError:  # UDP doesn't typically refuse
                log.error(
                    f"Client {client_id}: Bedrock connection refused (unusual).",
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return "FAILURE: Connection Refused"
            except Exception as e:
                log.error(
                    f"Client {client_id}: Unhandled Bedrock client error: {e}",
                    exc_info=True,
                    elapsed=f"{time.time() - start_client_time:.2f}s",
                )
                failed_connections += 1
                return f"FAILURE: Unhandled Bedrock Error ({type(e).__name__})"
            finally:
                if transport:
                    transport.close()
                    # Remove socket from active list if it was added
                    if transport._sock in active_sockets:
                        active_sockets.remove(transport._sock)
                # Ensure the future is set if not already, to prevent
                # RuntimeError: Task got bad future.
                if not transport_future.done():
                    transport_future.set_result(None)

    except asyncio.CancelledError:
        log.info(
            f"Client {client_id}: Task cancelled. Exiting.",
            elapsed=f"{time.time() - start_client_time:.2f}s",
        )
        # Do not count as success or failure if cancelled externally
        return "CANCELLED"
    except Exception as e:
        log.critical(
            f"Client {client_id}: CRITICAL error before main logic: {e}", exc_info=True
        )
        failed_connections += 1
        return f"FAILURE: Critical Pre-Loop Error ({type(e).__name__})"


async def ensure_container_stopped(container_name: str):
    """Asynchronously ensures a container is stopped before test starts."""
    log.info(f"Ensuring container '{container_name}' is stopped...")
    client = None
    try:
        client = aiodocker.Docker()
        container = await client.containers.get(container_name)
        status_info = await container.show()
        if status_info["State"]["Running"]:
            log.warning(f"Container '{container_name}' was running. Stopping now.")
            await container.stop(t=30)  # Give 30s to stop gracefully
            status_info = await container.show()  # Re-check status
            if status_info["State"]["Running"]:
                log.error(f"Container '{container_name}' failed to stop gracefully.")
                await container.kill()  # Force kill if still running
                log.info(f"Container '{container_name}' forcefully killed.")
        log.info(f"Container '{container_name}' confirmed stopped.")
    except DockerError as e:
        if e.status == 404:
            log.info(f"Container '{container_name}' not found, assumed stopped.")
        else:
            log.error(
                f"Docker error ensuring stop for {container_name}: {e}", exc_info=True
            )
    except Exception as e:
        log.error(
            f"Unexpected error ensuring stop for {container_name}: {e}", exc_info=True
        )
    finally:
        if client:
            await client.close()


async def dump_debug_info(container_names_to_log):
    """
    Asynchronously connects to Docker daemon to retrieve and print status
    and logs for specified containers.
    """
    print("\n" + "=" * 25 + " FINAL DIAGNOSTIC INFO " + "=" * 25)
    client = None
    try:
        client = aiodocker.Docker()
        all_test_containers = ["nether-bridge", "mc-bedrock", "mc-java", "nb-tester"]

        print("\n--- STATUS OF ALL TEST CONTAINERS ---")
        for container_name in all_test_containers:
            try:
                container = await client.containers.get(container_name)
                status_info = await container.show()
                status = status_info["State"]["Status"]
                running_status = status_info["State"]["Running"]
                health_status = status_info["State"].get("Health", {}).get("Status")
                log.info(
                    f"  - {container_name:<15} | Status: {status:<8} | "
                    f"Running: {str(running_status):<5} | Health: {health_status}"
                )
            except DockerError as e:
                if e.status == 404:
                    log.info(f"  - {container_name:<15} | Status: Not Found")
                else:
                    log.error(f"Error getting status for {container_name}: {e}")
            except Exception as e:
                log.error(f"Unexpected error getting status for {container_name}: {e}")

        for name in container_names_to_log:
            print(f"\n--- LOGS FOR CONTAINER: {name} ---")
            try:
                container = await client.containers.get(name)
                # aiodocker log streaming can be complex to capture
                # Get last 500 lines as a string
                logs = await container.log(stdout=True, stderr=True, tail=500)
                if logs:
                    # Logs come as a list of lines, join them.
                    print("\n".join(logs).strip())
                else:
                    print("[No logs found for this container]")
            except DockerError as e:
                if e.status == 404:
                    print(f"[Container '{name}' was not found]")
                else:
                    print(f"[Docker error retrieving logs for '{name}': {e}]")
            except Exception as e:
                print(f"[Could not retrieve logs for '{name}': {e}]")

    except Exception as e:
        log.critical(
            f"Could not gather debug info. Docker client failed: {e}", exc_info=True
        )
    finally:
        if client:
            await client.close()
        print("\n" + "=" * 27 + " END OF DEBUG INFO " + "=" * 27)


async def amain_load_test(args):
    """Asynchronous main entrypoint for the load tester."""
    # Global counters reset before each run (important if script is reused)
    global successful_connections, failed_connections, chaos_aborts, active_sockets
    successful_connections = 0
    failed_connections = 0
    chaos_aborts = 0
    active_sockets = []

    target_host = args.host
    port = 25565 if args.server_type == ServerType.JAVA else 19132
    target_container_name = (
        "mc-java" if args.server_type == ServerType.JAVA else "mc-bedrock"
    )
    # Containers to dump logs from on failure
    containers_to_log_on_fail = ["nether-bridge", target_container_name]

    overall_exit_code = 1  # Assume failure until proven otherwise

    try:
        log.info("--- Starting Nether-bridge True Cold-Start Load Test ---")
        log.info(f"Server Type:        {args.server_type.value}")
        log.info(f"Target Container:   {target_container_name}")
        log.info(f"Target Endpoint:    {target_host}:{port}")
        log.info(f"Concurrent Clients: {args.clients}")
        log.info(f"Client Duration:    {args.duration}s (for patient clients)")
        log.info(f"Chaos Percentage:   {args.chaos}%")
        log.info(f"Client Start Delay: {args.delay}s")
        log.info("---------------------------------------------")

        # Step 1: Ensure the target server container is stopped for a true cold start
        await ensure_container_stopped(target_container_name)

        # Step 2: Run load test clients concurrently
        log.info(f"--- Spawning {args.clients} clients... ---")
        start_overall_time = time.time()

        tasks = []
        for i in range(args.clients):
            task = asyncio.create_task(
                simulate_client(
                    args.server_type,
                    target_host,
                    port,
                    i,
                    args.duration,
                    args.chaos,
                )
            )
            tasks.append(task)
            if args.delay > 0:
                await asyncio.sleep(args.delay)

        await asyncio.gather(
            *tasks, return_exceptions=True
        )  # Let gather collect results

        end_overall_time = time.time()

        log.info("\n--- Load Test Summary ---")
        log.info(f"Total Time Elapsed: {end_overall_time - start_overall_time:.2f}s")
        log.info(f"Successful Clients: {successful_connections} / {args.clients}")
        log.info(f"Chaos Aborts:       {chaos_aborts} / {args.clients}")
        log.info(f"Failed Clients:     {failed_connections} / {args.clients}")
        log.info("-------------------------")

        if failed_connections > 0:
            log.error("Load test finished with connection failures.")
            overall_exit_code = 1
        else:
            log.info("Load test completed successfully.")
            overall_exit_code = 0

    except Exception as e:
        log.critical(
            "UNHANDLED EXCEPTION DURING TEST EXECUTION.", error=e, exc_info=True
        )
        overall_exit_code = 1

    finally:
        # Close any lingering sockets from active_sockets list
        for sock in list(active_sockets):  # Iterate copy to allow modification
            try:
                # Depending on socket type, close appropriately
                if hasattr(sock, "close") and callable(sock.close):
                    sock.close()
                elif hasattr(sock, "_sock") and hasattr(sock._sock, "close"):
                    sock._sock.close()
            except Exception as e:
                log.warning(f"Error closing lingering socket: {e}")
        active_sockets.clear()  # Clear the list

        log.info("\n--- DUMPING DIAGNOSTIC INFO ---")
        await dump_debug_info(containers_to_log_on_fail)
        sys.exit(overall_exit_code)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Load testing tool for Nether-bridge proxy."
    )
    parser.add_argument(
        "--server-type",
        type=lambda s: ServerType[s.upper()],  # Convert string to Enum member
        choices=list(ServerType),
        required=True,
        help="The type of Minecraft server to test ('java' or 'bedrock').",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=get_proxy_host(),  # Use helper for default host
        help="The IP address of the proxy.",
    )
    parser.add_argument(
        "-c", "--clients", type=int, default=25, help="Concurrent clients to simulate."
    )
    parser.add_argument(
        "--duration",  # Renamed from timeout for clarity in meaning
        type=int,
        default=240,
        help="Max time (seconds) for each client to stay connected (sustained load).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between starting clients (in seconds).",
    )
    parser.add_argument(
        "--chaos",
        type=int,
        default=0,
        choices=range(0, 101),
        metavar="[0-100]",
        help="Percentage of clients that will connect and abruptly disconnect.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Set the logging level (e.g., DEBUG, INFO, WARNING).",
    )

    args = parser.parse_args()

    # --- Logging Configuration ---
    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Call the asynchronous main function
    asyncio.run(amain_load_test(args))
