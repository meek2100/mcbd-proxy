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
import time

import structlog

log = structlog.get_logger()

# --- Statistics Tracking ---
successful_connections = 0
failed_connections = 0


async def simulate_client(host: str, port: int, client_id: int, chaos_percent: int):
    """
    Simulates a single client with different behaviors based on the chaos mode.
    """
    global successful_connections, failed_connections
    writer = None
    try:
        log.debug(f"Client {client_id}: Starting TCP connection.")
        # Increased timeout for more reliable connections under heavy load
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=20
        )
        successful_connections += 1
        log.debug(f"Client {client_id}: TCP connection successful.")

        # In chaos mode, there's a chance the client "crashes"
        if chaos_percent > 0 and random.randint(1, 100) <= chaos_percent:
            log.warning(f"Client {client_id}: Simulating crash (abrupt close).")
            if writer.get_extra_info("socket"):
                writer.get_extra_info("socket").close()
            return

        await asyncio.sleep(random.uniform(0.5, 2.0))
        log.debug(f"Client {client_id}: Closing connection gracefully.")
    except Exception as e:
        failed_connections += 1
        log.error(f"Client {client_id}: TCP connection failed.", error=str(e))
    finally:
        if writer and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def main(args):
    """Main function to orchestrate the load test."""
    port = 19132 if args.server_type == "bedrock" else 25565
    log.info(
        f"Starting test with {args.clients} clients "
        f"against {args.server_type} server on {args.host}:{port}",
        chaos_percent=args.chaos,
    )

    start_time = time.time()
    tasks = []
    for i in range(args.clients):
        task = asyncio.create_task(simulate_client(args.host, port, i, args.chaos))
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
        "--host", type=str, default="127.0.0.1", help="The IP address of the proxy."
    )
    parser.add_argument(
        "-c", "--clients", type=int, default=20, help="Concurrent clients to simulate."
    )
    parser.add_argument(
        "--delay", type=float, default=0.05, help="Delay between starting clients."
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
    logging.basicConfig(level=args.log_level.upper())
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )

    asyncio.run(main(args))
