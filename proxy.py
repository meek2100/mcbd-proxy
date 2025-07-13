import asyncio
import time
from pathlib import Path

import structlog
from prometheus_client import Gauge

from config import AppSettings, Server
from docker_manager import DockerManager


class NetherBridgeProxy:
    """
    The main proxy class that orchestrates listening for connections, starting
    Minecraft servers, and proxying data between clients and servers.
    """

    def __init__(
        self,
        settings: AppSettings,
        servers_list: list[Server],
        docker_manager: DockerManager,
        shutdown_event: asyncio.Event,
        reload_event: asyncio.Event,
        active_sessions_metric: Gauge,
        running_servers_metric: Gauge,
        bytes_transferred_metric: Gauge,
        server_startup_duration_metric: Gauge,
        config_path: Path,
    ):
        self.settings = settings
        self.servers = {server.listen_port: server for server in servers_list}
        self.docker_manager = docker_manager
        self.shutdown_event = shutdown_event
        self.reload_event = reload_event
        self.config_path = config_path

        # Prometheus Metrics
        self.active_sessions_metric = active_sessions_metric
        self.running_servers_metric = running_servers_metric
        self.bytes_transferred_metric = bytes_transferred_metric
        self.server_startup_duration_metric = server_startup_duration_metric

        self._listeners = []
        self._sessions = set()
        self.logger = structlog.get_logger(__name__)

    async def ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts.
        This is an async version of the original pre-warm logic.
        """
        self.logger.info(
            "Proxy startup: Ensuring all managed servers are initially stopped."
        )
        for port, server_config in self.servers.items():
            container_name = server_config.docker_container_name
            try:
                is_running = await self.docker_manager.is_container_running(
                    container_name
                )
                if is_running:
                    self.logger.warning(
                        "Server found running at startup. Issuing stop command.",
                        container_name=container_name,
                    )
                    await self.docker_manager.stop_server(container_name)
                    self.logger.info(
                        "Server confirmed to be stopped.",
                        container_name=container_name,
                    )
                else:
                    self.logger.info(
                        "Server is confirmed to be stopped.",
                        container_name=container_name,
                    )
            except Exception as e:
                self.logger.error(
                    "Error during startup check for server.",
                    container_name=container_name,
                    error=str(e),
                )

    async def run(self):
        """Starts the proxy, listens for connections, and handles events."""
        for port, server_config in self.servers.items():
            try:
                listener = await asyncio.start_server(
                    lambda r, w: self._accept_client(r, w, server_config),
                    host=server_config.listen_host,
                    port=port,
                )
                self._listeners.append(listener)
                self.logger.info(
                    "Proxy listening for connections",
                    server_name=server_config.server_name,
                    listen_host=server_config.listen_host,
                    listen_port=port,
                )
            except OSError as e:
                self.logger.error(
                    "Failed to start listener",
                    server_name=server_config.server_name,
                    error=str(e),
                )
        if not self._listeners:
            self.logger.critical("No listeners started. Shutting down.")
            self.shutdown_event.set()
            return

        heartbeat_task = asyncio.create_task(self._heartbeat())

        while not self.shutdown_event.is_set():
            await asyncio.sleep(0.5)

        self.logger.info("Shutdown event received. Closing proxy.")
        heartbeat_task.cancel()
        await self._shutdown_all_sessions()
        await self._close_listeners()

    async def _accept_client(self, reader, writer, server_config: Server):
        """Accepts a client and starts a new session to handle it."""
        session_task = asyncio.create_task(
            self._handle_client(reader, writer, server_config)
        )
        self._sessions.add(session_task)
        session_task.add_done_callback(self._sessions.discard)

    async def _handle_client(self, client_reader, client_writer, server_config: Server):
        """
        Handles a single client connection from start to finish.
        """
        client_addr = client_writer.get_extra_info("peername")
        self.logger.info(
            "Accepted connection",
            client_ip=client_addr[0],
            server_name=server_config.server_name,
        )
        self.active_sessions_metric.inc()

        server_reader, server_writer = None, None
        try:
            start_time = time.monotonic()
            server_ip = await self.docker_manager.ensure_server_running(
                server_config.docker_container_name
            )
            duration = time.monotonic() - start_time
            self.server_startup_duration_metric.set(duration)

            if not server_ip:
                self.logger.error(
                    "Failed to get server IP.",
                    container=server_config.docker_container_name,
                )
                return

            self.logger.info(
                "Server is running",
                container=server_config.docker_container_name,
                ip=server_ip,
                startup_time=f"{duration:.2f}s",
            )

            server_reader, server_writer = await asyncio.open_connection(
                server_ip, server_config.target_port
            )
            self.logger.info(
                "Connected to target server",
                server=server_config.server_name,
            )

            await self._proxy_data(
                client_reader, client_writer, server_reader, server_writer
            )
        except Exception as e:
            self.logger.error("Error in client handler", error=str(e), exc_info=True)
        finally:
            self.logger.info("Closing connection", client_ip=client_addr[0])
            if server_writer:
                server_writer.close()
                await server_writer.wait_closed()
            if client_writer:
                client_writer.close()
                await client_writer.wait_closed()
            self.active_sessions_metric.dec()

    async def _proxy_data(self, client_r, client_w, server_r, server_w):
        """Proxies data in both directions between client and server."""

        async def _pipe(reader, writer):
            try:
                while not reader.at_eof():
                    data = await reader.read(4096)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
                    self.bytes_transferred_metric.inc(len(data))
            finally:
                writer.close()

        await asyncio.gather(_pipe(client_r, server_w), _pipe(server_r, client_w))

    async def _heartbeat(self):
        """Periodically writes a timestamp for health checks."""
        heartbeat_file = self.config_path / "heartbeat.txt"
        while not self.shutdown_event.is_set():
            try:
                heartbeat_file.write_text(str(int(time.time())))
            except Exception as e:
                self.logger.warning("Could not write heartbeat file", error=str(e))
            await asyncio.sleep(10)

    async def _shutdown_all_sessions(self):
        """Gracefully shuts down all active proxy sessions."""
        if self._sessions:
            self.logger.info(f"Closing {len(self._sessions)} active session(s)...")
            for session in list(self._sessions):
                session.cancel()
            await asyncio.gather(*self._sessions, return_exceptions=True)

    async def _close_listeners(self):
        """Closes all server listeners."""
        self.logger.info("Closing all network listeners...")
        for listener in self._listeners:
            listener.close()
            await listener.wait_closed()
