# proxy.py
"""
Core asynchronous proxy logic for TCP (Java) and UDP (Bedrock) servers.
"""

import asyncio
import signal
import time

import structlog
from mcstatus import BedrockServer, JavaServer

from config import AppConfig, GameServerConfig
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
        self.server_tasks = {}
        self.metrics_manager = MetricsManager(app_config, docker_manager)

        self._server_state = {
            s.name: {"last_activity": 0.0, "is_running": False}
            for s in app_config.game_servers
        }
        self._startup_locks = {s.name: asyncio.Lock() for s in app_config.game_servers}
        self._ready_events = {s.name: asyncio.Event() for s in app_config.game_servers}

    async def start(self):
        """
        Starts all proxy listeners, pre-warms servers if configured,
        and starts the server monitoring task.
        """
        log.info("Starting async proxy...")
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._shutdown_handler)
        loop.add_signal_handler(signal.SIGTERM, self._shutdown_handler)

        for server_config in self.app_config.game_servers:
            if server_config.pre_warm:
                log.info("Pre-warming server.", server=server_config.name)
                asyncio.create_task(self._ensure_server_started(server_config))

        listener_tasks = [
            asyncio.create_task(self._start_listener(sc))
            for sc in self.app_config.game_servers
        ]
        monitor_task = asyncio.create_task(self._monitor_server_activity())
        metrics_task = asyncio.create_task(self.metrics_manager.start())

        self.server_tasks = {
            "listeners": listener_tasks,
            "monitor": monitor_task,
            "metrics": metrics_task,
        }
        await asyncio.gather(
            monitor_task, metrics_task, *listener_tasks, return_exceptions=True
        )

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
                await asyncio.Future()
            finally:
                transport.close()

    async def _ensure_server_started(self, server_config: GameServerConfig):
        """
        Ensures a server is running, handling startup logic concurrently.
        """
        if self._ready_events[server_config.name].is_set():
            return

        async with self._startup_locks[server_config.name]:
            state = self._server_state[server_config.name]
            if not state["is_running"]:
                log.info(
                    "Server not running. Initiating startup...",
                    server=server_config.name,
                )
                self._ready_events[server_config.name].clear()
                await self.docker_manager.start_server(server_config)
                state["is_running"] = True
                self._ready_events[server_config.name].set()
                log.info(
                    "Server startup complete. Event is set.",
                    server=server_config.name,
                )

        await self._ready_events[server_config.name].wait()

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
        self._update_activity(server_config.name)

        try:
            await self._ensure_server_started(server_config)

            server_reader, server_writer = await asyncio.open_connection(
                server_config.host, server_config.port
            )
            log.info("Connected to backend server", server=server_config.name)
            to_client = self._proxy_data(server_reader, client_writer)
            to_server = self._proxy_data(client_reader, server_writer)
            await asyncio.gather(to_client, to_server)
        except (ConnectionRefusedError, asyncio.TimeoutError) as e:
            log.error(
                "Could not connect to backend.", server=server_config.name, error=str(e)
            )
        finally:
            log.info("Closing TCP connection", client=client_addr)
            client_writer.close()
            await client_writer.wait_closed()
            self.metrics_manager.dec_active_connections(server_config.name)

    async def _get_player_count(self, server_config: GameServerConfig) -> int:
        """Asynchronously queries a server and returns the player count."""
        try:
            lookup_str = f"{server_config.host}:{server_config.query_port}"
            if server_config.game_type == "java":
                server = await JavaServer.async_lookup(lookup_str, timeout=3)
            else:  # bedrock
                # CORRECTED: Instantiate BedrockServer directly.
                server = BedrockServer.lookup(lookup_str, timeout=3)

            status = await server.async_status()
            return status.players.online
        except Exception:
            log.warning(
                "Could not query player count for server", server=server_config.name
            )
            return 1

    async def _monitor_server_activity(self):
        """Periodically checks for zero-player servers and stops them."""
        log.info("Starting server activity monitor (player count based).")
        while True:
            await asyncio.sleep(self.app_config.player_check_interval)
            for sc in self.app_config.game_servers:
                state = self._server_state[sc.name]
                if state["is_running"]:
                    player_count = await self._get_player_count(sc)
                    log.debug(
                        "Player count check", server=sc.name, players=player_count
                    )

                    if player_count > 0:
                        state["last_activity"] = time.time()
                    else:
                        idle_time = time.time() - state["last_activity"]
                        if idle_time > self.app_config.idle_timeout:
                            log.info(
                                "Server is empty and idle timeout exceeded. Stopping.",
                                server=sc.name,
                            )
                            await self.docker_manager.stop_server(
                                sc.container_name,
                                self.app_config.server_stop_timeout,
                            )
                            state["is_running"] = False
                            self._ready_events[sc.name].clear()
                            log.info("Server stopped.", server=sc.name)


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
        self.proxy._update_activity(self.server_config.name)
        if addr not in self.client_map:
            log.info("New UDP client", client=addr, server=self.server_config.name)
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
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: BackendProtocol(self.transport, client_addr),
            remote_addr=(self.server_config.host, self.server_config.port),
        )
        self.client_map[client_addr]["protocol"] = protocol
        if transport and not transport.is_closing():
            transport.sendto(initial_data)

    def connection_lost(self, exc):
        log.info("UDP listener closed.", server=self.server_config.name)
        for client in self.client_map.values():
            protocol = client.get("protocol")
            if protocol and protocol.transport:
                protocol.transport.close()
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
        if self.client_transport and not self.client_transport.is_closing():
            self.client_transport.sendto(data, self.client_addr)

    def connection_lost(self, _exc):
        log.info("Backend UDP connection closed", for_client=self.client_addr)
