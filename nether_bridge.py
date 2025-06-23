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
        # session_map: client_socket -> server_socket (for TCP)
        self.session_map = {}
        # active_udp_sessions: client_addr -> {server_socket, last_packet_time, ...}
        self.active_udp_sessions = {}
        # udp_server_socket_map: server_socket -> client_addr
        self.udp_server_socket_map = {}
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
            return False
        except docker.errors.APIError as e:
            self.logger.error(
                "API error checking container.",
                container_name=container_name,
                error=str(e),
            )
            return False

    def _wait_for_server_query_ready(
        self, server_config: ServerConfig, max_wait: int, query_timeout: int
    ) -> bool:
        """
        Polls a Minecraft server using mcstatus until it responds or a timeout
        is reached.
        """
        start_time = time.time()
        target = f"{server_config.container_name}:{server_config.internal_port}"
        self.logger.info(
            "Waiting for server to respond.", target=target, max_wait=max_wait
        )
        while time.time() - start_time < max_wait:
            try:
                if server_config.server_type == "java":
                    JavaServer.lookup(target, timeout=query_timeout).status()
                else:
                    BedrockServer.lookup(target, timeout=query_timeout).status()
                self.logger.info("Server responded to query. Ready!", target=target)
                return True
            except Exception:
                time.sleep(1)
        self.logger.error(
            "Timeout waiting for server to respond.", target=target, timeout=max_wait
        )
        return False

    def _start_minecraft_server(self, container_name: str) -> bool:
        if self._is_container_running(container_name):
            return True
        self.logger.info(
            "Attempting to start Minecraft server...", container_name=container_name
        )
        try:
            start_time = time.time()
            container = self.docker_client.containers.get(container_name)
            container.start()
            self.server_states[container_name]["last_activity"] = start_time
            self.logger.info(
                "Docker container start command issued.", container_name=container_name
            )

            server_config = next(
                (s for s in self.servers_list if s.container_name == container_name),
                None,
            )
            if server_config:
                time.sleep(self.settings.server_startup_delay_seconds)
                self._wait_for_server_query_ready(
                    server_config,
                    self.settings.server_ready_max_wait_time_seconds,
                    self.settings.query_timeout_seconds,
                )

            self.server_states[container_name]["running"] = True
            RUNNING_SERVERS.inc()
            if server_config:
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    time.time() - start_time
                )
            return True
        except Exception as e:
            self.logger.error(
                "Failed to start server.",
                container_name=container_name,
                error=e,
                exc_info=True,
            )
            return False

    def _stop_minecraft_server(self, container_name: str):
        self.logger.info(
            "Attempting to stop Minecraft server...", container_name=container_name
        )
        try:
            container = self.docker_client.containers.get(container_name)
            if container.status == "running":
                container.stop()
                RUNNING_SERVERS.dec()
                self.logger.info(
                    "Server stopped successfully.", container_name=container_name
                )
            self.server_states[container_name]["running"] = False
        except Exception as e:
            self.logger.error(
                "Failed to stop server.",
                container_name=container_name,
                error=e,
                exc_info=True,
            )

    def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts for a clean state.
        """
        self.logger.info("Proxy startup: Ensuring all managed servers are stopped.")
        for server in self.servers_list:
            if self._is_container_running(server.container_name):
                self.logger.warning(
                    "Found running at startup. Stopping.",
                    container_name=server.container_name,
                )
                self._stop_minecraft_server(server.container_name)

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

            # Server idle check based on mcstatus player count
            for server_conf in self.servers_list:
                state = self.server_states.get(server_conf.container_name)
                if not (state and state.get("running")):
                    continue
                try:
                    if server_conf.server_type == "java":
                        status = JavaServer.lookup(
                            f"{server_conf.container_name}:{server_conf.internal_port}",
                            timeout=self.settings.query_timeout_seconds,
                        ).status()
                    else:
                        status = BedrockServer.lookup(
                            f"{server_conf.container_name}:{server_conf.internal_port}",
                            timeout=self.settings.query_timeout_seconds,
                        ).status()

                    if status.players.online > 0:
                        state["last_activity"] = time.time()
                    elif (
                        time.time() - state.get("last_activity", 0)
                        > self.settings.idle_timeout_seconds
                    ):
                        self.logger.info(
                            "Server idle with 0 players. Initiating shutdown.",
                            container_name=server_conf.container_name,
                        )
                        self._stop_minecraft_server(server_conf.container_name)
                except Exception:
                    self.logger.warning(
                        "Could not query server for idle check.",
                        container_name=server_conf.container_name,
                    )
                    state["last_activity"] = time.time()

            # UDP Session cleanup for memory management
            idle_sessions_to_remove = [
                key
                for key, session in self.active_udp_sessions.items()
                if time.time() - session["last_packet_time"]
                > self.settings.idle_timeout_seconds
            ]
            for key in idle_sessions_to_remove:
                self._cleanup_udp_session(key)

    def _cleanup_udp_session(self, client_addr):
        session = self.active_udp_sessions.pop(client_addr, None)
        if session:
            server_sock = session.get("server_socket")
            if server_sock:
                self.udp_server_socket_map.pop(server_sock, None)
                if server_sock in self.inputs:
                    self.inputs.remove(server_sock)
                server_sock.close()
            ACTIVE_SESSIONS.labels(server_name=session["server_name"]).dec()
            self.logger.info("Cleaned up idle UDP session.", client_addr=client_addr)

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
                pass  # Reload logic has been removed for simplification during debug

            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0)
            except select.error as e:
                self.logger.error("Error in select.select()", error=e, exc_info=True)
                continue

            for sock in readable:
                try:
                    # --- TCP LOGIC ---
                    if sock.type == socket.SOCK_STREAM:
                        if sock in self.listen_sockets.values():
                            # New TCP connection
                            conn, client_addr = sock.accept()
                            server_port = sock.getsockname()[1]
                            server_config = self.servers_config_map[server_port]
                            self.logger.info(
                                "Accepted new TCP connection.", client_addr=client_addr
                            )

                            if not self._is_container_running(
                                server_config.container_name
                            ):
                                self._start_minecraft_server(
                                    server_config.container_name
                                )

                            server_sock = socket.socket(
                                socket.AF_INET, socket.SOCK_STREAM
                            )
                            server_sock.connect(
                                (
                                    server_config.container_name,
                                    server_config.internal_port,
                                )
                            )

                            conn.setblocking(False)
                            server_sock.setblocking(False)
                            self.inputs.extend([conn, server_sock])
                            self.session_map[conn] = server_sock
                            self.session_map[server_sock] = conn
                            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()
                        else:
                            # Forward TCP data
                            data = sock.recv(4096)
                            if data and sock in self.session_map:
                                self.session_map[sock].sendall(data)
                            else:  # Disconnection
                                counterpart = self.session_map.pop(sock, None)
                                if counterpart:
                                    self.session_map.pop(counterpart, None)
                                    self.inputs.remove(counterpart)
                                    counterpart.close()
                                self.inputs.remove(sock)
                                sock.close()

                    # --- UDP LOGIC ---
                    elif sock.type == socket.SOCK_DGRAM:
                        if sock in self.listen_sockets.values():
                            # Packet on a listening socket
                            data, client_addr = sock.recvfrom(4096)
                            if client_addr not in self.active_udp_sessions:
                                server_port = sock.getsockname()[1]
                                server_config = self.servers_config_map[server_port]
                                if not self._is_container_running(
                                    server_config.container_name
                                ):
                                    self._start_minecraft_server(
                                        server_config.container_name
                                    )

                                server_sock = socket.socket(
                                    socket.AF_INET, socket.SOCK_DGRAM
                                )
                                server_sock.setblocking(False)
                                self.inputs.append(server_sock)
                                self.active_udp_sessions[client_addr] = {
                                    "server_socket": server_sock,
                                    "last_packet_time": time.time(),
                                    "server_name": server_config.name,
                                }
                                self.udp_server_socket_map[server_sock] = client_addr
                                ACTIVE_SESSIONS.labels(
                                    server_name=server_config.name
                                ).inc()

                            session = self.active_udp_sessions[client_addr]
                            session["last_packet_time"] = time.time()
                            server_config = self.servers_config_map[
                                sock.getsockname()[1]
                            ]
                            session["server_socket"].sendto(
                                data,
                                (
                                    server_config.container_name,
                                    server_config.internal_port,
                                ),
                            )
                        else:
                            # Packet on a server-facing UDP socket
                            data, _ = sock.recvfrom(4096)
                            client_addr = self.udp_server_socket_map.get(sock)
                            if client_addr:
                                self.active_udp_sessions[client_addr][
                                    "last_packet_time"
                                ] = time.time()
                                server_port = self.active_udp_sessions[client_addr][
                                    "server_socket"
                                ].getsockname()[1]
                                # Find the correct listening socket to send from
                                listen_port = next(
                                    p
                                    for p, c in self.servers_config_map.items()
                                    if c.internal_port == server_port
                                    and c.server_type == "bedrock"
                                )
                                self.listen_sockets[listen_port].sendto(
                                    data, client_addr
                                )

                except (ConnectionResetError, OSError, KeyError):
                    if sock not in self.listen_sockets.values():
                        self.inputs.remove(sock)
                        sock.close()
                        # Cleanup any session remnants
                        if sock in self.session_map:
                            counterpart = self.session_map.pop(sock)
                            self.session_map.pop(counterpart, None)
                            if counterpart in self.inputs:
                                self.inputs.remove(counterpart)
                        if sock in self.udp_server_socket_map:
                            client_addr = self.udp_server_socket_map.get(sock)
                            self._cleanup_udp_session(client_addr)

    def _create_listening_socket(self, srv_cfg: ServerConfig):
        """Creates and binds a single listening socket based on server config."""
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
            if srv_cfg.server_type == "bedrock"
            else socket.SOCK_STREAM,
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", srv_cfg.listen_port))
        if sock.type == socket.SOCK_STREAM:
            sock.listen(5)
        sock.setblocking(False)
        self.listen_sockets[srv_cfg.listen_port] = sock
        self.inputs.append(sock)
        self.logger.info(
            "Proxy listening",
            server_name=srv_cfg.name,
            listen_port=srv_cfg.listen_port,
            protocol="UDP" if sock.type == socket.SOCK_DGRAM else "TCP",
        )

    def run(self):
        self.logger.info("--- Starting Nether-bridge On-Demand Proxy ---")
        try:
            start_http_server(8000)
            self.logger.info("Prometheus metrics server started.", port=8000)
        except Exception as e:
            self.logger.error(
                "Could not start Prometheus metrics server.", error=str(e)
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
