import json
import logging
import os
import select
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import docker
import structlog
from mcstatus import BedrockServer, JavaServer
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- Constants ---
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")
# Default values for settings, used if not overridden by env vars or settings.json
DEFAULT_SETTINGS = {
    "idle_timeout_seconds": 600,
    "player_check_interval_seconds": 60,
    "query_timeout_seconds": 5,
    "server_ready_max_wait_time_seconds": 120,
    "initial_boot_ready_max_wait_time_seconds": 180,
    "server_startup_delay_seconds": 5,
    "initial_server_query_delay_seconds": 10,
    "log_level": "INFO",
    "log_formatter": "json",
    "healthcheck_stale_threshold_seconds": 60,
    "proxy_heartbeat_interval_seconds": 15,
    "tcp_listen_backlog": 128,
    "max_concurrent_sessions": -1,  # -1 for unlimited
    "prometheus_enabled": True,
    "prometheus_port": 8000,
}

# --- Prometheus Metrics Definitions ---
ACTIVE_SESSIONS = Gauge(
    "netherbridge_active_sessions",
    "Number of active player sessions",
    ["server_name"],
)
RUNNING_SERVERS = Gauge(
    "netherbridge_running_servers",
    "Number of Minecraft server containers currently running",
)
SERVER_STARTUP_DURATION = Histogram(
    "netherbridge_server_startup_duration_seconds",
    "Time taken for a server to start and become ready",
    ["server_name"],
)
BYTES_TRANSFERRED = Counter(
    "netherbridge_bytes_transferred_total",
    "Total bytes transferred through the proxy",
    ["server_name", "direction"],
)


@dataclass
class ServerConfig:
    """Dataclass to hold a single server's configuration."""

    name: str
    server_type: str
    listen_port: int
    container_name: str
    internal_port: int
    idle_timeout_seconds: Optional[int] = None


@dataclass
class ProxySettings:
    """Dataclass to hold all proxy-wide settings."""

    idle_timeout_seconds: int
    player_check_interval_seconds: int
    query_timeout_seconds: int
    server_ready_max_wait_time_seconds: int
    initial_boot_ready_max_wait_time_seconds: int
    server_startup_delay_seconds: int
    initial_server_query_delay_seconds: int
    log_level: str
    log_formatter: str
    healthcheck_stale_threshold_seconds: int
    proxy_heartbeat_interval_seconds: int
    tcp_listen_backlog: int
    max_concurrent_sessions: int
    prometheus_enabled: bool
    prometheus_port: int


class NetherBridgeProxy:
    """
    Nether-bridge: On-Demand Minecraft Server Proxy.
    """

    def __init__(self, settings: ProxySettings, servers_list: list[ServerConfig]):
        self.logger = structlog.get_logger(__name__)
        self.settings = settings
        self.servers_list = servers_list
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}
        self.docker_client = None
        self.server_states = {
            s.container_name: {"running": False, "last_activity": 0.0}
            for s in self.servers_list
        }
        self.socket_to_session_map = {}
        self.active_sessions = {}
        self.listen_sockets = {}
        self.inputs = []
        self.last_heartbeat_time = time.time()
        self._shutdown_requested = False
        self._reload_requested = False

    def signal_handler(self, sig, frame):
        """Handles signals for graceful shutdown and configuration reloads."""
        if hasattr(signal, "SIGHUP") and sig == signal.SIGHUP:
            self._reload_requested = True
            self.logger.warning("SIGHUP signal received, flagging for reload.")
        else:  # SIGINT, SIGTERM
            self.logger.warning(
                "Shutdown signal received, initiating shutdown.", sig=sig
            )
            self._shutdown_requested = True

    def _connect_to_docker(self):
        """Connects to the Docker daemon via the mounted socket."""
        try:
            self.docker_client = docker.from_env()
            self.docker_client.ping()
            self.logger.info("Successfully connected to the Docker daemon.")
        except Exception as e:
            self.logger.critical(
                "FATAL: Could not connect to Docker daemon. "
                "Is /var/run/docker.sock mounted?",
                error=str(e),
            )
            sys.exit(1)

    def _is_container_running(self, container_name: str) -> bool:
        """Checks if a Docker container is currently running via the Docker API."""
        try:
            container = self.docker_client.containers.get(container_name)
            return container.status == "running"
        except docker.errors.NotFound:
            self.logger.debug(
                "Container not found.",
                container_name=container_name,
            )
            return False
        except docker.errors.APIError as e:
            self.logger.error(
                "API error checking container.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error checking container.",
                container_name=container_name,
                error=str(e),
            )
            return False

    def _wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_time_seconds: int,
        query_timeout_seconds: int,
    ) -> bool:
        """
        Polls a Minecraft server using mcstatus until it responds or a timeout
        is reached.
        """
        container_name = server_config.container_name
        target_ip = container_name
        target_port = server_config.internal_port
        server_type = server_config.server_type

        self.logger.info(
            "Waiting for server to respond to query",
            container_name=container_name,
            target=f"{target_ip}:{target_port}",
            max_wait_seconds=max_wait_time_seconds,
        )
        start_time = time.time()

        while time.time() - start_time < max_wait_time_seconds:
            try:
                status = None
                if server_type == "bedrock":
                    server = BedrockServer.lookup(
                        f"{target_ip}:{target_port}",
                        timeout=query_timeout_seconds,
                    )
                    status = server.status()
                elif server_type == "java":
                    server = JavaServer.lookup(
                        f"{target_ip}:{target_port}",
                        timeout=query_timeout_seconds,
                    )
                    status = server.status()

                if status:
                    self.logger.info(
                        "Server responded to query. Ready!",
                        container_name=container_name,
                        latency_ms=status.latency,
                    )
                    return True
            except Exception as e:
                self.logger.debug(
                    "Query failed, retrying...",
                    container_name=container_name,
                    error=str(e),
                )
            time.sleep(query_timeout_seconds)

        self.logger.error(
            f"Timeout: Server did not respond after {max_wait_time_seconds} seconds. "
            "Proceeding anyway.",
            container_name=container_name,
        )
        return False

    def _start_minecraft_server(self, container_name: str) -> bool:
        """Starts a Minecraft server container and waits for it to become ready."""
        if self._is_container_running(container_name):
            self.logger.debug(
                "Server already running, no start action needed.",
                container_name=container_name,
            )
            return True

        self.logger.info(
            "Attempting to start Minecraft server...",
            container_name=container_name,
        )
        try:
            startup_timer_start = time.time()
            container = self.docker_client.containers.get(container_name)
            container.start()
            self.logger.info(
                "Docker container start command issued.",
                container_name=container_name,
            )

            time.sleep(self.settings.server_startup_delay_seconds)

            target_server_config = next(
                (s for s in self.servers_list if s.container_name == container_name),
                None,
            )
            if not target_server_config:
                self.logger.error(
                    "Configuration not found. Cannot query for readiness.",
                    container_name=container_name,
                )
                return False

            self._wait_for_server_query_ready(
                target_server_config,
                self.settings.server_ready_max_wait_time_seconds,
                self.settings.query_timeout_seconds,
            )

            self.server_states[container_name]["running"] = True
            RUNNING_SERVERS.inc()

            duration = time.time() - startup_timer_start
            SERVER_STARTUP_DURATION.labels(
                server_name=target_server_config.name
            ).observe(duration)

            self.logger.info(
                "Startup process complete. Now handling traffic.",
                container_name=container_name,
                duration_seconds=duration,
            )
            return True
        except docker.errors.NotFound:
            self.logger.error(
                "Docker container not found. Cannot start.",
                container_name=container_name,
            )
            return False
        except docker.errors.APIError as e:
            self.logger.error(
                "Docker API error during start.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server startup.",
                container_name=container_name,
                error=str(e),
            )
            return False

    def _stop_minecraft_server(self, container_name: str) -> bool:
        """Stops a Minecraft server container."""
        try:
            container = self.docker_client.containers.get(container_name)
            if container.status == "running":
                self.logger.info(
                    "Attempting to stop Minecraft server...",
                    container_name=container_name,
                )
                container.stop()
                self.server_states[container_name]["running"] = False
                RUNNING_SERVERS.dec()
                self.logger.info(
                    "Server stopped successfully.",
                    container_name=container_name,
                )
                return True
            else:
                self.logger.debug(
                    "Server already in non-running state, no stop action needed.",
                    container_name=container_name,
                    status=container.status,
                )
                self.server_states[container_name]["running"] = False
                return True
        except docker.errors.NotFound:
            self.logger.debug(
                "Docker container not found. Assuming already stopped.",
                container_name=container_name,
            )
            if container_name in self.server_states:
                self.server_states[container_name]["running"] = False
            return True
        except docker.errors.APIError as e:
            self.logger.error(
                "Docker API error during stop.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server stop.",
                container_name=container_name,
                error=str(e),
            )
            return False

    def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts for a clean state.
        """
        self.logger.info(
            (
                "Proxy startup: Ensuring all managed Minecraft servers are "
                "initially stopped."
            )
        )
        for srv_conf in self.servers_list:
            container_name = srv_conf.container_name
            if self._is_container_running(container_name):
                self.logger.warning(
                    "Found running at proxy startup. Issuing a safe stop.",
                    container_name=container_name,
                )
                time.sleep(self.settings.initial_server_query_delay_seconds)
                self._wait_for_server_query_ready(
                    srv_conf,
                    self.settings.initial_boot_ready_max_wait_time_seconds,
                    self.settings.query_timeout_seconds,
                )
                self._stop_minecraft_server(container_name)
            else:
                self.logger.info(
                    "Is confirmed to be stopped.",
                    container_name=container_name,
                )

    def _monitor_servers_activity(self):
        """
        Monitors server and session activity in a dedicated thread.

        This method runs in a continuous loop and performs two main tasks:
        1.  **Session Cleanup:** It iterates through all active sessions and removes
            any that have been idle for longer than the configured timeout.
        2.  **Server Shutdown:** It checks each running server container. If a server
            has no active sessions and its last packet activity was longer ago
            than the idle timeout, it initiates a safe shutdown of that server.
        """
        while not self._shutdown_requested:
            time.sleep(self.settings.player_check_interval_seconds)
            if self._shutdown_requested:
                break

            # --- DEBUG LOGGING ---
            self.logger.info(
                "[DEBUG] Monitor thread running.",
                active_sessions=len(self.active_sessions),
            )

            current_time = time.time()

            for session_key, session_info in list(self.active_sessions.items()):
                server_config = self.servers_config_map.get(session_info["listen_port"])
                if not server_config:
                    continue

                idle_timeout = (
                    server_config.idle_timeout_seconds
                    or self.settings.idle_timeout_seconds
                )

                if current_time - session_info["last_packet_time"] > idle_timeout:
                    self.logger.info(
                        "Cleaning up idle client session.",
                        container_name=session_info["target_container"],
                        client_addr=session_key[0],
                    )
                    ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
                    self._close_session_sockets(session_info)
                    self.active_sessions.pop(session_key, None)
                    self.socket_to_session_map.pop(
                        session_info.get("client_socket"), None
                    )
                    self.socket_to_session_map.pop(
                        session_info.get("server_socket"), None
                    )

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                state = self.server_states.get(container_name)

                if not (state and state.get("running")):
                    continue

                has_active_sessions = any(
                    info["target_container"] == container_name
                    for info in self.active_sessions.values()
                )

                # --- DEBUG LOGGING ---
                if not has_active_sessions:
                    self.logger.info(
                        "[DEBUG] Server has no active sessions.",
                        container_name=container_name,
                    )

                if not has_active_sessions:
                    idle_timeout = (
                        server_conf.idle_timeout_seconds
                        or self.settings.idle_timeout_seconds
                    )
                    if current_time - state.get("last_activity", 0) > idle_timeout:
                        self.logger.info(
                            "Server idle with 0 sessions. Initiating shutdown.",
                            container_name=container_name,
                            idle_threshold_seconds=idle_timeout,
                        )
                        self._stop_minecraft_server(container_name)
                else:
                    self.logger.debug(
                        "Server has active sessions. Not stopping.",
                        container_name=container_name,
                    )

    def _close_session_sockets(self, session_info):
        """
        Helper to safely close sockets associated with a session and remove from inputs.
        """
        client_socket = session_info.get("client_socket")
        server_socket = session_info.get("server_socket")

        if client_socket:
            if client_socket in self.inputs:
                self.inputs.remove(client_socket)
            try:
                client_socket.close()
            except socket.error:
                pass

        if server_socket:
            if server_socket in self.inputs:
                self.inputs.remove(server_socket)
            try:
                server_socket.close()
            except socket.error:
                pass

    def _reload_configuration(self):
        """
        Reloads proxy settings and server definitions from source.
        """
        self.logger.warning("SIGHUP received. Reloading configuration...")

        try:
            new_settings, new_servers = load_application_config()
            configure_logging(new_settings.log_level, new_settings.log_formatter)
            self.settings = new_settings
            self.logger.info(
                "Proxy settings have been reloaded.",
                new_log_level=self.settings.log_level,
                new_log_formatter=self.settings.log_formatter,
            )
        except Exception as e:
            self.logger.error(
                "Failed to reload settings, aborting reload.",
                error=str(e),
                exc_info=True,
            )
            self._reload_requested = False
            return

        old_ports = set(self.servers_config_map.keys())
        new_servers_map = {s.listen_port: s for s in new_servers}
        new_ports = set(new_servers_map.keys())

        for port in old_ports - new_ports:
            # Note: This reload does not terminate existing player sessions for
            # removed servers. Those sessions will remain active until they disconnect
            # or are cleaned up by the idle activity monitor. This ensures a
            # graceful shutdown for in-progress games.
            old_server_config = self.servers_config_map.get(port)
            if not old_server_config:
                continue

            self.logger.info(
                "Removing listener for old server.",
                server_name=old_server_config.name,
                port=port,
            )
            sock = self.listen_sockets.pop(port, None)
            if sock:
                if sock in self.inputs:
                    self.inputs.remove(sock)
                sock.close()

            container_name = old_server_config.container_name
            sessions_to_terminate = [
                (key, info)
                for key, info in self.active_sessions.items()
                if info.get("target_container") == container_name
            ]

            if sessions_to_terminate:
                self.logger.warning(
                    "Terminating active sessions for server removed from "
                    "configuration.",
                    count=len(sessions_to_terminate),
                    container_name=container_name,
                )
                for session_key, session_info in sessions_to_terminate:
                    ACTIVE_SESSIONS.labels(server_name=old_server_config.name).dec()
                    self._close_session_sockets(session_info)
                    self.active_sessions.pop(session_key, None)
                    self.socket_to_session_map.pop(
                        session_info.get("client_socket"), None
                    )
                    self.socket_to_session_map.pop(
                        session_info.get("server_socket"), None
                    )

            self.servers_config_map.pop(port, None)

        for port in new_ports - old_ports:
            srv_cfg = new_servers_map[port]
            self.logger.info(
                "Adding new listener for server.",
                server_name=srv_cfg.name,
                port=port,
            )
            self._create_listening_socket(srv_cfg)
            if srv_cfg.container_name not in self.server_states:
                self.server_states[srv_cfg.container_name] = {
                    "running": False,
                    "last_activity": 0.0,
                }

        self.servers_config_map = new_servers_map
        self.servers_list = new_servers

        self.logger.info("Configuration reload complete.")
        self._reload_requested = False

    def _handle_new_tcp_connection(self, sock: socket.socket):
        """Accepts a new TCP connection and sets up the session."""
        if (
            self.settings.max_concurrent_sessions > 0
            and len(self.active_sessions) >= self.settings.max_concurrent_sessions
        ):
            self.logger.warning(
                "Max concurrent sessions reached. Rejecting new TCP connection.",
                max_sessions=self.settings.max_concurrent_sessions,
                current_sessions=len(self.active_sessions),
            )
            conn, _ = sock.accept()
            conn.close()
            return

        conn, client_addr = sock.accept()
        # DEFER setting non-blocking until after backend connection is made
        self.inputs.append(conn)

        server_port = sock.getsockname()[1]
        server_config = self.servers_config_map[server_port]
        container_name = server_config.container_name

        self.logger.info(
            "Accepted new TCP connection.",
            client_addr=client_addr,
            server_name=server_config.name,
        )

        ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

        if not self._is_container_running(container_name):
            self._start_minecraft_server(container_name)

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_sock.settimeout(5.0)
            server_sock.connect((container_name, server_config.internal_port))
            server_sock.setblocking(False)
        except (socket.error, socket.gaierror) as e:
            self.logger.error(
                "Failed to connect to backend server.",
                container_name=container_name,
                internal_port=server_config.internal_port,
                error=str(e),
            )
            self.inputs.remove(conn)
            conn.close()
            ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
            return

        self.inputs.append(server_sock)

        # NOW set the client socket to non-blocking
        conn.setblocking(False)

        session_key = (client_addr, server_port, "tcp")
        session_info = {
            "client_socket": conn,
            "server_socket": server_sock,
            "target_container": container_name,
            "last_packet_time": time.time(),
            "listen_port": server_port,
            "protocol": "tcp",
        }
        self.active_sessions[session_key] = session_info
        self.socket_to_session_map[conn] = (session_key, "client_socket")
        self.socket_to_session_map[server_sock] = (session_key, "server_socket")

    def _handle_new_udp_packet(self, sock: socket.socket):
        """Handles the first UDP packet from a client, establishing a session."""
        if (
            self.settings.max_concurrent_sessions > 0
            and len(self.active_sessions) >= self.settings.max_concurrent_sessions
        ):
            self.logger.warning(
                "Max concurrent sessions reached. Dropping new UDP packet.",
                max_sessions=self.settings.max_concurrent_sessions,
                current_sessions=len(self.active_sessions),
            )
            return

        data, client_addr = sock.recvfrom(4096)
        server_port = sock.getsockname()[1]
        server_config = self.servers_config_map[server_port]
        container_name = server_config.container_name

        if not self._is_container_running(container_name):
            self.logger.info(
                "First packet received for stopped server. Starting...",
                container_name=container_name,
                client_addr=client_addr,
            )
            self._start_minecraft_server(container_name)

        session_key = (client_addr, server_port, "udp")
        if session_key not in self.active_sessions:
            self.logger.info(
                "Establishing new UDP session for running server.",
                client_addr=client_addr,
                server_name=server_config.name,
            )
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            server_sock.setblocking(False)
            self.inputs.append(server_sock)

            session_info = {
                "client_socket": sock,
                "server_socket": server_sock,
                "target_container": container_name,
                "last_packet_time": time.time(),
                "listen_port": server_port,
                "protocol": "udp",
            }
            self.active_sessions[session_key] = session_info
            self.socket_to_session_map[server_sock] = (session_key, "server_socket")
            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

        session_info = self.active_sessions[session_key]
        session_info["last_packet_time"] = time.time()
        self.server_states[container_name]["last_activity"] = time.time()

        destination_address = (container_name, server_config.internal_port)
        session_info["server_socket"].sendto(data, destination_address)

    def _handle_new_connection(self, sock: socket.socket):
        """
        Dispatches handling for a new connection based on socket type.
        """
        if sock.type == socket.SOCK_STREAM:
            self._handle_new_tcp_connection(sock)
        elif sock.type == socket.SOCK_DGRAM:
            self._handle_new_udp_packet(sock)

    def _forward_packet(self, sock: socket.socket):
        """
        Forwards a packet from an established session (TCP or UDP).
        """
        session_info_tuple = self.socket_to_session_map.get(sock)
        if not session_info_tuple:
            if sock in self.inputs:
                self.inputs.remove(sock)
            sock.close()
            return

        session_key, socket_role = session_info_tuple
        session_info = self.active_sessions.get(session_key)

        if not session_info:
            if sock in self.inputs:
                self.inputs.remove(sock)
            sock.close()
            self.socket_to_session_map.pop(sock, None)
            return

        container_name = session_info["target_container"]
        protocol = session_info["protocol"]

        if protocol == "tcp":
            try:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionResetError("Connection closed by peer")
            except ConnectionResetError as e:
                # --- DEBUG LOGGING ---
                self.logger.info(
                    "[DEBUG] ConnectionResetError caught in _forward_packet.",
                    session_key=session_key,
                )
                raise e
        else:
            data, _ = sock.recvfrom(4096)

        session_info["last_packet_time"] = time.time()

        server_config = self.servers_config_map[session_info["listen_port"]]

        if socket_role == "client_socket":
            self.server_states[container_name]["last_activity"] = time.time()
            destination_socket = session_info["server_socket"]
            destination_address = (
                container_name,
                server_config.internal_port,
            )
            direction = "c2s"
        else:
            destination_socket = session_info["client_socket"]
            destination_address = session_key[0]
            direction = "s2c"

        if protocol == "tcp":
            destination_socket.sendall(data)
        else:
            destination_socket.sendto(data, destination_address)

        BYTES_TRANSFERRED.labels(
            server_name=server_config.name, direction=direction
        ).inc(len(data))

    def _run_proxy_loop(self):
        """
        The main event loop of the proxy.

        This method initializes the main `select` loop which monitors all active
        sockets (both listening sockets for new connections and established
        sockets for data forwarding).

        The loop's responsibilities are:
        1.  Checking for and applying configuration reloads (SIGHUP).
        2.  Waiting for sockets to become readable using `select.select()`.
        3.  Updating a file-based heartbeat for the health check.
        4.  Delegating socket handling to the appropriate methods:
            - `_handle_new_connection` for listening sockets.
            - `_forward_packet` for established session sockets.
        5.  Performing robust cleanup of disconnected or errored sessions.
        """
        self.logger.info("Starting main proxy packet forwarding loop.")

        while not self._shutdown_requested:
            if self._reload_requested:
                self._reload_configuration()

            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0)
            except select.error as e:
                self.logger.error(
                    "Error in select.select()", error=str(e), exc_info=True
                )
                time.sleep(1)
                continue

            current_time = time.time()
            if (
                current_time - self.last_heartbeat_time
                > self.settings.proxy_heartbeat_interval_seconds
            ):
                try:
                    HEARTBEAT_FILE.write_text(str(int(current_time)))
                    self.last_heartbeat_time = current_time
                except Exception:
                    self.logger.warning(
                        "Could not update heartbeat file.",
                        path=str(HEARTBEAT_FILE),
                    )

            for sock in readable:
                session_key_for_error = None
                try:
                    if sock in self.listen_sockets.values():
                        self._handle_new_connection(sock)
                    else:
                        session_tuple = self.socket_to_session_map.get(sock)
                        session_key_for_error = (
                            session_tuple[0] if session_tuple else None
                        )
                        self._forward_packet(sock)

                except (ConnectionResetError, socket.error, OSError) as e:
                    # --- DEBUG LOGGING ---
                    self.logger.info(
                        "[DEBUG] Session cleanup block triggered.",
                        session_key=session_key_for_error,
                        error=str(e),
                    )
                    session_info = self.active_sessions.pop(session_key_for_error, None)
                    if session_info:
                        server_config = self.servers_config_map.get(
                            session_info["listen_port"]
                        )
                        if server_config:
                            ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()

                        self._close_session_sockets(session_info)
                        self.socket_to_session_map.pop(
                            session_info.get("client_socket"), None
                        )
                        self.socket_to_session_map.pop(
                            session_info.get("server_socket"), None
                        )

                except Exception:
                    self.logger.error(
                        "Unhandled exception in proxy loop. Cleaning up session.",
                        socket_fileno=sock.fileno(),
                        exc_info=True,
                    )
                    session_info_tuple = self.socket_to_session_map.get(sock)
                    session_key_for_error = (
                        session_info_tuple[0] if session_info_tuple else None
                    )

                    session_info = self.active_sessions.pop(session_key_for_error, None)
                    if session_info:
                        server_config = self.servers_config_map.get(
                            session_info["listen_port"]
                        )
                        if server_config:
                            ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()

                        self._close_session_sockets(session_info)
                        self.socket_to_session_map.pop(
                            session_info.get("client_socket"), None
                        )
                        self.socket_to_session_map.pop(
                            session_info.get("server_socket"), None
                        )
                    else:
                        if sock in self.inputs:
                            self.inputs.remove(sock)
                        try:
                            sock.close()
                        except OSError:
                            pass
                        self.socket_to_session_map.pop(sock, None)

        self.logger.info("Shutdown requested. Closing all listening sockets.")
        for sock in self.listen_sockets.values():
            sock.close()

    def _create_listening_socket(self, srv_cfg: ServerConfig):
        """Creates and binds a single listening socket based on server config."""
        listen_port = srv_cfg.listen_port
        sock_type = (
            socket.SOCK_DGRAM
            if srv_cfg.server_type == "bedrock"
            else socket.SOCK_STREAM
        )
        protocol_str = "UDP" if sock_type == socket.SOCK_DGRAM else "TCP"

        sock = socket.socket(socket.AF_INET, sock_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind(("0.0.0.0", listen_port))
            if sock_type == socket.SOCK_STREAM:
                sock.listen(self.settings.tcp_listen_backlog)
            sock.setblocking(False)
            self.listen_sockets[listen_port] = sock
            self.inputs.append(sock)
            self.logger.info(
                "Proxy listening for server",
                server_name=srv_cfg.name,
                listen_port=listen_port,
                protocol=protocol_str,
                container_name=srv_cfg.container_name,
            )
        except OSError as e:
            self.logger.critical(
                "FATAL: Could not bind to port.",
                port=listen_port,
                error=str(e),
            )
            if not getattr(self, "_reload_requested", False):
                sys.exit(1)

    def run(self):
        """
        Starts the Nether-bridge proxy application.

        This is the main entry point for the application's execution. It
        initializes services, performs startup checks, and launches the primary
        threads for monitoring and packet forwarding.
        """
        self.logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

        if self.settings.prometheus_enabled:
            try:
                metrics_port = self.settings.prometheus_port
                start_http_server(metrics_port)
                self.logger.info(
                    "Prometheus metrics server started.",
                    port=metrics_port,
                )
            except Exception as e:
                self.logger.error(
                    "Could not start Prometheus metrics server.",
                    error=str(e),
                )
        else:
            self.logger.info("Prometheus metrics server is disabled by configuration.")

        app_metadata = os.environ.get("APP_IMAGE_METADATA")
        if app_metadata:
            try:
                meta = json.loads(app_metadata)
                self.logger.info("Application build metadata", **meta)
            except json.JSONDecodeError:
                self.logger.warning(
                    "Could not parse APP_IMAGE_METADATA",
                    metadata=app_metadata,
                )

        if HEARTBEAT_FILE.exists():
            try:
                HEARTBEAT_FILE.unlink()
                self.logger.info(
                    "Removed stale heartbeat file.",
                    path=str(HEARTBEAT_FILE),
                )
            except OSError as e:
                self.logger.warning(
                    "Could not remove stale heartbeat file.",
                    path=str(HEARTBEAT_FILE),
                    error=str(e),
                )

        self._connect_to_docker()
        self._ensure_all_servers_stopped_on_startup()

        for srv_cfg in self.servers_list:
            self._create_listening_socket(srv_cfg)

        monitor_thread = threading.Thread(
            target=self._monitor_servers_activity, daemon=True
        )
        monitor_thread.start()
        self._run_proxy_loop()


def configure_logging(log_level: str, log_formatter: str):
    """
    Configures logging for the application using structlog.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level.upper(),
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
    ]

    if log_formatter == "json":
        processors = shared_processors + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:  # "console" or any other value
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def perform_health_check():
    """Performs a self-sufficient two-stage health check."""
    logger = structlog.get_logger(__name__)
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            "Health Check FAIL: Error loading configuration.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)

    proxy_settings_for_healthcheck, _ = load_application_config()
    healthcheck_threshold = (
        proxy_settings_for_healthcheck.healthcheck_stale_threshold_seconds
    )

    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found.")
        sys.exit(1)
    try:
        age = int(time.time()) - int(HEARTBEAT_FILE.read_text())
        if age < healthcheck_threshold:
            logger.info("Health Check OK", age_seconds=age)
            sys.exit(0)
        else:
            logger.error("Health Check FAIL: Heartbeat is stale.", age_seconds=age)
            sys.exit(1)
    except Exception as e:
        logger.error(
            "Health Check FAIL: Could not read or parse heartbeat file.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)


def _load_settings_from_json(file_path: Path) -> dict:
    """Loads proxy-wide settings from a JSON file."""
    logger = structlog.get_logger(__name__)
    if not file_path.is_file():
        return {}
    try:
        with open(file_path, "r") as f:
            settings_from_file = json.load(f)
            logger.info("Loaded settings from file.", path=str(file_path))
            return settings_from_file
    except json.JSONDecodeError as e:
        logger.error(
            "Error decoding JSON from file.", path=str(file_path), error=str(e)
        )
        return {}


def _load_servers_from_json(file_path: Path) -> list[dict]:
    """Loads server definitions from a JSON file."""
    logger = structlog.get_logger(__name__)
    if not file_path.is_file():
        return []
    try:
        with open(file_path, "r") as f:
            servers_json_config = json.load(f)
            logger.info("Loaded server definitions from file.", path=str(file_path))
            return servers_json_config.get("servers", [])
    except json.JSONDecodeError as e:
        logger.error(
            "Error decoding JSON from file.", path=str(file_path), error=str(e)
        )
        return []


def _load_servers_from_env() -> list[dict]:
    """Loads server definitions from environment variables (NB_x_...)."""
    logger = structlog.get_logger(__name__)
    env_servers, i = [], 1
    while True:
        listen_port_str = os.environ.get(f"NB_{i}_LISTEN_PORT")
        if not listen_port_str:
            break
        try:
            idle_timeout_str = os.environ.get(f"NB_{i}_IDLE_TIMEOUT")
            server_def = {
                "name": os.environ.get(f"NB_{i}_NAME", f"Server {i}"),
                "server_type": os.environ.get(f"NB_{i}_SERVER_TYPE", "bedrock").lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f"NB_{i}_CONTAINER_NAME"),
                "internal_port": int(os.environ.get(f"NB_{i}_INTERNAL_PORT")),
                "idle_timeout_seconds": int(idle_timeout_str)
                if idle_timeout_str
                else None,
            }
            if not all(
                v is not None
                for v in [
                    server_def["container_name"],
                    server_def["internal_port"],
                ]
            ):
                raise ValueError(f"Incomplete definition for server index {i}.")
            if server_def["server_type"] not in ["bedrock", "java"]:
                raise ValueError(f"Invalid 'server_type' for server index {i}.")
            env_servers.append(server_def)
        except (ValueError, TypeError) as e:
            logger.error(
                "Invalid server definition in environment. Skipping.",
                index=i,
                error=str(e),
            )
        i += 1
    if env_servers:
        logger.info(
            "Loaded server(s) from environment variables.", count=len(env_servers)
        )
    return env_servers


def load_application_config() -> tuple[ProxySettings, list[ServerConfig]]:
    """
    Loads all configuration from files and environment, with env vars taking precedence.
    """
    logger = structlog.get_logger(__name__)
    settings_from_json = _load_settings_from_json(Path("settings.json"))
    final_settings = {}
    for key, default_val in DEFAULT_SETTINGS.items():
        env_map = {
            "idle_timeout_seconds": "NB_IDLE_TIMEOUT",
            "player_check_interval_seconds": "NB_PLAYER_CHECK_INTERVAL",
            "query_timeout_seconds": "NB_QUERY_TIMEOUT",
            "server_ready_max_wait_time_seconds": "NB_SERVER_READY_MAX_WAIT",
            "initial_boot_ready_max_wait_time_seconds": "NB_INITIAL_BOOT_READY_MAX_WAIT",  # noqa: E501
            "server_startup_delay_seconds": "NB_SERVER_STARTUP_DELAY",
            "initial_server_query_delay_seconds": "NB_INITIAL_SERVER_QUERY_DELAY",
            "log_level": "LOG_LEVEL",
            "log_formatter": "NB_LOG_FORMATTER",
            "healthcheck_stale_threshold_seconds": "NB_HEALTHCHECK_STALE_THRESHOLD",
            "proxy_heartbeat_interval_seconds": "NB_HEARTBEAT_INTERVAL",
            "tcp_listen_backlog": "NB_TCP_LISTEN_BACKLOG",
            "max_concurrent_sessions": "NB_MAX_SESSIONS",
            "prometheus_enabled": "NB_PROMETHEUS_ENABLED",
            "prometheus_port": "NB_PROMETHEUS_PORT",
        }
        env_var_name = env_map.get(key, key.upper())
        env_val = os.environ.get(env_var_name)
        if env_val is not None:
            try:
                if isinstance(default_val, bool):
                    final_settings[key] = env_val.lower() in (
                        "true",
                        "1",
                        "yes",
                    )
                elif isinstance(default_val, int):
                    final_settings[key] = int(env_val)
                else:
                    final_settings[key] = env_val
            except ValueError:
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)

    proxy_settings = ProxySettings(**final_settings)
    servers_list_raw = _load_servers_from_env() or _load_servers_from_json(
        Path("servers.json")
    )
    final_servers: list[ServerConfig] = []
    for srv_dict in servers_list_raw:
        try:
            final_servers.append(ServerConfig(**srv_dict))
        except TypeError as e:
            logger.error(
                "Failed to load server definition.",
                server_config=srv_dict,
                error=str(e),
            )
    if not final_servers:
        logger.critical("FATAL: No server configurations loaded.")
    return proxy_settings, final_servers


def main():
    """The main entrypoint for the Nether-bridge application."""
    # Early configuration for healthcheck and initial logs.
    # Defaults to console for better visibility before full config is loaded.
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_formatter = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_formatter)

    if "--healthcheck" in sys.argv:
        perform_health_check()
        sys.exit(0)

    settings, servers = load_application_config()

    # Reconfigure with final settings from files/env
    configure_logging(settings.log_level, settings.log_formatter)
    logger = structlog.get_logger(__name__)
    logger.info(
        "Log level and formatter set to final values.",
        log_level=settings.log_level,
        log_formatter=settings.log_formatter,
    )

    if not servers:
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)

    # Set the signal handlers to point to the new method on the proxy instance
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, proxy.signal_handler)
    signal.signal(signal.SIGINT, proxy.signal_handler)
    signal.signal(signal.SIGTERM, proxy.signal_handler)

    proxy.run()


if __name__ == "__main__":
    main()
