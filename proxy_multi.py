import socket
import subprocess
import time
import docker
import os
import threading
import json
import select
from collections import defaultdict
import logging

# --- Logger Setup ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configuration Loading ---
file_config = {}
try:
    with open('proxy_config.json', 'r') as f:
        file_config = json.load(f)
except FileNotFoundError:
    logger.warning("proxy_config.json not found. Using environment variables or default values only.")
except json.JSONDecodeError:
    logger.error("Error: proxy_config.json is not valid JSON. Using environment variables or default values only.")

def get_config_value(env_var_name, json_key_name, default_value, type_converter=str):
    env_val = os.environ.get(env_var_name)
    if env_val is not None:
        try:
            return type_converter(env_val)
        except ValueError:
            logger.warning(f"Invalid type for environment variable {env_var_name}='{env_val}'. Using file config or default.")
    
    file_val = file_config.get(json_key_name)
    if file_val is not None:
        return type_converter(file_val)
    
    return default_value

IDLE_TIMEOUT_SECONDS = get_config_value('PROXY_IDLE_TIMEOUT_SECONDS', 'idle_timeout_seconds', 600, int)
PLAYER_CHECK_INTERVAL_SECONDS = get_config_value('PROXY_PLAYER_CHECK_INTERVAL_SECONDS', 'player_check_interval_seconds', 60, int)
MINECRAFT_SERVER_STARTUP_DELAY_SECONDS = get_config_value('PROXY_SERVER_STARTUP_DELAY_SECONDS', 'minecraft_server_startup_delay_seconds', 15, int)
SERVER_READY_MAX_WAIT_TIME_SECONDS = get_config_value('PROXY_SERVER_READY_MAX_WAIT_TIME_SECONDS', 'server_ready_max_wait_time_seconds', 120, int)
SERVER_READY_POLL_INTERVAL_SECONDS = get_config_value('PROXY_SERVER_READY_POLL_INTERVAL_SECONDS', 'server_ready_poll_interval_seconds', 2, int)
INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS = get_config_value('PROXY_INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS', 'initial_boot_ready_max_wait_time_seconds', 1800, int) 

SERVERS_CONFIG = {s['listen_port']: s for s in file_config.get('servers', [])}

# Docker client setup
try:
    client = docker.from_env()
except Exception as e:
    logger.error(f"Error connecting to Docker daemon: {e}. Ensure /var/run/docker.sock is mounted.")
    exit(1)

# Global state variables
server_states = {s['container_name']: {"running": False, "last_activity": time.time()} for s in SERVERS_CONFIG.values()}

active_client_connections = {} 
clients_per_server = defaultdict(set) 
packet_buffers = defaultdict(list)


# --- Utility Functions ---
def is_container_running(container_name):
    """Checks if a Docker container is currently running."""
    try:
        container = client.containers.get(container_name)
        return container.status == 'running'
    except docker.errors.NotFound:
        return False
    except docker.errors.APIError as e:
        logger.error(f"Docker API error checking container {container_name}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking container {container_name}: {e}")
        return False

# --- Function to wait for server readiness from logs ---
def wait_for_server_ready(container_name, max_wait_time_seconds, poll_interval_seconds):
    """
    Polls the server's log file for the 'Server started.' message.
    Returns True if ready, False if timeout or error.
    """
    logger.info(f"Waiting for {container_name} to log 'Server started.' (max {max_wait_time_seconds}s)...")
    start_time = time.time()
    log_file_path = f"/mnt/{container_name}-data/Dedicated_Server.txt"

    while time.time() - start_time < max_wait_time_seconds:
        try:
            # Read entire file, as it's small, to avoid seeking issues
            with open(log_file_path, 'r') as f:
                lines = f.readlines()
                
                for line in reversed(lines):
                    if "Server started." in line:
                        logger.info(f"{container_name} logged 'Server started.'. Ready!")
                        return True
        except FileNotFoundError:
            logger.debug(f"Log file {log_file_path} not found yet for {container_name}. Waiting...")
        except Exception as e:
            logger.warning(f"Error reading log file {log_file_path} for {container_name}: {e}")
            
        time.sleep(poll_interval_seconds) # Sleep between polls
        
    logger.error(f"Timeout waiting for {container_name} to log 'Server started.' after {max_wait_time_seconds} seconds. Proceeding anyway.")
    return False


def start_mcbe_server(container_name):
    """Starts a Minecraft Bedrock server Docker container."""
    if not is_container_running(container_name):
        logger.info(f"Starting Minecraft Bedrock server: {container_name}...")
        result = subprocess.run(["/app/scripts/start-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Error starting {container_name}: {result.stderr}")
            return False
        logger.info(result.stdout.strip())
        
        # Replace fixed sleep with log polling
        if not wait_for_server_ready(container_name, SERVER_READY_MAX_WAIT_TIME_SECONDS, SERVER_READY_POLL_INTERVAL_SECONDS):
            logger.warning(f"Server {container_name} did not log 'Server started.' within max wait time. Proceeding anyway.")

        server_states[container_name]["running"] = True 
        server_states[container_name]["last_activity"] = time.time() 
        logger.info(f"Minecraft Bedrock server {container_name} initiated startup and should be ready for traffic.")
        return True
    return False

def stop_mcbe_server(container_name):
    """Stops a Minecraft Bedrock server Docker container."""
    if is_container_running(container_name):
        logger.info(f"Stopping Minecraft Bedrock server: {container_name}... No players detected.")
        result = subprocess.run(["/app/scripts/stop-server.sh", container_name], capture_output=True, text=True) 
        if result.returncode != 0:
            logger.error(f"Error stopping {container_name}: {result.stderr}")
            return False
        logger.info(result.stdout.strip())
        server_states[container_name]["running"] = False
        logger.info(f"Minecraft Bedrock server {container_name} stopped.")
        return True
    return False

# --- Function to ensure servers are stopped at proxy startup ---
def ensure_all_servers_stopped_on_startup():
    logger.info("Proxy startup detected. Ensuring all Minecraft server containers are initially stopped to enforce on-demand behavior.")
    for srv_conf in SERVERS_CONFIG.values():
        container_name = srv_conf['container_name']
        if is_container_running(container_name):
            logger.info(f"Found {container_name} running at proxy startup. Waiting for it to fully start (and update) before stopping.")
            # Use INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS for initial stop
            if not wait_for_server_ready(container_name, INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS, SERVER_READY_POLL_INTERVAL_SECONDS):
                logger.warning(f"{container_name} did not become ready during initial startup within max wait time. Attempting to force stop anyway.")
            
            stop_mcbe_server(container_name)
        else:
            logger.info(f"{container_name} is already stopped.")


# --- Monitor and Shutdown Thread ---
def monitor_servers_activity():
    """Periodically checks server activity and shuts down idle servers."""
    while True:
        time.sleep(PLAYER_CHECK_INTERVAL_SECONDS) 
        current_time = time.time()
        
        for server_conf_item in SERVERS_CONFIG.values(): 
            server_name = server_conf_item['container_name']
            state = server_states[server_name] 

            if state["running"]:
                active_players_on_server = 0
                for session_key, conn_info in active_client_connections.items():
                    if conn_info["target_container"] == server_name:
                        active_players_on_server += 1

                if active_players_on_server == 0 and (current_time - state["last_activity"] > IDLE_TIMEOUT_SECONDS):
                    logger.info(f"Server {server_name} idle for {IDLE_TIMEOUT_SECONDS}s and no active players detected by proxy. Initiating shutdown.")
                    stop_mcbe_server(server_name)
                else:
                    state["player_count"] = active_players_on_server 
                    if active_players_on_server > 0:
                        state["last_activity"] = current_time 

# --- Main Proxy Logic ---
def run_proxy():
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
            logger.info(f"Proxy listening for client connections on port {listen_port}")
        except OSError as e:
            logger.error(f"ERROR: Could not bind to port {listen_port}. Is it already in use? ({e})")
            exit(1)

    packet_buffers = defaultdict(list) 

    while True:
        try: 
            readable, _, _ = select.select(inputs, [], [], 0.05) 
            
            for s in readable:
                server_port = None
                for port, sock in client_listen_sockets.items():
                    if sock == s:
                        server_port = port
                        break

                if server_port is not None:
                    # --- This socket is a client listener socket (incoming packet from client) ---
                    try:
                        data, client_addr = s.recvfrom(4096)
                        logger.debug(f"Received packet from {client_addr} on port {server_port}")
                    except Exception as e:
                        logger.error(f"Error receiving from client socket {s}: {e}")
                        continue

                    server_config = SERVERS_CONFIG[server_port] 
                    container_name = server_config['container_name']
                    internal_port = server_config['internal_port']

                    # Update per-server activity time (only if a player is attempting to connect to it)
                    server_states[container_name]["last_activity"] = time.time() 

                    # If server is not running, start it and buffer packet
                    if not is_container_running(container_name):
                        logger.info(f"Server {container_name} not running for {client_addr}. Starting and buffering initial packet.")
                        start_mcbe_server(container_name)
                        packet_buffers[(client_addr, server_port)].append(data)
                        continue

                    # --- Robust Session Handling: Key is (client_addr, server_port) ---
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
                        clients_per_server[container_name].add(client_addr) 
                        logger.info(f"New client session {session_key} established with {container_name}")
                        
                        for buffered_packet in packet_buffers[session_key]:
                            try:
                                server_sock.sendto(buffered_packet, (container_name, internal_port))
                            except Exception as e:
                                logger.error(f"Error sending buffered packet for {session_key} to {container_name}: {e}")
                        if session_key in packet_buffers:
                            del packet_buffers[session_key]

                        try:
                            server_sock.sendto(data, (container_name, internal_port))
                        except Exception as e:
                            logger.error(f"Error sending initial packet for {session_key} to {container_name}: {e}")
                        
                    else: # It's an existing session (same client_addr and server_port)
                        conn_info = active_client_connections[session_key]
                        target_container = conn_info["target_container"]
                        server_sock = conn_info["client_to_server_socket"]
                        
                        target_server_config = next((s for s in SERVERS_CONFIG.values() if s['container_name'] == target_container), None)
                        if not target_server_config:
                            logger.error(f"Target server config not found for {target_container} for session {session_key}. Cleaning up.")
                            inputs.remove(server_sock)
                            server_sock.close()
                            if client_addr in clients_per_server[target_container]:
                                clients_per_server[target_container].remove(client_addr)
                            del active_client_connections[session_key]
                            continue

                        try:
                            server_sock.sendto(data, (container_name, target_server_config['internal_port'])) 
                            conn_info["last_packet_time"] = time.time()
                            logger.debug(f"Forwarded packet from {session_key} to {target_container}")
                        except socket.error as e:
                            logger.error(f"Error forwarding packet from {session_key} to {target_container}: {e}. Disconnecting client.")
                            inputs.remove(server_sock)
                            server_sock.close()
                            if client_addr in clients_per_server[target_container]:
                                clients_per_server[target_container].remove(client_addr)
                            del active_client_connections[session_key]
                            
                else: # --- This socket is a backend server socket (response from server) ---
                    found_session_key = None
                    for current_session_key, conn_info in active_client_connections.items():
                        if conn_info["client_to_server_socket"] is s:
                            found_session_key = current_session_key
                            break
                    
                    if found_session_key:
                        data, _ = s.recvfrom(4096)
                        
                        conn_info = active_client_connections[found_session_key]
                        client_facing_socket = client_listen_sockets[conn_info["listen_port"]]
                        client_addr_original = found_session_key[0] 
                        
                        try:
                            client_facing_socket.sendto(data, client_addr_original)
                            conn_info["last_packet_time"] = time.time()
                            logger.debug(f"Forwarded response from {conn_info['target_container']} to {found_session_key}")
                        except socket.error as e:
                            logger.error(f"Error sending response from server to client {found_session_key}: {e}. Disconnecting client.")
                            inputs.remove(conn_info["client_to_server_socket"])
                            conn_info["client_to_server_socket"].close()
                            if client_addr_original in clients_per_server[conn_info["target_container"]]:
                                clients_per_server[conn_info["target_container"]].remove(client_addr_original)
                            del active_client_connections[found_session_key]

                    else:
                        logger.error(f"Received unexpected data on backend socket {s}. Session not found. Closing socket.")
                        inputs.remove(s)
                        s.close()
            
        except select.error as e: 
            if e.errno != 4: 
                logger.error(f"Select error: {e}")
            time.sleep(0.01)
        except Exception as e:
            logger.error(f"An unexpected error occurred in main proxy loop: {e}", exc_info=True)
            time.sleep(1)

        # --- Cleanup idle client connections (outside the main loop's select.select try/except, but within while True) ---
        current_time = time.time()
        sessions_to_remove = []
        for session_key, conn_info in active_client_connections.items():
            if current_time - conn_info["last_packet_time"] > IDLE_TIMEOUT_SECONDS:
                sessions_to_remove.append(session_key)

        for session_key in sessions_to_remove:
            conn_info = active_client_connections[session_key]
            idle_secs = current_time - conn_info["last_packet_time"]
            logger.info(
                f"Client session {session_key} idle for {idle_secs:.1f}s. "
                f"Disconnecting from {conn_info['target_container']}."
            )

            inputs.remove(conn_info["client_to_server_socket"])
            conn_info["client_to_server_socket"].close()
            client_addr_original = session_key[0]
            if client_addr_original in clients_per_server[conn_info["target_container"]]:
                clients_per_server[conn_info["target_container"]].remove(client_addr_original)
            del active_client_connections[session_key]
            if session_key in packet_buffers:
                del packet_buffers[session_key]

# --- Main execution ---
if __name__ == "__main__":
    logger.info("Starting Bedrock On-Demand Proxy...")
    monitor_thread = threading.Thread(target=monitor_servers_activity)
    monitor_thread.daemon = True
    monitor_thread.start()

    run_proxy()
