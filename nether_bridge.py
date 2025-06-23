import json
import os
import select
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import docker
import structlog
from mcstatus import BedrockServer, JavaServer
from prometheus_client import Gauge, Histogram, start_http_server

from logging_config import setup_logging

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
    "log_format": "json",
    "healthcheck_stale_threshold_seconds": 60,
    "proxy_heartbeat_interval_seconds": 15,
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


@dataclass
class ServerConfig:
    """Dataclass to hold a single server's configuration."""

    name: str
    server_type: str
    listen_port: int
    container_name: str
    internal_port: int


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
    log_format: str
    healthcheck_stale_threshold_seconds: int
    proxy_heartbeat_interval_seconds: int


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
        else:
            self.logger.warning(
                "Shutdown signal received, initiating shutdown.", signal=sig
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
                "FATAL: Could not connect to Docker daemon.",
                help_text="Is /var/run/docker.sock mounted?",
                error=str(e),
                exc_info=True,
            )
            sys.exit(1)

    def _is_container_running(self, container_name: str) -> bool:
        """Checks if a Docker container is currently running via the Docker API."""
        try:
            container = self.docker_client.containers.get(container_name)
            return container.status == "running"
        except docker.errors.NotFound:
            self.logger.debug("Container not found.", container_name=container_name)
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
            "Timeout: Server did not respond to query. Proceeding anyway.",
            container_name=container_name,
            timeout_seconds=max_wait_time_seconds,
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
            # Set last_activity on start to begin the idle timer
            self.server_states[container_name]["last_activity"] = startup_timer_start
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
                duration_seconds=round(duration, 2),
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
                exc_info=True,
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
                exc_info=True,
            )
            return False

    def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts for a clean state.
        """
        self.logger.info(
            (
                "Proxy startup: Ensuring all managed Minecraft servers "
                "are initially stopped."
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

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                state = self.server_states.get(container_name)

                if not (state and state.get("running")):
                    continue

                player_count = -1
                try:
                    query_target = f"{container_name}:{server_conf.internal_port}"
                    if server_conf.server_type == "java":
                        server = JavaServer.lookup(
                            query_target, timeout=self.settings.query_timeout_seconds
                        )
                    else:  # bedrock
                        server = BedrockServer.lookup(
                            query_target, timeout=self.settings.query_timeout_seconds
                        )

                    status = server.status()
                    player_count = status.players.online

                    self.logger.debug(
                        "Queried server for player count",
                        container_name=container_name,
                        players=player_count,
                    )

                    if player_count > 0:
                        # If there are players, reset the idle timer.
                        state["last_activity"] = time.time()
                    elif player_count == 0:
                        # If there are no players, check if the idle timeout
                        # has been exceeded.
                        if (
                            time.time() - state.get("last_activity", 0)
                            > self.settings.idle_timeout_seconds
                        ):
                            self.logger.info(
                                "Server idle with 0 players. Initiating shutdown.",
                                container_name=container_name,
                                idle_seconds=self.settings.idle_timeout_seconds,
                            )
                            self._stop_minecraft_server(container_name)
                except Exception as e:
                    self.logger.warning(
                        "Could not query server for player count during idle check. "
                        "Resetting idle timer for safety.",
                        container_name=container_name,
                        error=str(e),
                    )
                    # Reset last_activity timer to prevent shutting down a
                    # server that may be busy but not responding to queries
                    state["last_activity"] = time.time()

    def _close_session_sockets(self, session_info):
        """
        Helper to safely close sockets associated with a session and remove from inputs.
        """
        client_socket = session_info.get("client_socket")
        server_socket = session_info.get("server_socket")

        if client_socket and client_socket.type == socket.SOCK_STREAM:
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
            setup_logging(
                log_level=new_settings.log_level, log_format=new_settings.log_format
            )
            self.settings = new_settings
            self.logger.info("Proxy settings have been reloaded.")
        except Exception as e:
            self.logger.error(
                "Failed to reload settings, aborting reload", error=e, exc_info=True
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
            server_name = self.servers_config_map.get(
                port, ServerConfig("Unknown", "", port, "", port)
            ).name
            self.logger.info(
                "Removing listener for old server", server_name=server_name, port=port
            )
            sock = self.listen_sockets.pop(port, None)
            if sock:
                if sock in self.inputs:
                    self.inputs.remove(sock)
                sock.close()
            self.servers_config_map.pop(port, None)

        for port in new_ports - old_ports:
            srv_cfg = new_servers_map[port]
            self.logger.info(
                "Adding new listener for server", server_name=srv_cfg.name, port=port
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
        conn, client_addr = sock.accept()
        conn.setblocking(False)
        self.inputs.append(conn)

        server_port = sock.getsockname()[1]
        server_config = self.servers_config_map[server_port]
        container_name = server_config.container_name

        self.logger.info(
            "Accepted new TCP connection.",
            client_addr=client_addr,
            server_name=server_config.name,
        )

        if not self._is_container_running(container_name):
            self._start_minecraft_server(container_name)
        else:
            # If server is already running, update its activity timer
            self.server_states[container_name]["last_activity"] = time.time()

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setblocking(False)
        try:
            server_sock.connect_ex((container_name, server_config.internal_port))
        except socket.gaierror:
            self.logger.error(
                "DNS resolution failed for container.",
                container_name=container_name,
            )
            self.inputs.remove(conn)
            conn.close()
            return

        self.inputs.append(server_sock)
        session_key = (client_addr, server_port)
        self.socket_to_session_map[conn] = (session_key, "client_socket")
        self.socket_to_session_map[server_sock] = (session_key, "server_socket")

    def _handle_new_udp_packet(self, sock: socket.socket):
        """Handles the first UDP packet from a client, establishing a session."""
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
        else:
            # If server is already running, update its activity timer
            self.server_states[container_name]["last_activity"] = time.time()

        session_key = (client_addr, server_port)
        if session_key not in self.active_sessions:
            self.logger.info(
                "Establishing new UDP session for client.",
                client_addr=client_addr,
                server_name=server_config.name,
            )
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            server_sock.setblocking(False)
            self.inputs.append(server_sock)

            session_info = {
                "client_socket": sock,
                "server_socket": server_sock,
            }
            self.active_sessions[session_key] = session_info
            self.socket_to_session_map[server_sock] = (session_key, "server_socket")

        session_info = self.active_sessions[session_key]
        destination_address = (container_name, server_config.internal_port)
        session_info["server_socket"].sendto(data, destination_address)

    def _forward_packet(self, sock: socket.socket):
        """
        Forwards a packet from an established session (TCP or UDP).
        This is called when `select` finds a non-listening socket is readable.
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

        is_tcp = sock.type == socket.SOCK_STREAM
        try:
            if is_tcp:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionResetError("Connection closed by peer")
            else:
                data, _ = sock.recvfrom(4096)
        except (ConnectionResetError, socket.error):
            self.logger.warning(
                "Session disconnected during recv.", session_key=session_key
            )
            self._cleanup_session(session_key)
            return

        if socket_role == "server_socket":
            destination_socket = session_info["client_socket"]
            destination_address = session_key[0]  # The client's (address, port) tuple
            try:
                if is_tcp:
                    destination_socket.sendall(data)
                else:
                    destination_socket.sendto(data, destination_address)
            except (ConnectionResetError, socket.error):
                self.logger.warning(
                    "Session disconnected during send to client.",
                    session_key=session_key,
                )
                self._cleanup_session(session_key)

    def _cleanup_session(self, session_key):
        """Removes a session and cleans up its resources."""
        session_info = self.active_sessions.pop(session_key, None)
        if session_info:
            self.logger.info(
                "Cleaning up disconnected session.", session_key=session_key
            )
            # Find the socket in the map and remove it, regardless of role
            sockets_to_remove = [
                s
                for s, (key, _) in self.socket_to_session_map.items()
                if key == session_key
            ]
            for s in sockets_to_remove:
                self.socket_to_session_map.pop(s, None)
            self._close_session_sockets(session_info)

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
                self.logger.error("Error in select.select()", error=e, exc_info=True)
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
                    self.logger.debug("Proxy heartbeat updated.")
                except Exception:
                    self.logger.warning(
                        "Could not update heartbeat file.",
                        path=str(HEARTBEAT_FILE),
                    )

            for sock in readable:
                try:
                    if sock in self.listen_sockets.values():
                        if sock.type == socket.SOCK_STREAM:
                            self._handle_new_tcp_connection(sock)
                        elif sock.type == socket.SOCK_DGRAM:
                            self._handle_new_udp_packet(sock)
                    else:
                        self._forward_packet(sock)
                except Exception:
                    self.logger.error(
                        "Unhandled exception during socket handling.",
                        socket_fileno=sock.fileno(),
                        exc_info=True,
                    )
                    # Attempt to find and cleanup session if possible
                    session_info_tuple = self.socket_to_session_map.get(sock)
                    if session_info_tuple:
                        self._cleanup_session(session_info_tuple[0])
                    elif sock in self.inputs:  # If it's a listening socket that errored
                        self.inputs.remove(sock)
                        sock.close()

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
                sock.listen(5)
            sock.setblocking(False)
            self.listen_sockets[listen_port] = sock
            self.inputs.append(sock)
            self.logger.info(
                "Proxy listening",
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

        try:
            metrics_port = 8000
            start_http_server(metrics_port)
            self.logger.info("Prometheus metrics server started.", port=metrics_port)
        except Exception as e:
            self.logger.error(
                "Could not start Prometheus metrics server.", error=str(e)
            )

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
                    "Removed stale heartbeat file.", path=str(HEARTBEAT_FILE)
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


def perform_health_check():
    """Performs a self-sufficient two-stage health check."""
    logger = structlog.get_logger("health_check")
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            "Health Check FAIL: Error loading configuration.", error=e, exc_info=True
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
            logger.info("Health Check OK", heartbeat_age_seconds=age)
            sys.exit(0)
        else:
            logger.error(
                "Health Check FAIL: Heartbeat is stale.", heartbeat_age_seconds=age
            )
            sys.exit(1)
    except Exception as e:
        logger.error(
            "Health Check FAIL: Could not read or parse heartbeat file.",
            error=e,
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
            logger.info("Loaded settings from file", path=str(file_path))
            return settings_from_file
    except json.JSONDecodeError as e:
        logger.error("Error decoding JSON from file", path=str(file_path), error=str(e))
        return {}


def _load_servers_from_json(file_path: Path) -> list[dict]:
    """Loads server definitions from a JSON file."""
    logger = structlog.get_logger(__name__)
    if not file_path.is_file():
        return []
    try:
        with open(file_path, "r") as f:
            servers_json_config = json.load(f)
            logger.info("Loaded server definitions from file", path=str(file_path))
            return servers_json_config.get("servers", [])
    except json.JSONDecodeError as e:
        logger.error("Error decoding JSON from file", path=str(file_path), error=str(e))
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
            server_def = {
                "name": os.environ.get(f"NB_{i}_NAME", f"Server {i}"),
                "server_type": os.environ.get(f"NB_{i}_SERVER_TYPE", "bedrock").lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f"NB_{i}_CONTAINER_NAME"),
                "internal_port": int(os.environ.get(f"NB_{i}_INTERNAL_PORT")),
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
                server_index=i,
                error=str(e),
            )
        i += 1
    if env_servers:
        logger.info("Loaded servers from environment variables", count=len(env_servers))
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
            "log_format": "LOG_FORMAT",
            "healthcheck_stale_threshold_seconds": "NB_HEALTHCHECK_STALE_THRESHOLD",
            "proxy_heartbeat_interval_seconds": "NB_HEARTBEAT_INTERVAL",
        }
        env_var_name = env_map.get(key, key.upper())
        env_val = os.environ.get(env_var_name)
        if env_val is not None:
            try:
                if isinstance(default_val, bool):
                    final_settings[key] = env_val.lower() in ("true", "1", "yes")
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
    setup_logging(
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        log_format=os.environ.get("LOG_FORMAT", "json"),
    )

    if "--healthcheck" in sys.argv:
        perform_health_check()
        sys.exit(0)

    settings, servers = load_application_config()

    setup_logging(log_level=settings.log_level, log_format=settings.log_format)

    if not servers:
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, proxy.signal_handler)
    signal.signal(signal.SIGINT, proxy.signal_handler)
    signal.signal(signal.SIGTERM, proxy.signal_handler)

    proxy.run()


if __name__ == "__main__":
    main()
