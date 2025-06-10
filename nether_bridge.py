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
    "log_level": "INFO" # Default log level
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
        self.active_client_connections = {} # (client_addr, listen_port) -> {"target_container": str, "client_to_server_socket": socket, "last_packet_time": float, "listen_port": int}
        self.packet_buffers = defaultdict(list) # (client_addr, listen_port) -> [packets]
        self.client_listen_sockets = {} # listen_port -> socket
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
        if self._is_container_running(container_name):
            self.logger.info(f"[{container_name}] Attempting to stop Minecraft server...")
            try:
                container = self.docker_client.containers.get(container_name)
                container.stop()
                self.server_states[container_name]["running"] = False
                self.logger.info(f"[{container_name}] Server stopped successfully.")
                return True
            except docker.errors.NotFound:
                self.logger.warning(f"[{container_name}] Docker container not found while trying to stop. Assuming already stopped.")
                self.server_states[container_name]["running"] = False
                return True # Consider successful if it's already gone
            except docker.errors.APIError as e:
                self.logger.error(f"[{container_name}] Docker API error during stop: {e}")
                return False
            except Exception as e:
                self.logger.error(f"[{container_name}] Unexpected error during server stop: {e}")
                return False
        self.logger.debug(f"[{container_name}] Server already stopped, no stop action needed.")
        return True # Considered successful if it's already stopped

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

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                state = self.server_states.get(container_name)

                # Skip if the server isn't supposed to be running (state not initialized or explicitly stopped)
                if not (state and state.get("running")):
                    continue

                active_players_on_server = 0
                try:
                    status = None
                    server_type = server_conf.server_type
                    target_ip = container_name
                    target_port = server_conf.internal_port

                    if server_type == 'bedrock':
                        server = BedrockServer.lookup(f"{target_ip}:{target_port}", timeout=self.settings.query_timeout_seconds)
                        status = server.status()
                    elif server_type == 'java':
                        server = JavaServer.lookup(f"{target_ip}:{target_port}", timeout=self.settings.query_timeout_seconds)
                        status = server.status()

                    if status and status.players:
                        active_players_on_server = status.players.online
                except Exception as e:
                    self.logger.debug(f"[{container_name}] Failed to query for player count: {e}. Assuming 0 players for now.")

                if active_players_on_server > 0:
                    self.logger.debug(f"[{container_name}] Found {active_players_on_server} active player(s). Resetting idle timer.")
                    state["last_activity"] = current_time
                elif (current_time - state.get("last_activity", 0) > self.settings.idle_timeout_seconds):
                    self.logger.info(f"[{container_name}] Idle for over {self.settings.idle_timeout_seconds}s with 0 players. Initiating shutdown.")
                    self._stop_minecraft_server(container_name)

    def _run_proxy_loop(self):
        """The main packet forwarding loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")

        while True:
            try:
                readable, _, _ = select.select(self.inputs, [], [], 0.05)

                # Update the heartbeat file for the Docker health check
                current_time = time.time()
                if current_time - self.last_heartbeat_time > HEARTBEAT_INTERVAL_SECONDS:
                    try:
                        HEARTBEAT_FILE.write_text(str(int(current_time)))
                        self.last_heartbeat_time = current_time
                        self.logger.debug("Proxy heartbeat updated.")
                    except Exception as e:
                        self.logger.warning(f"Could not update heartbeat file at {HEARTBEAT_FILE}: {e}")

                for sock in readable:
                    # --- Packet from a Client ---
                    if sock in self.client_listen_sockets.values():
                        try:
                            data, client_addr = sock.recvfrom(4096) # Using a common UDP packet size
                        except Exception as e:
                            self.logger.error(f"Error receiving from client socket {sock.getsockname()}: {e}")
                            continue

                        server_port = sock.getsockname()[1]
                        server_config = self.servers_config_map[server_port]
                        container_name = server_config.container_name
                        self.server_states[container_name]["last_activity"] = time.time()

                        # Start server if it's not running
                        if not self._is_container_running(container_name):
                            self.logger.info(f"[{container_name}] New connection from {client_addr}. Starting server and buffering packet.")
                            self._start_minecraft_server(container_name) # This will block until ready or timeout
                            # Buffer the first packet to be sent after the server is ready
                            self.packet_buffers[(client_addr, server_port)].append(data)
                            continue

                        session_key = (client_addr, server_port)
                        # Establish a new session if one doesn't exist
                        if session_key not in self.active_client_connections:
                            server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            server_sock.setblocking(False)
                            self.active_client_connections[session_key] = {
                                "target_container": container_name,
                                "client_to_server_socket": server_sock,
                                "last_packet_time": time.time(),
                                "listen_port": server_port
                            }
                            self.inputs.append(server_sock)
                            self.logger.info(f"[{container_name}] New client session for {client_addr} established.")

                            # Forward any buffered packets first
                            for buffered_packet in self.packet_buffers.pop(session_key, []):
                                try:
                                    server_sock.sendto(buffered_packet, (container_name, server_config.internal_port))
                                except Exception as e:
                                    self.logger.warning(f"[{container_name}] Error sending buffered packet to {container_name}:{server_config.internal_port}: {e}")

                            # Forward the current packet
                            try:
                                server_sock.sendto(data, (container_name, server_config.internal_port))
                            except Exception as e:
                                self.logger.warning(f"[{container_name}] Error sending initial packet to {container_name}:{server_config.internal_port}: {e}")
                        else:
                            # Forward to an existing session
                            conn_info = self.active_client_connections[session_key]
                            try:
                                conn_info["client_to_server_socket"].sendto(data, (container_name, server_config.internal_port))
                                conn_info["last_packet_time"] = time.time()
                            except Exception as e:
                                self.logger.warning(f"[{container_name}] Error forwarding packet for {client_addr} to {container_name}:{server_config.internal_port}: {e}")

                    # --- Packet from a Minecraft Server ---
                    else:
                        # Find which client session this server-side socket belongs to
                        found_session_key = next((key for key, info in self.active_client_connections.items() if info["client_to_server_socket"] is sock), None)

                        if found_session_key:
                            try:
                                data, _ = sock.recvfrom(4096) # Using a common UDP packet size
                                conn_info = self.active_client_connections[found_session_key]
                                client_facing_socket = self.client_listen_sockets[conn_info["listen_port"]]
                                client_addr_original = found_session_key[0]
                                client_facing_socket.sendto(data, client_addr_original)
                                conn_info["last_packet_time"] = time.time()
                            except Exception as e:
                                self.logger.error(f"Error receiving/forwarding from backend socket {sock.getsockname()}: {e}")
                                # Close the problematic backend socket
                                if sock in self.inputs:
                                    self.inputs.remove(sock)
                                sock.close()
                                # Clean up the session if it's tied to this socket
                                if found_session_key in self.active_client_connections:
                                    self.active_client_connections.pop(found_session_key)
                        else:
                            self.logger.warning(f"Received data on an unexpected backend socket {sock.getsockname()}. Closing it.")
                            if sock in self.inputs:
                                self.inputs.remove(sock)
                            sock.close()

            except Exception as e:
                self.logger.error(f"An unexpected error occurred in the main proxy loop: {e}", exc_info=True)
                time.sleep(1) # Prevent rapid error looping

            # --- Cleanup Idle Sessions ---
            current_time = time.time()
            sessions_to_remove = [
                key for key, info in self.active_client_connections.items()
                if current_time - info["last_packet_time"] > self.settings.idle_timeout_seconds
            ]
            for session_key in sessions_to_remove:
                conn_info = self.active_client_connections.pop(session_key)
                container_name = conn_info['target_container']
                self.logger.info(f"[{container_name}] Client session for {session_key[0]} idle for >{self.settings.idle_timeout_seconds}s. Disconnecting.")
                if conn_info["client_to_server_socket"] in self.inputs:
                    self.inputs.remove(conn_info["client_to_server_socket"])
                conn_info["client_to_server_socket"].close()
                # Clear any stray buffers for this session
                self.packet_buffers.pop(session_key, None)


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
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(('0.0.0.0', listen_port))
                sock.setblocking(False)
                self.client_listen_sockets[listen_port] = sock
                self.inputs.append(sock)
                self.logger.info(f"Proxy listening for '{srv_cfg.name}' on port {listen_port} -> forwards to container '{srv_cfg.container_name}'")
            except OSError as e:
                self.logger.critical(f"FATAL: Could not bind to port {listen_port}. Is it already in use? ({e})")
                sys.exit(1)

        # Start the background thread for monitoring server activity
        monitor_thread = threading.Thread(target=self._monitor_servers_activity, daemon=True)
        monitor_thread.start()

        # Start the main proxy loop
        self._run_proxy_loop()


# --- Health Check Function (outside class as it's a standalone entrypoint for Docker) ---
HEALTHCHECK_STALE_THRESHOLD_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 15

def perform_health_check():
    """
    Performs a self-sufficient two-stage health check.
    1. Checks if configuration is available (via environment variables or servers.json).
    2. Checks if the main process heartbeat is recent.
    """
    logger = logging.getLogger(__name__)

    # Stage 1: Check for a valid configuration.
    # We re-use the loading logic here to ensure configuration is present.
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(f"Health Check FAIL: Error loading configuration: {e}")
        sys.exit(1)

    # Stage 2: Check for a live heartbeat from the main process.
    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found (main process may be starting or crashed).")
        sys.exit(1)

    try:
        last_heartbeat = int(HEARTBEAT_FILE.read_text())
        current_time = int(time.time())
        age = current_time - last_heartbeat

        if age < HEALTHCHECK_STALE_THRESHOLD_SECONDS:
            logger.info(f"Health Check OK: Heartbeat is {age} seconds old.")
            sys.exit(0)
        else:
            logger.error(f"Health Check FAIL: Heartbeat is stale ({age} seconds old). Threshold: {HEALTHCHECK_STALE_THRESHOLD_SECONDS}s.")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Health Check FAIL: Could not read or parse heartbeat file. Error: {e}")
        sys.exit(1)


# --- Configuration Loading Functions ---

def _get_config_value(env_var_name: str, json_key_name: str, default_value, type_converter=str):
    """
    Loads a configuration value based on a priority order:
    1. Environment Variable
    2. JSON file (from pre-loaded settings_config)
    3. Hardcoded Default
    """
    logger = logging.getLogger(__name__)
    env_val = os.environ.get(env_var_name)
    if env_val is not None:
        try:
            logger.debug(f"Configuration '{env_var_name}' loaded from environment variable.")
            return type_converter(env_val)
        except (ValueError, TypeError):
            logger.warning(f"Invalid type for environment variable {env_var_name}='{env_val}'. Falling back.")

    # This function expects settings_config to be passed or accessible if used outside load_application_config
    # For now, it's designed to be called within load_application_config where settings_config is available.
    # We will pass settings_from_json to this function.
    
    # We'll adapt this function slightly to take the pre-loaded JSON dict.
    # This is a helper, not meant to be called directly from outside load_application_config
    pass

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
        return []

def _load_servers_from_env() -> list[dict]:
    """
    Loads server configurations from indexed environment variables.
    e.g., NB_1_LISTEN_PORT, NB_2_LISTEN_PORT, etc.
    """
    logger = logging.getLogger(__name__)
    env_servers = []
    i = 1
    while True:
        # Check for the listen port to determine if the server block exists
        listen_port_str = os.environ.get(f'NB_{i}_LISTEN_PORT')
        if not listen_port_str:
            break

        # Assemble the server definition
        try:
            server_def = {
                "name": os.environ.get(f'NB_{i}_NAME', f"Server {i}"),
                "server_type": os.environ.get(f'NB_{i}_SERVER_TYPE', 'bedrock').lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f'NB_{i}_CONTAINER_NAME'),
                "internal_port": int(os.environ.get(f'NB_{i}_INTERNAL_PORT'))
            }
            # Validate required fields
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

    # 1. Load settings from settings.json
    settings_from_json = _load_settings_from_json(Path('settings.json'))
    
    # 2. Apply environment variables on top of JSON settings and defaults for main settings
    final_settings = {}
    for key, default_val in DEFAULT_SETTINGS.items():
        env_var = key.upper() # Simple conversion, assumes direct mapping for now
        # Special handling for log_level if it's not a direct env var
        if key == "log_level":
            env_var = "LOG_LEVEL"
        
        env_val = os.environ.get(env_var)
        if env_val is not None:
            try:
                if isinstance(default_val, int):
                    final_settings[key] = int(env_val)
                elif isinstance(default_val, bool):
                    final_settings[key] = env_val.lower() == 'true'
                else: # string
                    final_settings[key] = env_val
                logger.debug(f"Setting '{key}' loaded from env var '{env_var}'.")
            except ValueError:
                logger.warning(f"Invalid type for env var '{env_var}' ('{env_val}'). Falling back to JSON/default.")
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)

    proxy_settings = ProxySettings(**final_settings)
    logger.info(f"Loaded Proxy Settings: {proxy_settings}")

    # 3. Load server definitions (Env Vars have highest priority)
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
    # Simplified Logging Setup (before full config load for healthcheck)
    LOG_LEVEL_DEFAULT = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL_DEFAULT, logging.INFO),
        format='%(asctime)s - %(levelname)-8s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logger = logging.getLogger(__name__)

    # Handle healthcheck argument
    if '--healthcheck' in sys.argv:
        perform_health_check()
        sys.exit(0)

    # --- Normal Startup Sequence ---
    settings, servers = load_application_config()

    # Re-configure logging with the determined log level
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.info(f"Log level set to {settings.log_level}.")

    if not servers:
        logger.critical("Entering dormant, unhealthy state due to missing server configurations.")
        # Create a stale heartbeat file to ensure health checks fail correctly
        HEARTBEAT_FILE.write_text("0")
        while True:
            time.sleep(3600) # Sleep indefinitely

    proxy = NetherBridgeProxy(settings, servers)
    proxy.run()