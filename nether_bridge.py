import socket
import time
import docker
import os
import threading
import json
import select
import sys
import logging
import signal
from collections import defaultdict
from mcstatus import BedrockServer, JavaServer
from pathlib import Path
from dataclasses import dataclass, field
from pythonjsonlogger import jsonlogger
from prometheus_client import start_http_server, Counter, Gauge, Histogram

# --- Constants ---
HEARTBEAT_FILE = Path("/tmp/proxy_heartbeat")
# Default values for settings, used if not overridden by env vars or settings.json
DEFAULT_SETTINGS = {
    "idle_timeout_seconds": 600,
    "player_check_interval_seconds": 60,
    "query_timeout_seconds": 5,
    "server_ready_max_wait_time_seconds": 120,
    "initial_boot_ready_max_wait_time_seconds": 180,
    "server_startup_delay_seconds": 5,
    "initial_server_query_delay_seconds": 10,
    "log_level": "INFO", # Default log level
    "healthcheck_stale_threshold_seconds": 60, # Added to settings
    "proxy_heartbeat_interval_seconds": 15 # Added to settings
}

# --- Prometheus Metrics Definitions ---
ACTIVE_SESSIONS = Gauge(
    'netherbridge_active_sessions',
    'Number of active player sessions',
    ['server_name']
)
RUNNING_SERVERS = Gauge(
    'netherbridge_running_servers',
    'Number of Minecraft server containers currently running'
)
SERVER_STARTUP_DURATION = Histogram(
    'netherbridge_server_startup_duration_seconds',
    'Time taken for a server to start and become ready',
    ['server_name']
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
    Nether-bridge: On-Demand Minecraft Server Proxy

    An intelligent UDP proxy for Minecraft servers running in Docker. This class
    dynamically starts and stops Dockerized Minecraft servers (both Bedrock and
    Java editions) based on player activity to conserve system resources. It
    supports multiple servers, flexible configuration, and robust health checks.
    """
    def __init__(self, settings: ProxySettings, servers_list: list[ServerConfig]):
        self.logger = logging.getLogger(__name__)
        self.settings = settings
        self.servers_list = servers_list
        # Create a lookup dictionary from the final list of servers for quick access by listen_port
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}

        self.docker_client = None
        # Initialize server states: container_name -> {"running": bool, "last_activity": float}
        self.server_states = {s.container_name: {"running": False, "last_activity": 0.0} for s in self.servers_list}
        
        # New structure for active sessions:
        # Map socket objects to their role and session key for quick lookup
        # socket -> (session_key, 'client_socket' or 'server_socket')
        self.socket_to_session_map = {}
        # Map session_key (client_addr, listen_port, protocol) to session_info:
        # session_info: {"target_container": str, "client_socket": socket, "server_socket": socket, "last_packet_time": float, "listen_port": int, "protocol": str}
        self.active_sessions = {}
        
        self.packet_buffers = defaultdict(list) # session_key -> [packets]

        self.listen_sockets = {} # listen_port -> socket (TCP or UDP listener)
        self.inputs = [] # List of sockets to monitor with select.select
        self.last_heartbeat_time = time.time()
        self._shutdown_requested = False


    def _connect_to_docker(self):
        """Connects to the Docker daemon via the mounted socket."""
        try:
            self.docker_client = docker.from_env()
            self.docker_client.ping() # Test connection
            self.logger.info("Successfully connected to the Docker daemon.")
        except Exception as e:
            self.logger.critical(f"FATAL: Could not connect to Docker daemon. Is /var/run/docker.sock mounted? Error: {e}")
            sys.exit(1)

    def _is_container_running(self, container_name: str) -> bool:
        """Checks if a Docker container is currently running via the Docker API."""
        try:
            container = self.docker_client.containers.get(container_name)
            return container.status == 'running'
        except docker.errors.NotFound:
            self.logger.debug(f"Container not found.", extra={"container_name": container_name})
            return False
        except docker.errors.APIError as e:
            self.logger.error(f"API error checking container.", extra={"container_name": container_name, "error": str(e)})
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error checking container.", extra={"container_name": container_name, "error": str(e)})
            return False

    def _wait_for_server_query_ready(self, server_config: ServerConfig, max_wait_time_seconds: int, query_timeout_seconds: int) -> bool:
        """Polls a Minecraft server using mcstatus until it responds or a timeout is reached."""
        container_name = server_config.container_name
        target_ip = container_name # Docker DNS resolves container name to IP
        target_port = server_config.internal_port
        server_type = server_config.server_type

        self.logger.info(f"Waiting for server to respond to query", extra={"container_name": container_name, "target": f"{target_ip}:{target_port}", "max_wait_seconds": max_wait_time_seconds})
        start_time = time.time()

        while time.time() - start_time < max_wait_time_seconds:
            try:
                status = None
                if server_type == 'bedrock':
                    server = BedrockServer.lookup(f"{target_ip}:{target_port}", timeout=query_timeout_seconds)
                    status = server.status()
                elif server_type == 'java':
                    server = JavaServer.lookup(f"{target_ip}:{target_port}", timeout=query_timeout_seconds)
                    status = server.status()

                if status:
                    self.logger.info(f"Server responded to query. Ready!", extra={"container_name": container_name, "latency_ms": status.latency})
                    return True
            except Exception as e:
                self.logger.debug(f"Query failed, retrying...", extra={"container_name": container_name, "error": str(e)})
            time.sleep(query_timeout_seconds)

        self.logger.error(f"Timeout: Server did not respond after {max_wait_time_seconds} seconds. Proceeding anyway.", extra={"container_name": container_name})
        return False

    def _start_minecraft_server(self, container_name: str) -> bool:
        """Starts a Minecraft server container and waits for it to become ready."""
        if not self._is_container_running(container_name):
            self.logger.info("Attempting to start Minecraft server...", extra={"container_name": container_name})
            try:
                startup_timer_start = time.time()
                container = self.docker_client.containers.get(container_name)
                container.start()
                self.logger.info("Docker container start command issued.", extra={"container_name": container_name})

                time.sleep(self.settings.server_startup_delay_seconds)

                target_server_config = next((s for s in self.servers_list if s.container_name == container_name), None)
                if not target_server_config:
                    self.logger.error("Configuration not found. Cannot query for readiness.", extra={"container_name": container_name})
                    return False

                self._wait_for_server_query_ready(target_server_config, self.settings.server_ready_max_wait_time_seconds, self.settings.query_timeout_seconds)

                self.server_states[container_name]["running"] = True
                RUNNING_SERVERS.inc()
                
                duration = time.time() - startup_timer_start
                SERVER_STARTUP_DURATION.labels(server_name=target_server_config.name).observe(duration)
                
                self.logger.info("Startup process complete. Now handling traffic.", extra={"container_name": container_name, "duration_seconds": duration})
                return True
            except docker.errors.NotFound:
                self.logger.error("Docker container not found. Cannot start.", extra={"container_name": container_name})
                return False
            except docker.errors.APIError as e:
                self.logger.error("Docker API error during start.", extra={"container_name": container_name, "error": str(e)})
                return False
            except Exception as e:
                self.logger.error("Unexpected error during server startup.", extra={"container_name": container_name, "error": str(e)})
                return False
        self.logger.debug("Server already running, no start action needed.", extra={"container_name": container_name})
        return True

    def _stop_minecraft_server(self, container_name: str) -> bool:
        """Stops a Minecraft server container."""
        try:
            container = self.docker_client.containers.get(container_name)
            if container.status == 'running':
                self.logger.info("Attempting to stop Minecraft server...", extra={"container_name": container_name})
                container.stop()
                self.server_states[container_name]["running"] = False
                RUNNING_SERVERS.dec()
                self.logger.info("Server stopped successfully.", extra={"container_name": container_name})
                return True
            else:
                self.logger.debug("Server already in non-running state, no stop action needed.", extra={"container_name": container_name, "status": container.status})
                self.server_states[container_name]["running"] = False
                return True
        except docker.errors.NotFound:
            self.logger.debug("Docker container not found. Assuming already stopped.", extra={"container_name": container_name})
            if container_name in self.server_states:
                self.server_states[container_name]["running"] = False
            return True
        except docker.errors.APIError as e:
            self.logger.error("Docker API error during stop.", extra={"container_name": container_name, "error": str(e)})
            return False
        except Exception as e:
            self.logger.error("Unexpected error during server stop.", extra={"container_name": container_name, "error": str(e)})
            return False

    def _ensure_all_servers_stopped_on_startup(self):
        """Ensures all managed servers are stopped when the proxy starts for a clean state."""
        self.logger.info("Proxy startup: Ensuring all managed Minecraft servers are initially stopped.")
        for srv_conf in self.servers_list:
            container_name = srv_conf.container_name
            if self._is_container_running(container_name):
                self.logger.warning("Found running at proxy startup. Waiting for it to be query-ready before issuing a safe stop.", extra={"container_name": container_name})
                time.sleep(self.settings.initial_server_query_delay_seconds)
                self._wait_for_server_query_ready(srv_conf, self.settings.initial_boot_ready_max_wait_time_seconds, self.settings.query_timeout_seconds)
                self._stop_minecraft_server(container_name)
            else:
                self.logger.info("Is confirmed to be stopped.", extra={"container_name": container_name})

    def _monitor_servers_activity(self):
        """Periodically checks running servers for player count and stops them if idle."""
        while not self._shutdown_requested:
            time.sleep(self.settings.player_check_interval_seconds)
            if self._shutdown_requested:
                break
            
            current_time = time.time()

            idle_sessions_to_remove = [
                key for key, info in self.active_sessions.items() 
                if current_time - info["last_packet_time"] > self.settings.idle_timeout_seconds
            ]
            for session_key in idle_sessions_to_remove:
                session_info = self.active_sessions.pop(session_key, None)
                if session_info:
                    server_config = self.servers_config_map.get(session_info["listen_port"])
                    if server_config:
                        ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
                    self.logger.info("Cleaning up idle client session.", extra={"container_name": session_info['target_container'], "client_addr": session_key[0]})
                    self._close_session_sockets(session_info)
                    self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                    self.socket_to_session_map.pop(session_info.get("server_socket"), None)

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                state = self.server_states.get(container_name)

                if not (state and state.get("running")):
                    continue

                has_active_sessions = any(
                    info["target_container"] == container_name for info in self.active_sessions.values()
                )

                if not has_active_sessions:
                    if (current_time - state.get("last_activity", 0) > self.settings.idle_timeout_seconds):
                        self.logger.info("Server idle with 0 sessions. Initiating shutdown.", extra={"container_name": container_name, "idle_threshold_seconds": self.settings.idle_timeout_seconds})
                        self._stop_minecraft_server(container_name)
                else:
                    self.logger.debug("Server has active sessions. Not stopping.", extra={"container_name": container_name})
    
    def _close_session_sockets(self, session_info):
        """Helper to safely close sockets associated with a session and remove from inputs."""
        client_socket = session_info.get("client_socket")
        server_socket = session_info.get("server_socket")
        protocol = session_info.get("protocol")

        if protocol == 'tcp' and client_socket:
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

    def _run_proxy_loop(self):
        """The main packet forwarding loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")

        while not self._shutdown_requested:
            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0) 

                current_time = time.time()
                if current_time - self.last_heartbeat_time > self.settings.proxy_heartbeat_interval_seconds:
                    try:
                        HEARTBEAT_FILE.write_text(str(int(current_time)))
                        self.last_heartbeat_time = current_time
                        self.logger.debug("Proxy heartbeat updated.")
                    except Exception as e:
                        self.logger.warning(f"Could not update heartbeat file.", extra={"path": str(HEARTBEAT_FILE), "error": str(e)})

                for sock in readable:
                    if sock.type == socket.SOCK_STREAM and sock in self.listen_sockets.values():
                        conn, client_addr = sock.accept()
                        conn.setblocking(False)
                        self.inputs.append(conn)

                        server_port = sock.getsockname()[1]
                        server_config = self.servers_config_map[server_port]
                        container_name = server_config.container_name

                        self.logger.info("Accepted new TCP connection.", extra={"client_addr": client_addr, "server_name": server_config.name})

                        if not self._is_container_running(container_name):
                            self._start_minecraft_server(container_name)

                        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        server_sock.setblocking(False)
                        try:
                            server_sock.connect_ex((container_name, server_config.internal_port))
                        except socket.gaierror:
                             self.logger.error("DNS resolution failed for container.", extra={"container_name": container_name})
                             self.inputs.remove(conn)
                             conn.close()
                             continue
                        
                        self.inputs.append(server_sock)

                        session_key = (client_addr, server_port, 'tcp')
                        session_info = { "client_socket": conn, "server_socket": server_sock, "target_container": container_name, "last_packet_time": time.time(), "listen_port": server_port, "protocol": 'tcp' }
                        self.active_sessions[session_key] = session_info
                        self.socket_to_session_map[conn] = (session_key, 'client_socket')
                        self.socket_to_session_map[server_sock] = (session_key, 'server_socket')
                        ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()
                        continue

                    elif sock.type == socket.SOCK_DGRAM and sock in self.listen_sockets.values():
                        data, client_addr = sock.recvfrom(4096)
                        server_port = sock.getsockname()[1]
                        server_config = self.servers_config_map[server_port]
                        container_name = server_config.container_name

                        # If the server is not running, start it. This is the trigger.
                        if not self._is_container_running(container_name):
                            self.logger.info("First packet received for stopped server. Starting...", extra={"container_name": container_name, "client_addr": client_addr})
                            # The start function now blocks until the server is ready
                            self._start_minecraft_server(container_name)

                        # At this point, the server is guaranteed to be running.
                        # We now establish a session if it doesn't exist and forward the packet.
                        
                        # CORRECTED: Use the full client_addr tuple for the session key
                        session_key = (client_addr, server_port, 'udp')
                        if session_key not in self.active_sessions:
                            self.logger.info("Establishing new UDP session for running server.", extra={"client_addr": client_addr, "server_name": server_config.name})
                            
                            server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            server_sock.setblocking(False)
                            self.inputs.append(server_sock)

                            session_info = {
                                "client_socket": sock, "server_socket": server_sock, "target_container": container_name,
                                "last_packet_time": time.time(), "listen_port": server_port, "protocol": 'udp'
                            }
                            self.active_sessions[session_key] = session_info
                            self.socket_to_session_map[server_sock] = (session_key, 'server_socket')
                            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

                        # Forward the current packet using the session's socket
                        session_info = self.active_sessions[session_key]
                        session_info["last_packet_time"] = time.time()
                        self.server_states[container_name]["last_activity"] = time.time()
                        
                        try:
                            session_info["server_socket"].sendto(data, (container_name, server_config.internal_port))
                        except Exception as e:
                            self.logger.warning("Error forwarding UDP packet to backend.", extra={"container_name": container_name, "error": str(e)})

                        continue # End of UDP handling

                    session_info_tuple = self.socket_to_session_map.get(sock)
                    if not session_info_tuple:
                        self.logger.debug("Ignoring data on a stale socket.", extra={"fileno": sock.fileno()})
                        if sock in self.inputs:
                            self.inputs.remove(sock)
                        try:
                            sock.close()
                        except OSError:
                            pass
                        continue

                    session_key, socket_role = session_info_tuple
                    session_info = self.active_sessions.get(session_key)

                    if not session_info:
                        self.logger.debug("Socket's session no longer active. Closing socket.", extra={"session_key": session_key})
                        if sock in self.inputs: self.inputs.remove(sock)
                        sock.close()
                        self.socket_to_session_map.pop(sock, None)
                        continue
                    
                    container_name = session_info["target_container"]
                    protocol = session_info["protocol"]
                    
                    try:
                        if protocol == 'tcp':
                            data = sock.recv(4096)
                            if not data: raise ConnectionResetError("Connection closed by peer")
                        else: # UDP
                            data, _ = sock.recvfrom(4096)

                        session_info["last_packet_time"] = time.time()
                        
                        if socket_role == 'client_socket':
                            self.server_states[container_name]["last_activity"] = time.time()
                            session_info["last_packet_time"] = time.time()
                            destination_socket = session_info["server_socket"]
                            destination_address = (container_name, self.servers_config_map[session_info["listen_port"]].internal_port)
                        
                        elif socket_role == 'server_socket':
                            destination_socket = session_info["client_socket"]
                            # CORRECTED: The destination address is the full client tuple, which is the first element of the session key.
                            destination_address = session_key[0] 
                        
                        if protocol == 'tcp':
                            destination_socket.sendall(data)
                        else: # UDP
                            destination_socket.sendto(data, destination_address)

                    except (ConnectionResetError, socket.error, OSError) as e:
                        self.logger.warning("Session disconnected. Cleaning up.", extra={"session_key": session_key, "protocol": protocol, "error": str(e)})
                        server_config = self.servers_config_map.get(session_info["listen_port"])
                        if server_config:
                            ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
                        self._close_session_sockets(session_info)
                        self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                        self.socket_to_session_map.pop(session_info.get("server_socket"), None)
                        self.active_sessions.pop(session_key, None)
            
            except Exception as e:
                self.logger.error("An unexpected error occurred in the main proxy loop.", exc_info=True)
                time.sleep(1)

        self.logger.info("Shutdown requested. Closing all listening sockets.")
        for sock in self.listen_sockets.values():
            sock.close()

    def run(self):
        """Starts the Nether-bridge proxy application."""
        self.logger.info("--- Starting Nether-bridge On-Demand Proxy ---")
        
        try:
            metrics_port = 8000
            start_http_server(metrics_port)
            self.logger.info("Prometheus metrics server started.", extra={"port": metrics_port})
        except Exception as e:
            self.logger.error("Could not start Prometheus metrics server.", extra={"error": str(e)})

        app_metadata = os.environ.get('APP_IMAGE_METADATA')
        if app_metadata:
            try:
                meta = json.loads(app_metadata)
                self.logger.info("Application build metadata", extra=meta)
            except json.JSONDecodeError:
                self.logger.warning("Could not parse APP_IMAGE_METADATA", extra={"metadata": app_metadata})

        if HEARTBEAT_FILE.exists():
            try:
                HEARTBEAT_FILE.unlink()
                self.logger.info("Removed stale heartbeat file.", extra={"path": str(HEARTBEAT_FILE)})
            except OSError as e:
                self.logger.warning("Could not remove stale heartbeat file.", extra={"path": str(HEARTBEAT_FILE), "error": str(e)})

        self._connect_to_docker()
        self._ensure_all_servers_stopped_on_startup()

        for listen_port, srv_cfg in self.servers_config_map.items():
            sock_type = socket.SOCK_DGRAM if srv_cfg.server_type == 'bedrock' else socket.SOCK_STREAM
            protocol_str = 'UDP' if srv_cfg.server_type == 'bedrock' else 'TCP'

            sock = socket.socket(socket.AF_INET, sock_type)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                sock.bind(('0.0.0.0', listen_port))
                if sock_type == socket.SOCK_STREAM:
                    sock.listen(5)
                sock.setblocking(False)
                self.listen_sockets[listen_port] = sock
                self.inputs.append(sock)
                self.logger.info(f"Proxy listening for '{srv_cfg.name}'", extra={"listen_port": listen_port, "protocol": protocol_str, "container_name": srv_cfg.container_name})
            except OSError as e:
                self.logger.critical(f"FATAL: Could not bind to port {listen_port}.", extra={"error": e.strerror, "errno": e.errno}, exc_info=True)
                sys.exit(1)
            except Exception as e:
                self.logger.critical(f"FATAL: Unexpected error during socket binding for port {listen_port}.", exc_info=True)
                sys.exit(1)

        monitor_thread = threading.Thread(target=self._monitor_servers_activity, daemon=True)
        monitor_thread.start()
        self._run_proxy_loop()


# --- Health Check and Config Loading ---

def perform_health_check():
    """Performs a self-sufficient two-stage health check."""
    logger = logging.getLogger(__name__) # Health check should also log.
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(f"Health Check FAIL: Error loading configuration: {e}")
        sys.exit(1)

    HEALTHCHECK_STALE_THRESHOLD_SECONDS_DEFAULT = 60 
    proxy_settings_for_healthcheck, _ = load_application_config()
    healthcheck_threshold = proxy_settings_for_healthcheck.healthcheck_stale_threshold_seconds if proxy_settings_for_healthcheck else HEALTHCHECK_STALE_THRESHOLD_SECONDS_DEFAULT
    
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
        logger.error(f"Health Check FAIL: Could not read or parse heartbeat file. Error: {e}")
        sys.exit(1)

def _load_settings_from_json(file_path: Path) -> dict:
    logger = logging.getLogger(__name__)
    try:
        with open(file_path, 'r') as f:
            settings_from_file = json.load(f)
            logger.info(f"Loaded settings from {file_path}.")
            return settings_from_file
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {file_path}", extra={"error": str(e)})
        return {}

def _load_servers_from_json(file_path: Path) -> list[dict]:
    logger = logging.getLogger(__name__)
    try:
        with open(file_path, 'r') as f:
            servers_json_config = json.load(f)
            logger.info(f"Loaded server definitions from {file_path}.")
            return servers_json_config.get('servers', [])
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {file_path}", extra={"error": str(e)})
        return []

def _load_servers_from_env() -> list[dict]:
    logger = logging.getLogger(__name__)
    env_servers, i = [], 1
    while True:
        listen_port_str = os.environ.get(f'NB_{i}_LISTEN_PORT')
        if not listen_port_str: break
        try:
            server_def = { "name": os.environ.get(f'NB_{i}_NAME', f"Server {i}"), "server_type": os.environ.get(f'NB_{i}_SERVER_TYPE', 'bedrock').lower(), "listen_port": int(listen_port_str), "container_name": os.environ.get(f'NB_{i}_CONTAINER_NAME'), "internal_port": int(os.environ.get(f'NB_{i}_INTERNAL_PORT')) }
            if not all(v is not None for v in [server_def['container_name'], server_def['internal_port']]):
                 raise ValueError(f"Incomplete definition for server index {i}.")
            if server_def['server_type'] not in ['bedrock', 'java']:
                raise ValueError(f"Invalid 'server_type' for server index {i}.")
            env_servers.append(server_def)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid server definition in environment for index {i}. Skipping.", extra={"error": str(e)})
        i += 1
    return env_servers

def load_application_config() -> tuple[ProxySettings, list[ServerConfig]]:
    logger = logging.getLogger(__name__)
    settings_from_json = _load_settings_from_json(Path('settings.json'))
    final_settings = {}
    for key, default_val in DEFAULT_SETTINGS.items():
        env_map = { "idle_timeout_seconds": "NB_IDLE_TIMEOUT", "player_check_interval_seconds": "NB_PLAYER_CHECK_INTERVAL", "query_timeout_seconds": "NB_QUERY_TIMEOUT", "server_ready_max_wait_time_seconds": "NB_SERVER_READY_MAX_WAIT", "initial_boot_ready_max_wait_time_seconds": "NB_INITIAL_BOOT_READY_MAX_WAIT", "server_startup_delay_seconds": "NB_SERVER_STARTUP_DELAY", "initial_server_query_delay_seconds": "NB_INITIAL_SERVER_QUERY_DELAY", "log_level": "LOG_LEVEL", "healthcheck_stale_threshold_seconds": "NB_HEALTHCHECK_STALE_THRESHOLD", "proxy_heartbeat_interval_seconds": "NB_HEARTBEAT_INTERVAL" }
        env_var_name = env_map.get(key, key.upper())
        env_val = os.environ.get(env_var_name)
        if env_val is not None:
            try:
                final_settings[key] = int(env_val) if isinstance(default_val, int) else (env_val.lower() == 'true' if isinstance(default_val, bool) else env_val)
            except ValueError:
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)
    proxy_settings = ProxySettings(**final_settings)
    servers_list_raw = _load_servers_from_env() or _load_servers_from_json(Path('servers.json'))
    final_servers: list[ServerConfig] = []
    for srv_dict in servers_list_raw:
        try:
            final_servers.append(ServerConfig(**srv_dict))
        except TypeError as e:
            logger.error("Failed to load server definition.", extra={"server_config": srv_dict, "error": str(e)})
    if not final_servers:
        logger.critical("FATAL: No server configurations loaded.")
    return proxy_settings, final_servers

# --- Main Execution ---
if __name__ == "__main__":
    logger = logging.getLogger()
    LOG_LEVEL_EARLY = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logger.setLevel(LOG_LEVEL_EARLY)
    logHandler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    logHandler.setFormatter(formatter)
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.addHandler(logHandler)
    
    if '--healthcheck' in sys.argv:
        perform_health_check()
        sys.exit(0)
    
    settings, servers = load_application_config()
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.info("Log level set to final value.", extra={"log_level": settings.log_level})
    
    if not servers:
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)
    
    def signal_handler(sig, frame):
        logger.warning("Received shutdown signal.", extra={"signal": sig})
        proxy._shutdown_requested = True
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    proxy.run()