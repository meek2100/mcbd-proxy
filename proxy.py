import asyncio
import time
from pathlib import Path
from typing import List

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager

# Removed direct import of metrics. Metrics will be passed via constructor.


# Using a module-level logger for this file
log = structlog.get_logger(__name__)


class UdpProxyProtocol(asyncio.DatagramProtocol):
    """
    A custom asyncio UDP protocol for handling Minecraft Bedrock Edition
    datagrams, forwarding them between clients and the backend server.
    """

    def __init__(self, proxy_instance, server_config: ServerConfig):
        self.proxy = proxy_instance
        self.server_config = server_config
        self.transport = None
        self.peername = None

    def connection_made(self, transport):
        """Called when a connection is made."""
        self.transport = transport
        self.peername = transport.get_extra_info("sockname")
        log.debug("UDP listener created.", listen_addr=self.peername)

    def datagram_received(self, data, addr):
        """Called when a datagram is received."""
        # Create a task to handle the datagram asynchronously
        asyncio.create_task(
            self.proxy._handle_udp_datagram(data, addr, self.server_config)
        )

    def error_received(self, exc):
        """Called when an error occurs during receiving."""
        log.error("UDP error received.", exc=str(exc))

    def connection_lost(self, exc):
        """Called when the connection is lost or closed."""
        if exc:
            log.warning("UDP connection lost with error.", exc=str(exc))
        else:
            log.debug("UDP connection closed gracefully.")
        # Clean up any associated sessions if necessary, though UDP sessions
        # are managed by inactivity timeout rather than connection loss.


class NetherBridgeProxy:
    """
    Nether-bridge: On-Demand Minecraft Server Proxy.
    Orchestrates listening sockets and sessions, and delegates Docker operations.
    """

    def __init__(
        self,
        settings: ProxySettings,
        servers_list: List[ServerConfig],
        docker_manager: DockerManager,
        shutdown_event: asyncio.Event,
        reload_event: asyncio.Event,
        active_sessions_metric,  # Prometheus metric for active sessions
        running_servers_metric,  # Prometheus metric for running servers
        bytes_transferred_metric,  # Prometheus metric for bytes transferred
        server_startup_duration_metric,  # Prometheus metric for startup duration
    ):
        self.settings = settings
        self.servers_list = servers_list
        self.docker_manager = docker_manager
        self.shutdown_event = shutdown_event
        self.reload_event = reload_event
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}

        # Store Prometheus metric objects
        self.active_sessions_metric = active_sessions_metric
        self.running_servers_metric = running_servers_metric
        self.bytes_transferred_metric = bytes_transferred_metric
        self.server_startup_duration_metric = server_startup_duration_metric

        # Initialize server states and locks (using asyncio.Lock)
        self.server_states = {}
        self.server_locks = {}
        for s in self.servers_list:
            self.server_states[s.container_name] = {
                "status": "stopped",
                "last_activity": 0.0,
                "sessions": 0,
            }
            # Use asyncio.Lock for async concurrency control
            self.server_locks[s.container_name] = asyncio.Lock()

        # Lock to protect access to session dictionaries from multiple coroutines
        self.session_lock = asyncio.Lock()
        self.active_sessions = {}  # {(client_ip, client_port): session_info}
        self.backend_connections = {}
        # {target_server_socket: (client_reader, client_writer)}

        self.tcp_listeners = []
        self.udp_listeners = []

    async def _is_server_ready_for_traffic(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """
        Polls a Minecraft server using mcstatus until it responds or a timeout
        is reached, confirming it's ready for traffic.
        """
        return await self.docker_manager.wait_for_server_query_ready(
            server_config,
            settings.server_ready_max_wait_time_seconds,
            settings.query_timeout_seconds,
        )

    async def _proxy_data(self, reader, writer, session_info):
        """Forwards data between client and server, updating session activity."""
        client_addr = session_info["client_addr"]
        listen_port = session_info["listen_port"]
        server_config = self.servers_config_map.get(listen_port)
        container_name = session_info["target_container"]

        try:
            while not self.shutdown_event.is_set():
                data = await reader.read(4096)
                if not data:
                    break  # Connection closed by peer

                # Ensure all buffered data is written before proceeding
                await writer.drain()

                async with self.session_lock:
                    # Check if session still exists (might be closed by other task)
                    if (
                        client_addr,
                        listen_port,
                        session_info["protocol"],
                    ) not in self.active_sessions:
                        break

                    session_info["last_packet_time"] = time.time()
                    self.server_states[container_name]["last_activity"] = time.time()

                # Increment BYTES_TRANSFERRED metric
                # Determine direction based on writer object reference
                is_client_to_server = writer is session_info.get("server_writer_tcp")
                direction = "c2s" if is_client_to_server else "s2c"
                self.bytes_transferred_metric.labels(
                    server_name=server_config.name, direction=direction
                ).inc(len(data))

                writer.write(data)
                await writer.drain()

        except asyncio.CancelledError:
            log.info(
                "Proxy data forwarding cancelled.",
                client_addr=client_addr,
                container_name=container_name,
            )
        except ConnectionResetError:
            log.info(
                "Connection reset during data forwarding. Client disconnected.",
                client_addr=client_addr,
                container_name=container_name,
            )
        except Exception as e:
            log.error(
                "Error during data forwarding.",
                client_addr=client_addr,
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
        finally:
            # Ensure the writers are closed properly
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            log.debug(
                "Data forwarding stream closed.",
                client_addr=client_addr,
                container_name=container_name,
            )

    async def _cleanup_tcp_session(self, client_addr, listen_port, container_name):
        """Cleans up a TCP session."""
        session_key = (client_addr, listen_port, "tcp")
        async with self.session_lock:
            session_info = self.active_sessions.pop(session_key, None)
            if session_info:
                server_config = self.servers_config_map.get(listen_port)
                if server_config:
                    self.active_sessions_metric.labels(server_config.name).dec()
                self.server_states[container_name]["sessions"] -= 1

                # Cancel associated tasks
                if "client_to_server_task" in session_info:
                    session_info["client_to_server_task"].cancel()
                    await asyncio.gather(
                        session_info["client_to_server_task"], return_exceptions=True
                    )
                if "server_to_client_task" in session_info:
                    session_info["server_to_client_task"].cancel()
                    await asyncio.gather(
                        session_info["server_to_client_task"], return_exceptions=True
                    )

                # Close writers
                if "client_writer" in session_info:
                    if not session_info["client_writer"].is_closing():
                        session_info["client_writer"].close()
                        await session_info["client_writer"].wait_closed()
                if "server_writer_tcp" in session_info:
                    if not session_info["server_writer_tcp"].is_closing():
                        session_info["server_writer_tcp"].close()
                        await session_info["server_writer_tcp"].wait_closed()

                log.info(
                    "TCP session cleaned up.",
                    client_addr=client_addr,
                    container_name=container_name,
                )

    async def _handle_tcp_connection(self, reader, writer, server_config):
        """
        Handles a new TCP connection, starting the server if necessary
        and setting up data forwarding.
        """
        client_addr_tuple = writer.get_extra_info("peername")
        client_addr = client_addr_tuple[0] if client_addr_tuple else "unknown"
        listen_port = server_config.listen_port
        container_name = server_config.container_name

        async with self.server_locks[container_name]:
            # Check if server is running, start if not
            if self.server_states[container_name]["status"] == "stopped":
                log.info(
                    "First TCP connection for stopped server. Starting...",
                    client_addr=client_addr,
                    container_name=container_name,
                )
                startup_timer_start = time.time()  # Capture start time for metric
                start_success = await self.docker_manager.start_server(
                    server_config, self.settings
                )
                if not start_success:
                    log.error(
                        "Server failed to become ready. Closing client connection.",
                        client_addr=client_addr,
                        container_name=container_name,
                    )
                    writer.close()
                    await writer.wait_closed()
                    return

                self.server_states[container_name]["status"] = "running"
                self.running_servers_metric.inc()
                duration = time.time() - startup_timer_start
                self.server_startup_duration_metric.labels(
                    server_name=server_config.name
                ).observe(duration)  # Observe startup duration

        log.info(
            "Establishing new TCP session for running server.",
            client_addr=client_addr,
            server_name=server_config.name,
        )

        try:
            # Connect to the backend Minecraft server inside the container
            server_reader, server_writer = await asyncio.open_connection(
                container_name, server_config.internal_port
            )
            log.debug(
                "Connected to backend server.",
                container_name=container_name,
                internal_port=server_config.internal_port,
            )

            session_info = {
                "client_addr": client_addr,
                "listen_port": listen_port,
                "protocol": "tcp",
                "target_container": container_name,
                "last_packet_time": time.time(),
                "client_writer": writer,
                "server_writer_tcp": server_writer,
            }

            async with self.session_lock:
                session_key = (client_addr, listen_port, "tcp")
                self.active_sessions[session_key] = session_info
                self.server_states[container_name]["sessions"] += 1
                self.active_sessions_metric.labels(server_name=server_config.name).inc()

            # Create tasks to forward data in both directions
            client_to_server_task = asyncio.create_task(
                self._proxy_data(reader, server_writer, session_info)
            )
            server_to_client_task = asyncio.create_task(
                self._proxy_data(server_reader, writer, session_info)
            )

            async with self.session_lock:
                session_info["client_to_server_task"] = client_to_server_task
                session_info["server_to_client_task"] = server_to_client_task

            # Wait for both forwarding tasks to complete (or be cancelled)
            await asyncio.gather(client_to_server_task, server_to_client_task)

        except ConnectionRefusedError:
            log.error(
                "Backend server connection refused. Is server running?",
                client_addr=client_addr,
                container_name=container_name,
            )
        except Exception as e:
            log.error(
                "Unhandled error during TCP session setup or forwarding.",
                client_addr=client_addr,
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
        finally:
            await self._cleanup_tcp_session(client_addr, listen_port, container_name)

    async def _handle_udp_datagram(self, data, addr, server_config):
        """
        Handles an incoming UDP datagram, starting the server if necessary
        and forwarding the packet.
        """
        client_addr, _ = addr  # Unpack addr (client_ip, client_port)
        listen_port = server_config.listen_port
        container_name = server_config.container_name

        session_key = (client_addr, listen_port, "udp")

        async with self.server_locks[container_name]:
            # Check if server is running, start if not
            if self.server_states[container_name]["status"] == "stopped":
                log.info(
                    "First UDP packet for stopped server. Starting...",
                    client_addr=client_addr,
                    container_name=container_name,
                )
                startup_timer_start = time.time()  # Capture start time for metric
                start_success = await self.docker_manager.start_server(
                    server_config, self.settings
                )
                if not start_success:
                    log.error(
                        "Server failed to become ready. Dropping client packet.",
                        client_addr=client_addr,
                        container_name=container_name,
                    )
                    return

                self.server_states[container_name]["status"] = "running"
                self.running_servers_metric.inc()
                duration = time.time() - startup_timer_start
                self.server_startup_duration_metric.labels(
                    server_name=server_config.name
                ).observe(duration)  # Observe startup duration

        async with self.session_lock:
            # Establish new UDP session if it doesn't exist
            if session_key not in self.active_sessions:
                log.info(
                    "Establishing new UDP session for running server.",
                    client_addr=client_addr,
                    server_name=server_config.name,
                )
                (
                    transport,
                    protocol,
                ) = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: UdpProxyProtocol(self, server_config),
                    local_addr=("0.0.0.0", 0),  # Bind to an ephemeral port
                )

                session_info = {
                    "client_addr": client_addr,
                    "listen_port": listen_port,
                    "protocol": "udp",
                    "target_container": container_name,
                    "last_packet_time": time.time(),
                    "transport": transport,
                    "protocol_instance": protocol,
                }
                self.active_sessions[session_key] = session_info
                self.server_states[container_name]["sessions"] += 1
                self.active_sessions_metric.labels(server_name=server_config.name).inc()

            session_info = self.active_sessions[session_key]
            session_info["last_packet_time"] = time.time()
            self.server_states[container_name]["last_activity"] = time.time()

            # Forward the packet
            try:
                session_info["transport"].sendto(
                    data, (container_name, server_config.internal_port)
                )
                self.bytes_transferred_metric.labels(
                    server_name=server_config.name, direction="c2s"
                ).inc(len(data))
            except Exception as e:
                log.error(
                    "Failed to forward UDP packet to server.",
                    client_addr=client_addr,
                    container_name=container_name,
                    error=str(e),
                )
                # If sending fails, clean up the session as it's likely broken
                await self._cleanup_udp_session(
                    client_addr, listen_port, container_name
                )

    async def _cleanup_udp_session(self, client_addr, listen_port, container_name):
        """Cleans up a UDP session."""
        session_key = (client_addr, listen_port, "udp")
        async with self.session_lock:
            session_info = self.active_sessions.pop(session_key, None)
            if session_info:
                server_config = self.servers_config_map.get(listen_port)
                if server_config:
                    self.active_sessions_metric.labels(server_config.name).dec()
                self.server_states[container_name]["sessions"] -= 1

                if (
                    "transport" in session_info
                    and not session_info["transport"].is_closing()
                ):
                    session_info["transport"].close()

                log.info(
                    "UDP session cleaned up.",
                    client_addr=client_addr,
                    container_name=container_name,
                )

    async def _start_listeners(self):
        """Starts all TCP and UDP listening sockets."""
        log.info("Starting proxy listeners...")
        for srv_cfg in self.servers_list:
            listen_port = srv_cfg.listen_port
            try:
                if srv_cfg.server_type == "java":
                    server = await asyncio.start_server(
                        lambda r, w: self._handle_tcp_connection(r, w, srv_cfg),
                        "0.0.0.0",
                        listen_port,
                        backlog=self.settings.tcp_listen_backlog,
                    )
                    self.tcp_listeners.append(server)
                    log.info(
                        "Listening for Java server (TCP)",
                        server_name=srv_cfg.name,
                        listen_port=listen_port,
                        container_name=srv_cfg.container_name,
                    )
                elif srv_cfg.server_type == "bedrock":
                    # For UDP, we need a datagram endpoint, not a server
                    (
                        transport,
                        protocol,
                    ) = await asyncio.get_running_loop().create_datagram_endpoint(
                        lambda: UdpProxyProtocol(self, srv_cfg),
                        local_addr=("0.0.0.0", listen_port),
                    )
                    self.udp_listeners.append(transport)
                    log.info(
                        "Listening for Bedrock server (UDP)",
                        server_name=srv_cfg.name,
                        listen_port=listen_port,
                        container_name=srv_cfg.container_name,
                    )
            except OSError as e:
                log.critical(
                    "FATAL: Could not bind to port. "
                    "Is another process or proxy already running?",
                    port=listen_port,
                    error=str(e),
                )
                # If a listener fails to start, we must shut down gracefully
                await self._close_listeners()
                self.shutdown_event.set()
                return

    async def _close_listeners(self):
        """Closes all active TCP and UDP listeners."""
        log.info("Closing proxy listeners...")
        for server in self.tcp_listeners:
            server.close()
            await server.wait_closed()
        self.tcp_listeners.clear()

        for transport in self.udp_listeners:
            transport.close()
        self.udp_listeners.clear()
        log.info("All listeners closed.")

    async def _reload_configuration(
        self, new_settings: ProxySettings, new_servers: List[ServerConfig]
    ):
        """
        Reloads configuration and re-initializes proxy state.
        This is a destructive operation that terminates all active
        player sessions before applying the new configuration.
        """
        log.info("Initiating configuration reload...")
        # Close all existing listeners
        await self._close_listeners()

        # Shutdown all active sessions
        await self._shutdown_all_sessions()

        self.settings = new_settings
        self.servers_list = new_servers
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}

        # Re-initialize server states and locks for new/updated servers
        for s in self.servers_list:
            if s.container_name not in self.server_states:
                self.server_states[s.container_name] = {
                    "status": "stopped",
                    "last_activity": 0.0,
                    "sessions": 0,
                }
                self.server_locks[s.container_name] = asyncio.Lock()
            # Reset state for existing servers if needed, or ensure consistent
            else:
                self.server_states[s.container_name]["sessions"] = 0
                # Don't reset last_activity to ensure idle detection works

        log.info("Starting listeners with new configuration...")
        await self._start_listeners()
        log.info("Configuration reload complete.")

    async def _shutdown_all_sessions(self):
        """Closes all active client and server sockets to terminate sessions."""
        async with self.session_lock:
            if not self.active_sessions:
                return

            log.info(
                "Closing all active sessions...",
                count=len(self.active_sessions),
            )
            # Collect session keys to avoid modifying dict during iteration
            sessions_to_close = list(self.active_sessions.keys())
            for session_key in sessions_to_close:
                protocol = session_key[2]  # 'tcp' or 'udp'
                client_addr, listen_port, _ = session_key
                container_name = self.active_sessions[session_key]["target_container"]

                if protocol == "tcp":
                    await self._cleanup_tcp_session(
                        client_addr, listen_port, container_name
                    )
                elif protocol == "udp":
                    await self._cleanup_udp_session(
                        client_addr, listen_port, container_name
                    )

            self.active_sessions.clear()
            log.info("All active sessions have been closed.")

    async def _monitor_activity(self):
        """Monitors server and session activity in a dedicated async task."""
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=self.settings.player_check_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass  # Continue loop if no shutdown event

            if self.shutdown_event.is_set():
                break

            current_time = time.time()

            # Process active sessions for idle cleanup
            sessions_to_check = list(self.active_sessions.items())
            for session_key, session_info in sessions_to_check:
                container_name = session_info["target_container"]
                idle_timeout = (
                    self.servers_config_map.get(
                        session_info["listen_port"]
                    ).idle_timeout_seconds
                    or self.settings.idle_timeout_seconds
                )

                if current_time - session_info["last_packet_time"] > idle_timeout:
                    log.info(
                        "Cleaning up idle client session.",
                        client_addr=session_info["client_addr"],
                        container_name=container_name,
                    )
                    if session_info["protocol"] == "tcp":
                        await self._cleanup_tcp_session(
                            session_info["client_addr"],
                            session_info["listen_port"],
                            container_name,
                        )
                    else:  # UDP
                        await self._cleanup_udp_session(
                            session_info["client_addr"],
                            session_info["listen_port"],
                            container_name,
                        )

            # Process servers for idle shutdown
            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                async with self.server_locks[container_name]:
                    state = self.server_states[container_name]

                    if state["status"] == "running" and state["sessions"] == 0:
                        idle_timeout = (
                            server_conf.idle_timeout_seconds
                            or self.settings.idle_timeout_seconds
                        )
                        if current_time - state["last_activity"] > idle_timeout:
                            log.info(
                                "Server idle with 0 sessions. Initiating shutdown.",
                                container_name=container_name,
                                idle_threshold_seconds=idle_timeout,
                            )
                            stop_success = await self.docker_manager.stop_server(
                                container_name
                            )
                            if stop_success:
                                state["status"] = "stopped"
                                self.running_servers_metric.dec()
                            else:
                                log.error(
                                    "Failed to stop idle server.",
                                    container_name=container_name,
                                )
                    # Update RUNNING_SERVERS gauge if status is out of sync
                    # with actual Docker container status (e.g., if manually
                    # stopped outside proxy)
                    elif (
                        state["status"] == "running"
                        and not await self.docker_manager.is_container_running(
                            container_name
                        )
                    ):
                        log.warning(
                            "Monitor found server stopped outside proxy. "
                            "Updating internal state.",
                            container_name=container_name,
                        )
                        state["status"] = "stopped"
                        self.running_servers_metric.dec()

    async def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed server containers are stopped when the proxy starts.
        This is a blocking operation adapted for asyncio.
        """
        log.info("Proxy startup: Ensuring all managed servers are initially stopped.")
        for srv_conf in self.servers_list:
            container_name = srv_conf.container_name
            # Check container status without relying on proxy's internal state
            # which is just initializing.
            if await self.docker_manager.is_container_running(container_name):
                log.warning(
                    "Found running container at proxy startup. Issuing stop.",
                    container_name=container_name,
                )
                await asyncio.sleep(self.settings.initial_server_query_delay_seconds)
                # Wait for server to become query-ready before stopping,
                # to allow for a graceful shutdown if it's currently starting.
                await self.docker_manager.wait_for_server_query_ready(
                    srv_conf,
                    self.settings.initial_boot_ready_max_wait_time_seconds,
                    self.settings.query_timeout_seconds,
                )
                await self.docker_manager.stop_server(container_name)

                log.info(
                    "Waiting for server to fully stop...",
                    container_name=container_name,
                )
                stop_timeout = 60  # seconds
                stop_start_time = time.time()
                while await self.docker_manager.is_container_running(container_name):
                    if time.time() - stop_start_time > stop_timeout:
                        log.error(
                            "Timeout waiting for container to stop.",
                            container_name=container_name,
                        )
                        break
                    await asyncio.sleep(1)
                else:
                    log.info(
                        "Server confirmed to be stopped.",
                        container_name=container_name,
                    )
                # Update internal state after confirming stop
                async with self.server_locks[container_name]:
                    if self.server_states[container_name]["status"] == "running":
                        self.server_states[container_name]["status"] = "stopped"
                        self.running_servers_metric.dec()
            else:
                log.info(
                    "Container confirmed to be stopped.",
                    container_name=container_name,
                )
                async with self.server_locks[container_name]:
                    self.server_states[container_name]["status"] = "stopped"

    async def run(self):
        """
        Starts all services and runs the main asynchronous proxy loop.
        This is the main entry point for the proxy's runtime.
        """
        log.info("--- Starting Nether-bridge On-Demand Proxy Async ---")

        # Ensure all managed servers are stopped on startup
        await self._ensure_all_servers_stopped_on_startup()

        # Start listeners
        await self._start_listeners()

        # Create monitor task
        monitor_task = asyncio.create_task(self._monitor_activity())

        # Heartbeat for health checks
        heartbeat_file = Path("config") / "heartbeat.txt"
        log.info("Heartbeat file location set.", file_path=heartbeat_file)

        last_heartbeat_time = time.time()

        try:
            # Main proxy loop: waits for shutdown/reload signals
            while not self.shutdown_event.is_set():
                if self.reload_event.is_set():
                    log.info("Reloading configuration (proxy initiated)...")
                    # Pass new settings/servers, actual loading happens in main.py
                    # This implies main.py passes these to reload_configuration
                    # when it triggers reload_event. This will be handled by
                    # main.py which calls this method.
                    self.reload_event.clear()

                # Update heartbeat file
                current_time = time.time()
                if (
                    current_time - last_heartbeat_time
                    > self.settings.proxy_heartbeat_interval_seconds
                ):
                    try:
                        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
                        heartbeat_file.write_text(str(int(current_time)))
                        last_heartbeat_time = current_time
                        log.debug("Heartbeat file updated.", file_path=heartbeat_file)
                    except Exception as e:
                        log.warning(
                            "Could not update heartbeat file.",
                            path=str(heartbeat_file),
                            error=str(e),
                        )

                # Wait for a signal or timeout
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass  # Continue loop

        except asyncio.CancelledError:
            log.info("Proxy run loop cancelled.")
        except Exception as e:
            log.error(
                "Unhandled exception in proxy run loop.", error=str(e), exc_info=True
            )
        finally:
            log.info("Graceful shutdown initiated (proxy loop).")

            # Cancel monitor task first
            if monitor_task:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
                log.info("Monitor task terminated.")

            # Close all active sessions
            await self._shutdown_all_sessions()

            # Close all listeners
            await self._close_listeners()

            log.info("Proxy run loop finished.")
