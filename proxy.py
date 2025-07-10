import select
import signal
import socket
import time
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import List

import structlog

from config import ProxySettings, ServerConfig
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
)

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
                "status": "stopped",
                "last_activity": 0.0,
                "pending_sockets": {},
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
        Runs in a background thread to start a server and process pending TCP clients.
        """
        container_name = server_config.container_name
        start_time = time.time()
        success = self.docker_manager.start_server(server_config, self.settings)

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if success:
                state["status"] = "running"
                state["last_activity"] = time.time()
                RUNNING_SERVERS.inc()
                duration = time.time() - start_time
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    duration
                )
                self.logger.info(
                    "Startup complete. Processing pending connections.",
                    pending_count=len(state["pending_sockets"]),
                )
                for sock, buffer in list(state["pending_sockets"].items()):
                    self._establish_tcp_session(
                        sock, sock.getpeername(), server_config, buffer
                    )
                state["pending_sockets"].clear()
            else:
                self.logger.error(
                    "Server startup failed. Closing pending sockets.",
                    container_name=container_name,
                )
                state["status"] = "stopped"
                for sock in list(state["pending_sockets"]):
                    self._remove_socket(sock)
                state["pending_sockets"].clear()

    def _stop_minecraft_server(self, container_name: str):
        """High-level wrapper to stop a server and update proxy state."""
        state = self.server_states.get(container_name, {})
        if state.get("status") == "running":
            RUNNING_SERVERS.dec()
        state["status"] = "stopped"
        self.docker_manager.stop_server(container_name)

    def _ensure_all_servers_stopped_on_startup(self):
        """
        Ensures all managed servers are stopped when the proxy starts.
        This allows servers to complete their first-time setup/update
        before being put into an on-demand state.
        """
        self.logger.info("Proxy startup: Beginning initial server state verification.")
        time.sleep(self.settings.initial_server_query_delay_seconds)

        for srv_conf in self.servers_list:
            if self.docker_manager.is_container_running(srv_conf.container_name):
                self.logger.warning(
                    "Found running server at startup. Stopping for on-demand.",
                    container_name=srv_conf.container_name,
                )
                self._stop_minecraft_server(srv_conf.container_name)
        self.logger.info("Initial server state verification complete.")

    def _monitor_servers_activity(self):
        """Monitors server and session activity in a dedicated thread."""
        while not self._shutdown_event.wait(
            self.settings.player_check_interval_seconds
        ):
            container_statuses = {
                s.container_name: self.docker_manager.is_container_running(
                    s.container_name
                )
                for s in self.servers_list
            }
            current_time = time.time()
            with self.session_lock:
                for key, session in list(self.active_sessions.items()):
                    if not container_statuses.get(session["target_container"]):
                        self._cleanup_session_by_key(key)
            for server_conf in self.servers_list:
                with self.server_locks[server_conf.container_name]:
                    state = self.server_states[server_conf.container_name]
                    if state["status"] != "running":
                        continue
                    if not container_statuses.get(server_conf.container_name):
                        if state.get("status") == "running":
                            RUNNING_SERVERS.dec()
                        state["status"] = "stopped"
                        continue
                    with self.session_lock:
                        has_sessions = any(
                            s["target_container"] == server_conf.container_name
                            for s in self.active_sessions.values()
                        )
                    if has_sessions:
                        state["last_activity"] = current_time
                        continue
                    idle_timeout = (
                        server_conf.idle_timeout_seconds
                        or self.settings.idle_timeout_seconds
                    )
                    if current_time - state["last_activity"] > idle_timeout:
                        self.logger.info(
                            "Server idle. Initiating shutdown.",
                            container_name=server_conf.container_name,
                        )
                        self._stop_minecraft_server(server_conf.container_name)

    def _remove_socket(self, sock: socket.socket):
        """Safely removes a socket from inputs and closes it."""
        if sock:
            if sock in self.inputs:
                self.inputs.remove(sock)
            try:
                sock.close()
            except (socket.error, AttributeError):
                pass

    def _shutdown_all_sessions(self):
        """Closes all active client and server sockets to terminate sessions."""
        self.logger.info("Closing all active sessions and pending sockets.")
        with self.session_lock:
            for session in list(self.active_sessions.values()):
                self._remove_socket(session.get("client_socket"))
                self._remove_socket(session.get("server_socket"))
            self.active_sessions.clear()
            self.socket_to_session_map.clear()
        for state in self.server_states.values():
            for sock in list(state["pending_sockets"]):
                self._remove_socket(sock)
            state["pending_sockets"].clear()

    def _establish_tcp_session(self, conn, client_addr, server_config, buffer):
        """Connects to the backend and establishes a full TCP session."""
        container_name = server_config.container_name
        self.logger.info("Establishing TCP session.", client_addr=client_addr)
        try:
            server_sock = socket.create_connection(
                (container_name, server_config.internal_port), timeout=5
            )
            server_sock.setblocking(False)
            self.inputs.append(server_sock)
            if buffer:
                server_sock.sendall(buffer)
        except (socket.error, ConnectionRefusedError) as e:
            self.logger.error("Failed to connect to backend.", error=str(e))
            self._remove_socket(conn)
            return

        with self.session_lock:
            key = (client_addr, server_config.listen_port, "tcp")
            session = {
                "client_socket": conn,
                "server_socket": server_sock,
                "target_container": container_name,
                "last_packet_time": time.time(),
                "listen_port": server_config.listen_port,
                "protocol": "tcp",
            }
            self.active_sessions[key] = session
            self.socket_to_session_map[conn] = (key, "client_socket")
            self.socket_to_session_map[server_sock] = (key, "server_socket")
            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

    def _handle_new_connection(self, sock: socket.socket):
        """Handles a new incoming TCP connection from a client."""
        conn, client_addr = sock.accept()
        conn.setblocking(False)
        self.inputs.append(conn)
        server_cfg = self.servers_config_map[sock.getsockname()[1]]
        container_name = server_cfg.container_name

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if state["status"] == "running":
                self._establish_tcp_session(conn, client_addr, server_cfg, b"")
            else:
                self.logger.info("Queuing connection.", client_addr=client_addr)
                state["pending_sockets"][conn] = b""
                if state["status"] == "stopped":
                    state["status"] = "starting"
                    self.logger.info("Starting server...", container=container_name)
                    Thread(
                        target=self._start_minecraft_server_task, args=(server_cfg,)
                    ).start()

    def _handle_udp_packet(self, sock: socket.socket):
        """Handles a UDP packet, starting server if needed."""
        data, client_addr = sock.recvfrom(4096)
        server_config = self.servers_config_map[sock.getsockname()[1]]
        container_name = server_config.container_name

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if state["status"] == "stopped":
                state["status"] = "starting"
                self.logger.info(
                    "First packet. Starting server...", client_addr=client_addr
                )
                Thread(
                    target=self._start_minecraft_server_task, args=(server_config,)
                ).start()
                return
            if state["status"] == "starting":
                return  # Drop packets while server is starting

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

            session = self.active_sessions[session_key]
            session["last_packet_time"] = time.time()
            self.server_states[container_name]["last_activity"] = time.time()
            destination = (container_name, server_config.internal_port)
            session["server_socket"].sendto(data, destination)

    def _forward_packet(self, sock: socket.socket):
        """Forwards a packet or appends to a buffer for a pending connection."""
        try:
            data = sock.recv(4096)
            if not data:
                raise ConnectionResetError("Connection closed")
        except (ConnectionResetError, socket.error, OSError) as e:
            self.logger.info("Connection error, cleaning up.", error=str(e))
            self._cleanup_session_by_socket(sock)
            return

        container_name = self._get_container_for_sock(sock)
        if container_name:
            with self.server_locks[container_name]:
                state = self.server_states[container_name]
                if sock in state["pending_sockets"]:
                    state["pending_sockets"][sock] += data
                    return

        with self.session_lock:
            session_tuple = self.socket_to_session_map.get(sock)
            if not session_tuple:
                self._remove_socket(sock)
                return
            session_key, role = session_tuple
            session = self.active_sessions[session_key]
            session["last_packet_time"] = time.time()
            server_cfg = self.servers_config_map[session["listen_port"]]

        dest, direction = (
            (session["server_socket"], "c2s")
            if role == "client_socket"
            else (session.get("client_socket"), "s2c")
        )

        if dest:
            if session["protocol"] == "udp":
                dest.sendto(data, (server_cfg.container_name, server_cfg.internal_port))
            else:
                dest.sendall(data)
            BYTES_TRANSFERRED.labels(
                server_name=server_cfg.name, direction=direction
            ).inc(len(data))
        else:
            self._cleanup_session_by_socket(sock)

    def _cleanup_session_by_socket(self, sock: socket.socket):
        """Finds and cleans up a session or pending socket."""
        for state in self.server_states.values():
            if sock in state["pending_sockets"]:
                del state["pending_sockets"][sock]
                self._remove_socket(sock)
                return
        with self.session_lock:
            key, _ = self.socket_to_session_map.pop(sock, (None, None))
            if key:
                self._cleanup_session_by_key(key)
            else:
                self._remove_socket(sock)

    def _cleanup_session_by_key(self, key):
        """Cleans up a session using its unique key."""
        with self.session_lock:
            session = self.active_sessions.pop(key, None)
            if session:
                server_cfg = self.servers_config_map.get(session["listen_port"])
                if server_cfg:
                    ACTIVE_SESSIONS.labels(server_name=server_cfg.name).dec()
                self._remove_socket(session.get("client_socket"))
                self._remove_socket(session.get("server_socket"))
                self.socket_to_session_map.pop(session.get("client_socket"), None)
                self.socket_to_session_map.pop(session.get("server_socket"), None)

    def _get_container_for_sock(self, sock: socket.socket):
        """Finds the container name associated with a given socket."""
        try:
            port = sock.getsockname()[1]
            return self.servers_config_map[port].container_name
        except (KeyError, OSError):
            with self.session_lock:
                session_tuple = self.socket_to_session_map.get(sock)
                if session_tuple:
                    session = self.active_sessions.get(session_tuple[0])
                    if session:
                        return session.get("target_container")
        return None

    def _run_proxy_loop(self, main_module):
        """The main event loop of the proxy."""
        self.logger.info("Starting main proxy packet forwarding loop.")
        while not self._shutdown_requested:
            if self._reload_requested:
                self._reload_configuration(main_module)

            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0)
            except select.error:
                continue

            if (
                time.time() - self.last_heartbeat_time
                > self.settings.proxy_heartbeat_interval_seconds
            ):
                HEARTBEAT_FILE.write_text(str(int(time.time())))
                self.last_heartbeat_time = time.time()

            for sock in readable:
                try:
                    if sock in self.listen_sockets.values():
                        if sock.type == socket.SOCK_STREAM:
                            self._handle_new_connection(sock)
                        else:
                            self._handle_udp_packet(sock)
                    else:
                        self._forward_packet(sock)
                except Exception:
                    self.logger.exception("Error handling socket.")
                    self._cleanup_session_by_socket(sock)
        self._shutdown_all_sessions()

    def _create_listening_socket(self, srv_cfg: ServerConfig):
        """Creates and binds a single listening socket."""
        sock_type = (
            socket.SOCK_STREAM if srv_cfg.server_type == "java" else socket.SOCK_DGRAM
        )
        sock = socket.socket(socket.AF_INET, sock_type)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", srv_cfg.listen_port))
            if srv_cfg.server_type == "java":
                sock.listen(self.settings.tcp_listen_backlog)
            sock.setblocking(False)
            self.listen_sockets[srv_cfg.listen_port] = sock
            self.inputs.append(sock)
            self.logger.info(
                "Proxy listening", server_name=srv_cfg.name, port=srv_cfg.listen_port
            )
        except OSError as e:
            self.logger.critical(
                "FATAL: Could not bind to port.", port=srv_cfg.listen_port, error=str(e)
            )
            raise
