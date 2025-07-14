# tests/load_tester.py
"""
An asynchronous load and chaos tester for the Nether-bridge proxy.
Simulates multiple concurrent clients connecting to a server, with different
behavioral modes to test proxy stability.
"""

import argparse
import asyncio
import random
import time

import structlog

log = structlog.get_logger()

# --- Statistics Tracking (Restored) ---
successful_connections = 0
failed_connections = 0


async def simulate_tcp_client(host: str, port: int, client_id: int, mode: str):
    """
    Simulates a single TCP client with different behaviors based on the mode.
    - 'load': Connects and disconnects cleanly.
    - 'chaos': May disconnect improperly to test server resilience.
    """
    global successful_connections, failed_connections
    writer = None
    try:
        log.debug(f"Client {client_id}: Starting TCP connection in '{mode}' mode.")
        # Increased timeout for more reliable connections under heavy load
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=20
        )
        successful_connections += 1
        log.debug(f"Client {client_id}: TCP connection successful.")

        # In chaos mode, there's a chance the client "crashes"
        if mode == "chaos" and random.random() < 0.25:
            log.warning(f"Client {client_id}: Simulating crash (abrupt close).")
            # Abruptly close the underlying transport without a proper handshake
            if writer.get_extra_info("socket"):
                writer.get_extra_info("socket").close()
            return

        await asyncio.sleep(2)
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
                pass  # Ignore errors on close, as we are already handling them


async def main(args):
    """Main function to orchestrate the load test."""
    log.info(
        f"Starting test with {args.clients} clients in '{args.mode}' mode "
        f"against {args.host}:{args.port}"
    )

    start_time = time.time()
    tasks = []
    for i in range(args.clients):
        task = asyncio.create_task(
            simulate_tcp_client(args.host, args.port, i, args.mode)
        )
        tasks.append(task)
        if args.delay > 0:
            await asyncio.sleep(args.delay)

    await asyncio.gather(*tasks)

    duration = time.time() - start_time

    # --- Final Report (Restored) ---
    print("\n--- Test Results ---")
    print(f"Mode: {args.mode.upper()}")
    print(f"Total Duration: {duration:.2f} seconds")
    print(f"Total Clients Simulated: {args.clients}")
    print(f"Successful Connections: {successful_connections}")
    print(f"Failed Connections: {failed_connections}")
    print("----------------------")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nether-bridge Load/Chaos Tester")
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="The IP address of the proxy."
    )
    parser.add_argument(
        "--port", type=int, default=25565, help="The port of the proxy."
    )
    parser.add_argument(
        "-c", "--clients", type=int, default=20, help="Concurrent clients to simulate."
    )
    parser.add_argument(
        "--delay", type=float, default=0.05, help="Delay between starting clients."
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["load", "chaos"],
        default="load",
        help="Test mode: 'load' for clean connections, 'chaos' for unstable ones.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Set the logging level (e.g., DEBUG, INFO, WARNING).",
    )

    args = parser.parse_args()

    # --- Logging Configuration (Restored) ---
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    log.setLevel(args.log_level.upper())

    asyncio.run(main(args))
