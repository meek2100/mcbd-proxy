import asyncio
import signal
import time
from threading import Lock, RLock, Thread
from typing import Dict, List

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
)

HEARTBEAT_INTERVAL_SECONDS = 30


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
                "lock": RLock(),
                "status": "stopped",
                "last_activity": 0,
                "pending_connections": [],
            }
            for s in servers
        }

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

        if self._initiate_server_start(server_config):
            # Wait for server to be ready
            while self.server_states[container_name]["status"] == "starting":
                await asyncio.sleep(1)

        if self.server_states[container_name]["status"] != "running":
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

    def _initiate_server_start(self, server_config: ServerConfig):
        """Atomically checks state and starts the server startup thread if needed."""
        container_name = server_config.container_name
        with self.server_locks[container_name]:
            if self.server_states[container_name]["status"] == "stopped":
                self.logger.info(
                    "Server is stopped. Initiating startup sequence.",
                    server_name=server_config.name,
                )
                self.server_states[container_name]["status"] = "starting"
                thread = Thread(
                    target=self._start_minecraft_server_task,
                    args=(server_config,),
                    daemon=True,
                )
                thread.start()
                return True
        return False

    def _start_minecraft_server_task(self, server_config: ServerConfig):
        """
        Runs in a background thread to start a server.
        """
        container_name = server_config.container_name
        startup_timer_start = time.time()

        success = self.docker_manager.start_server(server_config, self.settings)

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if success:
                state["status"] = "running"
                RUNNING_SERVERS.inc()
                duration = time.time() - startup_timer_start
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    duration
                )
                self.logger.info(
                    "Startup process complete.",
                    container_name=container_name,
                    duration_seconds=duration,
                )
            else:
                self.logger.error(
                    "Server startup process failed.", container_name=container_name
                )
                state["status"] = "stopped"

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
                self.docker_manager.stop_server(server_config)

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
                        try:
                            active_sessions = ACTIVE_SESSIONS.get_metric_value().get(
                                (server_config.name,), 0
                            )
                        except AttributeError:
                            # This can happen if the metric hasn't been initialized yet
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
                                self.docker_manager.stop_server(
                                    server_config.container_name
                                )
                                self.server_states[server_config.container_name][
                                    "status"
                                ] = "stopped"
                                RUNNING_SERVERS.dec()

    async def _run_proxy_loop(self):
        """The main async event loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")

        server_tasks = []
        for srv_cfg in self.servers_list:
            if srv_cfg.server_type == "java":
                server = await asyncio.start_server(
                    self._handle_tcp_client, "0.0.0.0", srv_cfg.listen_port
                )
                server_tasks.append(asyncio.create_task(server.serve_forever()))
                self.logger.info(
                    "Proxy listening for TCP server", server_name=srv_cfg.name
                )
            # UDP handling would need a different approach with asyncio.Protocol
            # For now, focusing on the TCP part that was causing issues.

        await self._shutdown_event.wait()

        for task in server_tasks:
            task.cancel()
        await asyncio.gather(*server_tasks, return_exceptions=True)

        self.logger.info("Proxy loop is exiting.")
        return self._reload_requested
