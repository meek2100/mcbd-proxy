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

import docker
from mcstatus import BedrockServer, JavaServer
from prometheus_client import Gauge, Histogram, start_http_server
from pythonjsonlogger.json import JsonFormatter

# --- Constants ---
HEARTBEAT_FILE = Path("/tmp/proxy_heartbeat")
DEFAULT_SETTINGS = {
    "idle_timeout_seconds": 600,
    "player_check_interval_seconds": 60,
    "query_timeout_seconds": 5,
    "server_ready_max_wait_time_seconds": 120,
    "initial_boot_ready_max_wait_time_seconds": 180,
    "server_startup_delay_seconds": 5,
    "initial_server_query_delay_seconds": 10,
    "log_level": "INFO",
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
    healthcheck_stale_threshold_seconds: int
    proxy_heartbeat_interval_seconds: int


class NetherBridgeProxy:
    """
    Nether-bridge: On-Demand Minecraft Server Proxy.
    """

    def __init__(self, settings: ProxySettings, servers_list: list[ServerConfig]):
        self.logger = logging.getLogger(__name__)
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
        self.state_lock = threading.RLock()
        self._shutdown_requested = False
        self._reload_requested = False

    def _connect_to_docker(self):
        """Connects to the Docker daemon via the mounted socket."""
        try:
            self.docker_client = docker.from_env()
            self.docker_client.ping()
            self.logger.info("Successfully connected to the Docker daemon.")
        except Exception as e:
            self.logger.critical(
                "FATAL: Could not connect to Docker daemon. "
                f"Is /var/run/docker.sock mounted? Error: {e}"
            )
            sys.exit(1)

    def _get_container_ip(self, container_name: str) -> str | None:
        """Gets the internal IP address of a container from the Docker API."""
        try:
            container = self.docker_client.containers.get(container_name)
            networks = container.attrs["NetworkSettings"]["Networks"]
            if networks:
                first_network_name = next(iter(networks))
                ip_address = networks[first_network_name].get("IPAddress")
                if ip_address:
                    return ip_address
            self.logger.warning(
                "Could not find IP address in container network settings.",
                extra={"container_name": container_name},
            )
            return None
        except docker.errors.NotFound:
            return None
        except (KeyError, StopIteration) as e:
            self.logger.error(
                "Failed to parse container IP from API response.",
                extra={"container_name": container_name, "error": str(e)},
            )
            return None

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
                extra={"container_name": container_name, "error": str(e)},
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error checking container.",
                extra={"container_name": container_name, "error": str(e)},
            )
            return False

    def _wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_time_seconds: int,
        query_timeout_seconds: int,
    ) -> bool:
        """Polls a Minecraft server using its IP address until it responds."""
        container_name = server_config.container_name
        target_ip = self._get_container_ip(container_name)
        if not target_ip:
            self.logger.error(
                "Could not resolve container IP. Cannot perform readiness check.",
                extra={"container_name": container_name},
            )
            return False

        target_port = server_config.internal_port
        server_type = server_config.server_type

        self.logger.info(
            "Waiting for server to respond to query",
            extra={
                "container_name": container_name,
                "target": f"{target_ip}:{target_port}",
                "max_wait_seconds": max_wait_time_seconds,
            },
        )
        start_time = time.time()
        while time.time() - start_time < max_wait_time_seconds:
            try:
                status = None
                if server_type == "bedrock":
                    server = BedrockServer.lookup(
                        f"{target_ip}:{target_port}", timeout=query_timeout_seconds
                    )
                    status = server.status()
                elif server_type == "java":
                    server = JavaServer.lookup(
                        f"{target_ip}:{target_port}", timeout=query_timeout_seconds
                    )
                    status = server.status()

                if status:
                    self.logger.info(
                        "Server responded to query. Ready!",
                        extra={
                            "container_name": container_name,
                            "latency_ms": status.latency,
                        },
                    )
                    return True
            except Exception as e:
                self.logger.debug(
                    "Query failed, retrying...",
                    extra={"container_name": container_name, "error": str(e)},
                )
            time.sleep(query_timeout_seconds)

        self.logger.error(
            (
                f"Timeout: Server did not respond after "
                f"{max_wait_time_seconds} seconds. Proceeding anyway."
            ),
            extra={"container_name": container_name},
        )
        return False

    def _start_minecraft_server(self, container_name: str) -> bool:
        """Starts a Minecraft server container and waits for it to become ready."""
        if self._is_container_running(container_name):
            self.logger.debug(
                "Server already running, no start action needed.",
                extra={"container_name": container_name},
            )
            return True

        self.logger.info(
            "Attempting to start Minecraft server...",
            extra={"container_name": container_name},
        )
        try:
            startup_timer_start = time.time()
            container = self.docker_client.containers.get(container_name)
            container.start()
            self.logger.info(
                "Docker container start command issued.",
                extra={"container_name": container_name},
            )
            time.sleep(self.settings.server_startup_delay_seconds)
            with self.state_lock:
                target_server_config = next(
                    (
                        s
                        for s in self.servers_list
                        if s.container_name == container_name
                    ),
                    None,
                )
            if not target_server_config:
                self.logger.error(
                    "Configuration not found for readiness check.",
                    extra={"container_name": container_name},
                )
                return False
            self._wait_for_server_query_ready(
                target_server_config,
                self.settings.server_ready_max_wait_time_seconds,
                self.settings.query_timeout_seconds,
            )
            with self.state_lock:
                self.server_states[container_name]["running"] = True
            RUNNING_SERVERS.inc()
            duration = time.time() - startup_timer_start
            SERVER_STARTUP_DURATION.labels(
                server_name=target_server_config.name
            ).observe(duration)
            self.logger.info(
                "Startup process complete. Now handling traffic.",
                extra={"container_name": container_name, "duration_seconds": duration},
            )
            return True
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            self.logger.error(
                f"Docker error during server startup: {e}",
                extra={"container_name": container_name},
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server startup.",
                extra={"container_name": container_name, "error": str(e)},
            )
            return False

    def _stop_minecraft_server(self, container_name: str) -> bool:
        """Stops a Minecraft server container."""
        try:
            container = self.docker_client.containers.get(container_name)
            if container.status == "running":
                self.logger.info(
                    "Attempting to stop Minecraft server...",
                    extra={"container_name": container_name},
                )
                container.stop()
                with self.state_lock:
                    self.server_states[container_name]["running"] = False
                RUNNING_SERVERS.dec()
                self.logger.info(
                    "Server stopped successfully.",
                    extra={"container_name": container_name},
                )
                return True
            else:
                self.logger.debug(
                    "Server already in non-running state.",
                    extra={
                        "container_name": container_name,
                        "status": container.status,
                    },
                )
                with self.state_lock:
                    self.server_states[container_name]["running"] = False
                return True
        except docker.errors.NotFound:
            self.logger.debug(
                "Docker container not found. Assuming already stopped.",
                extra={"container_name": container_name},
            )
            with self.state_lock:
                if container_name in self.server_states:
                    self.server_states[container_name]["running"] = False
            return True
        except docker.errors.APIError as e:
            self.logger.error(
                "Docker API error during stop.",
                extra={"container_name": container_name, "error": str(e)},
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server stop.",
                extra={"container_name": container_name, "error": str(e)},
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
                    extra={"container_name": container_name},
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
                    extra={"container_name": container_name},
                )

    def _monitor_servers_activity(self):
        """
        Periodically checks running servers for player count and stops them if idle.
        """
        while not self._shutdown_requested:
            time.sleep(self.settings.player_check_interval_seconds)
            if self._shutdown_requested:
                break
            containers_to_stop = []
            with self.state_lock:
                current_time = time.time()
                idle_timeout = self.settings.idle_timeout_seconds
                idle_sessions_to_remove = [
                    k
                    for k, v in self.active_sessions.items()
                    if current_time - v["last_packet_time"] > idle_timeout
                ]
                for session_key in idle_sessions_to_remove:
                    session_info = self.active_sessions.pop(session_key, None)
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
                for server_conf in self.servers_list:
                    container_name = server_conf.container_name
                    state = self.server_states.get(container_name)
                    if not (state and state.get("running")):
                        continue
                    has_active_sessions = any(
                        info["target_container"] == container_name
                        for info in self.active_sessions.values()
                    )
                    if not has_active_sessions:
                        if current_time - state.get("last_activity", 0) > idle_timeout:
                            containers_to_stop.append(container_name)
            for container_name in containers_to_stop:
                self._stop_minecraft_server(container_name)

    def _close_session_sockets(self, session_info):
        """Helper to safely close sockets. Must be called from within a lock."""
        client_socket = session_info.get("client_socket")
        server_socket = session_info.get("server_socket")
        protocol = session_info.get("protocol")
        if protocol == "tcp" and client_socket in self.inputs:
            self.inputs.remove(client_socket)
        if server_socket in self.inputs:
            self.inputs.remove(server_socket)
        for sock in [client_socket, server_socket]:
            if sock:
                try:
                    sock.close()
                except socket.error:
                    pass

    def _reload_configuration(self):
        """Reloads proxy settings and server definitions from source."""
        self.logger.warning("SIGHUP received. Reloading configuration...")
        try:
            new_settings, new_servers = load_application_config()
        except Exception as e:
            self.logger.error(
                f"Failed to reload configuration files, aborting reload: {e}",
                exc_info=True,
            )
            self._reload_requested = False
            return
        with self.state_lock:
            self.settings = new_settings
            old_ports = set(self.servers_config_map.keys())
            new_servers_map = {s.listen_port: s for s in new_servers}
            for port in old_ports - set(new_servers_map.keys()):
                sock = self.listen_sockets.pop(port, None)
                if sock:
                    if sock in self.inputs:
                        self.inputs.remove(sock)
                    sock.close()
            for port, srv_cfg in new_servers_map.items():
                if port not in old_ports:
                    self._create_listening_socket(srv_cfg)
                    if srv_cfg.container_name not in self.server_states:
                        self.server_states[srv_cfg.container_name] = {
                            "running": False,
                            "last_activity": 0.0,
                        }
            self.servers_config_map = new_servers_map
            self.servers_list = new_servers
        self._reload_requested = False

    def _run_proxy_loop(self):
        """The main packet forwarding loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")
        while not self._shutdown_requested:
            if self._reload_requested:
                self._reload_configuration()
            with self.state_lock:
                inputs_copy = list(self.inputs)
            try:
                readable, _, _ = select.select(inputs_copy, [], [], 1.0)
            except select.error:
                continue

            if readable:
                with self.state_lock:
                    for sock in readable:
                        try:
                            if sock in self.listen_sockets.values():
                                if sock.type == socket.SOCK_STREAM:  # TCP
                                    conn, client_addr = sock.accept()
                                    conn.setblocking(False)
                                    self.inputs.append(conn)
                                    server_port = sock.getsockname()[1]
                                    server_config = self.servers_config_map[server_port]
                                    container_name = server_config.container_name
                                    self.logger.info(
                                        "Accepted new TCP connection.",
                                        extra={
                                            "client_addr": client_addr,
                                            "server_name": server_config.name,
                                        },
                                    )
                                    if not self._is_container_running(container_name):
                                        self._start_minecraft_server(container_name)
                                    server_sock = socket.socket(
                                        socket.AF_INET, socket.SOCK_STREAM
                                    )
                                    server_sock.setblocking(False)
                                    target_ip = self._get_container_ip(container_name)
                                    if not target_ip:
                                        continue
                                    server_sock.connect_ex(
                                        (target_ip, server_config.internal_port)
                                    )
                                    self.inputs.append(server_sock)
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
                                    self.socket_to_session_map[conn] = (
                                        session_key,
                                        "client_socket",
                                    )
                                    self.socket_to_session_map[server_sock] = (
                                        session_key,
                                        "server_socket",
                                    )
                                    ACTIVE_SESSIONS.labels(
                                        server_name=server_config.name
                                    ).inc()
                                else:  # UDP
                                    data, client_addr = sock.recvfrom(4096)
                                    server_port = sock.getsockname()[1]
                                    server_config = self.servers_config_map[server_port]
                                    container_name = server_config.container_name
                                    if not self._is_container_running(container_name):
                                        self.logger.info(
                                            (
                                                "First packet received for stopped "
                                                "server. Starting..."
                                            ),
                                            extra={
                                                "container_name": container_name,
                                                "client_addr": client_addr,
                                            },
                                        )
                                        self._start_minecraft_server(container_name)
                                    session_key = (client_addr, server_port, "udp")
                                    if session_key not in self.active_sessions:
                                        target_ip = self._get_container_ip(
                                            container_name
                                        )
                                        if not target_ip:
                                            continue
                                        server_sock = socket.socket(
                                            socket.AF_INET, socket.SOCK_DGRAM
                                        )
                                        server_sock.setblocking(False)
                                        self.inputs.append(server_sock)
                                        session_info = {
                                            "client_socket": sock,
                                            "server_socket": server_sock,
                                            "target_container": container_name,
                                            "target_ip": target_ip,
                                            "last_packet_time": time.time(),
                                            "listen_port": server_port,
                                            "protocol": "udp",
                                        }
                                        self.active_sessions[session_key] = session_info
                                        self.socket_to_session_map[server_sock] = (
                                            session_key,
                                            "server_socket",
                                        )
                                        ACTIVE_SESSIONS.labels(
                                            server_name=server_config.name
                                        ).inc()
                                    session_info = self.active_sessions[session_key]
                                    session_info["last_packet_time"] = time.time()
                                    self.server_states[container_name][
                                        "last_activity"
                                    ] = time.time()
                                    session_info["server_socket"].sendto(
                                        data,
                                        (
                                            session_info["target_ip"],
                                            server_config.internal_port,
                                        ),
                                    )
                            else:  # Established connection
                                session_info_tuple = self.socket_to_session_map.get(
                                    sock
                                )
                                if not session_info_tuple:
                                    continue
                                session_key, socket_role = session_info_tuple
                                session_info = self.active_sessions.get(session_key)
                                if not session_info:
                                    continue
                                data = (
                                    sock.recv(4096)
                                    if session_info["protocol"] == "tcp"
                                    else sock.recvfrom(4096)[0]
                                )
                                if not data and session_info["protocol"] == "tcp":
                                    raise ConnectionResetError("Connection closed")
                                session_info["last_packet_time"] = time.time()
                                if socket_role == "client_socket":
                                    self.server_states[
                                        session_info["target_container"]
                                    ]["last_activity"] = time.time()
                                    destination_socket = session_info["server_socket"]
                                    destination_address = (
                                        session_info.get(
                                            "target_ip",
                                            self._get_container_ip(
                                                session_info["target_container"]
                                            ),
                                        ),
                                        self.servers_config_map[
                                            session_info["listen_port"]
                                        ].internal_port,
                                    )
                                else:
                                    destination_socket = session_info["client_socket"]
                                    destination_address = session_key[0]
                                if session_info["protocol"] == "tcp":
                                    destination_socket.sendall(data)
                                else:
                                    destination_socket.sendto(data, destination_address)
                        except (ConnectionResetError, socket.error, OSError):
                            session_key_tuple = self.socket_to_session_map.get(sock)
                            if session_key_tuple:
                                session_info = self.active_sessions.pop(
                                    session_key_tuple[0], None
                                )
                                if session_info:
                                    server_config = self.servers_config_map.get(
                                        session_info["listen_port"]
                                    )
                                    if server_config:
                                        ACTIVE_SESSIONS.labels(
                                            server_name=server_config.name
                                        ).dec()
                                    self._close_session_sockets(session_info)
                                    self.socket_to_session_map.pop(
                                        session_info.get("client_socket"), None
                                    )
                                    self.socket_to_session_map.pop(
                                        session_info.get("server_socket"), None
                                    )
                        except Exception:
                            self.logger.error(
                                f"Unhandled exception for socket {sock.fileno()}.",
                                exc_info=True,
                            )

    def _create_listening_socket(self, srv_cfg: ServerConfig):
        """Creates and binds a socket. Must be called from within a lock."""
        sock_type = (
            socket.SOCK_DGRAM
            if srv_cfg.server_type == "bedrock"
            else socket.SOCK_STREAM
        )
        sock = socket.socket(socket.AF_INET, sock_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", srv_cfg.listen_port))
            if sock_type == socket.SOCK_STREAM:
                sock.listen(5)
            sock.setblocking(False)
            self.listen_sockets[srv_cfg.listen_port] = sock
            self.inputs.append(sock)
        except OSError as e:
            self.logger.critical(
                f"FATAL: Could not bind to port {srv_cfg.listen_port}.",
                extra={"error": str(e)},
            )
            sys.exit(1)

    def run(self):
        self.logger.info("--- Starting Nether-bridge On-Demand Proxy ---")
        try:
            start_http_server(8000)
        except Exception as e:
            self.logger.error(
                "Could not start Prometheus metrics server.", extra={"error": str(e)}
            )
        self._connect_to_docker()
        self._ensure_all_servers_stopped_on_startup()
        with self.state_lock:
            for srv_cfg in self.servers_list:
                self._create_listening_socket(srv_cfg)
        monitor_thread = threading.Thread(
            target=self._monitor_servers_activity, daemon=True
        )
        monitor_thread.start()
        self._run_proxy_loop()


# (The rest of the file remains unchanged from your baseline)
def perform_health_check():
    logger = logging.getLogger(__name__)
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            f"Health Check FAIL: Error loading configuration. Error: {e}", exc_info=True
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
            logger.info(f"Health Check OK: Heartbeat is {age} seconds old.")
            sys.exit(0)
        else:
            logger.error(f"Health Check FAIL: Heartbeat is stale ({age} seconds old).")
            sys.exit(1)
    except Exception as e:
        logger.error(
            f"Health Check FAIL: Could not read or parse heartbeat file. Error: {e}",
            exc_info=True,
        )
        sys.exit(1)


def _load_settings_from_json(file_path: Path) -> dict:
    logger = logging.getLogger(__name__)
    if not file_path.is_file():
        return {}
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {file_path}", extra={"error": str(e)})
        return {}


def _load_servers_from_json(file_path: Path) -> list[dict]:
    logger = logging.getLogger(__name__)
    if not file_path.is_file():
        return []
    try:
        with open(file_path, "r") as f:
            return json.load(f).get("servers", [])
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {file_path}", extra={"error": str(e)})
        return []


def _load_servers_from_env() -> list[dict]:
    logger = logging.getLogger(__name__)
    env_servers, i = [], 1
    while True:
        if not os.environ.get(f"NB_{i}_LISTEN_PORT"):
            break
        try:
            server_def = {
                "name": os.environ.get(f"NB_{i}_NAME", f"Server {i}"),
                "server_type": os.environ.get(f"NB_{i}_SERVER_TYPE", "bedrock").lower(),
                "listen_port": int(os.environ[f"NB_{i}_LISTEN_PORT"]),
                "container_name": os.environ[f"NB_{i}_CONTAINER_NAME"],
                "internal_port": int(os.environ[f"NB_{i}_INTERNAL_PORT"]),
            }
            if server_def["server_type"] not in ["bedrock", "java"]:
                raise ValueError("Invalid server_type")
            env_servers.append(server_def)
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                f"Invalid server definition in environment for index {i}.",
                extra={"error": str(e)},
            )
        i += 1
    return env_servers


def load_application_config() -> tuple[ProxySettings, list[ServerConfig]]:
    logger = logging.getLogger(__name__)
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
            "healthcheck_stale_threshold_seconds": "NB_HEALTHCHECK_STALE_THRESHOLD",
            "proxy_heartbeat_interval_seconds": "NB_HEARTBEAT_INTERVAL",
        }
        env_val = os.environ.get(env_map.get(key, key.upper()))
        if env_val is not None:
            try:
                final_settings[key] = type(default_val)(env_val)
            except ValueError:
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)
    proxy_settings = ProxySettings(**final_settings)
    servers_list_raw = _load_servers_from_env() or _load_servers_from_json(
        Path("servers.json")
    )
    final_servers = [ServerConfig(**s) for s in servers_list_raw]
    if not final_servers:
        logger.critical("FATAL: No server configurations loaded.")
    return proxy_settings, final_servers


if __name__ == "__main__":
    logger = logging.getLogger()
    logHandler = logging.StreamHandler()
    formatter = JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    logHandler.setFormatter(formatter)
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.addHandler(logHandler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    if "--healthcheck" in sys.argv:
        perform_health_check()
        sys.exit(0)

    settings, servers = load_application_config()
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    if not servers:
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)

    def signal_handler(sig, frame):
        if hasattr(signal, "SIGHUP") and sig == signal.SIGHUP:
            proxy._reload_requested = True
        else:
            proxy._shutdown_requested = True

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    proxy.run()
