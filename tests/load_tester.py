# tests/load_tester.py
"""
An asynchronous load and chaos tester for the Nether-bridge proxy.
Simulates multiple concurrent clients connecting to a server, with different
behavioral modes to test proxy stability.
"""

import argparse
import asyncio
import logging
import time

import structlog

log = structlog.get_logger()

# --- Statistics Tracking ---
successful_connections = 0
failed_connections = 0
chaos_aborts = 0


class UDPClientProtocol(asyncio.DatagramProtocol):
    """A simple protocol that signals when a UDP packet is received."""

    def __init__(self, on_con_lost):
        self.transport = None
        self.on_con_lost = on_con_lost
        self.received = asyncio.Event()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        log.debug("UDP client received response.", addr=addr)
        self.received.set()

    def error_received(self, exc):
        log.error("UDP client error", exc=exc)

    def connection_lost(self, exc):
        self.on_con_lost.set_result(True)


async def simulate_client(
    server_type: str, host: str, port: int, client_id: int, chaos_percent: int
):
    """
    Simulates a single client connecting via TCP or UDP.
    """
    global successful_connections, failed_connections, chaos_aborts
    log.debug(f"Client {client_id}: Starting connection.", type=server_type)

    if server_type == "java":
        # --- TCP (Java) Test ---
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=60
            )
            successful_connections += 1
        except Exception as e:
            failed_connections += 1
            log.error(f"Client {client_id}: TCP connection failed.", error=str(e))
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
    else:
        # --- UDP (Bedrock) Test ---
        loop = asyncio.get_running_loop()
        on_con_lost = loop.create_future()
        try:

            def protocol_factory():
                return UDPClientProtocol(on_con_lost)

            transport, protocol = await loop.create_datagram_endpoint(
                protocol_factory, remote_addr=(host, port)
            )
            # Send a Bedrock "unconnected ping" packet
            transport.sendto(b"\x01\x00\x00\x00\x00\x01\x23\x45\x67\x89\xab\xcd\xef")
            await asyncio.wait_for(protocol.received.wait(), timeout=10)
            successful_connections += 1
            transport.close()
        except Exception as e:
            failed_connections += 1
            log.error(f"Client {client_id}: UDP connection failed.", error=str(e))

    # Shared chaos/graceful close logic could be added here if needed


async def main(args):
    """Main function to orchestrate the load test."""
    port = 25565 if args.server_type == "java" else 19132
    log.info(
        f"Starting test with {args.clients} clients "
        f"against {args.server_type} server on {args.host}:{port}",
        chaos_percent=args.chaos,
    )

    start_time = time.time()
    tasks = []
    for i in range(args.clients):
        task = asyncio.create_task(
            simulate_client(args.server_type, args.host, port, i, args.chaos)
        )
        tasks.append(task)
        if args.delay > 0:
            await asyncio.sleep(args.delay)

    await asyncio.gather(*tasks)

    duration = time.time() - start_time

    print("\n--- Test Results ---")
    print(f"Server Type: {args.server_type.upper()}")
    print(f"Chaos Percentage: {args.chaos}%")
    print(f"Total Duration: {duration:.2f} seconds")
    print(f"Total Clients Simulated: {args.clients}")
    print(f"Successful Connections: {successful_connections}")
    print(f"Failed Connections: {failed_connections}")
    print("----------------------")

    if failed_connections > 0:
        log.error("Load test finished with connection failures.")
        exit(1)
    else:
        log.info("Load test completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nether-bridge Load/Chaos Tester")
    parser.add_argument(
        "--server-type",
        type=str,
        choices=["java", "bedrock"],
        required=True,
        help="The type of server to target.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="The IP address of the proxy.",
    )
    parser.add_argument(
        "-c", "--clients", type=int, default=20, help="Concurrent clients to simulate."
    )
    parser.add_argument(
        "--delay", type=float, default=0.1, help="Delay between starting clients."
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

    asyncio.run(main(args))
