# proxy.py
"""
Core asynchronous proxy logic for TCP (Java) and UDP (Bedrock) servers.
"""

import asyncio
import signal
import time
from unittest.mock import (
    MagicMock,  # Added for use in proxy.py for robust client_map access
)

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

    def _shutdown_handler(self, sig=None):
        """Initiates a graceful shutdown of all tasks."""
        if sig:
            log.warning(
                "Shutdown signal received. Cancelling tasks...", signal=sig.name
            )
        else:
            log.warning("Shutdown requested. Cancelling tasks...")

        # Cancel all active TCP session tasks
        for task in list(self.active_tcp_sessions.keys()):
            task.cancel()

        # Cancel all background server tasks (listeners, monitor, metrics)
        for task_list in self.server_tasks.values():
            if isinstance(task_list, list):  # Listeners are a list of tasks
                for task in task_list:
                    task.cancel()
            else:  # Monitor and metrics are single tasks
                task_list.cancel()

    async def _reload_configuration(self):
        """Gracefully reloads the configuration and restarts services."""
        log.info("Starting configuration reload. Active connections will be dropped.")
        self._reload_requested = False

        # Cancel all existing TCP sessions
        # Use gather to await their completion gracefully
        if self.active_tcp_sessions:
            log.info(
                "Cancelling active TCP sessions for reload.",
                count=len(self.active_tcp_sessions),
            )
            await asyncio.gather(
                *list(self.active_tcp_sessions.keys()), return_exceptions=True
            )
        self.active_tcp_sessions.clear()

        # Cancel all existing listener tasks
        listener_tasks = self.server_tasks.get("listeners", [])
        if listener_tasks:
            log.info(
                "Cancelling old listener tasks for reload.", count=len(listener_tasks)
            )
            for task in listener_tasks:
                task.cancel()
            await asyncio.gather(*listener_tasks, return_exceptions=True)
        self.udp_protocols.clear()  # Clear UDP protocols
        log.info("Old listeners and connections shut down.")

        # Reload configuration
        self.app_config = load_app_config()
        # Update app_config reference in dependent managers
        self.docker_manager.app_config = self.app_config
        self.metrics_manager.app_config = self.app_config
        log.info("Config reloaded.", servers=len(self.app_config.game_servers))

        # Re-initialize server state based on new config
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

        # Ensure all servers are stopped based on new config
        await self._ensure_all_servers_stopped_on_startup()

        # Start new listeners based on reloaded config
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
                    "Server found running. Waiting for it to be queryable before "
                    "issuing a safe stop.",
                    server=sc.name,
                )
                await asyncio.sleep(self.app_config.initial_server_query_delay)
                # Max wait time for initial boot is longer to account for
                # a possibly crashed/slowly starting server
                await self.docker_manager.wait_for_server_query_ready(
                    sc, timeout=self.app_config.initial_boot_ready_max_wait
                )

                await self.docker_manager.stop_server(
                    sc.container_name, self.app_config.server_stop_timeout
                )
                # Wait for container to actually be stopped/exited
                for _ in range(self.app_config.server_stop_timeout + 5):  # Added buffer
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
        # Register signal handlers for graceful shutdown and reload
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown_handler, sig)
        if hasattr(signal, "SIGHUP"):
            loop.add_signal_handler(signal.SIGHUP, self.schedule_reload)

        await self._ensure_all_servers_stopped_on_startup()

        # Start pre-warmed servers concurrently
        pre_warm_tasks = []
        for server_config in self.app_config.game_servers:
            if server_config.pre_warm:
                log.info("Pre-warming server.", server=server_config.name)
                pre_warm_tasks.append(
                    asyncio.create_task(self._ensure_server_started(server_config))
                )
        if pre_warm_tasks:
            await asyncio.gather(*pre_warm_tasks, return_exceptions=True)

        # Create and store main background tasks
        self.server_tasks["listeners"] = [
            asyncio.create_task(self._start_listener(sc))
            for sc in self.app_config.game_servers
        ]
        self.server_tasks["monitor"] = asyncio.create_task(
            self._monitor_server_activity()
        )
        self.server_tasks["metrics"] = asyncio.create_task(self.metrics_manager.start())

        # Gather all persistent tasks to run indefinitely until cancelled
        all_tasks = [
            *self.server_tasks.get("listeners", []),
            self.server_tasks.get("monitor"),
            self.server_tasks.get("metrics"),
        ]
        # Filter out None in case some tasks were not created
        # (e.g. pre_warm_tasks)
        await asyncio.gather(*filter(None, all_tasks), return_exceptions=True)

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

                # Store the protocol instance to access its client_map later
                transport, protocol = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=(server_config.proxy_host, server_config.proxy_port),
                )
                self.udp_protocols[server_config.name] = protocol
                # This Future keeps the UDP listener task alive
                await asyncio.Future()
        except asyncio.CancelledError:
            log.info("Listener task cancelled.", server=server_config.name)
        except Exception as e:
            log.critical(
                "Failed to start listener. Port might be in use or "
                "permissions missing.",
                server=server_config.name,
                port=server_config.proxy_port,
                error=str(e),
                exc_info=True,
            )
            # Re-raise to ensure main loop handles fatal listener errors
            raise
        finally:
            if server and server.is_serving():
                server.close()
                await server.wait_closed()
            if server_config.name in self.udp_protocols:
                # Remove protocol if listener is closed
                del self.udp_protocols[server_config.name]

    async def _ensure_server_started(self, server_config: GameServerConfig):
        """Ensures a server is running, handling startup logic concurrently."""
        # Check if already marked ready AND container is running (robustness)
        if self._ready_events[
            server_config.name
        ].is_set() and await self.docker_manager.is_container_running(
            server_config.container_name
        ):
            log.debug("Server already running and ready.", server=server_config.name)
            return

        async with self._startup_locks[server_config.name]:
            # Double check inside the lock, in case another task started it
            if self._ready_events[server_config.name].is_set():
                log.debug(
                    "Server became ready while waiting for lock.",
                    server=server_config.name,
                )
                return

            log.info(
                "Server not running. Initiating startup...", server=server_config.name
            )
            start_time = time.time()
            success = await self.docker_manager.start_server(server_config)
            if not success:
                log.error(
                    "Server failed to start, aborting connection.",
                    server=server_config.name,
                )
                return

            duration = time.time() - start_time
            self.metrics_manager.observe_startup_duration(server_config.name, duration)
            self._server_state[server_config.name]["is_running"] = True
            self._ready_events[server_config.name].set()
            log.info("Server startup complete.", server=server_config.name)

        # Wait for the server to be truly ready (only if it wasn't pre-warmed)
        # If pre_warm, the pre_warm_tasks in start() will handle the waiting.
        if not server_config.pre_warm:
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
                if not data:  # EOF or connection closed
                    break
                self.metrics_manager.inc_bytes_transferred(
                    server_name, direction, len(data)
                )
                writer.write(data)
                await writer.drain()  # Ensure data is sent
        except asyncio.CancelledError:
            log.debug(
                "Proxy data task cancelled.", server=server_name, direction=direction
            )
        except ConnectionResetError:
            log.info(
                "Connection reset during data proxy.",
                server=server_name,
                direction=direction,
            )
        except Exception as e:
            log.error(
                "Error during data proxy.",
                server=server_name,
                direction=direction,
                error=str(e),
                exc_info=True,
            )
        finally:
            # Ensure writers are closed even on error
            if not writer.is_closing():
                await writer.close()  # FIX: Await writer.close()
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

        max_sessions = self.app_config.max_concurrent_sessions
        # Only check max sessions if limit is set (> -1)
        if max_sessions != -1 and len(self.active_tcp_sessions) >= max_sessions:
            log.warning(
                "Max concurrent TCP sessions reached. Rejecting connection.",
                client=client_addr,
                max_sessions=max_sessions,
            )
            client_writer.close()
            await client_writer.wait_closed()
            return

        self.metrics_manager.inc_active_connections(server_config.name)
        self._update_activity(server_config.name)

        proxy_task = None
        try:
            # Ensure the backend server is running and ready
            await self._ensure_server_started(server_config)

            # Establish connection to the backend Minecraft server
            server_reader, server_writer = await asyncio.open_connection(
                server_config.host, server_config.port
            )
            log.info("Connected to backend server", server=server_config.name)

            # Create tasks for bidirectional data proxying
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
            await proxy_task  # Wait for both proxying tasks to complete
        except (ConnectionRefusedError, asyncio.TimeoutError) as e:
            log.error(
                "Could not connect to backend.",
                error=e,
                exc_info=True,
                server=server_config.name,
                client=client_addr,
            )
        except asyncio.CancelledError:
            log.info("TCP session cancelled.", client=client_addr)
        except Exception as e:
            log.error(
                "Unhandled error in TCP session.",
                error=e,
                exc_info=True,
                client=client_addr,
            )
        finally:
            if proxy_task:
                # Remove from active sessions regardless of how it ended
                self.active_tcp_sessions.pop(proxy_task, None)
            if not client_writer.is_closing():
                await client_writer.close()  # FIX: Await client_writer.close()
            await client_writer.wait_closed()
            self.metrics_manager.dec_active_connections(server_config.name)
            log.info("TCP session closed", client=client_addr)

    async def _monitor_server_activity(self):
        """Periodically checks for idle servers and reload requests."""
        log.info("Starting server activity monitor.")
        while True:
            try:
                await asyncio.sleep(self.app_config.player_check_interval)
            except asyncio.CancelledError:
                log.info("Server activity monitor task cancelled.")
                break  # Exit loop on cancellation

            if self._reload_requested:
                await self._reload_configuration()
                continue  # Restart loop after reload

            for sc in self.app_config.game_servers:
                is_running = await self.docker_manager.is_container_running(
                    sc.container_name
                )
                # Update internal state based on actual container status
                self._server_state[sc.name]["is_running"] = is_running

                if not is_running:
                    continue  # Server is already stopped, no need to check idle

                # Count active sessions for this specific server
                # FIX: Ensure default value has client_map for len() check
                udp_protocols_for_server = self.udp_protocols.get(
                    sc.name, MagicMock(client_map={})
                )
                udp_sessions = len(udp_protocols_for_server.client_map)
                tcp_sessions = sum(
                    1 for name in self.active_tcp_sessions.values() if name == sc.name
                )

                if (tcp_sessions + udp_sessions) > 0:
                    self._update_activity(sc.name)  # Update activity if players present
                    continue

                # Server is running but has no active sessions, check idle timeout
                idle_time = time.time() - self._server_state[sc.name]["last_activity"]
                if idle_time > self.app_config.idle_timeout:
                    log.info(
                        "Server idle with 0 players. Stopping.",
                        server=sc.name,
                        idle_seconds=int(idle_time),
                    )
                    await self.docker_manager.stop_server(
                        sc.container_name, self.app_config.server_stop_timeout
                    )
                    self._server_state[sc.name]["is_running"] = False
                    self._ready_events[sc.name].clear()  # Clear ready state
                    log.info("Server stopped.", server=sc.name)


class BedrockProtocol(asyncio.DatagramProtocol):
    """
    Handles UDP traffic for Bedrock servers. It creates a "UDP session" for
    each new client address, routing traffic to a dedicated backend socket.
    """

    def __init__(self, proxy: AsyncProxy, server_config: GameServerConfig):
        self.proxy = proxy
        self.server_config = server_config
        self.transport = None  # The transport for the *listening* UDP socket
        self.client_map = {}  # Maps client_addr -> {'task': task,
        # 'last_activity': float, 'protocol': BackendProtocol}
        # Start background task to monitor and cleanup idle UDP clients
        self.cleanup_task = asyncio.create_task(self._monitor_idle_clients())
        super().__init__()

    def _cleanup_client(self, addr: tuple):
        """Gracefully cleans up a single client session."""
        client_info = self.client_map.pop(addr, None)
        if client_info:
            log.info(
                "Cleaning up idle UDP client",
                client=addr,
                server=self.server_config.name,
            )
            if client_info.get("task"):
                client_info.get("task").cancel()  # Cancel backend connection task
            protocol = client_info.get("protocol")
            if protocol and protocol.transport:
                protocol.transport.close()  # Close backend transport
            self.proxy.metrics_manager.dec_active_connections(self.server_config.name)

    async def _monitor_idle_clients(self):
        """Periodically scans for and removes idle UDP clients."""
        while True:
            try:
                await asyncio.sleep(self.proxy.app_config.player_check_interval)
            except asyncio.CancelledError:
                log.info(
                    "UDP client monitor task cancelled.", server=self.server_config.name
                )
                break  # Exit loop on cancellation
            idle_timeout = self.proxy.app_config.idle_timeout
            now = time.time()
            # Iterate over a copy of keys to allow modification during iteration
            for addr, info in list(self.client_map.items()):
                if now - info.get("last_activity", 0) > idle_timeout:
                    self._cleanup_client(addr)

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        log.debug("UDP listener connection made.", server=self.server_config.name)

    def datagram_received(self, data: bytes, addr: tuple):
        self.proxy._update_activity(self.server_config.name)
        self.proxy.metrics_manager.inc_bytes_transferred(
            self.server_config.name, "c2s", len(data)
        )

        if addr not in self.client_map:
            max_sessions = self.proxy.app_config.max_concurrent_sessions
            if max_sessions != -1 and len(self.client_map) >= max_sessions:
                log.warning(
                    "Max concurrent UDP sessions reached. Dropping packet.",
                    client=addr,
                    max_sessions=max_sessions,
                    server=self.server_config.name,
                )
                return  # Drop packet if max sessions reached

            log.info("New UDP client", client=addr, server=self.server_config.name)
            self.proxy.metrics_manager.inc_active_connections(self.server_config.name)
            loop = asyncio.get_running_loop()
            # Create a task to handle backend connection and initial data send
            task = loop.create_task(self._create_backend_connection(addr, data))
            self.client_map[addr] = {"task": task, "last_activity": time.time()}
        else:
            # Update last activity and forward data for existing clients
            self.client_map[addr]["last_activity"] = time.time()
            backend_protocol = self.client_map[addr].get("protocol")
            if backend_protocol and backend_protocol.transport:
                backend_protocol.transport.sendto(data)  # Forward to backend

    async def _create_backend_connection(self, client_addr: tuple, initial_data: bytes):
        """
        Establishes a backend UDP connection and sends initial data.
        """
        try:
            # Ensure the backend server is running and ready before connecting
            await self.proxy._ensure_server_started(self.server_config)

            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: BackendProtocol(
                    self.proxy, self.server_config, self.transport, client_addr
                ),
                remote_addr=(self.server_config.host, self.server_config.port),
            )
            # Store the backend protocol for future data forwarding
            if client_addr in self.client_map:  # Check if client still active
                self.client_map[client_addr]["protocol"] = protocol
            # Send the initial buffered data
            if transport and not transport.is_closing():
                transport.sendto(initial_data)
        except (ConnectionRefusedError, asyncio.TimeoutError) as e:
            log.error(
                "Failed to connect to backend UDP server after startup.",
                client=client_addr,
                server=self.server_config.name,
                error=str(e),
                exc_info=True,
            )
            self._cleanup_client(client_addr)  # Clean up client if backend fails
        except asyncio.CancelledError:
            log.debug("Backend connection task cancelled.", client=client_addr)
            self._cleanup_client(client_addr)
        except Exception as e:
            log.error(
                "Unhandled error creating backend UDP connection.",
                client=client_addr,
                server=self.server_config.name,
                error=str(e),
                exc_info=True,
            )
            self._cleanup_client(client_addr)

    def connection_lost(self, exc):
        # This is called when the *listening* UDP socket is closed.
        if exc:
            log.error(
                "UDP listener connection lost due to error.",
                server=self.server_config.name,
                exc=exc,
            )
        else:
            log.info("UDP listener connection lost.", server=self.server_config.name)

        if self.transport and not self.transport.is_closing():
            self.transport.close()  # Close the listening transport
        self.cleanup_task.cancel()  # Cancel the idle client monitor task

        # Clean up all active UDP client sessions for this server
        for addr in list(self.client_map.keys()):  # Iterate over copy
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
        self.client_transport = client_transport  # The main listening proxy transport
        self.client_addr = client_addr  # Original client's address
        self.transport = None  # This protocol's transport (to backend server)
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        log.debug(
            "Backend UDP connection made.",
            for_client=self.client_addr,
            server=self.server_config.name,
        )

    def datagram_received(self, data: bytes, _addr: tuple):
        # Data received from the backend Minecraft server, forward to client
        self.proxy.metrics_manager.inc_bytes_transferred(
            self.server_config.name, "s2c", len(data)
        )
        if self.client_transport and not self.client_transport.is_closing():
            self.client_transport.sendto(data, self.client_addr)
        else:
            log.warning(
                "Client transport missing or closing, dropping backend UDP.",
                client=self.client_addr,
            )

    def connection_lost(self, exc):
        # This is called when the backend UDP socket is closed.
        if exc:
            log.error(
                "Backend UDP connection lost due to error.",
                for_client=self.client_addr,
                server=self.server_config.name,
                exc=exc,
            )
        else:
            log.info(
                "Backend UDP connection closed.",
                for_client=self.client_addr,
                server=self.server_config.name,
            )
        if self.transport and not self.transport.is_closing():
            self.transport.close()
        # No need to cleanup client via proxy._cleanup_client here,
        # as it's the backend side. Client cleanup is handled by idle monitor.
