import select
import signal
import socket
import time
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import List

import structlog

from config import ProxySettings, ServerConfig, load_application_config
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
)

# --- Constants ---
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")


class NetherBridgeProxy:
    """
    Nether-bridge: On-Demand Minecraft Server Proxy.
    Orchestrates listening sockets and sessions, and delegates Docker operations.
    """

    def __init__(self, settings: ProxySettings, servers_list: List[ServerConfig]):
        self.logger = structlog.get_logger(__name__)
        self.settings = settings
        self.servers_list = servers_list
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}

        self.docker_manager = DockerManager()

        self.server_states = {
            s.container_name: {
                "status": "stopped",  # stopped, starting, running
                "last_activity": 0.0,
                "pending_sockets": {},  # Maps client socket to its buffer
            }
            for s in self.servers_list
        }
        self.server_locks = {s.container_name: RLock() for s in self.servers_list}

        self.session_lock = Lock()
        self.socket_to_session_map = {}
        self.active_sessions = {}

        self.listen_sockets = {}
        self.inputs = []
        self.last_heartbeat_time = time.time()
        self._shutdown_requested = False
        self._reload_requested = False
        self._shutdown_event = Event()

    def signal_handler(self, sig, frame):
        """Handles signals for graceful shutdown and configuration reloads."""
        if hasattr(signal, "SIGHUP") and sig == signal.SIGHUP:
            self._reload_requested = True
            self.logger.warning("SIGHUP received. Reloading configuration...")
        else:
            self.logger.warning(
                "Shutdown signal received, initiating shutdown.", sig=sig
            )
            self._shutdown_requested = True
            self._shutdown_event.set()

    def _start_minecraft_server_task(self, server_config: ServerConfig):
        """
        Runs in a background thread to start a server and process pending clients.
        """
        container_name = server_config.container_name
        startup_timer_start = time.time()
        success = self.docker_manager.start_server(server_config, self.settings)

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if success:
                state["status"] = "running"
                RUNNING_SERVERS.inc()
                duration = time.time() - startup_timer_start
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    duration
                )
                self.logger.info(
                    "Startup complete. Processing pending TCP connections.",
                    container_name=container_name,
                    pending_count=len(state["pending_sockets"]),
                )
                for client_sock, buffer in list(state["pending_sockets"].items()):
                    self._establish_tcp_session(
                        client_sock, client_sock.getpeername(), server_config, buffer
                    )
                state["pending_sockets"].clear()
            else:
                self.logger.error(
                    "Server startup failed. Closing pending sockets.",
                    container_name=container_name,
                )
                state["status"] = "stopped"
                for client_sock in list(state["pending_sockets"]):
                    self._remove_socket(client_sock)
                state["pending_sockets"].clear()

    def _stop_minecraft_server(self, container_name: str):
        """High-level wrapper to stop a server and update proxy state."""
        state = self.server_states.get(container_name, {})
        was_running = state.get("status") == "running"
        if self.docker_manager.stop_server(container_name):
            if was_running:
                RUNNING_SERVERS.dec()
            state["status"] = "stopped"

    def _ensure_all_servers_stopped_on_startup(self):
        """Ensures all managed servers are stopped when the proxy starts."""
        self.logger.info(
            "Proxy startup: Ensuring all managed servers are initially stopped."
        )
        for srv_conf in self.servers_list:
            if self.docker_manager.is_container_running(srv_conf.container_name):
                self.logger.warning(
                    "Found running at startup. Issuing a safe stop.",
                    container_name=srv_conf.container_name,
                )
                self._stop_minecraft_server(srv_conf.container_name)
                time.sleep(2)  # Give time for the container to stop

    def _monitor_servers_activity(self):
        """Monitors server and session activity in a dedicated thread."""
        while not self._shutdown_requested:
            self._shutdown_event.wait(self.settings.player_check_interval_seconds)
            if self._shutdown_requested:
                break
            container_statuses = {
                s.container_name: self.docker_manager.is_container_running(
                    s.container_name
                )
                for s in self.servers_list
            }
            current_time = time.time()
            with self.session_lock:
                sessions_to_check = list(self.active_sessions.items())
            for session_key, session_info in sessions_to_check:
                container_name = session_info["target_container"]
                if not container_statuses.get(container_name):
                    self.logger.warning(
                        "Backend container not running. Cleaning up session.",
                        container_name=container_name,
                    )
                    self._cleanup_session_by_key(session_key)
                    continue
                server_config = self.servers_config_map.get(session_info["listen_port"])
                if not server_config:
                    continue
                idle_timeout = (
                    server_config.idle_timeout_seconds
                    or self.settings.idle_timeout_seconds
                )
                if current_time - session_info["last_packet_time"] > idle_timeout:
                    self.logger.info(
                        "Cleaning up idle session.",
                        container_name=container_name,
                    )
                    self._cleanup_session_by_key(session_key)

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                with self.server_locks[container_name]:
                    state = self.server_states[container_name]
                    if state.get("status") != "running":
                        continue
                    if not container_statuses.get(container_name):
                        self.logger.info(
                            "Monitor found server stopped. Updating state.",
                            container_name=container_name,
                        )
                        if state.get("status") == "running":
                            RUNNING_SERVERS.dec()
                        state["status"] = "stopped"
                        continue
                    with self.session_lock:
                        has_active_sessions = any(
                            s["target_container"] == container_name
                            for s in self.active_sessions.values()
                        )
                    if has_active_sessions:
                        state["last_activity"] = time.time()
                        continue
                    idle_timeout = (
                        server_conf.idle_timeout_seconds
                        or self.settings.idle_timeout_seconds
                    )
                    if current_time - state.get("last_activity", 0) > idle_timeout:
                        self.logger.info(
                            "Server idle. Initiating shutdown.",
                            container_name=container_name,
                        )
                        self._stop_minecraft_server(container_name)

    def _close_session_sockets(self, session_info):
        """Helper to safely close sockets associated with a session."""
        self._remove_socket(session_info.get("client_socket"))
        self._remove_socket(session_info.get("server_socket"))

    def _remove_socket(self, sock: socket.socket):
        """Safely removes a socket from inputs and closes it."""
        if not sock:
            return
        if sock in self.inputs:
            self.inputs.remove(sock)
        try:
            sock.close()
        except socket.error:
            pass

    def _shutdown_all_sessions(self):
        """Closes all active client and server sockets to terminate sessions."""
        with self.session_lock:
            if self.active_sessions:
                self.logger.info("Closing all active sessions...")
                for session_info in list(self.active_sessions.values()):
                    self._close_session_sockets(session_info)
                self.active_sessions.clear()
                self.socket_to_session_map.clear()
        for state in self.server_states.values():
            for sock in list(state["pending_sockets"]):
                self._remove_socket(sock)
            state["pending_sockets"].clear()
        self.logger.info("All sessions and pending sockets closed.")

    def _reload_configuration(self, main_module):
        """
        Reloads configuration and re-initializes proxy state.
        NOTE: This is a destructive operation that terminates all active
        player sessions before applying the new configuration.
        """
        try:
            new_settings, new_servers = load_application_config()
            main_module.configure_logging(
                new_settings.log_level, new_settings.log_formatter
            )
            self.settings = new_settings
            self.logger.info("Proxy settings have been reloaded.")
        except Exception as e:
            self.logger.error("Failed to reload settings.", error=str(e))
            self._reload_requested = False
            return

        self.logger.info("Closing all current listeners for reconfiguration.")
        for port, sock in self.listen_sockets.items():
            self._remove_socket(sock)
        self.listen_sockets.clear()
        self._shutdown_all_sessions()

        self.logger.info("Applying new server configuration.")
        self.servers_list = new_servers
        for srv_cfg in self.servers_list:
            self.servers_config_map[srv_cfg.listen_port] = srv_cfg
            self._create_listening_socket(srv_cfg)
            if srv_cfg.container_name not in self.server_states:
                self.server_states[srv_cfg.container_name] = {
                    "status": "stopped",
                    "last_activity": 0.0,
                    "pending_sockets": {},
                }
                self.server_locks[srv_cfg.container_name] = RLock()
        self.logger.info("Configuration reload complete.")
        self._reload_requested = False

    def _establish_tcp_session(self, conn, client_addr, server_config, buffer=None):
        """Connects to the backend and establishes a full TCP session."""
        container_name = server_config.container_name
        self.logger.info("Establishing TCP session.", client_addr=client_addr)
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_sock.connect((container_name, server_config.internal_port))
        except (socket.error, ConnectionRefusedError) as e:
            self.logger.error(
                "Failed to connect to backend.",
                container_name=container_name,
                error=str(e),
            )
            self._remove_socket(conn)
            return

        server_sock.setblocking(False)
        self.inputs.append(server_sock)

        if buffer:
            try:
                server_sock.sendall(buffer)
            except socket.error as e:
                self.logger.warning("Error sending buffer.", error=str(e))
                self._cleanup_session_by_socket(conn)
                return

        with self.session_lock:
            session_key = (client_addr, server_config.listen_port, "tcp")
            session_info = {
                "client_socket": conn,
                "server_socket": server_sock,
                "target_container": container_name,
                "last_packet_time": time.time(),
                "listen_port": server_config.listen_port,
                "protocol": "tcp",
            }
            self.active_sessions[session_key] = session_info
            self.socket_to_session_map[conn] = (session_key, "client_socket")
            self.socket_to_session_map[server_sock] = (session_key, "server_socket")
            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

    def _handle_new_tcp_connection(self, sock: socket.socket):
        """Handles a new incoming TCP connection."""
        conn, client_addr = sock.accept()
        conn.setblocking(False)
        self.inputs.append(conn)

        server_config = self.servers_config_map[sock.getsockname()[1]]
        container_name = server_config.container_name

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if state["status"] == "running":
                self._establish_tcp_session(conn, client_addr, server_config)
            elif state["status"] == "starting":
                self.logger.info("Queuing connection.", client_addr=client_addr)
                state["pending_sockets"][conn] = b""
            elif state["status"] == "stopped":
                self.logger.info(
                    "First connection. Starting server...", client_addr=client_addr
                )
                state["status"] = "starting"
                state["pending_sockets"][conn] = b""
                Thread(
                    target=self._start_minecraft_server_task, args=(server_config,)
                ).start()

    def _handle_new_udp_packet(self, sock: socket.socket):
        """Handles a UDP packet, starting server if needed."""
        data, client_addr = sock.recvfrom(4096)
        server_config = self.servers_config_map[sock.getsockname()[1]]
        container_name = server_config.container_name

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if state["status"] == "stopped":
                self.logger.info(
                    "First packet. Starting server...", client_addr=client_addr
                )
                state["status"] = "starting"
                Thread(
                    target=self._start_minecraft_server_task, args=(server_config,)
                ).start()
                return
            if state["status"] == "starting":
                return

        session_key = (client_addr, server_config.listen_port, "udp")
        with self.session_lock:
            if session_key not in self.active_sessions:
                server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                server_sock.setblocking(False)
                self.inputs.append(server_sock)
                self.active_sessions[session_key] = {
                    "server_socket": server_sock,
                    "target_container": container_name,
                    "last_packet_time": time.time(),
                    "listen_port": server_config.listen_port,
                    "protocol": "udp",
                }
                self.socket_to_session_map[server_sock] = (session_key, "server_socket")
                ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

            session_info = self.active_sessions[session_key]
            session_info["last_packet_time"] = time.time()
            self.server_states[container_name]["last_activity"] = time.time()
            session_info["server_socket"].sendto(
                data, (container_name, server_config.internal_port)
            )

    def _handle_new_connection(self, sock: socket.socket):
        """Dispatches handling for a new connection."""
        if sock.type == socket.SOCK_STREAM:
            self._handle_new_tcp_connection(sock)
        else:
            self._handle_new_udp_packet(sock)

    def _forward_packet(self, sock: socket.socket, is_pending: bool):
        """Forwards a packet or appends to a buffer for a pending connection."""
        try:
            data = sock.recv(4096)
            if not data:
                raise ConnectionResetError("Connection closed by peer")
        except (ConnectionResetError, socket.error, OSError) as e:
            self.logger.info("Connection error, cleaning up.", error=str(e))
            self._cleanup_session_by_socket(sock)
            return

        if is_pending:
            server_config = self.servers_config_map[sock.getsockname()[1]]
            state = self.server_states[server_config.container_name]
            state["pending_sockets"][sock] += data
            return

        with self.session_lock:
            session_info_tuple = self.socket_to_session_map.get(sock)
            if not session_info_tuple:
                self._remove_socket(sock)
                return
            session_key, socket_role = session_info_tuple
            session_info = self.active_sessions[session_key]
            session_info["last_packet_time"] = time.time()

        server_config = self.servers_config_map[session_info["listen_port"]]
        if socket_role == "client_socket":
            dest_sock = session_info["server_socket"]
            direction = "c2s"
        else:
            dest_sock = session_info.get("client_socket")
            direction = "s2c"

        if not dest_sock:
            return

        try:
            dest_sock.sendall(data)
            BYTES_TRANSFERRED.labels(
                server_name=server_config.name, direction=direction
            ).inc(len(data))
        except socket.error as e:
            self.logger.warning("Socket error on send.", error=str(e))
            self._cleanup_session_by_socket(sock)

    def _cleanup_session_by_key(self, session_key):
        """Finds and cleans up a session by its key."""
        with self.session_lock:
            session_info = self.active_sessions.pop(session_key, None)
            if session_info:
                server_config = self.servers_config_map.get(session_info["listen_port"])
                if server_config:
                    ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()
                self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                self.socket_to_session_map.pop(session_info.get("server_socket"), None)
                self._close_session_sockets(session_info)

    def _cleanup_session_by_socket(self, sock: socket.socket):
        """Finds and cleans up a session or pending socket."""
        with self.session_lock:
            # Check if it's a pending socket first
            for state in self.server_states.values():
                if sock in state["pending_sockets"]:
                    del state["pending_sockets"][sock]
                    self._remove_socket(sock)
                    return

            session_info_tuple = self.socket_to_session_map.pop(sock, None)
            if session_info_tuple:
                session_key, _ = session_info_tuple
                self._cleanup_session_by_key(session_key)
            else:
                self._remove_socket(sock)

    def _run_proxy_loop(self, main_module):
        """The main event loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")
        while not self._shutdown_requested:
            if self._reload_requested:
                self._reload_configuration(main_module)

            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0)
            except select.error as e:
                self.logger.error("Error in select.select()", error=str(e))
                time.sleep(1)
                continue

            current_time = time.time()
            if (
                current_time - self.last_heartbeat_time
                > self.settings.proxy_heartbeat_interval_seconds
            ):
                try:
                    HEARTBEAT_FILE.write_text(str(int(current_time)))
                    self.last_heartbeat_time = current_time
                except Exception:
                    self.logger.warning("Could not update heartbeat file.")

            for sock in readable:
                if sock in self.listen_sockets.values():
                    self._handle_new_connection(sock)
                else:
                    with self.server_locks[
                        self.servers_config_map[sock.getsockname()[1]].container_name
                    ]:
                        is_pending = any(
                            sock in s["pending_sockets"]
                            for s in self.server_states.values()
                        )
                    self._forward_packet(sock, is_pending)

        self.logger.info("Shutdown requested. Closing all listening sockets.")
        for sock in self.listen_sockets.values():
            self._remove_socket(sock)
