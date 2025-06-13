import socket
import time
import docker
import os
import threading
import json
import select
import sys
import logging
from collections import defaultdict
from mcstatus import BedrockServer, JavaServer
from pathlib import Path
from dataclasses import dataclass, field

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
            self.logger.debug(f"[Docker] Container '{container_name}' not found. Assuming not running.")
            return False
        except docker.errors.APIError as e:
            self.logger.error(f"[Docker] API error checking container '{container_name}': {e}")
            return False
        except Exception as e:
            self.logger.error(f"[Docker] Unexpected error checking container '{container_name}': {e}")
            return False

    def _wait_for_server_query_ready(self, server_config: ServerConfig, max_wait_time_seconds: int, query_timeout_seconds: int) -> bool:
        """Polls a Minecraft server using mcstatus until it responds or a timeout is reached."""
        container_name = server_config.container_name
        target_ip = container_name # Docker DNS resolves container name to IP
        target_port = server_config.internal_port
        server_type = server_config.server_type

        self.logger.info(f"[{container_name}] Waiting for server to respond to query at {target_ip}:{target_port} (max {max_wait_time_seconds}s)...")
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
                    self.logger.info(f"[{container_name}] Server responded to query. Latency: {status.latency:.2f}ms. Ready!")
                    return True
            except Exception as e:
                self.logger.debug(f"[{container_name}] Query failed: {e}. Retrying in {query_timeout_seconds}s...")
            time.sleep(query_timeout_seconds)

        self.logger.error(f"[{container_name}] Timeout: Server did not respond after {max_wait_time_seconds} seconds. Proceeding anyway.")
        return False

    def _start_minecraft_server(self, container_name: str) -> bool:
        """Starts a Minecraft server container and waits for it to become ready."""
        if not self._is_container_running(container_name):
            self.logger.info(f"[{container_name}] Attempting to start Minecraft server...")
            try:
                container = self.docker_client.containers.get(container_name)
                container.start()
                self.logger.info(f"[{container_name}] Docker container start command issued.")

                # Give the server a moment to initialize before probing
                time.sleep(self.settings.server_startup_delay_seconds)

                target_server_config = next((s for s in self.servers_list if s.container_name == container_name), None)
                if not target_server_config:
                    self.logger.error(f"[{container_name}] Configuration not found. Cannot query for readiness.")
                    return False

                self._wait_for_server_query_ready(target_server_config, self.settings.server_ready_max_wait_time_seconds, self.settings.query_timeout_seconds)

                self.server_states[container_name]["running"] = True
                self.server_states[container_name]["last_activity"] = time.time() 
                self.logger.info(f"[{container_name}] Startup process complete. Now handling traffic.")
                return True
            except docker.errors.NotFound:
                self.logger.error(f"[{container_name}] Docker container not found. Cannot start.")
                return False
            except docker.errors.APIError as e:
                self.logger.error(f"[{container_name}] Docker API error during start: {e}")
                return False
            except Exception as e:
                self.logger.error(f"[{container_name}] Unexpected error during server startup: {e}")
                return False
        self.logger.debug(f"[{container_name}] Server already running, no start action needed.")
        return True # Considered successful if it's already running

    def _stop_minecraft_server(self, container_name: str) -> bool:
        """Stops a Minecraft server container."""
        try:
            container = self.docker_client.containers.get(container_name)
            if container.status == 'running':
                self.logger.info(f"[{container_name}] Attempting to stop Minecraft server...")
                container.stop()
                self.server_states[container_name]["running"] = False
                self.logger.info(f"[{container_name}] Server stopped successfully.")
                return True
            else:
                # Container exists but is not running (e.g., 'exited', 'paused', 'created')
                self.logger.debug(f"[{container_name}] Server already in non-running state '{container.status}', no stop action needed.")
                self.server_states[container_name]["running"] = False # Ensure internal state is false
                return True
        except docker.errors.NotFound:
            # Container does not exist. Treat as already stopped.
            self.logger.debug(f"[{container_name}] Docker container not found. Assuming already stopped.")
            # Important: Update internal state to reflect it's not running
            if container_name in self.server_states: # Ensure the key exists before modifying
                self.server_states[container_name]["running"] = False
            return True
        except docker.errors.APIError as e:
            self.logger.error(f"[{container_name}] Docker API error during stop: {e}")
            # Keep internal state as is if API occurred, as we don't know true status
            return False
        except Exception as e:
            self.logger.error(f"[{container_name}] Unexpected error during server stop: {e}")
            return False

    def _ensure_all_servers_stopped_on_startup(self):
        """Ensures all managed servers are stopped when the proxy starts for a clean state."""
        self.logger.info("Proxy startup: Ensuring all managed Minecraft servers are initially stopped.")
        for srv_conf in self.servers_list:
            container_name = srv_conf.container_name
            if self._is_container_running(container_name):
                self.logger.warning(f"[{container_name}] Found running at proxy startup. Waiting for it to be query-ready before issuing a safe stop.")
                # Give it a moment before query
                time.sleep(self.settings.initial_server_query_delay_seconds)
                self._wait_for_server_query_ready(srv_conf, self.settings.initial_boot_ready_max_wait_time_seconds, self.settings.query_timeout_seconds)
                self._stop_minecraft_server(container_name)
            else:
                self.logger.info(f"[{container_name}] Is confirmed to be stopped.")

    def _monitor_servers_activity(self):
        """Periodically checks running servers for player count and stops them if idle."""
        while True:
            time.sleep(self.settings.player_check_interval_seconds)
            current_time = time.time()

            # First, clean up any client sessions that have seen no packets
            idle_sessions_to_remove = [
                key for key, info in self.active_sessions.items() 
                if current_time - info["last_packet_time"] > self.settings.idle_timeout_seconds
            ]
            for session_key in idle_sessions_to_remove:
                session_info = self.active_sessions.pop(session_key, None)
                if session_info:
                    self.logger.info(f"[{session_info['target_container']}] Cleaning up idle client session for {session_key[0]}.")
                    self._close_session_sockets(session_info)
                    self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                    self.socket_to_session_map.pop(session_info.get("server_socket"), None)

            # Now, check each server for shutdown conditions
            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                state = self.server_states.get(container_name)

                if not (state and state.get("running")):
                    continue

                # Check if there are any active sessions for this server
                has_active_sessions = any(
                    info["target_container"] == container_name for info in self.active_sessions.values()
                )

                if has_active_sessions:
                    # If there are sessions, reset the server's idle timer.
                    state["last_activity"] = current_time
                    self.logger.debug(f"[{container_name}] Server has active sessions. Resetting idle timer.")
                else:
                    # No sessions, now check if the idle timeout has passed
                    if (current_time - state.get("last_activity", 0) > self.settings.idle_timeout_seconds):
                        self.logger.info(f"[{container_name}] Idle for over {self.settings.idle_timeout_seconds}s with 0 sessions. Initiating shutdown.")
                        self._stop_minecraft_server(container_name)
    
    def _close_session_sockets(self, session_info):
        """Helper to safely close sockets associated with a session and remove from inputs."""
        client_socket = session_info.get("client_socket")
        server_socket = session_info.get("server_socket")
        protocol = session_info.get("protocol")

        # For TCP, the client_socket is a unique connection socket that must be closed.
        # For UDP, the client_socket is the shared listener, which must NOT be closed here.
        if protocol == 'tcp' and client_socket:
            if client_socket in self.inputs:
                self.inputs.remove(client_socket)
            try:
                client_socket.close()
            except socket.error:
                pass # Ignore errors on close

        # The server_socket is always a unique socket (for both TCP and UDP) and should be closed.
        if server_socket:
            if server_socket in self.inputs:
                self.inputs.remove(server_socket)
            try:
                server_socket.close()
            except socket.error:
                pass # Ignore errors on close


    def _run_proxy_loop(self):
        """The main packet forwarding loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")

        while True:
            try:
                readable, _, _ = select.select(self.inputs, [], [], 0.1) # Short timeout for background tasks

                current_time = time.time()
                if current_time - self.last_heartbeat_time > self.settings.proxy_heartbeat_interval_seconds:
                    try:
                        HEARTBEAT_FILE.write_text(str(int(current_time)))
                        self.last_heartbeat_time = current_time
                        self.logger.debug("Proxy heartbeat updated.")
                    except Exception as e:
                        self.logger.warning(f"Could not update heartbeat file at {HEARTBEAT_FILE}: {e}")

                for sock in readable:
                    # --- 1. Handle new TCP connections on a listening socket ---
                    if sock.type == socket.SOCK_STREAM and sock in self.listen_sockets.values():
                        conn, client_addr = sock.accept()
                        conn.setblocking(False)
                        self.inputs.append(conn)

                        server_port = sock.getsockname()[1]
                        server_config = self.servers_config_map[server_port]
                        container_name = server_config.container_name

                        self.logger.info(f"Accepted new TCP connection from {client_addr} for '{server_config.name}'.")

                        # Immediately start the server if it's not running
                        if not self._is_container_running(container_name):
                            self._start_minecraft_server(container_name)

                        # Create a backend socket to connect to the Minecraft server
                        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        server_sock.setblocking(False)
                        try:
                            # Use container name for Docker DNS resolution
                            server_sock.connect_ex((container_name, server_config.internal_port))
                        except socket.gaierror:
                             self.logger.error(f"[{container_name}] DNS resolution failed. Is the container on the correct Docker network?")
                             # Clean up client connection
                             self.inputs.remove(conn)
                             conn.close()
                             continue
                        
                        self.inputs.append(server_sock)

                        session_key = (client_addr, server_port, 'tcp')
                        session_info = {
                            "client_socket": conn,
                            "server_socket": server_sock,
                            "target_container": container_name,
                            "last_packet_time": time.time(),
                            "listen_port": server_port,
                            "protocol": 'tcp'
                        }
                        self.active_sessions[session_key] = session_info
                        self.socket_to_session_map[conn] = (session_key, 'client_socket')
                        self.socket_to_session_map[server_sock] = (session_key, 'server_socket')
                        continue

                    # --- 2. Handle new UDP packets on a listening socket ---
                    elif sock.type == socket.SOCK_DGRAM and sock in self.listen_sockets.values():
                        data, client_addr = sock.recvfrom(4096)
                        server_port = sock.getsockname()[1]
                        server_config = self.servers_config_map[server_port]
                        container_name = server_config.container_name

                        session_key = (client_addr, server_port, 'udp')

                        if session_key not in self.active_sessions:
                            self.logger.info(f"Establishing new UDP session for {client_addr} for '{server_config.name}'.")
                            
                            # Create a unique backend socket for this session
                            server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            server_sock.setblocking(False)
                            self.inputs.append(server_sock)

                            session_info = {
                                "client_socket": sock, # The shared listener
                                "server_socket": server_sock, # The unique backend socket
                                "target_container": container_name,
                                "last_packet_time": time.time(),
                                "listen_port": server_port,
                                "protocol": 'udp'
                            }
                            self.active_sessions[session_key] = session_info
                            # Map the *backend* socket to the session
                            self.socket_to_session_map[server_sock] = (session_key, 'server_socket')

                            # Start server if not running
                            if not self._is_container_running(container_name):
                                self.logger.info(f"[{container_name}] First UDP packet from {client_addr}. Starting server...")
                                self._start_minecraft_server(container_name)

                        # Get the session and forward the packet
                        session_info = self.active_sessions[session_key]
                        session_info["last_packet_time"] = time.time()
                        self.server_states[container_name]["last_activity"] = time.time()
                        
                        try:
                            # Forward packet from client to the backend server via the session's server_socket
                            session_info["server_socket"].sendto(data, (container_name, server_config.internal_port))
                        except Exception as e:
                            self.logger.warning(f"[{container_name}] Error forwarding UDP packet to backend: {e}")
                        continue

                    # --- 3. Handle data on an existing client or server socket ---
                    session_info_tuple = self.socket_to_session_map.get(sock)
                    if not session_info_tuple:
                        # This can happen if a socket receives data after its session has been cleaned up
                        self.logger.debug(f"Received data on a socket {sock.getsockname()} with no active session. Closing it.")
                        if sock in self.inputs:
                            self.inputs.remove(sock)
                        sock.close()
                        continue

                    session_key, socket_role = session_info_tuple
                    session_info = self.active_sessions.get(session_key)

                    if not session_info:
                        self.logger.debug(f"Socket's session {session_key} no longer active. Closing socket.")
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
                        
                        # --- START OF FIX ---
                        # Forward the data and update server activity timestamp in one motion
                        if socket_role == 'client_socket': # Data from client -> server
                            self.server_states[container_name]["last_activity"] = time.time()
                            session_info["server_socket"].sendall(data) if protocol == 'tcp' else session_info["server_socket"].sendto(data, (container_name, self.servers_config_map[session_info["listen_port"]].internal_port))
                        elif socket_role == 'server_socket': # Data from server -> client
                            self.server_states[container_name]["last_activity"] = time.time()
                            if protocol == 'tcp':
                                session_info["client_socket"].sendall(data)
                            else: # UDP
                                client_addr_original = session_key[0]
                                session_info["client_socket"].sendto(data, client_addr_original)
                        # --- END OF FIX ---

                    except (ConnectionResetError, socket.error, OSError) as e:
                        self.logger.warning(f"Session {session_key[0]} ({protocol}) disconnected: {e}. Cleaning up.")
                        self._close_session_sockets(session_info)
                        self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                        self.socket_to_session_map.pop(session_info.get("server_socket"), None)
                        self.active_sessions.pop(session_key, None)
            
            except Exception as e:
                self.logger.error(f"An unexpected error occurred in the main proxy loop: {e}", exc_info=True)
                time.sleep(1)

    def run(self):
        """Starts the Nether-bridge proxy application."""
        self.logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

        # Log application build metadata if available
        app_metadata = os.environ.get('APP_IMAGE_METADATA')
        if app_metadata:
            try:
                meta = json.loads(app_metadata)
                self.logger.info(f"Version: {meta.get('version', 'N/A')}, Build Date: {meta.get('build_date', 'N/A')}, Commit: {meta.get('commit', 'N/A')}")
            except json.JSONDecodeError:
                self.logger.warning(f"Could not parse APP_IMAGE_METADATA: {app_metadata}")

        # Clean up old heartbeat file on start.
        if HEARTBEAT_FILE.exists():
            try:
                HEARTBEAT_FILE.unlink()
                self.logger.info(f"Removed stale heartbeat file: {HEARTBEAT_FILE}")
            except OSError as e:
                self.logger.warning(f"Could not remove stale heartbeat file {HEARTBEAT_FILE}: {e}")


        # Connect to Docker
        self._connect_to_docker()

        # Ensure a clean state on startup
        self._ensure_all_servers_stopped_on_startup()

        # Bind sockets for listening
        for listen_port, srv_cfg in self.servers_config_map.items():
            if srv_cfg.server_type == 'bedrock':
                sock_type = socket.SOCK_DGRAM # UDP for Bedrock
                protocol_str = 'UDP'
            elif srv_cfg.server_type == 'java':
                sock_type = socket.SOCK_STREAM # TCP for Java gameplay
                protocol_str = 'TCP'
            else:
                self.logger.error(f"Unknown server type '{srv_cfg.server_type}' for port {listen_port}. Skipping.")
                continue

            sock = socket.socket(socket.AF_INET, sock_type)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                self.logger.debug(f"Attempting to bind {protocol_str} socket to 0.0.0.0:{listen_port}...")
                sock.bind(('0.0.0.0', listen_port))
                self.logger.debug(f"{protocol_str} socket bound to 0.0.0.0:{listen_port}.")
                
                if sock_type == socket.SOCK_STREAM:
                    self.logger.debug(f"Attempting to listen on TCP socket 0.0.0.0:{listen_port}...")
                    sock.listen(5)
                    self.logger.debug(f"TCP socket listening on 0.0.0.0:{listen_port}.")
                
                sock.setblocking(False)

                self.listen_sockets[listen_port] = sock
                self.inputs.append(sock)
                self.logger.info(f"Proxy listening for '{srv_cfg.name}' on port {listen_port} ({protocol_str}) -> forwards to container '{srv_cfg.container_name}'")
            except OSError as e:
                self.logger.critical(f"FATAL: Could not bind to port {listen_port}. Is it already in use? ({e.errno}) - {e.strerror}", exc_info=True) 
                sys.exit(1)
            except Exception as e:
                self.logger.critical(f"FATAL: Unexpected error during socket binding for port {listen_port}: {e}", exc_info=True)
                sys.exit(1)

        # Start the background thread for monitoring server activity
        monitor_thread = threading.Thread(target=self._monitor_servers_activity, daemon=True)
        monitor_thread.start()

        # Start the main proxy loop
        self._run_proxy_loop()


# --- Health Check Function (outside class as it's a standalone entrypoint for Docker) ---

def perform_health_check():
    """
    Performs a self-sufficient two-stage health check.
    1. Checks if configuration is available (via environment variables or servers.json).
    2. Checks if the main process heartbeat is recent.
    """
    logger = logging.getLogger(__name__)

    # Stage 1: Check for a valid configuration.
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(f"Health Check FAIL: Error loading configuration: {e}")
        sys.exit(1)

    # Stage 2: If configured, check for a live heartbeat from the main process.
    HEALTHCHECK_STALE_THRESHOLD_SECONDS_DEFAULT = 60 
    proxy_settings_for_healthcheck = None
    try:
        proxy_settings_for_healthcheck, _ = load_application_config()
    except Exception:
        pass 

    healthcheck_threshold = HEALTHCHECK_STALE_THRESHOLD_SECONDS_DEFAULT
    if proxy_settings_for_healthcheck and hasattr(proxy_settings_for_healthcheck, 'healthcheck_stale_threshold_seconds'):
        healthcheck_threshold = proxy_settings_for_healthcheck.healthcheck_stale_threshold_seconds

    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found (main process may be starting or crashed).")
        sys.exit(1)

    try:
        last_heartbeat = int(HEARTBEAT_FILE.read_text())
        current_time = int(time.time())
        age = current_time - last_heartbeat

        if age < healthcheck_threshold:
            logger.info(f"Health Check OK: Heartbeat is {age} seconds old.")
            sys.exit(0)
        else:
            logger.error(f"Health Check FAIL: Heartbeat is stale ({age} seconds old). Threshold: {healthcheck_threshold}s.")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Health Check FAIL: Could not read or parse heartbeat file. Error: {e}")
        sys.exit(1)


# --- Configuration Loading Functions ---

def _load_settings_from_json(file_path: Path) -> dict:
    """Loads settings from a JSON file."""
    logger = logging.getLogger(__name__)
    try:
        with open(file_path, 'r') as f:
            settings_from_file = json.load(f)
            logger.info(f"Loaded settings from {file_path}.")
            return settings_from_file
    except FileNotFoundError:
        logger.info(f"Settings file '{file_path}' not found. Using defaults and environment variables.")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error: '{file_path}' is not valid JSON and will be ignored. Error: {e}")
        return {}

def _load_servers_from_json(file_path: Path) -> list[dict]:
    """Loads server configurations from a JSON file."""
    logger = logging.getLogger(__name__)
    try:
        with open(file_path, 'r') as f:
            servers_json_config = json.load(f)
            servers_list_dicts = servers_json_config.get('servers', [])
            logger.info(f"Loaded server definitions from {file_path}.")
            return servers_list_dicts
    except FileNotFoundError:
        logger.info(f"Servers file '{file_path}' not found. Using environment variables for server definitions.")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error: '{file_path}' is not valid JSON and will be ignored. Error: {e}")
        return {}

def _load_servers_from_env() -> list[dict]:
    """
    Loads server configurations from indexed environment variables.
    e.g., NB_1_LISTEN_PORT, NB_2_LISTEN_LISTEN_PORT, etc.
    """
    logger = logging.getLogger(__name__)
    env_servers = []
    i = 1
    while True:
        listen_port_str = os.environ.get(f'NB_{i}_LISTEN_PORT')
        if not listen_port_str:
            break

        try:
            server_def = {
                "name": os.environ.get(f'NB_{i}_NAME', f"Server {i}"),
                "server_type": os.environ.get(f'NB_{i}_SERVER_TYPE', 'bedrock').lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f'NB_{i}_CONTAINER_NAME'),
                "internal_port": int(os.environ.get(f'NB_{i}_INTERNAL_PORT'))
            }
            if not all(v is not None for v in [server_def['container_name'], server_def['internal_port']]):
                 raise ValueError(f"Incomplete definition for server index {i}. 'container_name' and 'internal_port' are required.")
            if server_def['server_type'] not in ['bedrock', 'java']:
                raise ValueError(f"Invalid 'server_type' for server index {i}: must be 'bedrock' or 'java'.")
            env_servers.append(server_def)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid or incomplete server definition for index {i} in environment variables. Skipping. Error: {e}")
        i += 1
    return env_servers


def load_application_config() -> tuple[ProxySettings, list[ServerConfig]]:
    """
    Loads all application settings and server configurations, prioritizing:
    Environment Variables > JSON Files > Default Values.
    """
    logger = logging.getLogger(__name__)

    settings_from_json = _load_settings_from_json(Path('settings.json'))
    
    final_settings = {}
    for key, default_val in DEFAULT_SETTINGS.items():
        env_var_name = key.upper() 
        if key == "log_level":
            env_var_name = "LOG_LEVEL"
        elif key == "idle_timeout_seconds":
            env_var_name = "NB_IDLE_TIMEOUT"
        elif key == "player_check_interval_seconds":
            env_var_name = "NB_PLAYER_CHECK_INTERVAL"
        elif key == "query_timeout_seconds":
            env_var_name = "NB_QUERY_TIMEOUT"
        elif key == "server_ready_max_wait_time_seconds":
            env_var_name = "NB_SERVER_READY_MAX_WAIT"
        elif key == "initial_boot_ready_max_wait_time_seconds":
            env_var_name = "NB_INITIAL_BOOT_READY_MAX_WAIT"
        elif key == "server_startup_delay_seconds":
            env_var_name = "NB_SERVER_STARTUP_DELAY"
        elif key == "initial_server_query_delay_seconds":
            env_var_name = "NB_INITIAL_SERVER_QUERY_DELAY"
        elif key == "healthcheck_stale_threshold_seconds":
            env_var_name = "NB_HEALTHCHECK_STALE_THRESHOLD"
        elif key == "proxy_heartbeat_interval_seconds":
            env_var_name = "NB_HEARTBEAT_INTERVAL"

        env_val = os.environ.get(env_var_name)
        if env_val is not None:
            try:
                if isinstance(default_val, int):
                    final_settings[key] = int(env_val)
                elif isinstance(default_val, bool):
                    final_settings[key] = env_val.lower() == 'true'
                else: # string
                    final_settings[key] = env_val
                logger.debug(f"Setting '{key}' loaded from env var '{env_var_name}'.")
            except ValueError:
                logger.warning(f"Invalid type for env var '{env_var_name}' ('{env_val}'). Falling back to JSON/default.")
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)

    proxy_settings = ProxySettings(**final_settings)
    logger.info(f"Loaded Proxy Settings: {proxy_settings}")

    servers_list_raw = _load_servers_from_env()
    if not servers_list_raw:
        logger.info("No server definitions found in environment variables. Attempting to load from servers.json.")
        servers_list_raw = _load_servers_from_json(Path('servers.json'))

    final_servers: list[ServerConfig] = []
    for srv_dict in servers_list_raw:
        try:
            final_servers.append(ServerConfig(**srv_dict))
        except TypeError as e:
            logger.error(f"Failed to load server definition {srv_dict}: Missing or invalid field. Error: {e}")

    if not final_servers:
        logger.critical("FATAL: No server configurations loaded. Please define servers via environment variables (e.g., NB_1_...) or a servers.json file.")

    return proxy_settings, final_servers


# --- Main Execution ---
if __name__ == "__main__":
    LOG_LEVEL_EARLY = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL_EARLY, logging.INFO),
        format='%(asctime)s - %(levelname)-8s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logger = logging.getLogger(__name__)

    if '--healthcheck' in sys.argv:
        perform_health_check()
        sys.exit(0)
    
    settings, servers = load_application_config()

    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.info(f"Log level set to {settings.log_level}.")

    if not servers:
        logger.critical("Entering dormant, unhealthy state due to missing server configurations.")
        HEARTBEAT_FILE.write_text("0")
        while True:
            time.sleep(3600)

    proxy = NetherBridgeProxy(settings, servers)
    proxy.run()