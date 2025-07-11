import asyncio
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import structlog

from config import ProxySettings, ServerConfig, load_application_config
from docker_manager import DockerManager

logger = structlog.get_logger(__name__)
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")


class UdpProxyProtocol(asyncio.DatagramProtocol):
    def __init__(
        self, proxy_instance: "NetherBridgeProxy", server_config: ServerConfig
    ):
        self.proxy = proxy_instance
        self.server_config = server_config
        self.transport = None
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        asyncio.create_task(
            self.proxy._handle_udp_datagram(data, addr, self.server_config)
        )

    def error_received(self, exc: Exception):
        logger.error("UDP protocol error", error=exc, server=self.server_config.name)


class NetherBridgeProxy:
    def __init__(
        self,
        settings: ProxySettings,
        servers: List[ServerConfig],
        docker_manager: DockerManager,
        shutdown_event: asyncio.Event,
        reload_event: asyncio.Event,
    ):
        self.settings = settings
        self.servers = servers
        self.docker_manager = docker_manager
        self._shutdown_event = shutdown_event
        self._reload_event = reload_event
        self.server_states: Dict[str, Dict] = defaultdict(
            lambda: {"status": "stopped", "last_activity": 0.0, "sessions": 0}
        )
        self.udp_clients: Dict[Tuple[str, int], bool] = {}
        self.last_heartbeat_time = 0.0
        self.last_idle_check_time = 0.0
        self.listeners = []

    async def _proxy_data(self, reader, writer, server_name: str):
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            logger.debug("TCP connection closed.")
        finally:
            writer.close()

    async def _handle_tcp_connection(self, reader, writer, server_config: ServerConfig):
        state = self.server_states[server_config.container_name]
        state["sessions"] += 1
        client_addr = writer.get_extra_info("peername")
        logger.info("New TCP client", client=client_addr, server=server_config.name)
        try:
            if state["status"] != "running":
                if not await self.docker_manager.start_server(
                    server_config, self.settings
                ):
                    return
                state["status"] = "running"
            s_reader, s_writer = await asyncio.open_connection(
                "127.0.0.1", server_config.internal_port
            )
            await asyncio.gather(
                self._proxy_data(reader, s_writer, server_config.name),
                self._proxy_data(s_reader, writer, server_config.name),
            )
        finally:
            writer.close()
            state["sessions"] -= 1
            state["last_activity"] = time.time()
            logger.info(
                "TCP client disconnected", client=client_addr, server=server_config.name
            )

    async def _handle_udp_datagram(
        self, data: bytes, addr: Tuple[str, int], server_config: ServerConfig
    ):
        state = self.server_states[server_config.container_name]
        if addr not in self.udp_clients:
            state["sessions"] += 1
            self.udp_clients[addr] = True
        state["last_activity"] = time.time()
        if state["status"] != "running":
            if not await self.docker_manager.start_server(server_config, self.settings):
                return
            state["status"] = "running"
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(),
            remote_addr=("127.0.0.1", server_config.internal_port),
        )
        transport.sendto(data)
        transport.close()

    async def _start_listeners(self):
        loop = asyncio.get_running_loop()
        for server_config in self.servers:
            try:
                if server_config.server_type == "java":
                    server = await asyncio.start_server(
                        lambda r, w, sc=server_config: self._handle_tcp_connection(
                            r, w, sc
                        ),
                        "0.0.0.0",
                        server_config.listen_port,
                    )
                    self.listeners.append(server)
                elif server_config.server_type == "bedrock":
                    await loop.create_datagram_endpoint(
                        lambda sc=server_config: UdpProxyProtocol(self, sc),
                        local_addr=("0.0.0.0", server_config.listen_port),
                    )
            except OSError as e:
                logger.error(
                    "Failed to bind to port", port=server_config.listen_port, error=e
                )

    async def _close_listeners(self):
        for server in self.listeners:
            server.close()
        await asyncio.gather(
            *[s.wait_closed() for s in self.listeners], return_exceptions=True
        )
        self.listeners.clear()

    async def _reload_configuration(self):
        logger.info("Reloading configuration...")
        await self._close_listeners()
        try:
            self.settings, self.servers = load_application_config()
            logger.info("Configuration files reloaded.")
        except Exception as e:
            logger.error("Failed to reload config files.", error=e)
            await self._start_listeners()  # Restore old listeners
            return
        await self._start_listeners()
        logger.info("New listeners started. Reload complete.")

    async def _monitor_activity(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._reload_event.wait(), timeout=5)
                if self._reload_event.is_set():
                    await self._reload_configuration()
                    self._reload_event.clear()
                    continue
            except asyncio.TimeoutError:
                pass

            current_time = time.time()
            if current_time - self.last_heartbeat_time > 10:
                HEARTBEAT_FILE.write_text(str(int(current_time)))
                self.last_heartbeat_time = current_time

            if (
                current_time - self.last_idle_check_time
                > self.settings.player_check_interval_seconds
            ):
                self.last_idle_check_time = current_time
                for server in self.servers:
                    state = self.server_states[server.container_name]
                    if state["status"] == "running" and state["sessions"] == 0:
                        if (
                            current_time - state["last_activity"]
                            > self.settings.idle_timeout_seconds
                        ):
                            if await self.docker_manager.stop_server(
                                server.container_name
                            ):
                                state["status"] = "stopped"

    async def run(self):
        await self._start_listeners()
        monitor_task = asyncio.create_task(self._monitor_activity())
        await self._shutdown_event.wait()
        monitor_task.cancel()
        await self._close_listeners()
        await asyncio.gather(monitor_task, return_exceptions=True)
