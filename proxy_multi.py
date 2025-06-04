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
try:
    with open('proxy_config.json', 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    logger.error("Error: proxy_config.json not found. Ensure it's mounted correctly.")
    exit(1)
except json.JSONDecodeError:
    logger.error("Error: proxy_config.json is not valid JSON. Check its syntax.")
    exit(1)

IDLE_TIMEOUT_SECONDS = config.get('idle_timeout_seconds', 600) 
PLAYER_CHECK_INTERVAL_SECONDS = config.get('player_check_interval_seconds', 60) 

# --- NEW GLOBAL WARM-UP CONFIG ---
ALL_SERVERS_WARM_UP_THRESHOLD_SECONDS = config.get('all_servers_warm_up_threshold_seconds', 5) 
GLOBAL_IDLE_TIMEOUT_SECONDS = config.get('global_idle_timeout_seconds', 1800) 

SERVERS_CONFIG = {s['listen_port']: s for s in config.get('servers', [])}

# Docker client setup
try:
    client = docker.from_env()
except Exception as e:
    logger.error(f"Error connecting to Docker daemon: {e}. Ensure /var/run/docker.sock is mounted.")
    exit(1)

# Global state variables
all_servers_warmed_up = False
last_proxy_activity_time = time.time()

server_states = {s['container_name']: {"running": False, "last_activity": time.time()} for s in config['servers']}

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

def start_mcbe_server(container_name):
    """Starts a Minecraft Bedrock server Docker container."""
    if not is_container_running(container_name):
        logger.info(f"Starting Minecraft Bedrock server: {container_name}...")
        result = subprocess.run(["./scripts/start-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Error starting {container_name}: {result.stderr}")
            return False
        logger.info(result.stdout.strip())
        
        logger.info(f"Waiting for {container_name} to finish initial startup (approx. 15 seconds)...")
        time.sleep(15)

        server_states[container_name]["running"] = True
        server_states[container_name]["last_activity"] = time.time()
        logger.info(f"Minecraft Bedrock server {container_name} initiated startup and should be ready for traffic.")
        return True
    return False

def stop_mcbe_server(container_name):
    """Stops a Minecraft Bedrock server Docker container."""
    if is_container_running(container_name):
        logger.info(f"Stopping Minecraft Bedrock server: {container_name}... No players detected.")
        result = subprocess.run(["./scripts/stop-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Error stopping {container_name}: {result.stderr}")
            return False
        logger.info(result.stdout.strip())
        server_states[container_name]["running"] = False
        logger.info(f"Minecraft Bedrock server {container_name} stopped.")
        return True
    return False


# --- Monitor and Shutdown Thread ---
def monitor_servers_activity():
    """Periodically checks server activity and shuts down idle servers."""
    global all_servers_warmed_up, last_proxy_activity_time

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

        total_active_players = sum(len(clients_per_server[srv_name]) for srv_name in server_states.keys())
        
        if all_servers_warmed_up and total_active_players == 0 and \
           (current_time - last_proxy_activity_time > GLOBAL_IDLE_TIMEOUT_SECONDS):
            
            logger.info(f"Global idle detected ({GLOBAL_IDLE_TIMEOUT_SECONDS}s of no proxy activity and no active players). Shutting down all warmed-up servers.")
            for srv_conf in SERVERS_CONFIG.values():
                stop_mcbe_server(srv_conf['container_name'])
            all_servers_warmed_up = False
            last_proxy_activity_time = current_time

# --- Main Proxy Logic ---
def run_proxy():
    global all_servers_warmed_up, last_proxy_activity_time

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

    packet_buffers = defaultdict(list) # Defined here locally, as it's only used within run_proxy

    while True:
        try: # This try block covers the entire main loop iteration
            readable, _, _ = select.select(inputs, [], [], 0.05) 
            
            for s in readable:
                # Identify if the socket is one of our client listener sockets
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

                    # --- NEW: Warm-up All Servers on First Activity (if not already warmed up) ---
                    if not all_servers_warmed_up:
                        last_proxy_activity_time = time.time() # Update proxy activity on any packet
                        
                        if (time.time() - last_proxy_activity_time < ALL_SERVERS_WARM_UP_THRESHOLD_SECONDS):
                            logger.debug(f"Proxy received initial activity from {client_addr}, waiting for warm-up threshold.")
                            packet_buffers[(client_addr, server_port)].append(data) # Buffer initial packets
                            continue # Don't process this packet yet, just buffer
                        
                        logger.info("First client activity detected and warm-up threshold met. Starting all configured Minecraft servers.")
                        for srv_conf in SERVERS_CONFIG.values():
                            start_mcbe_server(srv_conf['container_name']) # This includes the sleep
                        all_servers_warmed_up = True
                        logger.info("All configured servers initiated startup.")

                    # Update global proxy activity time (any packet keeps the system "awake")
                    last_proxy_activity_time = time.time() 

                    server_config = SERVERS_CONFIG[server_port] # Get config based on incoming port
                    container_name = server_config['container_name']
                    internal_port = server_config['internal_port']

                    # Update per-server activity time (only if a player is attempting to connect to it)
                    server_states[container_name]["last_activity"] = time.time() 

                    # If server is not running (and should be, i.e., system is warmed up), start it and buffer packet
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
                        inputs.append(server_sock) # Add server-facing socket to select list
                        clients_per_server[container_name].add(client_addr) # Track original client_addr for player count
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
            
        except select.error as e: # This try/except block now correctly pairs with the 'while True' loop
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
