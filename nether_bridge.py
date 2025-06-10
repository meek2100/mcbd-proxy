"""
Nether-bridge: On-Demand Game Server Proxy

An intelligent UDP proxy for Minecraft game servers running in Docker. This script
dynamically starts and stops Dockerized game servers (e.g., Minecraft)
based on player activity to conserve system resources. It supports multiple
servers, both Bedrock and Java editions, flexible configuration, and a
robust health check.

Author: meek2100 (github.com/meek2100)
"""
import socket
import subprocess
import time
import docker
import os
import threading
import json
import select
import sys
from collections import defaultdict
import logging
from mcstatus import BedrockServer, JavaServer
from pathlib import Path

# --- Constants ---
HEARTBEAT_FILE = Path("/tmp/proxy_heartbeat")
HEALTHCHECK_STALE_THRESHOLD_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 15
DEBUG_FLAG_PATH = "/app/local_debug.flag"

# --- Health Check Function ---
def perform_health_check():
    """
    Performs a self-sufficient two-stage health check.
    1. Checks if configuration is available.
    2. Checks if the main process heartbeat is recent.
    """
    # Stage 1: Check for a valid configuration.
    local_servers_list = load_servers_from_env()
    if not local_servers_list:
        try:
            with open('proxy_config.json', 'r') as f:
                local_file_config = json.load(f)
            local_servers_list = local_file_config.get('servers', [])
        except (FileNotFoundError, json.JSONDecodeError):
            local_servers_list = []

    if not local_servers_list:
        print("Health Check FAIL: No server configuration found.")
        sys.exit(1)

    # Stage 2: If configured, check for a live heartbeat from the main process.
    if not HEARTBEAT_FILE.is_file():
        print("Health Check FAIL: Heartbeat file not found (main process may be starting).")
        sys.exit(1)

    try:
        last_heartbeat = int(HEARTBEAT_FILE.read_text())
        current_time = int(time.time())
        age = current_time - last_heartbeat

        if age < HEALTHCHECK_STALE_THRESHOLD_SECONDS:
            print(f"Health Check OK: Heartbeat is {age} seconds old.")
            sys.exit(0)
        else:
            print(f"Health Check FAIL: Heartbeat is stale ({age} seconds old).")
            sys.exit(1)
    except Exception as e:
        print(f"Health Check FAIL: Could not read or parse heartbeat file. Error: {e}")
        sys.exit(1)

# --- Configuration Loading ---
file_config = {}
try:
    with open('proxy_config.json', 'r') as f:
        file_config = json.load(f)
except FileNotFoundError:
    # This is not an error, as env vars are the primary method
    pass
except json.JSONDecodeError:
    # This is an error and should be logged if the file exists
    logging.getLogger(__name__).error("Error: proxy_config.json is not valid JSON. Using environment variables or default values only.")


def get_config_value(env_var_name, json_key_name, default_value, type_converter=str):
    """Loads a configuration value from an environment variable or a JSON file, with a fallback to a default."""
    env_val = os.environ.get(env_var_name)
    if env_val is not None:
        try:
            return type_converter(env_val)
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning(f"Invalid type for environment variable {env_var_name}='{env_val}'. Using file config or default.")

    file_val = file_config.get(json_key_name)
    if file_val is not None:
        try:
            return type_converter(file_val)
        except (ValueError, TypeError):
             logging.getLogger(__name__).warning(f"Invalid type for JSON key {json_key_name}='{file_val}'. Using default.")

    return default_value

def load_servers_from_env():
    """
    Loads server configurations from indexed environment variables.
    e.g., NETHER_BRIDGE_SERVER_1_LISTEN_PORT, NETHER_BRIDGE_SERVER_2_LISTEN_PORT, etc.
    """
    env_servers = []
    i = 1
    while True:
        listen_port_str = os.environ.get(f'NETHER_BRIDGE_SERVER_{i}_LISTEN_PORT')
        if not listen_port_str:
            break
        try:
            server_def = {
                "name": os.environ.get(f'NETHER_BRIDGE_SERVER_{i}_NAME', f"Server {i}"),
                "server_type": os.environ.get(f'NETHER_BRIDGE_SERVER_{i}_SERVER_TYPE', 'bedrock').lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f'NETHER_BRIDGE_SERVER_{i}_CONTAINER_NAME'),
                "internal_port": int(os.environ.get(f'NETHER_BRIDGE_SERVER_{i}_INTERNAL_PORT'))
            }
            if not all(v is not None for v in [server_def['container_name'], server_def['internal_port']]):
                 raise ValueError(f"Incomplete definition for server index {i}. 'container_name' and 'internal_port' are required.")
            if server_def['server_type'] not in ['bedrock', 'java']:
                raise ValueError(f"Invalid 'server_type' for server index {i}: must be 'bedrock' or 'java'.")
            env_servers.append(server_def)
        except (ValueError, TypeError, AttributeError) as e:
            logging.getLogger(__name__).error(f"Invalid or incomplete server definition for server index {i}. Skipping. Error: {e}")
        i += 1
    return env_servers


# --- Main Application ---
# Top-level variables and objects
IDLE_TIMEOUT_SECONDS = get_config_value('NETHER_BRIDGE_IDLE_TIMEOUT_SECONDS', 'idle_timeout_seconds', 600, int)
PLAYER_CHECK_INTERVAL_SECONDS = get_config_value('NETHER_BRIDGE_PLAYER_CHECK_INTERVAL_SECONDS', 'player_check_interval_seconds', 60, int)
QUERY_TIMEOUT_SECONDS = get_config_value('NETHER_BRIDGE_QUERY_TIMEOUT_SECONDS', 'query_timeout_seconds', 5, int)
SERVER_READY_MAX_WAIT_TIME_SECONDS = get_config_value('NETHER_BRIDGE_SERVER_READY_MAX_WAIT_TIME_SECONDS', 'server_ready_max_wait_time_seconds', 120, int)
INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS = get_config_value('NETHER_BRIDGE_INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS', 'initial_boot_ready_max_wait_time_seconds', 180, int)
SERVER_STARTUP_DELAY_SECONDS = get_config_value('NETHER_BRIDGE_SERVER_STARTUP_DELAY_SECONDS', 'server_startup_delay_seconds', 5, int)
INITIAL_SERVER_QUERY_DELAY_SECONDS = get_config_value('NETHER_BRIDGE_INITIAL_SERVER_QUERY_DELAY_SECONDS', 'initial_server_query_delay_seconds', 10, int)

servers_list = load_servers_from_env()
if not servers_list:
    logging.getLogger(__name__).info("No server definitions found in environment variables. Falling back to proxy_config.json.")
    servers_list = file_config.get('servers', [])

SERVERS_CONFIG = {s['listen_port']: s for s in servers_list if s.get('listen_port')}
docker_client = None
server_states = {}
active_client_connections = {}
clients_per_server = defaultdict(set)
packet_buffers = defaultdict(list)

def is_container_running(container_name):
    """Checks if a Docker container is currently running via the Docker API."""
    try:
        container = docker_client.containers.get(container_name)
        return container.status == 'running'
    except docker.errors.NotFound:
        return False
    except docker.errors.APIError as e:
        logging.getLogger(__name__).error(f"Docker API error checking container {container_name}: {e}")
        return False
    except Exception as e:
        logging.getLogger(__name__).error(f"Unexpected error checking container {container_name}: {e}")
        return False

def wait_for_server_query_ready(server_config, max_wait_time_seconds, query_timeout_seconds):
    """Polls a game server using mcstatus until it responds or a timeout is reached."""
    container_name = server_config['container_name']
    target_ip = container_name # Docker DNS resolves container name to IP
    target_port = server_config['internal_port']
    server_type = server_config.get('server_type', 'bedrock')

    logging.getLogger(__name__).info(f"Waiting for {container_name} ({server_type}) to respond to query at {target_ip}:{target_port} (max {max_wait_time_seconds}s)...")
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
                logging.getLogger(__name__).info(f"{container_name} responded to query. Latency: {status.latency:.2f}ms. Ready!")
                return True
        except Exception as e:
            logging.getLogger(__name__).debug(f"Query to {container_name} ({target_ip}:{target_port}) failed: {e}. Retrying...")
        time.sleep(query_timeout_seconds)

    logging.getLogger(__name__).error(f"Timeout waiting for {container_name} to respond after {max_wait_time_seconds} seconds. Proceeding anyway.")
    return False

def start_server_container(container_name):
    """Starts a game server container and waits for it to become ready."""
    if not is_container_running(container_name):
        logging.getLogger(__name__).info(f"Starting game server: {container_name}...")
        result = subprocess.run(["/app/scripts/start-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logging.getLogger(__name__).error(f"Error starting {container_name}: {result.stderr}")
            return False
        logging.getLogger(__name__).info(result.stdout.strip())

        time.sleep(SERVER_STARTUP_DELAY_SECONDS)

        target_server_config = next((s for s in SERVERS_CONFIG.values() if s['container_name'] == container_name), None)
        if not target_server_config:
            logging.getLogger(__name__).error(f"Config for {container_name} not found. Cannot query for readiness.")
            return False

        if not wait_for_server_query_ready(target_server_config, SERVER_READY_MAX_WAIT_TIME_SECONDS, QUERY_TIMEOUT_SECONDS):
            logging.getLogger(__name__).warning(f"Server {container_name} did not respond to query within max wait time. Proceeding with traffic forwarding anyway.")

        server_states[container_name]["running"] = True
        server_states[container_name]["last_activity"] = time.time()
        logging.getLogger(__name__).info(f"Server {container_name} startup process complete. Ready for traffic.")
        return True
    return False

def stop_server_container(container_name):
    """Stops a game server container."""
    if is_container_running(container_name):
        logging.getLogger(__name__).info(f"Stopping game server: {container_name}...")
        result = subprocess.run(["/app/scripts/stop-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logging.getLogger(__name__).error(f"Error stopping {container_name}: {result.stderr}")
            return False
        logging.getLogger(__name__).info(result.stdout.strip())
        server_states[container_name]["running"] = False
        logging.getLogger(__name__).info(f"Server {container_name} stopped.")
        return True
    return False

def ensure_all_servers_stopped_on_startup():
    """Ensures all managed servers are stopped when the proxy starts."""
    logger = logging.getLogger(__name__)
    logger.info("Proxy startup: Ensuring all managed game servers are initially stopped.")
    for srv_conf in SERVERS_CONFIG.values():
        container_name = srv_conf['container_name']
        if is_container_running(container_name):
            logger.info(f"Found {container_name} running at proxy startup. Waiting for it to fully start before issuing a safe stop.")
            time.sleep(INITIAL_SERVER_QUERY_DELAY_SECONDS)
            if not wait_for_server_query_ready(srv_conf, INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS, QUERY_TIMEOUT_SECONDS):
                logger.warning(f"{container_name} did not respond to query during initial startup. Attempting to force stop anyway.")
            stop_server_container(container_name)
        else:
            logger.info(f"{container_name} is already stopped.")

def monitor_servers_activity():
    """Periodically checks running servers for player count and idle time."""
    logger = logging.getLogger(__name__)
    while True:
        time.sleep(PLAYER_CHECK_INTERVAL_SECONDS)
        current_time = time.time()
        for server_conf in SERVERS_CONFIG.values():
            server_name = server_conf['container_name']
            state = server_states.get(server_name)
            if not (state and state["running"]):
                continue

            active_players_on_server = 0
            try:
                status = None
                server_type = server_conf.get('server_type', 'bedrock')
                target_ip = server_name
                target_port = server_conf['internal_port']

                if server_type == 'bedrock':
                    server = BedrockServer.lookup(f"{target_ip}:{target_port}", timeout=QUERY_TIMEOUT_SECONDS)
                    status = server.status()
                elif server_type == 'java':
                    server = JavaServer.lookup(f"{target_ip}:{target_port}", timeout=QUERY_TIMEOUT_SECONDS)
                    status = server.status()
                
                if status and status.players:
                    active_players_on_server = status.players.online
            except Exception as e:
                logger.debug(f"Failed to query {server_name} for player count: {e}. Assuming 0 players for now.")

            if active_players_on_server == 0 and (current_time - state["last_activity"] > IDLE_TIMEOUT_SECONDS):
                logger.info(f"Server {server_name} idle for over {IDLE_TIMEOUT_SECONDS}s with 0 players. Initiating shutdown.")
                stop_server_container(server_name)
            elif active_players_on_server > 0:
                logger.debug(f"{server_name} has {active_players_on_server} players. Resetting idle timer.")
                state["last_activity"] = current_time

def run_proxy(client_listen_sockets, inputs):
    """The main packet forwarding loop of the proxy."""
    global active_client_connections, clients_per_server, packet_buffers
    logger = logging.getLogger(__name__)
    last_heartbeat_time = time.time()
    while True:
        try:
            readable, _, _ = select.select(inputs, [], [], 0.05)

            current_time = time.time()
            if current_time - last_heartbeat_time > HEARTBEAT_INTERVAL_SECONDS:
                try:
                    HEARTBEAT_FILE.write_text(str(int(current_time)))
                    last_heartbeat_time = current_time
                    logger.debug("Proxy heartbeat updated.")
                except Exception as e:
                    logger.warning(f"Could not update heartbeat file at {HEARTBEAT_FILE}: {e}")

            for sock in readable:
                is_from_client = any(sock == s for s in client_listen_sockets.values())
                if is_from_client:
                    try:
                        data, client_addr = sock.recvfrom(4096)
                    except Exception as e:
                        logger.error(f"Error receiving from client socket {sock}: {e}")
                        continue
                    
                    server_port = sock.getsockname()[1]
                    server_config = SERVERS_CONFIG[server_port]
                    container_name = server_config['container_name']
                    server_states[container_name]["last_activity"] = time.time()
                    
                    if not is_container_running(container_name):
                        logger.info(f"Server {container_name} not running. Starting for {client_addr} and buffering initial packet.")
                        start_server_container(container_name)
                        packet_buffers[(client_addr, server_port)].append(data)
                        continue
                    
                    session_key = (client_addr, server_port)
                    if session_key not in active_client_connections:
                        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        server_sock.setblocking(False)
                        active_client_connections[session_key] = {
                            "target_container": container_name,
                            "client_to_server_socket": server_sock,
                            "last_packet_time": time.time(),
                            "listen_port": server_port
                        }
                        inputs.append(server_sock)
                        logger.info(f"New client session {session_key} established with {container_name}")
                        # Forward any buffered packets first
                        for buffered_packet in packet_buffers.pop(session_key, []):
                            server_sock.sendto(buffered_packet, (container_name, server_config['internal_port']))
                        server_sock.sendto(data, (container_name, server_config['internal_port']))
                    else:
                        conn_info = active_client_connections[session_key]
                        conn_info["client_to_server_socket"].sendto(data, (container_name, server_config['internal_port']))
                        conn_info["last_packet_time"] = time.time()
                else: # Data is from a game server
                    found_session_key = None
                    for key, conn_info in active_client_connections.items():
                        if conn_info["client_to_server_socket"] is sock:
                            found_session_key = key
                            break
                    if found_session_key:
                        data, _ = sock.recvfrom(4096)
                        conn_info = active_client_connections[found_session_key]
                        client_facing_socket = client_listen_sockets[conn_info["listen_port"]]
                        client_addr_original = found_session_key[0]
                        client_facing_socket.sendto(data, client_addr_original)
                        conn_info["last_packet_time"] = time.time()
                    else:
                        logger.warning(f"Received unexpected data on backend socket {sock}. Session not found. Closing socket.")
                        inputs.remove(sock)
                        sock.close()
        except Exception as e:
            logger.error(f"An unexpected error occurred in the main proxy loop: {e}", exc_info=True)
            time.sleep(1)

        # Cleanup idle sessions
        current_time = time.time()
        sessions_to_remove = [key for key, info in active_client_connections.items() if current_time - info["last_packet_time"] > IDLE_TIMEOUT_SECONDS]
        for session_key in sessions_to_remove:
            conn_info = active_client_connections.pop(session_key)
            logger.info(f"Client session {session_key} idle for >{IDLE_TIMEOUT_SECONDS}s. Disconnecting from {conn_info['target_container']}.")
            inputs.remove(conn_info["client_to_server_socket"])
            conn_info["client_to_server_socket"].close()
            packet_buffers.pop(session_key, None)


# --- Main Execution ---
if __name__ == "__main__":
    if '--healthcheck' in sys.argv:
        perform_health_check()
        sys.exit(0)
    
    # --- Step 1: Auto-detect if running from a local volume mount ---
    IS_LOCAL_OVERRIDE = os.path.exists(DEBUG_FLAG_PATH)

    # --- Step 2: Determine Log Level based on override status and environment ---
    explicit_log_level = os.environ.get('LOG_LEVEL')
    final_log_level = 'INFO'  # The production default

    if explicit_log_level:
        # An explicitly set LOG_LEVEL always wins.
        final_log_level = explicit_log_level.upper()
    elif IS_LOCAL_OVERRIDE:
        # If no log level is set AND we are in override mode, default to DEBUG.
        final_log_level = 'DEBUG'

    # --- Step 3: Configure Logging ---
    logging.basicConfig(
        level=getattr(logging, final_log_level, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    # --- Step 4: Announce Execution Mode and Final Log Level ---
    if IS_LOCAL_OVERRIDE:
        logger.warning("==========================================================")
        logger.warning("===  RUNNING FROM LOCAL OVERRIDE (DEBUG MODE)          ===")
        logger.warning(f"===  Log Level: {final_log_level:<35} ===")
        logger.warning("==========================================================")
    else:
        logger.info(f"--- Running from Docker Image (Production Mode) | Log Level: {final_log_level} ---")
    
    # --- Normal Startup Sequence ---
    logger.info("Starting Nether-bridge on-demand proxy...")

    # Clean up old heartbeat file on start.
    if HEARTBEAT_FILE.exists():
        HEARTBEAT_FILE.unlink()

    if not SERVERS_CONFIG:
        logger.error("FATAL: No server configurations loaded. Please configure servers via environment variables or proxy_config.json. Entering dormant, unhealthy state.")
        # Create a stale heartbeat file to ensure health checks fail correctly
        HEARTBEAT_FILE.write_text("0")
        while True:
            time.sleep(3600)

    try:
        docker_client = docker.from_env()
    except Exception as e:
        logger.error(f"Error connecting to Docker daemon: {e}. Ensure /var/run/docker.sock is mounted.")
        sys.exit(1)

    server_states = {s['container_name']: {"running": False, "last_activity": time.time()} for s in SERVERS_CONFIG.values()}

    ensure_all_servers_stopped_on_startup()

    client_listen_sockets = {}
    inputs = []
    for listen_port, srv_cfg in SERVERS_CONFIG.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', listen_port))
            sock.setblocking(False)
            client_listen_sockets[listen_port] = sock
            inputs.append(sock)
            logger.info(f"Proxy listening for players on port {listen_port} -> {srv_cfg['container_name']} ({srv_cfg.get('server_type', 'bedrock')})")
        except OSError as e:
            logger.error(f"ERROR: Could not bind to port {listen_port}. Is it already in use? ({e})")
            sys.exit(1)

    monitor_thread = threading.Thread(target=monitor_servers_activity, daemon=True)
    monitor_thread.start()

    run_proxy(client_listen_sockets, inputs)
