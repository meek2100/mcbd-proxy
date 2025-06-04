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
SERVERS_CONFIG = {s['listen_port']: s for s in config.get('servers', [])}

# --- Global State ---
active_client_connections = {}  # { (client_addr, client_port, server_port) : {client_to_server_socket, last_packet_time, target_container} }
clients_per_server = defaultdict(set)  # { server_container_name : set of client_addresses }
packet_buffers = {}  # {(client_addr, client_port, server_port): buffer_data}
docker_client = docker.from_env()

def find_running_container_for_port(port):
    """
    Look up which container is running on a given port.
    Returns container name or None if not found/running.
    """
    for container in docker_client.containers.list():
        ports = container.attrs.get('NetworkSettings', {}).get('Ports') or {}
        for binding, mappings in ports.items():
            if mappings:
                container_port = int(binding.split('/')[0])
                if container_port == port:
                    return container.name
    return None

def monitor_servers_activity():
    """
    Periodically check each configured server container:
     - If no active client has sent traffic to that container for PLAYER_CHECK_INTERVAL_SECONDS, shut it down.
     - Restart it again when a new connection from a client arrives.
    """
    while True:
        for server_name, server_conf in SERVERS_CONFIG.items():
            # Skip if container not running
            try:
                container = docker_client.containers.get(server_conf['container_name'])
            except docker.errors.NotFound:
                continue

            last_activity = server_conf.get('last_activity_time', 0)
            if time.time() - last_activity > PLAYER_CHECK_INTERVAL_SECONDS:
                # no client activity recently; stop the container
                try:
                    logger.info(f"Server {server_name} idle...and no active players detected by proxy. Initiating shutdown.")
                    container.stop()
                except Exception as e:
                    logger.error(f"Error stopping container {server_name}: {e}")
        time.sleep(PLAYER_CHECK_INTERVAL_SECONDS)

def start_container_if_needed(server_port):
    """
    Ensure the container for this server_port is running. If not, start it.
    Update SERVERS_CONFIG with last_activity_time when started.
    """
    server_conf = SERVERS_CONFIG.get(server_port)
    if not server_conf:
        return None

    container_name = server_conf['container_name']
    try:
        container = docker_client.containers.get(container_name)
        if container.status != 'running':
            container.start()
            logger.info(f"Started container {container_name} for port {server_port}.")
    except docker.errors.NotFound:
        # container does not exist; create and start
        try:
            new_container = docker_client.containers.run(
                server_conf['image'],
                name=container_name,
                detach=True,
                ports={f"{server_conf['target_port']}/udp": server_port},
                environment=server_conf.get('environment', {}),
            )
            logger.info(f"Created and started container {container_name} on port {server_port}.")
        except Exception as e:
            logger.error(f"Failed to create/start container {container_name}: {e}")
            return None

    # record last activity
    server_conf['last_activity_time'] = time.time()
    return container_name

def run_proxy():
    """
    Main loop: listen on all configured UDP ports. 
    For each incoming packet from a client:
      - Check if the target container is running; if not, start it.
      - Open (or reuse) a UDP socket to forward packets to the container.
      - Track last_packet_time to implement idle timeouts.
    """
    # Create listening sockets
    listen_sockets = {}
    for listen_port in SERVERS_CONFIG:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', listen_port))
        listen_sockets[listen_port] = sock
        logger.info(f"Proxy listening on UDP port {listen_port}.")

    inputs = list(listen_sockets.values())
    outputs = []

    while True:
        try:
            readable, writable, exceptional = select.select(inputs, outputs, inputs, 1.0)

            # Handle incoming data
            for s in readable:
                # Identify server_port by matching socket object
                server_port = None
                for port, sock in listen_sockets.items():
                    if sock == s:
                        server_port = port
                        break
                if server_port is None:
                    continue

                try:
                    data, client_addr = s.recvfrom(4096)
                except Exception as e:
                    logger.error(f"Error receiving from socket {s}: {e}")
                    continue

                # Launch or get container for this server_port
                container_name = start_container_if_needed(server_port)
                if not container_name:
                    continue

                target_port = SERVERS_CONFIG[server_port]['target_port']
                # Resolve container IP
                try:
                    container = docker_client.containers.get(container_name)
                    container_ip = container.attrs['NetworkSettings']['IPAddress']
                except Exception as e:
                    logger.error(f"Could not get IP for container {container_name}: {e}")
                    continue

                session_key = (client_addr[0], client_addr[1], server_port)
                if session_key not in active_client_connections:
                    # create new outbound socket to container
                    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    active_client_connections[session_key] = {
                        "client_to_server_socket": out_sock,
                        "last_packet_time": time.time(),
                        "target_container": container_name
                    }
                    clients_per_server[container_name].add(client_addr[0])
                else:
                    out_sock = active_client_connections[session_key]["client_to_server_socket"]
                    active_client_connections[session_key]["last_packet_time"] = time.time()

                # forward data to container
                try:
                    out_sock.sendto(data, (container_ip, target_port))
                except Exception as e:
                    logger.error(f"Error forwarding data from client {client_addr} to container {container_name}: {e}")

                # Store packet in buffer for potential replies
                packet_buffers[session_key] = True

            # Check for replies from containers
            for session_key, conn_info in list(active_client_connections.items()):
                out_sock = conn_info["client_to_server_socket"]
                rlist, _, _ = select.select([out_sock], [], [], 0)
                if rlist:
                    try:
                        data, _ = out_sock.recvfrom(4096)
                        client_ip, client_port, _ = session_key
                        # send back to client
                        for listen_sock in listen_sockets.values():
                            if listen_sock.getsockname()[1] == session_key[2]:
                                listen_sock.sendto(data, (client_ip, client_port))
                                break
                    except Exception as e:
                        logger.error(f"Error receiving from backend socket {out_sock}: {e}")

        except select.error as e:
            if e.errno != 4: 
                logger.error(f"Select error: {e}")
            time.sleep(0.01)
        except Exception as e:
            logger.error(f"An unexpected error occurred in main proxy loop: {e}", exc_info=True)
            time.sleep(1)

        # --- Cleanup idle client connections (updated to use session_key) ---
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
