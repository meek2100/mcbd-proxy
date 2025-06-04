import socket
import subprocess
import time
import docker
import os
import threading
import json
import select
from collections import defaultdict

# --- Configuration Loading ---
# This file is expected at /app/proxy_config.json due to volume mount
try:
    with open('proxy_config.json', 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print("Error: proxy_config.json not found. Ensure it's mounted correctly.")
    exit(1)
except json.JSONDecodeError:
    print("Error: proxy_config.json is not valid JSON. Check its syntax.")
    exit(1)

IDLE_TIMEOUT_SECONDS = config.get('idle_timeout_seconds', 300)
PLAYER_CHECK_INTERVAL_SECONDS = config.get('player_check_interval_seconds', 30)
SERVERS_CONFIG = {s['listen_port']: s for s in config.get('servers', [])}

# Docker client setup
try:
    client = docker.from_env()
except Exception as e:
    print(f"Error connecting to Docker daemon: {e}. Ensure /var/run/docker.sock is mounted.")
    exit(1)

# Global state to track server status and activity
server_states = {s['container_name']: {"running": False, "last_activity": time.time()} for s in config['servers']}

# active_client_connections: {client_addr: {target_container, client_to_server_socket, last_packet_time, listen_port}}
active_client_connections = {}
clients_per_server = defaultdict(list)

# --- Utility Functions ---
def is_container_running(container_name):
    """Checks if a Docker container is currently running."""
    try:
        container = client.containers.get(container_name)
        return container.status == 'running'
    except docker.errors.NotFound:
        return False
    except docker.errors.APIError as e:
        print(f"Docker API error checking container {container_name}: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error checking container {container_name}: {e}")
        return False

def start_mcbe_server(container_name):
    """Starts a Minecraft Bedrock server Docker container."""
    if not is_container_running(container_name):
        print(f"Starting Minecraft Bedrock server: {container_name}...")
        result = subprocess.run(["./scripts/start-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error starting {container_name}: {result.stderr}")
            return False
        print(result.stdout.strip())
        server_states[container_name]["running"] = True
        server_states[container_name]["last_activity"] = time.time()
        print(f"Minecraft Bedrock server {container_name} initiated startup.")
        return True
    return False

def stop_mcbe_server(container_name):
    """Stops a Minecraft Bedrock server Docker container."""
    if is_container_running(container_name):
        print(f"Stopping Minecraft Bedrock server: {container_name}... No players detected.")
        result = subprocess.run(["./scripts/stop-server.sh", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error stopping {container_name}: {result.stderr}")
            return False
        print(result.stdout.strip())
        server_states[container_name]["running"] = False
        print(f"Minecraft Bedrock server {container_name} stopped.")
        return True
    return False

# --- Monitor and Shutdown Thread ---
def monitor_servers_activity():
    """Periodically checks server activity and shuts down idle servers."""
    while True:
        time.sleep(PLAYER_CHECK_INTERVAL_SECONDS)
        current_time = time.time()
        
        for server_name, state in list(server_states.items()):
            if state["running"]:
                active_players_on_server = len(clients_per_server[server_name])
                
                if active_players_on_server == 0 and (current_time - state["last_activity"] > IDLE_TIMEOUT_SECONDS):
                    print(f"Server {server_name} idle for {IDLE_TIMEOUT_SECONDS}s and no active players detected by proxy. Initiating shutdown.")
                    stop_mcbe_server(server_name)
                    clients_per_server[server_name].clear()
                else:
                    state["player_count"] = active_players_on_server
                    if active_players_on_server > 0:
                         state["last_activity"] = current_time

# --- Main Proxy Logic ---
def run_proxy():
    """Main loop for the UDP proxy, handling incoming and outgoing traffic."""
    client_listen_sockets = {}
    inputs = []

    for listen_port, srv_cfg in SERVERS_CONFIG.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', listen_port))
            sock.setblocking(False)
            client_listen_sockets[listen_port] = sock
            inputs.append(sock)
            print(f"Proxy listening for client connections on port {listen_port}")
        except OSError as e:
            print(f"ERROR: Could not bind to port {listen_port}. Is it already in use? ({e})")
            exit(1)

    packet_buffers = defaultdict(list)

    while True:
        try:
            readable, _, _ = select.select(inputs, [], [], 0.05)
            
            for s in readable:
                if s in client_listen_sockets.values():
                    data, client_addr = s.recvfrom(4096)
                    
                    listen_port = None
                    for p, sock_obj in client_listen_sockets.items():
                        if sock_obj is s:
                            listen_port = p
                            break

                    if listen_port is None:
                        print(f"Error: Received data on unmapped client socket {s}")
                        continue

                    server_config = SERVERS_CONFIG[listen_port]
                    container_name = server_config['container_name']
                    internal_port = server_config['internal_port']

                    server_states[container_name]["last_activity"] = time.time() 

                    if not is_container_running(container_name):
                        print(f"Server {container_name} not running for {client_addr}. Starting and buffering initial packet.")
                        start_mcbe_server(container_name)
                        packet_buffers[(client_addr, listen_port)].append(data)
                        continue

                    if client_addr not in active_client_connections:
                        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        server_sock.setblocking(False)
                        
                        active_client_connections[client_addr] = {
                            "target_container": container_name,
                            "client_to_server_socket": server_sock,
                            "last_packet_time": time.time(),
                            "listen_port": listen_port
                        }
                        inputs.append(server_sock)
                        clients_per_server[container_name].append(client_addr)
                        print(f"New client {client_addr} established session with {container_name} via port {listen_port}")
                        
                        for buffered_packet in packet_buffers[(client_addr, listen_port)]:
                            try:
                                server_sock.sendto(buffered_packet, (container_name, internal_port))
                            except Exception as e:
                                print(f"Error sending buffered packet for {client_addr} to {container_name}: {e}")
                        if (client_addr, listen_port) in packet_buffers:
                            del packet_buffers[(client_addr, listen_port)]

                        try:
                            server_sock.sendto(data, (container_name, internal_port))
                        except Exception as e:
                             print(f"Error sending initial packet for {client_addr} to {container_name}: {e}")
                        
                    else:
                        conn_info = active_client_connections[client_addr]
                        target_container = conn_info["target_container"]
                        server_sock = conn_info["client_to_server_socket"]
                        
                        target_server_config = next((s for s in SERVERS_CONFIG.values() if s['container_name'] == target_container), None)
                        if not target_server_config:
                            print(f"Error: Target server config not found for {target_container} for existing connection {client_addr}. Cleaning up.")
                            inputs.remove(server_sock)
                            server_sock.close()
                            if client_addr in clients_per_server[target_container]:
                                clients_per_server[target_container].remove(client_addr)
                            del active_client_connections[client_addr]
                            continue

                        try:
                            server_sock.sendto(data, (target_container, target_server_config['internal_port']))
                            conn_info["last_packet_time"] = time.time()
                        except socket.error as e:
                            print(f"Error forwarding packet from {client_addr} to {target_container}: {e}. Disconnecting client.")
                            inputs.remove(server_sock)
                            server_sock.close()
                            if client_addr in clients_per_server[target_container]:
                                clients_per_server[target_container].remove(client_addr)
                            del active_client_connections[client_addr]
                            
                else:
                    found_client_addr = None
                    for c_addr, conn_info in active_client_connections.items():
                        if conn_info["client_to_server_socket"] is s:
                            found_client_addr = c_addr
                            break
                    
                    if found_client_addr:
                        data, _ = s.recvfrom(4096)
                        
                        conn_info = active_client_connections[found_client_addr]
                        client_facing_socket = client_listen_sockets[conn_info["listen_port"]]
                        
                        try:
                            client_facing_socket.sendto(data, found_client_addr)
                            conn_info["last_packet_time"] = time.time()
                        except socket.error as e:
                            print(f"Error sending response from server to client {found_client_addr}: {e}. Disconnecting client.")
                            inputs.remove(conn_info["client_to_server_socket"])
                            conn_info["client_to_server_socket"].close()
                            if found_client_addr in clients_per_server[conn_info["target_container"]]:
                                clients_per_server[conn_info["target_container"]].remove(found_client_addr)
                            del active_client_connections[found_client_addr]

                    else:
                        print(f"Received unexpected data on backend socket {s}. Client not found. Closing socket.")
                        inputs.remove(s)
                        s.close()
        
        except select.error as e:
            if e.errno != 4:
                print(f"Select error: {e}")
            time.sleep(0.01)
        except Exception as e:
            print(f"An unexpected error occurred in main proxy loop: {e}")
            time.sleep(1)

# --- Main execution ---
if __name__ == "__main__":
    print("Starting Bedrock On-Demand Proxy...")
    monitor_thread = threading.Thread(target=monitor_servers_activity)
    monitor_thread.daemon = True
    monitor_thread.start()

    run_proxy()
