import asyncio
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager

logger = structlog.get_logger(__name__)


class UdpProxyProtocol(asyncio.DatagramProtocol):
    """Protocol to handle UDP traffic for a specific Bedrock server."""

    def __init__(
        self,
        proxy_instance: "NetherBridgeProxy",
        server_config: ServerConfig,
    ):
        self.proxy = proxy_instance
        self.server_config = server_config
        self.transport = None
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """
        Handles incoming datagrams from clients and forwards them to the
        appropriate handler in the main proxy class.
        """
        asyncio.create_task(
            self.proxy._handle_udp_datagram(data, addr, self.server_config)
        )

    def error_received(self, exc: Exception):
        logger.error("UDP protocol error", error=exc, server=self.server_config.name)

    def connection_lost(self, exc: Exception | None):
        logger.warning(
            "UDP transport closed", server=self.server_config.name, error=exc
        )


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
        # For UDP, we need to track clients by their address
        self.udp_clients: Dict[Tuple[str, int], asyncio.DatagramTransport] = {}

    async def _proxy_data(self, reader, writer, server_name: str):
        """Forwards data between a client and a server stream for TCP."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            logger.debug("TCP connection closed during data forwarding.")
        except Exception as e:
            logger.error("Error in TCP proxy task", error=e, server=server_name)
        finally:
            writer.close()

    async def _handle_tcp_connection(self, reader, writer, server_config: ServerConfig):
        """Handles a new TCP client connection (for Java servers)."""
        state = self.server_states[server_config.container_name]
        state["sessions"] += 1
        state["last_activity"] = time.time()
        client_addr = writer.get_extra_info("peername")
        logger.info(
            "New TCP client connection", client=client_addr, server=server_config.name
        )

        try:
            if state["status"] != "running":
                logger.info(
                    "Server is not running. Initiating startup...",
                    server=server_config.name,
                )
                if not await self.docker_manager.start_server(
                    server_config, self.settings
                ):
                    logger.error(
                        "Failed to start server. Closing connection.",
                        server=server_config.name,
                    )
                    return
                state["status"] = "running"

            s_reader, s_writer = await asyncio.open_connection(
                "127.0.0.1", server_config.internal_port
            )
            logger.info("Proxy connection established", server=server_config.name)

            await asyncio.gather(
                self._proxy_data(reader, s_writer, server_config.name),
                self._proxy_data(s_reader, writer, server_config.name),
            )
        except Exception as e:
            logger.error(
                "An error occurred in the TCP connection handler.",
                error=str(e),
                server=server_config.name,
            )
        finally:
            writer.close()
            state["sessions"] -= 1
            state["last_activity"] = time.time()
            logger.info(
                "TCP client connection closed",
                client=client_addr,
                server=server_config.name,
            )

    async def _handle_udp_datagram(
        self, data: bytes, addr: Tuple[str, int], server_config: ServerConfig
    ):
        """Handles a new UDP datagram (for Bedrock servers)."""
        state = self.server_states[server_config.container_name]

        # A very simplified session management for UDP
        if addr not in self.udp_clients:
            state["sessions"] += 1
            logger.info(
                "New UDP client session", client=addr, server=server_config.name
            )
            # This is not a real transport, just a placeholder for session tracking
            self.udp_clients[addr] = True

        state["last_activity"] = time.time()

        if state["status"] != "running":
            logger.info(
                "Server is not running. Initiating startup...",
                server=server_config.name,
            )
            if not await self.docker_manager.start_server(server_config, self.settings):
                logger.error(
                    "Failed to start UDP server. Packet dropped.",
                    server=server_config.name,
                )
                return
            state["status"] = "running"

        # Forward the packet to the internal server port
        loop = asyncio.get_running_loop()
        # This is a simplified forwarding mechanism. A real implementation might
        # open a dedicated socket for each client session if needed.
        transport, _ = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(),
            remote_addr=("127.0.0.1", server_config.internal_port),
        )
        transport.sendto(data)
        transport.close()

    async def _monitor_activity(self):
        """Periodically checks for idle servers and shuts them down."""
        logger.info("Starting server activity monitor.")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.settings.player_check_interval_seconds)
                for server in self.servers:
                    state = self.server_states[server.container_name]
                    # Simple session clearing for UDP, a more robust solution
                    # would involve timeouts per client.
                    if server.server_type == "bedrock":
                        idle_clients = []
                        for client_addr in self.udp_clients:
                            # Check last activity time for each client if tracked
                            idle_clients.append(client_addr)

                        for client_addr in idle_clients:
                            del self.udp_clients[client_addr]
                            state["sessions"] -= 1

                    if state["status"] == "running" and state["sessions"] == 0:
                        idle_time = time.time() - state["last_activity"]
                        if idle_time > self.settings.idle_timeout_seconds:
                            logger.info(
                                "Server is idle. Initiating shutdown.",
                                server=server.name,
                            )
                            if await self.docker_manager.stop_server(
                                server.container_name
                            ):
                                state["status"] = "stopped"
            except asyncio.CancelledError:
                break
        logger.info("Server activity monitor has stopped.")

    async def run(self):
        """Starts all proxy listeners and the activity monitor."""
        listeners = []
        loop = asyncio.get_running_loop()

        for server_config in self.servers:
            if server_config.server_type == "java":

                def handler_factory(sc=server_config):
                    return lambda r, w: self._handle_tcp_connection(r, w, sc)

                server = await asyncio.start_server(
                    handler_factory(), "0.0.0.0", server_config.listen_port
                )
                listeners.append(server)
                addr = server.sockets[0].getsockname()
                logger.info(
                    "Proxy listening for Java (TCP)",
                    server=server_config.name,
                    address=addr,
                )
            elif server_config.server_type == "bedrock":

                def protocol_factory(sc=server_config):
                    return UdpProxyProtocol(self, sc)

                transport, _ = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=("0.0.0.0", server_config.listen_port),
                )
                # We don't add UDP transports to the listeners list for shutdown
                # as they are handled differently.
                logger.info(
                    "Proxy listening for Bedrock (UDP)",
                    server=server_config.name,
                    address=transport.get_extra_info("sockname"),
                )

        monitor_task = asyncio.create_task(self._monitor_activity())

        await self._shutdown_event.wait()
        logger.info("Shutting down proxy listeners and tasks...")

        for server in listeners:
            server.close()
            await server.wait_closed()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
