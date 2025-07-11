# proxy.py
import asyncio
import signal  # Import the signal module
import time
from threading import Lock  # Removed 'Thread' as it's used in main.py
from typing import Dict, List  # Added for type hints based on prior context

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    SERVER_STARTUP_DURATION,
)

# Constants, if any, should be defined here or imported from config
HEARTBEAT_INTERVAL_SECONDS = (
    30  # This constant was in the old proxy.py, keeping for context
)


class NetherBridgeUDPProxyProtocol(asyncio.DatagramProtocol):
    """
    A UDP protocol for proxying Bedrock Edition traffic.
    Handles client connections and forwards data to backend server.
    """

    def __init__(self, proxy: "NetherBridgeProxy", server_config: ServerConfig):
        self.proxy = proxy
        self.server_config = server_config
        self.transport = None  # This is the transport for the listening UDP socket
        self.logger = structlog.get_logger(__name__).bind(
            server_name=server_config.name, server_type=server_config.server_type
        )
        # Map (client_addr) -> (backend_transport, last_activity_time)
        # This helps manage "sessions" for connectionless UDP
        self.client_to_backend_info = {}

    def connection_made(self, transport):
        """Called when the listening UDP socket is ready."""
        self.transport = transport
        self.logger.info(
            "UDP proxy listener started.", listen_port=self.server_config.listen_port
        )

    def datagram_received(self, data, client_addr):
        """Called when a UDP datagram is received from a client."""
        self.proxy.logger.debug(
            "UDP packet received from client",
            client_addr=client_addr,
            packet_size=len(data),
        )

        container_name = self.server_config.container_name
        server_state = self.proxy.server_states[container_name]

        asyncio.create_task(self._handle_udp_packet(data, client_addr, server_state))

    async def _handle_udp_packet(
        self, data: bytes, client_addr: tuple, server_state: dict
    ):
        """Asynchronously handles a received UDP packet."""
        container_name = self.server_config.container_name

        # 1. Initiate server start if needed
        # This will be largely similar to TCP logic, but specific to UDP needs
        if await self.proxy._initiate_server_start(self.server_config):
            self.logger.info(
                "First UDP packet for non-running server. Triggering async "
                "server start.",
                container_name=container_name,
            )

        # 2. Wait for the server to be ready
        # For UDP, we need to ensure the server is ready before forwarding.
        # This might involve buffering packets for a short period.
        try:
            # We don't want to block the entire event loop here indefinitely if
            # the server never starts. Use a timeout for waiting for the ready event.
            await asyncio.wait_for(
                server_state["ready_event"].wait(),
                timeout=self.proxy.settings.server_ready_max_wait_time_seconds,
            )
            if not server_state["ready_event"].is_set():
                # This means timeout was hit but event not set
                self.logger.warning(
                    "Server not ready in time for UDP packet, dropping.",
                    container_name=container_name,
                )
                return
        except asyncio.TimeoutError:
            self.logger.error(
                "Server did not become ready for UDP packet within timeout. "
                "Packet dropped.",
                container_name=container_name,
            )
            return
        except Exception as e:
            self.logger.error(
                "Error waiting for server readiness for UDP packet. Packet dropped.",
                container_name=container_name,
                error=str(e),
            )
            return

        # 3. Forward packet to backend server
        try:
            backend_transport = self.client_to_backend_info.get(client_addr)

            if not backend_transport:
                # Create a new UDP endpoint to the backend server for this client
                # session
                self.logger.debug(
                    "Creating new UDP backend connection for client.",
                    client_addr=client_addr,
                )
                loop = asyncio.get_running_loop()
                _, protocol = await loop.create_datagram_endpoint(
                    lambda: NetherBridgeUDPRemoteProtocol(self, client_addr),
                    remote_addr=(
                        self.server_config.container_name,
                        self.server_config.internal_port,
                    ),
                )
                backend_transport = protocol.transport
                self.client_to_backend_info[client_addr] = (
                    backend_transport,
                    time.time(),
                )
                ACTIVE_SESSIONS.labels(server_name=self.server_config.name).inc()

            backend_transport.sendto(data)
            BYTES_TRANSFERRED.labels(
                server_name=self.server_config.name, direction="c2s"
            ).inc(len(data))
            self.proxy.server_states[container_name]["last_activity"] = time.time()
            self.client_to_backend_info[client_addr] = (
                backend_transport,
                time.time(),
            )  # Update last activity for this client

        except Exception as e:
            self.logger.error(
                "Error forwarding UDP packet to backend server.",
                client_addr=client_addr,
                error=str(e),
                exc_info=True,
            )
            # Consider closing the backend transport for this client if there's a
            # permanent error
            if client_addr in self.client_to_backend_info:
                try:
                    self.client_to_backend_info[client_addr][0].close()
                except Exception as close_e:
                    self.logger.warning(
                        "Error closing backend UDP transport.",
                        client_addr=client_addr,
                        error=str(close_e),
                    )
                finally:
                    del self.client_to_backend_info[client_addr]
                    ACTIVE_SESSIONS.labels(server_name=self.server_config.name).dec()

    def error_received(self, exc):
        """Called when a send or receive operation raises an OSError."""
        self.logger.error("UDP listener error received", error=str(exc), exc_info=True)

    def connection_lost(self, exc):
        """Called when the listening socket is closed or loses connection."""
        self.logger.info("UDP proxy listener connection lost.", exc=str(exc))
        # No need to explicitly close client backend transports here, as they're
        # managed per packet/session.


class NetherBridgeUDPRemoteProtocol(asyncio.DatagramProtocol):
    """
    Handles UDP packet forwarding from the backend server back to the client.
    Each instance represents a connection from a client to the backend server.
    """

    def __init__(self, main_protocol: NetherBridgeUDPProxyProtocol, client_addr: tuple):
        self.main_protocol = main_protocol
        self.client_addr = client_addr
        self.transport = None  # This is the transport for the backend server socket
        self.logger = structlog.get_logger(__name__).bind(
            server_name=main_protocol.server_config.name, client_addr=client_addr
        )

    def connection_made(self, transport):
        """Called when the UDP socket to the backend server is ready."""
        self.transport = transport
        self.logger.debug(
            "UDP backend connection made to server.",
            remote_addr=transport.get_extra_info("peername"),
        )

    def datagram_received(self, data, server_addr):
        """Called when a datagram is received from the backend server."""
        self.logger.debug(
            "UDP packet received from backend server",
            server_addr=server_addr,
            packet_size=len(data),
        )

        # Forward the data back to the original client
        try:
            if self.main_protocol.transport:
                self.main_protocol.transport.sendto(data, self.client_addr)
                BYTES_TRANSFERRED.labels(
                    server_name=self.main_protocol.server_config.name,
                    direction="s2c",
                ).inc(len(data))
                # Update main proxy's activity time
                self.main_protocol.proxy.server_states[
                    self.main_protocol.server_config.container_name
                ]["last_activity"] = time.time()
            else:
                self.logger.warning(
                    "Main UDP listener transport not available to send data back to"
                    " client."
                )
        except Exception as e:
            self.logger.error(
                "Error sending UDP packet back to client.",
                client_addr=self.client_addr,
                error=str(e),
                exc_info=True,
            )

    def error_received(self, exc):
        """Called when a send or receive operation on the backend socket raises an
        OSError.
        """
        self.logger.error("UDP backend error received", error=str(exc), exc_info=True)
        # Consider cleaning up the client's backend info in the main protocol
        if self.client_addr in self.main_protocol.client_to_backend_info:
            try:
                self.main_protocol.client_to_backend_info[self.client_addr][0].close()
            except Exception as close_e:
                self.logger.warning(
                    "Error closing remote backend UDP transport after error.",
                    error=str(close_e),
                )
            finally:
                del self.main_protocol.client_to_backend_info[self.client_addr]
                ACTIVE_SESSIONS.labels(
                    server_name=self.main_protocol.server_config.name
                ).dec()

    def connection_lost(self, exc):
        """Called when the UDP socket to the backend server is closed or loses
        connection.
        """
        self.logger.info("UDP backend connection lost.", exc=str(exc))
        # When the backend connection to the server is lost, we should
        # invalidate the client's mapping
        if self.client_addr in self.main_protocol.client_to_backend_info:
            del self.main_protocol.client_to_backend_info[self.client_addr]
            ACTIVE_SESSIONS.labels(
                server_name=self.main_protocol.server_config.name
            ).dec()


class NetherBridgeProxy:
    """
    A proxy server that dynamically manages Docker containers for Minecraft servers.
    """

    def __init__(self, settings: ProxySettings, servers: List[ServerConfig]):
        self.logger = structlog.get_logger(__name__)
        self.settings = settings
        self.servers_list = servers
        self.servers_config_map: Dict[int, ServerConfig] = {
            s.listen_port: s for s in servers
        }
        self.docker_manager = DockerManager()

        self._shutdown_event = asyncio.Event()
        self._shutdown_requested = False
        self._reload_requested = False
        self.server_locks: Dict[str, Lock] = {s.container_name: Lock() for s in servers}
        self.server_states: Dict[str, Dict] = {
            s.container_name: {
                "lock": Lock(),
                "status": "stopped",
                "last_activity": 0,
                "pending_connections": [],
                "ready_event": asyncio.Event(),
            }
            for s in servers
        }
        self.udp_transports = {}  # To hold active UDP transports
        self.udp_protocols = {}  # To hold active UDP protocols

    def signal_handler(self, sig, frame):
        """Handles signals for graceful shutdown and configuration reloads."""
        if hasattr(signal, "SIGHUP") and sig == signal.SIGHUP:
            self.logger.warning("SIGHUP received. Requesting a configuration reload.")
            self._reload_requested = True
            self._shutdown_event.set()
        else:  # SIGINT, SIGTERM
            self.logger.warning(
                "Shutdown signal received, initiating shutdown.", sig=sig
            )
            self._shutdown_requested = True
            self._shutdown_event.set()

    async def _forward_data(self, reader, writer, server_name, direction):
        """Asynchronously forward data between two streams."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
                BYTES_TRANSFERRED.labels(
                    server_name=server_name, direction=direction
                ).inc(len(data))
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            self.logger.debug("Connection closed during forwarding.", error=str(e))
        finally:
            writer.close()

    async def _handle_tcp_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ):
        """Callback to handle a new TCP client connection."""
        client_addr = client_writer.get_extra_info("peername")
        listen_port = client_writer.get_extra_info("sockname")[1]
        server_config = self.servers_config_map[listen_port]
        container_name = server_config.container_name

        self.logger.info(
            "New TCP connection", client_addr=client_addr, server=server_config.name
        )

        server_state = self.server_states[container_name]

        # Use await self._initiate_server_start as it is now async
        if await self._initiate_server_start(server_config):
            self.logger.info(
                "First TCP connection for stopped server. Starting...",
                server_name=server_config.name,
            )
            # Wait for the server to be ready using the asyncio.Event
            await server_state["ready_event"].wait()
            self.logger.info(
                "Server reported as ready. Proceeding with connection.",
                server_name=server_config.name,
            )

        if server_state["status"] != "running":
            self.logger.error("Server is not running. Closing connection.")
            client_writer.close()
            return

        try:
            server_reader, server_writer = await asyncio.open_connection(
                host=container_name, port=server_config.internal_port
            )
        except Exception as e:
            self.logger.error("Failed to connect to backend", error=e)
            client_writer.close()
            return

        ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

        c2s_task = asyncio.create_task(
            self._forward_data(client_reader, server_writer, server_config.name, "c2s")
        )
        s2c_task = asyncio.create_task(
            self._forward_data(server_reader, client_writer, server_config.name, "s2c")
        )

        try:
            await asyncio.gather(c2s_task, s2c_task)
        finally:
            ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
            self.server_states[container_name]["last_activity"] = time.time()
            self.logger.info(
                "TCP session ended.",
                client_addr=client_addr,
                server_name=server_config.name,
            )

    async def _initiate_server_start(self, server_config: ServerConfig) -> bool:
        """Atomically checks state and starts the server startup task if needed."""
        container_name = server_config.container_name
        with self.server_locks[container_name]:
            if self.server_states[container_name]["status"] == "stopped":
                self.logger.info(
                    "Server is stopped. Initiating startup sequence.",
                    server_name=server_config.name,
                )
                self.server_states[container_name]["status"] = "starting"
                self.server_states[container_name]["ready_event"].clear()
                # Create an asyncio task for the server startup
                asyncio.create_task(self._start_minecraft_server_task(server_config))
                return True
        return False

    async def _start_minecraft_server_task(self, server_config: ServerConfig):
        """
        Runs as an asyncio task to start a server and wait for it to be ready.
        """
        container_name = server_config.container_name
        startup_timer_start = time.time()

        success = await self.docker_manager.start_server(server_config, self.settings)

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if success:
                state["status"] = "running"
                state["ready_event"].set()
                # RUNNING_SERVERS.inc() # This metric is not defined in metrics.py
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    time.time() - startup_timer_start
                )
                self.logger.info(
                    "Startup process complete.",
                    container_name=container_name,
                    duration_seconds=(time.time() - startup_timer_start),
                )
            else:
                self.logger.error(
                    "Server startup process failed.", container_name=container_name
                )
                state["status"] = "stopped"
                state["ready_event"].clear()

    def _ensure_all_servers_stopped_on_startup(self):
        """Ensures all managed servers are stopped when the proxy starts."""
        self.logger.info(
            "Proxy startup: Ensuring all managed servers are initially stopped."
        )
        for server_config in self.servers_list:
            if self.docker_manager.is_container_running(server_config.container_name):
                self.logger.warning(
                    "Found running at proxy startup. Issuing a safe stop.",
                    container_name=server_config.container_name,
                )
                # Call async stop_server from sync context using asyncio.run
                # This creates a temporary event loop to run the async function.
                try:
                    asyncio.run(
                        self.docker_manager.stop_server(server_config.container_name)
                    )
                except Exception as e:
                    self.logger.error(
                        "Error stopping server during initial cleanup.",
                        container_name=server_config.container_name,
                        error=str(e),
                    )

    def _monitor_servers_activity(self):
        """Monitors server activity and shuts down idle servers."""
        polling_rate = self.settings.player_check_interval_seconds
        while not self._shutdown_event.is_set():
            time.sleep(polling_rate)
            for server_config in self.servers_list:
                with self.server_states[server_config.container_name]["lock"]:
                    if (
                        self.server_states[server_config.container_name]["status"]
                        == "running"
                    ):
                        # Use a simpler approach to get metric value
                        active_sessions = (
                            ACTIVE_SESSIONS.labels(
                                server_name=server_config.name
                            )._value
                        )  # Access internal value directly for simplicity in thread
                        # Or, retrieve from the metrics endpoint if you want real-time.
                        # For this background thread, internal value should be fine.
                        if (
                            active_sessions is None
                        ):  # Handle case where label isn't set yet
                            active_sessions = 0

                        if active_sessions == 0:
                            idle_time = (
                                time.time()
                                - self.server_states[server_config.container_name][
                                    "last_activity"
                                ]
                            )
                            idle_timeout = (
                                server_config.idle_timeout_seconds
                                or self.settings.idle_timeout_seconds
                            )
                            if idle_time > idle_timeout:
                                self.logger.info(
                                    "Server idle. Initiating shutdown.",
                                    server_name=server_config.name,
                                    idle_threshold_seconds=idle_timeout,
                                )
                                # Call async stop_server from sync context
                                try:
                                    asyncio.run(
                                        self.docker_manager.stop_server(
                                            server_config.container_name
                                        )
                                    )
                                    self.server_states[server_config.container_name][
                                        "status"
                                    ] = "stopped"
                                    # RUNNING_SERVERS.dec() # Not defined
                                except Exception as e:
                                    self.logger.error(
                                        "Error stopping server from monitor thread.",
                                        container_name=server_config.container_name,
                                        error=str(e),
                                    )

    async def _run_proxy_loop(self):
        """The main async event loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")

        server_tasks = []
        loop = asyncio.get_running_loop()

        for srv_cfg in self.servers_list:
            if srv_cfg.server_type == "java":
                server = await asyncio.start_server(
                    self._handle_tcp_client, "0.0.0.0", srv_cfg.listen_port
                )
                server_tasks.append(asyncio.create_task(server.serve_forever()))
                self.logger.info(
                    "Proxy listening for TCP server",
                    server_name=srv_cfg.name,
                    port=srv_cfg.listen_port,
                )
            elif srv_cfg.server_type == "bedrock":
                # === NEW UDP LISTENER FOR BEDROCK ===
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: NetherBridgeUDPProxyProtocol(self, srv_cfg),
                    local_addr=("0.0.0.0", srv_cfg.listen_port),
                )
                # Store transport and protocol to keep references
                self.udp_transports[srv_cfg.name] = transport
                self.udp_protocols[srv_cfg.name] = protocol

                # Unlike TCP servers, UDP endpoints don't have a serve_forever()
                # method. The protocol itself handles the incoming datagrams.
                # We just need to ensure the event loop keeps running.
                self.logger.info(
                    "Proxy listening for UDP server",
                    server_name=srv_cfg.name,
                    port=srv_cfg.listen_port,
                )

        await self._shutdown_event.wait()

        # Graceful shutdown for TCP servers
        for task in server_tasks:
            task.cancel()
        await asyncio.gather(*server_tasks, return_exceptions=True)

        # Graceful shutdown for UDP transports
        for name, transport in self.udp_transports.items():
            self.logger.info("Closing UDP transport for server.", server_name=name)
            transport.close()

        self.logger.info("Proxy loop is exiting.")
        return self._reload_requested
