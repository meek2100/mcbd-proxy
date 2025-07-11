# proxy.py
import asyncio
import time
from collections import defaultdict
from typing import Dict, List

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager


class NetherBridgeProxy:
    """
    Manages Minecraft server proxying and on-demand startup/shutdown.
    """

    def __init__(
        self,
        settings: ProxySettings,
        servers: List[ServerConfig],
        docker_manager: DockerManager,
        shutdown_event: asyncio.Event,
    ):
        self.settings = settings
        self.servers = servers
        self.docker_manager = docker_manager
        self._shutdown_event = shutdown_event
        self.server_states: Dict[str, Dict] = defaultdict(
            lambda: {"status": "stopped", "last_activity": 0.0, "sessions": 0}
        )
        self.logger = structlog.get_logger(__name__)

    async def _proxy_data(self, reader, writer, server_name, direction):
        """Forwards data between a client and a server stream."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self.logger.debug("Connection closed during data forwarding.")
        finally:
            writer.close()

    async def _handle_connection(self, reader, writer, server_config):
        """Handles a new client connection for a specific server."""
        state = self.server_states[server_config.container_name]
        state["sessions"] += 1
        state["last_activity"] = time.time()
        client_addr = writer.get_extra_info("peername")
        self.logger.info("New client connection", client=client_addr)

        try:
            if state["status"] != "running":
                self.logger.info(
                    "Server is not running. Initiating startup...",
                    server=server_config.name,
                )
                if not await self.docker_manager.start_server(
                    server_config, self.settings
                ):
                    self.logger.error(
                        "Failed to start server. Closing connection.",
                        server=server_config.name,
                    )
                    return
                state["status"] = "running"

            s_reader, s_writer = await asyncio.open_connection(
                "127.0.0.1", server_config.internal_port
            )
            self.logger.info("Proxy connection established", server=server_config.name)

            await asyncio.gather(
                self._proxy_data(reader, s_writer, server_config.name, "c2s"),
                self._proxy_data(s_reader, writer, server_config.name, "s2c"),
            )
        except Exception as e:
            self.logger.error(
                "An error occurred in the connection handler.", error=str(e)
            )
        finally:
            writer.close()
            state["sessions"] -= 1
            state["last_activity"] = time.time()
            self.logger.info("Client connection closed", client=client_addr)

    async def _monitor_activity(self):
        """Periodically checks for idle servers and shuts them down."""
        self.logger.info("Starting server activity monitor.")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.settings.player_check_interval_seconds)
                for server in self.servers:
                    state = self.server_states[server.container_name]
                    if state["status"] == "running" and state["sessions"] == 0:
                        idle_time = time.time() - state["last_activity"]
                        if idle_time > self.settings.idle_timeout_seconds:
                            self.logger.info(
                                "Server is idle. Initiating shutdown.",
                                server=server.name,
                            )
                            if await self.docker_manager.stop_server(
                                server.container_name
                            ):
                                state["status"] = "stopped"
            except asyncio.CancelledError:
                break  # Exit gracefully on cancellation
        self.logger.info("Server activity monitor has stopped.")

    async def run(self):
        """Starts all proxy listeners and the activity monitor."""
        listeners = []
        for server_config in self.servers:

            def handler(r, w, sc=server_config):
                return self._handle_connection(r, w, sc)

            server = await asyncio.start_server(
                handler, "0.0.0.0", server_config.listen_port
            )
            listeners.append(server)
            addr = server.sockets[0].getsockname()
            self.logger.info(
                "Proxy listening for server", server=server_config.name, address=addr
            )

        monitor_task = asyncio.create_task(self._monitor_activity())
        await self._shutdown_event.wait()
        self.logger.info("Shutting down proxy listeners and tasks...")

        # Graceful shutdown
        for server in listeners:
            server.close()
            await server.wait_closed()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
