# proxy.py
import asyncio

import structlog

from config import AppConfig
from docker_manager import DockerManager
from metrics import MetricsManager


class Proxy:
    """
    A TCP proxy server that handles Minecraft client connections and routes them
    to the appropriate backend server, managed by DockerManager.
    """

    def __init__(
        self,
        config: AppConfig,
        docker_manager: DockerManager,
        metrics_manager: MetricsManager,
    ):
        self.config = config
        self.docker_manager = docker_manager
        self.metrics_manager = metrics_manager
        self.logger = structlog.get_logger(__name__)
        self.server = None  # To hold the asyncio server instance

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """
        Handles an incoming client connection and proxies it to the
        Minecraft server.
        """
        client_addr = writer.get_extra_info("peername")
        self.logger.info("Client connected.", client_addr=client_addr)
        self.metrics_manager.connections_total.inc()

        backend_ip = await self.docker_manager.ensure_server_running(
            self.config.mc_container_name
        )

        if not backend_ip:
            self.logger.error(
                "Could not get backend IP. Closing connection.",
                client_addr=client_addr,
            )
            writer.close()
            await writer.wait_closed()
            self.metrics_manager.connections_failed.inc()
            return

        try:
            (
                backend_reader,
                backend_writer,
            ) = await asyncio.open_connection(backend_ip, self.config.mc_server_port)
            self.logger.info(
                "Proxy connection established.",
                client_addr=client_addr,
                backend_addr=(backend_ip, self.config.mc_server_port),
            )
            self.metrics_manager.connections_proxied.inc()

            # Create tasks to shuttle data between client and backend
            client_to_backend = asyncio.create_task(
                self.shuttle_data(reader, backend_writer, "C->S")
            )
            backend_to_client = asyncio.create_task(
                self.shuttle_data(backend_reader, writer, "S->C")
            )

            # Wait for either side to close the connection
            await asyncio.gather(client_to_backend, backend_to_client)

        except ConnectionRefusedError:
            self.logger.error("Backend connection refused.", backend_ip=backend_ip)
            self.metrics_manager.connections_failed.inc()
        except Exception as e:
            self.logger.error("Proxy error.", error=str(e), exc_info=True)
        finally:
            self.logger.info("Closing connection.", client_addr=client_addr)
            writer.close()
            await writer.wait_closed()

    async def shuttle_data(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        direction: str,
    ):
        """Reads data from the reader and writes it to the writer."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except asyncio.CancelledError:
            pass  # Task was cancelled, expected during shutdown
        except Exception as e:
            self.logger.error("Data shuttle error.", direction=direction, error=e)
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self):
        """Starts the TCP proxy server."""
        self.server = await asyncio.start_server(
            self.handle_client,
            self.config.proxy_host,
            self.config.proxy_port,
        )
        addr = self.server.sockets[0].getsockname()
        self.logger.info("Proxy server started.", addr=addr)
        await self.server.serve_forever()

    async def stop(self):
        """Stops the TCP proxy server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.logger.info("Proxy server stopped.")
