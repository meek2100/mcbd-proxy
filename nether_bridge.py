"""
Nether-bridge: On-Demand Minecraft Server Proxy

An intelligent UDP proxy for Minecraft servers running in Docker. This script
dynamically starts and stops Dockerized Minecraft servers (both Bedrock and
Java editions) based on player activity to conserve system resources. It
supports multiple servers, flexible configuration, and a robust health check.

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
            with open('servers.json', 'r') as f:
                servers_json_config = json.load(f)
            local_servers_list = servers_json_config.get('servers', [])
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
settings_config = {}
try:
    with open('settings.json', 'r') as f:
        settings_config = json.load(f)
except FileNotFoundError:
    # This is not an error, as env vars are the primary method
    pass
except json.JSONDecodeError as e:
    # This is an error and should be logged if the file exists
    logging.getLogger(__name__).error(f"Error: settings.json is not valid JSON and will be ignored. Error: {e}")


def get_config_value(env_var_name, json_key_name, default_value, type_converter=str):
    """
    Loads a configuration value based on a priority order:
    1. Environment Variable
    2. JSON file ('settings.json')
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

    file_val = settings_config.get(json_key_name)
    if file_val is not None:
        try:
            logger.debug(f"Configuration '{json_key_name}' loaded from settings.json.")
            return type_converter(file_val)
        except (ValueError, TypeError):
             logger.warning(f"Invalid type for JSON key {json_key_name}='{file_val}'. Falling back.")

    logger.debug(f"Configuration '{env_var_name}' not found in environment or file. Using default value: {default_value}.")
    return default_value

def load_servers_from_env():
    """
    Loads server configurations from indexed environment variables.
    e.g., NB_1_LISTEN_PORT, NB_2_LISTEN_PORT, etc.
    """
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
            logging.getLogger(__name__).error(f"Invalid or incomplete server definition for index {i} in environment variables. Skipping. Error: {e}")
        i += 1
    return env_servers


# --- Main Application ---
# Top-level variables and objects
IDLE_TIMEOUT_SECONDS = get_config_value('NB_IDLE_TIMEOUT', 'idle_timeout_seconds', 600, int)
PLAYER_CHECK_INTERVAL_SECONDS = get_config_value('NB_PLAYER_CHECK_INTERVAL', 'player_check_interval_seconds', 60, int)
QUERY_TIMEOUT_SECONDS = get_config_value('NB_QUERY_TIMEOUT', 'query_timeout_seconds', 5, int)
SERVER_READY_MAX_WAIT_TIME_SECONDS = get_config_value('NB_SERVER_READY_MAX_WAIT', 'server_ready_max_wait_time_seconds', 120, int)
INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS = get_config_value('NB_INITIAL_BOOT_READY_MAX_WAIT', 'initial_boot_ready_max_wait_time_seconds', 180, int)
SERVER_STARTUP_DELAY_SECONDS = get_config_value('NB_SERVER_STARTUP_DELAY', 'server_startup_delay_seconds', 5, int)
INITIAL_SERVER_QUERY_DELAY_SECONDS = get_config_value('NB_INITIAL_SERVER_QUERY_DELAY', 'initial_server_query_delay_seconds', 10, int)

# Load server definitions from environment variables first, then fall back to servers.json
servers_list = load_servers_from_env()
if not servers_list:
    logging.getLogger(__name__).info("No server definitions found in environment variables. Attempting to load from servers.json.")
    try:
        with open('servers.json', 'r') as f:
            servers_json_config = json.load(f)
        servers_list = servers_json_config.get('servers', [])
    except FileNotFoundError:
        # This is expected if the file doesn't exist
        servers_list = []
    except json.JSONDecodeError as e:
        logging.getLogger(__name__).error(f"Error: servers.json is not valid JSON and will be ignored. Error: {e}")
        servers_list = []

# Create a lookup dictionary from the final list of servers
SERVERS_CONFIG = {s['listen_port']: s for s in servers_list if s.get('listen_port')}
docker_client = None
server_states = {}
active_client_connections = {}
packet_buffers = defaultdict(list)

# (The rest of the script remains the same)
def is_container_running(container_name):
    """Checks if a Docker container is currently running via the Docker API."""
    try:
        container = docker_client.containers.get(container_name)
        return container.status == 'running'
    except docker.errors.NotFound:
        logging.warning(f"[Docker] Container '{container_name}' not found.")
        return False
    except docker.errors.APIError as e:
        logging.error(f"[Docker] API error checking container '{container_name}': {e}")
        return False
    except Exception as e:
        logging.error(f"[Docker] Unexpected error checking container '{container_name}': {e}")
        return False

def wait_for_server_query_ready(server_config, max_wait_time_seconds, query_timeout_seconds):
    """Polls a Minecraft server using mcstatus until it responds or a timeout is reached."""
    container_name = server_config['container_name']
    target_ip = container_name # Docker DNS resolves container name to IP
    target_port = server_config['internal_port']
    server_type = server_config.get('server_type', 'bedrock')
    logger = logging.getLogger(__name__)

    logger.info(f"[{container_name}] Waiting for server to respond to query at {target_ip}:{target_port} (max {max_wait_time_seconds}s)...")
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
                logger.info(f"[{container_name}] Server responded to query. Latency: {status.latency:.2f}ms. Ready!")
                return True
        except Exception as e:
            logger.debug(f"[{container_name}] Query failed: {e}. Retrying in {query_timeout_seconds}s...")
        time.sleep(query_timeout_seconds)

    logger.error(f"[{container_name}] Timeout: Server did not respond after {max_wait_time_seconds} seconds. Proceeding anyway.")
    return False

def start_minecraft_server(container_name):
    """Starts a Minecraft server container and waits for it to become ready."""
    logger = logging.getLogger(__name__)
    if not is_container_running(container_name):
        logger.info(f"[{container_name}] Starting Minecraft server...")
        # Scripts handle their own logging now
        result = subprocess.run(["/app/scripts/start-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[{container_name}] Error during startup script: {result.stderr.strip()}")
            return False
        
        # Give the server a moment to initialize before probing
        time.sleep(SERVER_STARTUP_DELAY_SECONDS)

        target_server_config = next((s for s in SERVERS_CONFIG.values() if s['container_name'] == container_name), None)
        if not target_server_config:
            logger.error(f"[{container_name}] Configuration not found. Cannot query for readiness.")
            return False

        wait_for_server_query_ready(target_server_config, SERVER_READY_MAX_WAIT_TIME_SECONDS, QUERY_TIMEOUT_SECONDS)

        server_states[container_name]["running"] = True
        server_states[container_name]["last_activity"] = time.time()
        logger.info(f"[{container_name}] Startup process complete. Now handling traffic.")
        return True
    return False

def stop_minecraft_server(container_name):
    """Stops a Minecraft server container."""
    logger = logging.getLogger(__name__)
    if is_container_running(container_name):
        logger.info(f"[{container_name}] Stopping Minecraft server...")
        # Scripts handle their own logging
        result = subprocess.run(["/app/scripts/stop-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[{container_name}] Error during stop script: {result.stderr.strip()}")
            return False
        
        server_states[container_name]["running"] = False
        logger.info(f"[{container_name}] Server stopped successfully.")
        return True
    return False

def ensure_all_servers_stopped_on_startup():
    """Ensures all managed servers are stopped when the proxy starts for a clean state."""
    logger = logging.getLogger(__name__)
    logger.info("Proxy startup: Ensuring all managed Minecraft servers are initially stopped.")
    for srv_conf in SERVERS_CONFIG.values():
        container_name = srv_conf['container_name']
        if is_container_running(container_name):
            logger.warning(f"[{container_name}] Found running at proxy startup. Waiting for it to be query-ready before issuing a safe stop.")
            # Give it a moment before query
            time.sleep(INITIAL_SERVER_QUERY_DELAY_SECONDS)
            wait_for_server_query_ready(srv_conf, INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS, QUERY_TIMEOUT_SECONDS)
            stop_minecraft_server(container_name)
        else:
            logger.info(f"[{container_name}] Is confirmed to be stopped.")

def monitor_servers_activity():
    """Periodically checks running servers for player count and stops them if idle."""
    logger = logging.getLogger(__name__)
    while True:
        time.sleep(PLAYER_CHECK_INTERVAL_SECONDS)
        current_time = time.time()
        
        for server_conf in SERVERS_CONFIG.values():
            container_name = server_conf['container_name']
            state = server_states.get(container_name)
            
            # Skip if the server isn't supposed to be running
            if not (state and state.get("running")):
                continue

            active_players_on_server = 0
            try:
                status = None
                server_type = server_conf.get('server_type', 'bedrock')
                target_ip = container_name
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
                logger.debug(f"[{container_name}] Failed to query for player count: {e}. Assuming 0 players for now.")

            if active_players_on_server > 0:
                logger.debug(f"[{container_name}] Found {active_players_on_server} active player(s). Resetting idle timer.")
                state["last_activity"] = current_time
            elif (current_time - state.get("last_activity", 0) > IDLE_TIMEOUT_SECONDS):
                logger.info(f"[{container_name}] Idle for over {IDLE_TIMEOUT_SECONDS}s with 0 players. Initiating shutdown.")
                stop_minecraft_server(container_name)


def run_proxy(client_listen_sockets, inputs):
    """The main packet forwarding loop of the proxy."""
    global active_client_connections, packet_buffers
    logger = logging.getLogger(__name__)
    last_heartbeat_time = time.time()
    
    while True:
        try:
            readable, _, _ = select.select(inputs, [], [], 0.05)

            # Update the heartbeat file for the Docker health check
            current_time = time.time()
            if current_time - last_heartbeat_time > HEARTBEAT_INTERVAL_SECONDS:
                try:
                    HEARTBEAT_FILE.write_text(str(int(current_time)))
                    last_heartbeat_time = current_time
                    logger.debug("Proxy heartbeat updated.")
                except Exception as e:
                    logger.warning(f"Could not update heartbeat file at {HEARTBEAT_FILE}: {e}")

            for sock in readable:
                # --- Packet from a Client ---
                if sock in client_listen_sockets.values():
                    try:
                        data, client_addr = sock.recvfrom(4096)
                    except Exception as e:
                        logger.error(f"Error receiving from client socket {sock.getsockname()}: {e}")
                        continue
                    
                    server_port = sock.getsockname()[1]
                    server_config = SERVERS_CONFIG[server_port]
                    container_name = server_config['container_name']
                    server_states[container_name]["last_activity"] = time.time()
                    
                    # Start server if it's not running
                    if not is_container_running(container_name):
                        logger.info(f"[{container_name}] New connection from {client_addr}. Starting server and buffering packet.")
                        start_minecraft_server(container_name)
                        # Buffer the first packet to be sent after the server is ready
                        packet_buffers[(client_addr, server_port)].append(data)
                        continue
                    
                    session_key = (client_addr, server_port)
                    # Establish a new session if one doesn't exist
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
                        logger.info(f"[{container_name}] New client session for {client_addr} established.")
                        
                        # Forward any buffered packets first
                        for buffered_packet in packet_buffers.pop(session_key, []):
                            server_sock.sendto(buffered_packet, (container_name, server_config['internal_port']))
                        
                        # Forward the current packet
                        server_sock.sendto(data, (container_name, server_config['internal_port']))
                    else:
                        # Forward to an existing session
                        conn_info = active_client_connections[session_key]
                        conn_info["client_to_server_socket"].sendto(data, (container_name, server_config['internal_port']))
                        conn_info["last_packet_time"] = time.time()
                
                # --- Packet from a Minecraft Server ---
                else: 
                    # Find which client session this server-side socket belongs to
                    found_session_key = next((key for key, info in active_client_connections.items() if info["client_to_server_socket"] is sock), None)
                    
                    if found_session_key:
                        data, _ = sock.recvfrom(4096)
                        conn_info = active_client_connections[found_session_key]
                        client_facing_socket = client_listen_sockets[conn_info["listen_port"]]
                        client_addr_original = found_session_key[0]
                        client_facing_socket.sendto(data, client_addr_original)
                        conn_info["last_packet_time"] = time.time()
                    else:
                        logger.warning(f"Received data on an unexpected backend socket {sock}. Closing it.")
                        inputs.remove(sock)
                        sock.close()
                        
        except Exception as e:
            logger.error(f"An unexpected error occurred in the main proxy loop: {e}", exc_info=True)
            time.sleep(1)

        # --- Cleanup Idle Sessions ---
        current_time = time.time()
        sessions_to_remove = [key for key, info in active_client_connections.items() if current_time - info["last_packet_time"] > IDLE_TIMEOUT_SECONDS]
        for session_key in sessions_to_remove:
            conn_info = active_client_connections.pop(session_key)
            container_name = conn_info['target_container']
            logger.info(f"[{container_name}] Client session for {session_key[0]} idle for >{IDLE_TIMEOUT_SECONDS}s. Disconnecting.")
            inputs.remove(conn_info["client_to_server_socket"])
            conn_info["client_to_server_socket"].close()
            # Clear any stray buffers for this session
            packet_buffers.pop(session_key, None)


# --- Main Execution ---
if __name__ == "__main__":
    # Simplified Logging Setup
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format='%(asctime)s - %(levelname)-8s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logger = logging.getLogger(__name__)

    # Handle healthcheck argument
    if '--healthcheck' in sys.argv:
        perform_health_check()
        sys.exit(0)
    
    # --- Normal Startup Sequence ---
    logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

    # Log application build metadata if available
    app_metadata = os.environ.get('APP_IMAGE_METADATA')
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            logger.info(f"Version: {meta.get('version', 'N/A')}, Build Date: {meta.get('build_date', 'N/A')}, Commit: {meta.get('commit', 'N/A')}")
        except json.JSONDecodeError:
            logger.warning(f"Could not parse APP_IMAGE_METADATA: {app_metadata}")

    # Clean up old heartbeat file on start.
    if HEARTBEAT_FILE.exists():
        HEARTBEAT_FILE.unlink()

    # Exit if no servers are configured
    if not SERVERS_CONFIG:
        logger.error("FATAL: No server configurations loaded. Please define servers via environment variables (e.g., NB_1_...) or a servers.json file.")
        logger.error("Entering dormant, unhealthy state.")
        # Create a stale heartbeat file to ensure health checks fail correctly
        HEARTBEAT_FILE.write_text("0")
        while True:
            time.sleep(3600) # Sleep indefinitely

    # Connect to Docker
    try:
        docker_client = docker.from_env()
        docker_client.ping()
        logger.info("Successfully connected to the Docker daemon.")
    except Exception as e:
        logger.error(f"FATAL: Could not connect to Docker daemon. Is /var/run/docker.sock mounted? Error: {e}")
        sys.exit(1)

    # Initialize server states
    server_states = {s['container_name']: {"running": False, "last_activity": 0} for s in SERVERS_CONFIG.values()}

    # Ensure a clean state on startup
    ensure_all_servers_stopped_on_startup()

    # Bind sockets for listening
    client_listen_sockets = {}
    inputs = []
    for listen_port, srv_cfg in SERVERS_CONFIG.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', listen_port))
            sock.setblocking(False)
            client_listen_sockets[listen_port] = sock
            inputs.append(sock)
            logger.info(f"Proxy listening for '{srv_cfg['name']}' on port {listen_port} -> forwards to container '{srv_cfg['container_name']}'")
        except OSError as e:
            logger.error(f"FATAL: Could not bind to port {listen_port}. Is it already in use? ({e})")
            sys.exit(1)

    # Start the background thread for monitoring server activity
    monitor_thread = threading.Thread(target=monitor_servers_activity, daemon=True)
    monitor_thread.start()

    # Start the main proxy loop
    run_proxy(client_listen_sockets, inputs)
