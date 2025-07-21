# proxy.py
import asyncio
import signal
import time
from typing import Optional
from unittest.mock import MagicMock

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

    def _shutdown_handler(self, sig: Optional[signal.Signals] = None):
        """Schedules the async shutdown task from a synchronous signal handler."""
        if sig:
            log.warning("Shutdown signal received", signal=sig.name)
        # This is a non-async method to be compatible with signal handlers.
        # It creates a task to run the actual async shutdown logic.
        asyncio.create_task(self.shutdown())

    async def shutdown(self):
        """Gracefully cancels all running tasks."""
        log.info("Cancelling active tasks for shutdown...")

        # Cancel all active player TCP sessions
        for task in list(self.active_tcp_sessions.keys()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.active_tcp_sessions.keys(), return_exceptions=True)

        # Cancel all main server tasks (listeners, monitor, etc.)
        all_server_tasks = []
        for task_list_or_task in self.server_tasks.values():
            if isinstance(task_list_or_task, list):
                all_server_tasks.extend(task_list_or_task)
            else:
                all_server_tasks.append(task_list_or_task)

        for task in all_server_tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*all_server_tasks, return_exceptions=True)
        log.info("All tasks cancelled.")

    async def _reload_configuration(self):
        """Gracefully reloads the configuration and restarts services."""
        log.info("Starting config reload. Active connections will be dropped.")
        self._reload_requested = False

        if self.active_tcp_sessions:
            log.info(
                "Cancelling active TCP sessions",
                count=len(self.active_tcp_sessions),
            )
            for task in list(self.active_tcp_sessions.keys()):
                task.cancel()
            await asyncio.gather(
                *self.active_tcp_sessions.keys(), return_exceptions=True
            )
            self.active_tcp_sessions.clear()

        listener_tasks = self.server_tasks.get("listeners", [])
        if listener_tasks:
            log.info("Cancelling old listeners", count=len(listener_tasks))
            for task in listener_tasks:
                task.cancel()
            await asyncio.gather(*listener_tasks, return_exceptions=True)
        self.udp_protocols.clear()
        log.info("Old listeners and connections shut down.")

        self.app_config = load_app_config()
        self.docker_manager.app_config = self.app_config
        self.metrics_manager.app_config = self.app_config
        log.info("Config reloaded", servers=len(self.app_config.game_servers))

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
        Ensures any running servers are gracefully stopped before proceeding.
        """
        log.info("Checking for running servers to perform initial cleanup...")
        for sc in self.app_config.game_servers:
            if await self.docker_manager.is_container_running(sc.container_name):
                log.warning(
                    "Server found running. Waiting for it to be queryable "
                    "before issuing a safe stop.",
                    server=sc.name,
                )
                await asyncio.sleep(self.app_config.initial_server_query_delay)
                await self.docker_manager.wait_for_server_query_ready(
                    sc, timeout=self.app_config.initial_boot_ready_max_wait
                )

                await self.docker_manager.stop_server(
                    sc.container_name, self.app_config.server_stop_timeout
                )
                for _ in range(self.app_config.server_stop_timeout + 5):
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
        try:
            await self._ensure_all_servers_stopped_on_startup()

            pre_warm_tasks = [
                asyncio.create_task(self._ensure_server_started(sc))
                for sc in self.app_config.game_servers
                if sc.pre_warm
            ]
            if pre_warm_tasks:
                log.info("Pre-warming servers...", count=len(pre_warm_tasks))
                await asyncio.gather(*pre_warm_tasks, return_exceptions=True)

            self.server_tasks["listeners"] = [
                asyncio.create_task(self._start_listener(sc))
                for sc in self.app_config.game_servers
            ]
            self.server_tasks["monitor"] = asyncio.create_task(
                self._monitor_server_activity()
            )
            self.server_tasks["metrics"] = asyncio.create_task(
                self.metrics_manager.start()
            )

            all_tasks = (
                self.server_tasks.get("listeners", [])
                + [self.server_tasks.get("monitor")]
                + [self.server_tasks.get("metrics")]
            )
            await asyncio.gather(*filter(None, all_tasks))
        except asyncio.CancelledError:
            log.info("Proxy start task cancelled.")
        finally:
            log.debug("Proxy start method finished.")

    async def _start_listener(self, server_config: GameServerConfig):
        """Starts a TCP or UDP listener for a specific game server."""
        log.info(
            "Starting listener",
            server=server_config.name,
            host=server_config.proxy_host,
            port=server_config.proxy_port,
            game_type=server_config.game_type,
        )
        server = None
        try:
            if server_config.game_type == "java":
                server = await asyncio.start_server(
                    lambda r, w: self._handle_tcp_connection(r, w, server_config),
                    server_config.proxy_host,
                    server_config.proxy_port,
                    backlog=self.app_config.tcp_listen_backlog,
                )
                await server.serve_forever()
            else:  # Bedrock (UDP)
                loop = asyncio.get_running_loop()

                def protocol_factory():
                    return BedrockProtocol(self, server_config)

                transport, protocol = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=(
                        server_config.proxy_host,
                        server_config.proxy_port,
                    ),
                )
                self.udp_protocols[server_config.name] = protocol
                await asyncio.Future()  # Wait forever
        except asyncio.CancelledError:
            log.info("Listener task cancelled.", server=server_config.name)
        except Exception:
            log.critical(
                "Failed to start listener. Port might be in use.",
                server=server_config.name,
                port=server_config.proxy_port,
                exc_info=True,
            )
        finally:
            if server and server.is_serving():
                server.close()
                await server.wait_closed()
            if server_config.name in self.udp_protocols:
                del self.udp_protocols[server_config.name]

    async def _ensure_server_started(self, server_config: GameServerConfig):
        """
        Ensures a server is running, handling startup logic concurrently.
        """
        if self._ready_events[
            server_config.name
        ].is_set() and await self.docker_manager.is_container_running(
            server_config.container_name
        ):
            return

        async with self._startup_locks[server_config.name]:
            if self._ready_events[server_config.name].is_set():
                return

            log.info("Server not running, starting...", server=server_config.name)
            start_time = time.time()
            success = await self.docker_manager.start_server(server_config)
            if not success:
                log.error("Server failed to start.", server=server_config.name)
                return

            duration = time.time() - start_time
            self.metrics_manager.observe_startup_duration(server_config.name, duration)
            self._server_state[server_config.name]["is_running"] = True
            self._ready_events[server_config.name].set()
            log.info(
                "Server startup complete.",
                server=server_config.name,
                duration=f"{duration:.2f}s",
            )

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
        """
        Asynchronously streams data from a reader to a writer.
        """
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
        except asyncio.CancelledError:
            log.debug("Proxy data task cancelled.", server=server_name)
        except ConnectionResetError:
            log.info("Connection reset during data proxy.", server=server_name)
        except Exception:
            log.error("Error during data proxy.", server=server_name, exc_info=True)
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
        """
        Handles the entire lifecycle of a single client TCP connection.
        """
        client_addr = client_writer.get_extra_info("peername")
        log.info("New TCP connection", client=client_addr, server=server_config.name)

        max_sessions = self.app_config.max_concurrent_sessions
        if max_sessions != -1 and len(self.active_tcp_sessions) >= max_sessions:
            log.warning("Max sessions reached. Rejecting.", client=client_addr)
            client_writer.close()
            await client_writer.wait_closed()
            return

        self.metrics_manager.inc_active_connections(server_config.name)
        self._update_activity(server_config.name)

        proxy_task = None
        try:
            await self._ensure_server_started(server_config)

            server_reader, server_writer = await asyncio.open_connection(
                server_config.host, server_config.port
            )
            log.info("Connected to backend server", server=server_config.name)

            to_client_task = asyncio.create_task(
                self._proxy_data(
                    server_reader, client_writer, server_config.name, "s2c"
                )
            )
            to_server_task = asyncio.create_task(
                self._proxy_data(
                    client_reader, server_writer, server_config.name, "c2s"
                )
            )

            proxy_task = asyncio.gather(to_client_task, to_server_task)
            self.active_tcp_sessions[proxy_task] = server_config.name
            await proxy_task
        except (ConnectionRefusedError, asyncio.TimeoutError) as e:
            log.error(
                "Could not connect to backend.",
                error=e,
                server=server_config.name,
                client=client_addr,
            )
        except asyncio.CancelledError:
            log.info("TCP session cancelled.", client=client_addr)
        finally:
            if proxy_task:
                self.active_tcp_sessions.pop(proxy_task, None)
            if not client_writer.is_closing():
                client_writer.close()
                await client_writer.wait_closed()
            self.metrics_manager.dec_active_connections(server_config.name)
            log.info("TCP session closed", client=client_addr)

    async def _monitor_server_activity(self):
        """
        A background task that periodically checks for idle servers and handles
        scheduled configuration reloads.
        """
        log.info("Starting server activity monitor.")
        while True:
            try:
                await asyncio.sleep(self.app_config.player_check_interval)
            except asyncio.CancelledError:
                log.info("Server activity monitor task cancelled.")
                break

            if self._reload_requested:
                await self._reload_configuration()
                continue

            for sc in self.app_config.game_servers:
                is_running = await self.docker_manager.is_container_running(
                    sc.container_name
                )
                self._server_state[sc.name]["is_running"] = is_running

                if not is_running:
                    continue

                udp_proto = self.udp_protocols.get(sc.name, MagicMock(client_map={}))
                udp_sessions = len(udp_proto.client_map)
                tcp_sessions = sum(
                    1 for name in self.active_tcp_sessions.values() if name == sc.name
                )

                if (tcp_sessions + udp_sessions) > 0:
                    self._update_activity(sc.name)
                    continue

                idle_timeout = (
                    sc.idle_timeout
                    if sc.idle_timeout is not None
                    else self.app_config.idle_timeout
                )
                idle_time = time.time() - self._server_state[sc.name]["last_activity"]
                if idle_time > idle_timeout:
                    log.info(
                        "Server idle. Stopping.",
                        server=sc.name,
                        idle_seconds=int(idle_time),
                    )
                    await self.docker_manager.stop_server(
                        sc.container_name, self.app_config.server_stop_timeout
                    )
                    self._server_state[sc.name]["is_running"] = False
                    self._ready_events[sc.name].clear()
                    log.info("Server stopped.", server=sc.name)


class BedrockProtocol(asyncio.DatagramProtocol):
    """
    Protocol for the main public-facing UDP listener for a Bedrock server.
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
                await asyncio.sleep(self.proxy.app_config.player_check_interval)
            except asyncio.CancelledError:
                break
            idle_timeout = (
                self.server_config.idle_timeout
                if self.server_config.idle_timeout is not None
                else self.proxy.app_config.idle_timeout
            )
            now = time.time()
            for addr, info in list(self.client_map.items()):
                if now - info.get("last_activity", 0) > idle_timeout:
                    self._cleanup_client(addr)

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        """Handles incoming datagrams from clients."""
        self.proxy._update_activity(self.server_config.name)
        self.proxy.metrics_manager.inc_bytes_transferred(
            self.server_config.name, "c2s", len(data)
        )

        client_info = self.client_map.get(addr)

        if not client_info:
            max_sessions = self.proxy.app_config.max_concurrent_sessions
            if max_sessions != -1 and len(self.client_map) >= max_sessions:
                log.warning("Max UDP sessions reached. Dropping packet.", client=addr)
                return

            log.info("New UDP client", client=addr, server=self.server_config.name)
            self.proxy.metrics_manager.inc_active_connections(self.server_config.name)
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._create_backend_connection(addr, data))
            self.client_map[addr] = {
                "task": task,
                "last_activity": time.time(),
                "protocol": None,
                "queue": [],
            }
        else:
            client_info["last_activity"] = time.time()
            backend_protocol = client_info.get("protocol")
            if backend_protocol and backend_protocol.transport:
                backend_protocol.transport.sendto(data)
            else:
                client_info["queue"].append(data)

    async def _create_backend_connection(self, client_addr: tuple, initial_data: bytes):
        """
        Establishes a backend UDP connection and processes queued packets.
        """
        try:
            await self.proxy._ensure_server_started(self.server_config)
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: BackendProtocol(
                    self.proxy,
                    self.server_config,
                    self.transport,
                    client_addr,
                ),
                remote_addr=(
                    self.server_config.host,
                    self.server_config.port,
                ),
            )

            if client_addr in self.client_map:
                client_info = self.client_map[client_addr]
                client_info["protocol"] = protocol

                if transport and not transport.is_closing():
                    transport.sendto(initial_data)
                    for queued_data in client_info["queue"]:
                        transport.sendto(queued_data)
                    client_info["queue"].clear()
        except Exception:
            log.error(
                "Error creating backend UDP connection.",
                client=client_addr,
                exc_info=True,
            )
            self._cleanup_client(client_addr)

    def connection_lost(self, exc):
        if exc:
            log.error("UDP listener connection lost.", exc=exc)
        self.cleanup_task.cancel()
        for addr in list(self.client_map.keys()):
            self._cleanup_client(addr)


class BackendProtocol(asyncio.DatagramProtocol):
    """
    Protocol to handle communication from the backend Bedrock server.
    """

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

    def connection_lost(self, exc):
        if exc:
            log.error("Backend UDP connection lost.", exc=exc)
