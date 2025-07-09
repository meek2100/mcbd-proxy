import select
import signal
import socket
import time
from pathlib import Path
from threading import Event, Lock, RLock
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
            s.container_name: {"running": False, "last_activity": 0.0}
            for s in self.servers_list
        }
        self.server_locks = {s.container_name: RLock() for s in self.servers_list}

        # Lock to protect access to session dictionaries from multiple threads
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
        else:  # SIGINT, SIGTERM
            self.logger.warning(
                "Shutdown signal received, initiating shutdown.", sig=sig
            )
            self._shutdown_requested = True
            self._shutdown_event.set()

    def _start_minecraft_server(self, server_config: ServerConfig) -> bool:
        """
        High-level wrapper to start a server and update proxy state.

        Returns:
            bool: True if the server is running and ready, False otherwise.
        """
        container_name = server_config.container_name
        if self.docker_manager.is_container_running(container_name):
            self.logger.debug(
                "Server start requested, but already running.",
                container_name=container_name,
            )
            if not self.server_states[container_name].get("running", False):
                self.server_states[container_name]["running"] = True
                RUNNING_SERVERS.inc()
            return True

        startup_timer_start = time.time()
        success = self.docker_manager.start_server(server_config, self.settings)

        if success:
            self.server_states[container_name]["running"] = True
            RUNNING_SERVERS.inc()
            duration = time.time() - startup_timer_start
            SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                duration
            )
            self.logger.info(
                "Startup process complete. Now handling traffic.",
                container_name=container_name,
                duration_seconds=duration,
            )
        else:
            self.logger.error(
                "Server startup process failed.", container_name=container_name
            )
            self.server_states[container_name]["running"] = False

        return success

    def _stop_minecraft_server(self, container_name: str):
        """High-level wrapper to stop a server and update proxy state."""
        was_running = self.server_states.get(container_name, {}).get("running", False)

        if self.docker_manager.stop_server(container_name):
            if was_running:
                RUNNING_SERVERS.dec()
            self.server_states[container_name]["running"] = False

    def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts.
        This is a blocking operation to prevent race conditions in tests.
        """
        self.logger.info(
            "Proxy startup: Ensuring all managed servers are initially stopped."
        )
        for srv_conf in self.servers_list:
            container_name = srv_conf.container_name
            if self.docker_manager.is_container_running(container_name):
                self.logger.warning(
                    "Found running at proxy startup. Issuing a safe stop.",
                    container_name=container_name,
                )
                time.sleep(self.settings.initial_server_query_delay_seconds)
                self.docker_manager.wait_for_server_query_ready(
                    srv_conf,
                    self.settings.initial_boot_ready_max_wait_time_seconds,
                    self.settings.query_timeout_seconds,
                )
                self._stop_minecraft_server(container_name)

                self.logger.info(
                    "Waiting for server to fully stop...",
                    container_name=container_name,
                )
                stop_timeout = 60
                stop_start_time = time.time()
                while self.docker_manager.is_container_running(container_name):
                    if time.time() - stop_start_time > stop_timeout:
                        self.logger.error(
                            "Timeout waiting for container to stop.",
                            container_name=container_name,
                        )
                        break
                    time.sleep(1)
                else:
                    self.logger.info(
                        "Server confirmed to be stopped.",
                        container_name=container_name,
                    )
            else:
                self.logger.info(
                    "Is confirmed to be stopped.", container_name=container_name
                )

    def _monitor_servers_activity(self):
        """Monitors server and session activity in a dedicated thread."""
        while not self._shutdown_requested:
            self._shutdown_event.wait(self.settings.player_check_interval_seconds)
            if self._shutdown_requested:
                break

            current_time = time.time()

            # Create a copy of sessions to check for thread safety
            with self.session_lock:
                sessions_to_check = list(self.active_sessions.items())

            for session_key, session_info in sessions_to_check:
                container_name = session_info["target_container"]
                server_config = self.servers_config_map.get(session_info["listen_port"])

                if not self.docker_manager.is_container_running(container_name):
                    self.logger.warning(
                        "Backend active session container not running. Cleaning up.",
                        container_name=container_name,
                        client_addr=session_key[0],
                    )
                    self._cleanup_session_by_key(session_key)
                    continue

                if not server_config:
                    continue

                idle_timeout = (
                    server_config.idle_timeout_seconds
                    or self.settings.idle_timeout_seconds
                )
                if current_time - session_info["last_packet_time"] > idle_timeout:
                    self.logger.info(
                        "Cleaning up idle client session.",
                        container_name=session_info["target_container"],
                        client_addr=session_key[0],
                    )
                    self._cleanup_session_by_key(session_key)

            for server_conf in self.servers_list:
                container_name = server_conf.container_name
                with self.server_locks[container_name]:
                    state = self.server_states.get(container_name)
                    if not (state and state.get("running")):
                        continue

                    if not self.docker_manager.is_container_running(container_name):
                        log_msg = "Monitor found server stopped. Updating state."
                        self.logger.info(
                            log_msg,
                            container_name=container_name,
                        )
                        if state.get("running"):
                            RUNNING_SERVERS.dec()
                        state["running"] = False
                        continue

                    has_active_sessions = any(
                        info["target_container"] == container_name
                        for info in self.active_sessions.values()
                    )
                    if has_active_sessions:
                        continue

                    idle_timeout = (
                        server_conf.idle_timeout_seconds
                        or self.settings.idle_timeout_seconds
                    )
                    if current_time - state.get("last_activity", 0) > idle_timeout:
                        log_msg = "Server idle with 0 sessions. Initiating shutdown."
                        self.logger.info(
                            log_msg,
                            container_name=container_name,
                            idle_threshold_seconds=idle_timeout,
                        )
                        self._stop_minecraft_server(container_name)

    def _close_session_sockets(self, session_info):
        """Helper to safely close sockets associated with a session."""
        server_socket = session_info.get("server_socket")
        if session_info.get("protocol") == "tcp":
            client_socket = session_info.get("client_socket")
            if client_socket:
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

    def _shutdown_all_sessions(self):
        """Closes all active client and server sockets to terminate sessions."""
        with self.session_lock:
            if not self.active_sessions:
                return

            self.logger.info(
                "Closing all active sessions...",
                count=len(self.active_sessions),
            )
            for session_info in self.active_sessions.values():
                self._close_session_sockets(session_info)

            self.active_sessions.clear()
            self.socket_to_session_map.clear()
            self.logger.info("All active sessions have been closed.")

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
            self.logger.error(
                "Failed to reload settings, aborting reload.", error=str(e)
            )
            self._reload_requested = False
            return

        self.logger.info("Closing all current listeners for reconfiguration.")
        for port, sock in self.listen_sockets.items():
            if sock in self.inputs:
                self.inputs.remove(sock)
            try:
                sock.close()
            except socket.error as e:
                self.logger.warning(
                    "Error closing old socket.", port=port, error=str(e)
                )

        self.listen_sockets.clear()
        self.servers_config_map.clear()
        self._shutdown_all_sessions()  # Also clears session maps

        self.logger.info("Applying new server configuration.")
        self.servers_list = new_servers
        for srv_cfg in self.servers_list:
            self.servers_config_map[srv_cfg.listen_port] = srv_cfg
            self._create_listening_socket(srv_cfg)
            if srv_cfg.container_name not in self.server_states:
                self.server_states[srv_cfg.container_name] = {
                    "running": False,
                    "last_activity": 0.0,
                }
                self.server_locks[srv_cfg.container_name] = RLock()

        self.logger.info("Configuration reload complete.")
        self._reload_requested = False

    def _handle_new_tcp_connection(self, sock: socket.socket):
        """Handles the first TCP packet from a client, establishing a session."""
        with self.session_lock:
            max_sessions = self.settings.max_concurrent_sessions
            if max_sessions > 0 and len(self.active_sessions) >= max_sessions:
                self.logger.warning(
                    "Max concurrent sessions reached. Rejecting new TCP connection.",
                    max_sessions=max_sessions,
                )
                conn, _ = sock.accept()
                conn.close()
                return

        conn, client_addr = sock.accept()
        server_port = sock.getsockname()[1]
        server_config = self.servers_config_map[server_port]
        container_name = server_config.container_name

        with self.server_locks[container_name]:
            if not self.docker_manager.is_container_running(container_name):
                self.logger.info(
                    "First TCP connection for stopped server. Starting...",
                    container_name=container_name,
                    client_addr=client_addr,
                )
                if not self._start_minecraft_server(server_config):
                    self.logger.error(
                        "Server failed to become ready. Closing client connection.",
                        container_name=container_name,
                    )
                    conn.close()
                    return

            self.logger.info(
                "Establishing new TCP session for running server.",
                client_addr=client_addr,
                server_name=server_config.name,
            )
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            connected_to_backend = False
            connect_start_time = time.time()
            connect_timeout = 5

            while (
                not connected_to_backend
                and time.time() - connect_start_time < connect_timeout
            ):
                try:
                    server_sock.settimeout(1.0)
                    server_sock.connect((container_name, server_config.internal_port))
                    connected_to_backend = True
                except (socket.error, ConnectionRefusedError):
                    time.sleep(0.25)
                    continue

            if not connected_to_backend:
                self.logger.error(
                    "Failed to connect to backend server after multiple retries.",
                    container_name=container_name,
                )
                conn.close()
                return

            conn.setblocking(False)
            server_sock.setblocking(False)
            self.inputs.append(conn)
            self.inputs.append(server_sock)

            with self.session_lock:
                session_key = (client_addr, server_port, "tcp")
                session_info = {
                    "client_socket": conn,
                    "server_socket": server_sock,
                    "target_container": container_name,
                    "last_packet_time": time.time(),
                    "listen_port": server_port,
                    "protocol": "tcp",
                }
                self.active_sessions[session_key] = session_info
                self.socket_to_session_map[conn] = (session_key, "client_socket")
                self.socket_to_session_map[server_sock] = (
                    session_key,
                    "server_socket",
                )
                ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

    def _handle_new_udp_packet(self, sock: socket.socket):
        """Handles the first UDP packet from a client, establishing a session."""
        with self.session_lock:
            max_sessions = self.settings.max_concurrent_sessions
            if max_sessions > 0 and len(self.active_sessions) >= max_sessions:
                self.logger.warning(
                    "Max concurrent sessions reached. Dropping UDP packet.",
                    max_sessions=max_sessions,
                )
                return

        data, client_addr = sock.recvfrom(4096)
        server_port = sock.getsockname()[1]
        server_config = self.servers_config_map[server_port]
        container_name = server_config.container_name

        with self.server_locks[container_name]:
            is_actually_running = self.docker_manager.is_container_running(
                container_name
            )
            self.server_states[container_name]["running"] = is_actually_running

            if not self.server_states[container_name]["running"]:
                self.logger.info(
                    "First packet received for stopped server. Starting...",
                    container_name=container_name,
                    client_addr=client_addr,
                )
                startup_success = self._start_minecraft_server(server_config)
                if not startup_success:
                    self.logger.error(
                        "Server failed to become ready. Dropping client packet.",
                        container_name=container_name,
                    )
                    return

        session_key = (client_addr, server_port, "udp")

        with self.session_lock:
            if session_key not in self.active_sessions:
                server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                server_sock.setblocking(False)
                self.inputs.append(server_sock)
                session_info = {
                    "server_socket": server_sock,
                    "target_container": container_name,
                    "last_packet_time": time.time(),
                    "listen_port": server_port,
                    "protocol": "udp",
                }
                self.active_sessions[session_key] = session_info
                self.socket_to_session_map[server_sock] = (
                    session_key,
                    "server_socket",
                )
                ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()
                self.logger.info(
                    "Establishing new UDP session for running server.",
                    client_addr=client_addr,
                    server_name=server_config.name,
                )

            session_info = self.active_sessions[session_key]
            session_info["last_packet_time"] = time.time()
            self.server_states[container_name]["last_activity"] = time.time()
            session_info["server_socket"].sendto(
                data, (container_name, server_config.internal_port)
            )

    def _handle_new_connection(self, sock: socket.socket):
        """Dispatches handling for a new connection based on socket type."""
        if sock.type == socket.SOCK_STREAM:
            self._handle_new_tcp_connection(sock)
        elif sock.type == socket.SOCK_DGRAM:
            self._handle_new_udp_packet(sock)

    def _forward_packet(self, sock: socket.socket):
        """Forwards a packet from an established session (TCP or UDP)."""
        with self.session_lock:
            session_info_tuple = self.socket_to_session_map.get(sock)
            if session_info_tuple:
                session_key, socket_role = session_info_tuple
                session_info = self.active_sessions.get(session_key)
            else:
                session_info = None

        if not session_info:
            if sock in self.inputs:
                self.inputs.remove(sock)
            try:
                sock.close()
            except socket.error:
                pass
            return

        try:
            if session_info["protocol"] == "tcp":
                data = sock.recv(4096)
                if not data:
                    raise ConnectionResetError("Connection closed by peer")
            else:  # UDP
                data, _ = sock.recvfrom(4096)
        except (ConnectionResetError, socket.error, OSError) as e:
            self.logger.info(
                "[DEBUG] Connection error, raising to trigger cleanup.",
                session_key=session_key,
                error=str(e),
            )
            raise e

        with self.session_lock:
            # Re-verify session still exists before proceeding
            if not self.active_sessions.get(session_key):
                return
            session_info["last_packet_time"] = time.time()

        server_config = self.servers_config_map[session_info["listen_port"]]

        if socket_role == "client_socket":
            self.server_states[server_config.container_name]["last_activity"] = (
                time.time()
            )
            destination_socket = session_info["server_socket"]
            destination_address = (
                server_config.container_name,
                server_config.internal_port,
            )
            direction = "c2s"
        else:  # s2c
            destination_address = session_key[0]
            direction = "s2c"
            destination_socket = (
                session_info.get("client_socket")
                if session_info["protocol"] == "tcp"
                else self.listen_sockets.get(session_info["listen_port"])
            )

        if not destination_socket:
            return

        try:
            if session_info["protocol"] == "tcp":
                destination_socket.sendall(data)
            else:  # UDP
                destination_socket.sendto(data, destination_address)
        except socket.error as e:
            self.logger.warning(
                "Socket error on send.", error=str(e), direction=direction
            )
            raise e

        BYTES_TRANSFERRED.labels(
            server_name=server_config.name, direction=direction
        ).inc(len(data))

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
        """Finds and cleans up a session associated with a given socket."""
        with self.session_lock:
            session_info_tuple = self.socket_to_session_map.pop(sock, None)
            if not session_info_tuple:
                return

            session_key, _ = session_info_tuple
            session_info = self.active_sessions.pop(session_key, None)

            if session_info:
                server_config = self.servers_config_map.get(session_info["listen_port"])
                if server_config:
                    ACTIVE_SESSIONS.labels(server_name=server_config.name).dec()

                self.socket_to_session_map.pop(session_info.get("client_socket"), None)
                self.socket_to_session_map.pop(session_info.get("server_socket"), None)
                self._close_session_sockets(session_info)

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
                    self.logger.warning(
                        "Could not update heartbeat file.", path=str(HEARTBEAT_FILE)
                    )

            for sock in readable:
                try:
                    if sock in self.listen_sockets.values():
                        self._handle_new_connection(sock)
                    else:
                        self._forward_packet(sock)
                except (ConnectionResetError, socket.error, OSError) as e:
                    self.logger.info(
                        "[DEBUG] Session cleanup block triggered by connection error.",
                        error=str(e),
                    )
                    self._cleanup_session_by_socket(sock)
                except Exception:
                    self.logger.error(
                        "Unhandled exception in proxy loop. Cleaning up session.",
                        socket_fileno=sock.fileno(),
                        exc_info=True,
                    )
                    self._cleanup_session_by_socket(sock)

        self.logger.info("Shutdown requested. Closing all listening sockets.")
        for sock in self.listen_sockets.values():
            sock.close()

    def _create_listening_socket(self, srv_cfg: ServerConfig):
        """Creates and binds a single listening socket."""
        listen_port = srv_cfg.listen_port
        sock_type = (
            socket.SOCK_DGRAM
            if srv_cfg.server_type == "bedrock"
            else socket.SOCK_STREAM
        )
        protocol_str = "UDP" if sock_type == socket.SOCK_DGRAM else "TCP"

        sock = socket.socket(socket.AF_INET, sock_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind(("0.0.0.0", listen_port))
            if sock_type == socket.SOCK_STREAM:
                sock.listen(self.settings.tcp_listen_backlog)
            sock.setblocking(False)
            self.listen_sockets[listen_port] = sock
            self.inputs.append(sock)
            self.logger.info(
                "Proxy listening for server",
                server_name=srv_cfg.name,
                listen_port=listen_port,
                protocol=protocol_str,
                container_name=srv_cfg.container_name,
            )
        except OSError as e:
            self.logger.critical(
                "FATAL: Could not bind to port.",
                port=listen_port,
                error=str(e),
            )
            raise
