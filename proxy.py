# proxy.py
"""
Core asynchronous proxy logic for TCP (Java) and UDP (Bedrock) servers.
"""

import asyncio
import signal
import time

import structlog

from config import AppConfig, GameServerConfig
from docker_manager import DockerManager
from metrics import MetricsManager

log = structlog.get_logger()

# Prometheus metrics are now managed by the MetricsManager to decouple logic.


class AsyncProxy:
    """
    An asynchronous proxy server that handles TCP and UDP traffic for
    Minecraft servers, automatically starting and stopping them on demand.
    """

    def __init__(self, app_config: AppConfig, docker_manager: DockerManager):
        self.app_config = app_config
        self.docker_manager = docker_manager
        self.server_tasks = {}
        # The MetricsManager is now responsible for all Prometheus updates.
        self.metrics_manager = MetricsManager(app_config, docker_manager)

        # This dictionary holds the internal state, independent of Prometheus.
        self._server_state = {
            server.name: {"last_activity": time.time(), "is_running": False}
            for server in app_config.game_servers
        }

    async def start(self):
        """
        Starts proxy listeners, pre-warms servers, and starts monitoring.
        """
        log.info("Starting async proxy...")
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._shutdown_handler)
        loop.add_signal_handler(signal.SIGTERM, self._shutdown_handler)

        # --- Pre-warming Logic Restored ---
        for server_config in self.app_config.game_servers:
            if server_config.pre_warm:
                log.info("Pre-warming server.", server=server_config.name)
                # Ensure startup errors don't stop the whole proxy
                try:
                    await self._ensure_server_started(server_config)
                except Exception:
                    log.error(
                        "Failed to pre-warm server",
                        server=server_config.name,
                        exc_info=True,
                    )

        # Start listeners for each game server
        listener_tasks = [
            asyncio.create_task(self._start_listener(sc))
            for sc in self.app_config.game_servers
        ]

        # Start the monitoring tasks
        monitor_task = asyncio.create_task(self._monitor_server_activity())
        metrics_task = asyncio.create_task(self.metrics_manager.start())
        self.server_tasks = {
            "listeners": listener_tasks,
            "monitor": monitor_task,
            "metrics": metrics_task,
        }

        # Gather all tasks to run concurrently
        all_tasks = listener_tasks + [monitor_task, metrics_task]
        await asyncio.gather(*all_tasks, return_exceptions=True)

    def _shutdown_handler(self):
        """Initiates a graceful shutdown of all tasks."""
        log.warning("Shutdown signal received. Cancelling tasks...")
        for task_group in self.server_tasks.values():
            if isinstance(task_group, list):
                for task in task_group:
                    task.cancel()
            else:
                task_group.cancel()

    async def _start_listener(self, server_config: GameServerConfig):
        """Starts a TCP or UDP listener for a specific game server."""
        log.info(
            "Starting listener",
            server=server_config.name,
            host=server_config.proxy_host,
            port=server_config.proxy_port,
        )
        try:
            if server_config.game_type == "java":
                server = await asyncio.start_server(
                    lambda r, w: self._handle_tcp_connection(r, w, server_config),
                    server_config.proxy_host,
                    server_config.proxy_port,
                )
                await server.serve_forever()
            else:  # 'bedrock'
                loop = asyncio.get_running_loop()
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: BedrockProtocol(self, server_config),
                    local_addr=(server_config.proxy_host, server_config.proxy_port),
                )
                try:
                    await asyncio.Future()  # Keep UDP server running
                finally:
                    transport.close()
        except asyncio.CancelledError:
            log.info("Listener cancelled", server=server_config.name)
        except Exception:
            log.error("Listener failed", server=server_config.name, exc_info=True)

    async def _ensure_server_started(self, server_config: GameServerConfig):
        """Checks if a server is running and starts it if not."""
        state = self._server_state[server_config.name]
        if not state["is_running"]:
            log.info("Server not running. Starting...", server=server_config.name)
            await self.docker_manager.start_server(server_config)
            state["is_running"] = True
            log.info("Server started.", server=server_config.name)
        else:
            log.debug("Server already running.", server=server_config.name)

    def _update_activity(self, server_name: str):
        """Updates the last activity timestamp for a server."""
        self._server_state[server_name]["last_activity"] = time.time()
        self.metrics_manager.inc_active_connections(server_name)

    async def _proxy_data(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Proxies data between a client and a server until EOF."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            log.info("Connection closed during proxying.")
        finally:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()

    async def _handle_tcp_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_config: GameServerConfig,
    ):
        """Handles a new client connection for a TCP server."""
        client_addr = client_writer.get_extra_info("peername")
        log.info("New TCP connection", client=client_addr, server=server_config.name)
        self._update_activity(server_config.name)

        server_writer = None
        try:
            await self._ensure_server_started(server_config)
            server_reader, server_writer = await asyncio.open_connection(
                server_config.host, server_config.port
            )
            log.info("Connected to backend server", server=server_config.name)

            to_client = self._proxy_data(server_reader, client_writer)
            to_server = self._proxy_data(client_reader, server_writer)
            await asyncio.gather(to_client, to_server)

        except (ConnectionRefusedError, asyncio.TimeoutError):
            log.error("Could not connect to backend.", server=server_config.name)
        except Exception:
            log.error("Error in TCP handler", exc_info=True)
        finally:
            log.info("Closing TCP connection", client=client_addr)
            if not client_writer.is_closing():
                client_writer.close()
                await client_writer.wait_closed()
            if server_writer and not server_writer.is_closing():
                server_writer.close()
                await server_writer.wait_closed()
            self.metrics_manager.dec_active_connections(server_config.name)

    async def _monitor_server_activity(self):
        """Periodically checks for server inactivity and stops them."""
        log.info("Starting server activity monitor.")
        try:
            while True:
                await asyncio.sleep(self.app_config.server_check_interval)
                now = time.time()
                for sc in self.app_config.game_servers:
                    state = self._server_state[sc.name]
                    if state["is_running"]:
                        idle_time = now - state["last_activity"]
                        if idle_time > sc.stop_after_idle:
                            log.info("Server idle timeout.", server=sc.name)
                            await self.docker_manager.stop_server(
                                sc.container_name, self.app_config.server_stop_timeout
                            )
                            state["is_running"] = False
                            log.info("Server stopped.", server=sc.name)
        except asyncio.CancelledError:
            log.info("Server activity monitor stopped.")


class BedrockProtocol(asyncio.DatagramProtocol):
    """Protocol for handling UDP traffic for Bedrock servers."""

    def __init__(self, proxy: AsyncProxy, server_config: GameServerConfig):
        self.proxy = proxy
        self.server_config = server_config
        self.transport = None
        self.client_map = {}
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        """Called when a datagram is received from a client."""
        log.debug("UDP datagram received", from_addr=addr)
        self.proxy._update_activity(self.server_config.name)

        if addr not in self.client_map:
            log.info("New UDP client", client=addr)
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._create_backend_connection(addr, data))
            self.client_map[addr] = {"task": task}
        else:
            backend_protocol = self.client_map[addr].get("protocol")
            if backend_protocol and backend_protocol.transport:
                backend_protocol.transport.sendto(data)

    async def _create_backend_connection(self, client_addr: tuple, initial_data: bytes):
        await self.proxy._ensure_server_started(self.server_config)
        loop = asyncio.get_running_loop()

        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: BackendProtocol(self.transport, client_addr),
                remote_addr=(self.server_config.host, self.server_config.port),
            )
            self.client_map[client_addr]["protocol"] = protocol
            self.client_map[client_addr]["transport"] = transport
            protocol.transport.sendto(initial_data)
        except Exception:
            log.error("Failed to create backend UDP connection", exc_info=True)
            self.proxy.metrics_manager.dec_active_connections(self.server_config.name)
            del self.client_map[client_addr]

    def connection_lost(self, exc):
        log.info("UDP listener closed.")
        for client in self.client_map.values():
            if client.get("transport"):
                client["transport"].close()
            self.proxy.metrics_manager.dec_active_connections(self.server_config.name)


class BackendProtocol(asyncio.DatagramProtocol):
    """Protocol to handle communication from the backend Bedrock server."""

    def __init__(self, client_transport: asyncio.DatagramTransport, client_addr: tuple):
        self.client_transport = client_transport
        self.client_addr = client_addr
        self.transport = None
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, _addr: tuple):
        """Forwards data from the backend server back to the client."""
        if self.client_transport and not self.client_transport.is_closing():
            self.client_transport.sendto(data, self.client_addr)

    def error_received(self, exc: Exception):
        log.error("Error in backend UDP connection", exc_info=exc)

    def connection_lost(self, _exc):
        log.info("Backend UDP connection closed", for_client=self.client_addr)
