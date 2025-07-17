# proxy.py
"""
Core asynchronous proxy logic for TCP (Java) and UDP (Bedrock) servers.
"""

import asyncio
import signal
import time

import structlog

from config import AppConfig, GameServerConfig, load_app_config
from docker_manager import DockerManager
from metrics import MetricsManager

log = structlog.get_logger()


class AsyncProxy:
    """
    An asynchronous proxy server that handles TCP and UDP traffic for
    Minecraft servers, automatically starting and stopping them on demand.
    """

    def __init__(self, app_config: AppConfig, docker_manager: DockerManager):
        self.app_config = app_config
        self.docker_manager = docker_manager
        self.metrics_manager = MetricsManager(app_config, docker_manager)
        self.server_tasks = {}
        # This now maps a session task to its server name for internal tracking
        self.active_tcp_sessions = {}
        self.udp_protocols = {}
        self._reload_requested = False

        self._server_state = {
            s.name: {"last_activity": 0.0, "is_running": False}
            for s in app_config.game_servers
        }
        self._startup_locks = {s.name: asyncio.Lock() for s in app_config.game_servers}
        self._ready_events = {s.name: asyncio.Event() for s in app_config.game_servers}

    def schedule_reload(self):
        """Sets a flag to trigger a reload on the next monitor cycle."""
        self._reload_requested = True
        log.warning("Configuration reload has been scheduled.")

    def _shutdown_handler(self, sig=None):
        """Initiates a graceful shutdown of all tasks."""
        if sig:
            log.warning(
                "Shutdown signal received. Cancelling tasks...", signal=sig.name
            )
        else:
            log.warning("Shutdown requested. Cancelling tasks...")

        # Cancel session tasks first
        for task in self.active_tcp_sessions.keys():
            task.cancel()

        # Cancel main service tasks
        for task in self.server_tasks.values():
            if isinstance(task, list):
                for sub_task in task:
                    sub_task.cancel()
            else:
                task.cancel()

    async def _reload_configuration(self):
        """Gracefully reloads the configuration and restarts services."""
        log.info("Starting configuration reload. Active connections will be dropped.")
        self._reload_requested = False

        for task in self.active_tcp_sessions.keys():
            task.cancel()
        if self.active_tcp_sessions:
            await asyncio.gather(
                *self.active_tcp_sessions.keys(), return_exceptions=True
            )
        self.active_tcp_sessions.clear()

        listener_tasks = self.server_tasks.get("listeners", [])
        for task in listener_tasks:
            task.cancel()
        if listener_tasks:
            await asyncio.gather(*listener_tasks, return_exceptions=True)
        self.udp_protocols.clear()
        log.info("Old listeners and connections shut down.")

        self.app_config = load_app_config()
        self.docker_manager.app_config = self.app_config
        self.metrics_manager.app_config = self.app_config
        log.info("Config reloaded.", servers=len(self.app_config.game_servers))

        self._server_state = {
            s.name: {"last_activity": 0.0, "is_running": False}
            for s in self.app_config.game_servers
        }
        self._startup_locks = {
            s.name: asyncio.Lock() for s in self.app_config.game_servers
        }
        self._ready_events = {
            s.name: asyncio.Event() for s in self.app_config.game_servers
        }
        await self._ensure_all_servers_stopped_on_startup()

        self.server_tasks["listeners"] = [
            asyncio.create_task(self._start_listener(sc))
            for sc in self.app_config.game_servers
        ]
        log.info("New listeners started. Reload complete.")

    async def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures any running servers are gracefully stopped and confirmed exited
        before proceeding to prevent race conditions.
        """
        log.info("Checking for running servers to perform initial cleanup...")
        for sc in self.app_config.game_servers:
            if await self.docker_manager.is_container_running(sc.container_name):
                log.warning(
                    "Server found running at startup. Waiting up to 30s for it to be "
                    "queryable before issuing a safe stop.",
                    server=sc.name,
                )
                await self.docker_manager.wait_for_server_query_ready(sc, timeout=30)

                await self.docker_manager.stop_server(
                    sc.container_name, self.app_config.server_stop_timeout
                )

                for _ in range(self.app_config.server_stop_timeout):
                    if not await self.docker_manager.is_container_running(
                        sc.container_name
                    ):
                        log.info("Server confirmed stopped.", server=sc.name)
                        self._ready_events[sc.name].clear()
                        break
                    await asyncio.sleep(1)
                else:
                    log.error("Timeout waiting for server to stop!", server=sc.name)

    async def start(self):
        """Starts all proxy services and manages their lifecycle."""
        log.info("Starting async proxy...")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown_handler, sig)

        await self._ensure_all_servers_stopped_on_startup()

        for server_config in self.app_config.game_servers:
            if server_config.pre_warm:
                log.info("Pre-warming server.", server=server_config.name)
                asyncio.create_task(self._ensure_server_started(server_config))

        self.server_tasks["listeners"] = [
            asyncio.create_task(self._start_listener(sc))
            for sc in self.app_config.game_servers
        ]
        self.server_tasks["monitor"] = asyncio.create_task(
            self._monitor_server_activity()
        )
        self.server_tasks["metrics"] = asyncio.create_task(self.metrics_manager.start())

        all_tasks = [
            *self.server_tasks.get("listeners", []),
            self.server_tasks.get("monitor"),
            self.server_tasks.get("metrics"),
        ]
        await asyncio.gather(*filter(None, all_tasks), return_exceptions=True)

    async def _start_listener(self, server_config: GameServerConfig):
        """Starts a TCP or UDP listener for a specific game server."""
        log.info(
            "Starting listener",
            server=server_config.name,
            host=server_config.proxy_host,
            port=server_config.proxy_port,
        )
        server = None
        try:
            if server_config.game_type == "java":
                server = await asyncio.start_server(
                    lambda r, w: self._handle_tcp_connection(r, w, server_config),
                    server_config.proxy_host,
                    server_config.proxy_port,
                )
                await server.serve_forever()
            else:
                loop = asyncio.get_running_loop()
                protocol = BedrockProtocol(self, server_config)
                self.udp_protocols[server_config.name] = protocol
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: protocol,
                    local_addr=(server_config.proxy_host, server_config.proxy_port),
                )
                # Keep the listener alive until it's cancelled
                await asyncio.Future()
        except asyncio.CancelledError:
            log.info("Listener task cancelled.", server=server_config.name)
        finally:
            if server and server.is_serving():
                server.close()
                await server.wait_closed()
            # UDP transport is closed by the protocol's connection_lost
            if server_config.name in self.udp_protocols:
                del self.udp_protocols[server_config.name]

    async def _ensure_server_started(self, server_config: GameServerConfig):
        """Ensures a server is running, handling startup logic concurrently."""
        if self._ready_events[server_config.name].is_set():
            if await self.docker_manager.is_container_running(
                server_config.container_name
            ):
                return
            else:
                log.warning(
                    "Ready event was set, but container is stopped. Resetting.",
                    server=server_config.name,
                )
                self._ready_events[server_config.name].clear()

        async with self._startup_locks[server_config.name]:
            if not self._ready_events[server_config.name].is_set():
                log.info("Server not running. Initiating startup...")
                start_time = time.time()
                success = await self.docker_manager.start_server(server_config)
                if not success:
                    log.error("Server failed to start, aborting connection.")
                    return

                duration = time.time() - start_time
                self.metrics_manager.observe_startup_duration(
                    server_config.name, duration
                )
                self._server_state[server_config.name]["is_running"] = True
                self._ready_events[server_config.name].set()
                log.info("Server startup complete.", server=server_config.name)

        await self._ready_events[server_config.name].wait()

    def _update_activity(self, server_name: str):
        """Updates the last activity timestamp for a server."""
        self._server_state[server_name]["last_activity"] = time.time()

    async def _proxy_data(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server_name: str,
        direction: str,
    ):
        """Proxies data between a client and a server until EOF."""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                self.metrics_manager.inc_bytes_transferred(
                    server_name, direction, len(data)
                )
                writer.write(data)
                await writer.drain()
        finally:
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
        self.metrics_manager.inc_active_connections(server_config.name)
        self._update_activity(server_config.name)

        task = None
        try:
            await self._ensure_server_started(server_config)

            server_reader, server_writer = await asyncio.open_connection(
                server_config.host, server_config.port
            )
            log.info("Connected to backend server", server=server_config.name)
            to_client = self._proxy_data(
                server_reader, client_writer, server_config.name, "s2c"
            )
            to_server = self._proxy_data(
                client_reader, server_writer, server_config.name, "c2s"
            )

            task = asyncio.gather(to_client, to_server)
            self.active_tcp_sessions[task] = server_config.name
            await task
        except (ConnectionRefusedError, asyncio.TimeoutError) as e:
            log.error("Could not connect to backend.", error=e, exc_info=True)
        finally:
            if task:
                self.active_tcp_sessions.pop(task, None)
            log.info("Closing TCP connection", client=client_addr)
            client_writer.close()
            await client_writer.wait_closed()
            self.metrics_manager.dec_active_connections(server_config.name)

    async def _monitor_server_activity(self):
        """Periodically checks for idle servers and reload requests."""
        log.info("Starting server activity monitor.")
        while True:
            try:
                await asyncio.sleep(self.app_config.player_check_interval)
            except asyncio.CancelledError:
                break

            if self._reload_requested:
                await self._reload_configuration()
                continue

            for sc in self.app_config.game_servers:
                is_running = await self.docker_manager.is_container_running(
                    sc.container_name
                )
                if not is_running:
                    self._server_state[sc.name]["is_running"] = False
                    continue

                self._server_state[sc.name]["is_running"] = True

                # Check for active sessions for this server
                udp_sessions = len(self.udp_protocols.get(sc.name, {}).client_map)
                tcp_sessions = sum(
                    1 for name in self.active_tcp_sessions.values() if name == sc.name
                )

                if (tcp_sessions + udp_sessions) > 0:
                    self._update_activity(sc.name)
                    continue

                idle_time = time.time() - self._server_state[sc.name]["last_activity"]
                if idle_time > self.app_config.idle_timeout:
                    log.info("Server idle with 0 players. Stopping.", server=sc.name)
                    await self.docker_manager.stop_server(
                        sc.container_name,
                        self.app_config.server_stop_timeout,
                    )
                    self._server_state[sc.name]["is_running"] = False
                    self._ready_events[sc.name].clear()
                    log.info("Server stopped.", server=sc.name)


class BedrockProtocol(asyncio.DatagramProtocol):
    """
    Handles UDP traffic for Bedrock servers. It creates a "UDP session" for
    each new client address, routing traffic to a dedicated backend socket.
    """

    def __init__(self, proxy: AsyncProxy, server_config: GameServerConfig):
        self.proxy = proxy
        self.server_config = server_config
        self.transport = None
        self.client_map = {}
        self.cleanup_task = asyncio.create_task(self._monitor_idle_clients())
        super().__init__()

    def _cleanup_client(self, addr: tuple):
        """Gracefully cleans up a single client session."""
        client_info = self.client_map.pop(addr, None)
        if client_info:
            log.info("Cleaning up idle UDP client", client=addr)
            if client_info.get("task"):
                client_info.get("task").cancel()
            protocol = client_info.get("protocol")
            if protocol and protocol.transport:
                protocol.transport.close()
            self.proxy.metrics_manager.dec_active_connections(self.server_config.name)

    async def _monitor_idle_clients(self):
        """Periodically scans for and removes idle UDP clients."""
        while True:
            try:
                await asyncio.sleep(30)
                idle_timeout = self.proxy.app_config.idle_timeout
                now = time.time()
                for addr, info in list(self.client_map.items()):
                    if now - info.get("last_activity", 0) > idle_timeout:
                        self._cleanup_client(addr)
            except asyncio.CancelledError:
                break

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        self.proxy._update_activity(self.server_config.name)
        self.proxy.metrics_manager.inc_bytes_transferred(
            self.server_config.name, "c2s", len(data)
        )
        if addr not in self.client_map:
            log.info("New UDP client", client=addr, server=self.server_config.name)
            self.proxy.metrics_manager.inc_active_connections(self.server_config.name)
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._create_backend_connection(addr, data))
            self.client_map[addr] = {"task": task, "last_activity": time.time()}
        else:
            self.client_map[addr]["last_activity"] = time.time()
            backend_protocol = self.client_map[addr].get("protocol")
            if backend_protocol and backend_protocol.transport:
                backend_protocol.transport.sendto(data)

    async def _create_backend_connection(self, client_addr: tuple, initial_data: bytes):
        await self.proxy._ensure_server_started(self.server_config)
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: BackendProtocol(
                    self.proxy, self.server_config, self.transport, client_addr
                ),
                remote_addr=(self.server_config.host, self.server_config.port),
            )
            if client_addr in self.client_map:
                self.client_map[client_addr]["protocol"] = protocol
            if transport and not transport.is_closing():
                transport.sendto(initial_data)
        except Exception as e:
            log.error("Failed to create backend UDP connection", error=e)
            self._cleanup_client(client_addr)

    def connection_lost(self, exc):
        if self.transport:
            self.transport.close()
        log.info("UDP listener closed.", server=self.server_config.name)
        self.cleanup_task.cancel()
        for addr in list(self.client_map.keys()):
            self._cleanup_client(addr)


class BackendProtocol(asyncio.DatagramProtocol):
    """Protocol to handle communication from the backend Bedrock server."""

    def __init__(
        self,
        proxy: AsyncProxy,
        server_config: GameServerConfig,
        client_transport: asyncio.DatagramTransport,
        client_addr: tuple,
    ):
        self.proxy = proxy
        self.server_config = server_config
        self.client_transport = client_transport
        self.client_addr = client_addr
        self.transport = None
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, _addr: tuple):
        self.proxy.metrics_manager.inc_bytes_transferred(
            self.server_config.name, "s2c", len(data)
        )
        if self.client_transport and not self.client_transport.is_closing():
            self.client_transport.sendto(data, self.client_addr)

    def connection_lost(self, _exc):
        if self.transport:
            self.transport.close()
        log.info("Backend UDP connection closed", for_client=self.client_addr)
