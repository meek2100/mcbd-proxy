import select
import signal
import socket
import time
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Dict, List

import structlog

from config import ProxySettings, ServerConfig, load_application_config
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
        self.servers_config_map: Dict[int, ServerConfig] = {
            s.listen_port: s for s in self.servers_list
        }
        self.docker_manager = DockerManager()
        self.server_states = {
            s.container_name: {
                "status": "stopped",
                "last_activity": 0.0,
                "pending_tcp_sockets": {},
                "pending_udp_packets": {},
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
        start_time = time.time()
        success = self.docker_manager.start_server(server_config, self.settings)

        with self.server_locks[container_name]:
            state = self.server_states[container_name]
            if success:
                self.logger.info("Server is query-ready. Stabilizing...")
                time.sleep(3)
                state["status"] = "running"
                state["last_activity"] = time.time()
                RUNNING_SERVERS.inc()
                duration = time.time() - start_time
                SERVER_STARTUP_DURATION.labels(server_name=server_config.name).observe(
                    duration
                )
                self.logger.info(
                    "Processing pending connections.",
                    pending_tcp=len(state["pending_tcp_sockets"]),
                    pending_udp=len(state["pending_udp_packets"]),
                )
                for sock, buffer in list(state["pending_tcp_sockets"].items()):
                    self._establish_tcp_session(
                        sock, sock.getpeername(), server_config, buffer
                    )
                state["pending_tcp_sockets"].clear()
                for client_addr, data in list(state["pending_udp_packets"].items()):
                    self._establish_udp_session(client_addr, data, server_config)
                state["pending_udp_packets"].clear()
            else:
                self.logger.error("Server startup failed. Closing sockets.")
                state["status"] = "stopped"
                for sock in list(state["pending_tcp_sockets"]):
                    self._remove_socket(sock)
                state["pending_tcp_sockets"].clear()
                state["pending_udp_packets"].clear()

    def _stop_minecraft_server(self, container_name: str):
        """High-level wrapper to stop a server and update proxy state."""
        state = self.server_states.get(container_name, {})
        if state.get("status") == "running":
            RUNNING_SERVERS.dec()
        state["status"] = "stopped"
        self.docker_manager.stop_server(container_name)

    def _ensure_all_servers_stopped_on_startup(self):
        """Ensures all managed servers are stopped when the proxy starts."""
        self.logger.info("Ensuring all servers are stopped on startup.")
        for srv_conf in self.servers_list:
            if self.docker_manager.is_container_running(srv_conf.container_name):
                self.logger.warning(
                    "Found running server at startup. Stopping.",
                    container_name=srv_conf.container_name,
                )
                self._stop_minecraft_server(srv_conf.container_name)
        self.logger.info("Initial server state verified.")

    def _monitor_servers_activity(self):
        """Monitors server and session activity in a dedicated thread."""
        while not self._shutdown_event.wait(
            self.settings.player_check_interval_seconds
        ):
            current_time = time.time()
            self._cleanup_crashed_and_idle_sessions(current_time)
            self._shutdown_idle_servers(current_time)

    def _cleanup_crashed_and_idle_sessions(self, current_time: float):
        """Cleanup sessions for crashed containers or idle clients."""
        with self.session_lock:
            for key, session in list(self.active_sessions.items()):
                is_crashed = not self.docker_manager.is_container_running(
                    session["target_container"]
                )
                server_conf = self.servers_config_map[session["listen_port"]]
                idle_timeout = (
                    server_conf.idle_timeout_seconds
                    or self.settings.idle_timeout_seconds
                )
                is_idle = (current_time - session["last_packet_time"]) > idle_timeout
                if is_crashed or is_idle:
                    self._cleanup_session_by_key(key)

    def _shutdown_idle_servers(self, current_time: float):
        """Shutdown servers that are running but have no activity."""
        for server_conf in self.servers_list:
            with self.server_locks[server_conf.container_name]:
                state = self.server_states[server_conf.container_name]
                if state["status"] != "running":
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
                if current_time - state.get("last_activity", 0) > idle_timeout:
                    self.logger.info(
                        "Server idle, shutting down.",
                        container=server_conf.container_name,
                    )
                    self._stop_minecraft_server(server_conf.container_name)

    def _remove_socket(self, sock: socket.socket):
        """Safely removes a socket from inputs and closes it."""
        if sock in self.inputs:
            self.inputs.remove(sock)
        try:
            sock.close()
        except (socket.error, AttributeError):
            pass

    def _shutdown_all_sessions(self):
        """Closes all active client and server sockets."""
        self.logger.info("Closing all active sessions.")
        with self.session_lock:
            for session in self.active_sessions.values():
                self._remove_socket(session.get("client_socket"))
                self._remove_socket(session.get("server_socket"))
            self.active_sessions.clear()
            self.socket_to_session_map.clear()

    def _reload_configuration(self, main_module):
        """Reloads configuration and re-initializes proxy state."""
        self.logger.info("Reloading configuration.")
        try:
            new_settings, new_servers = load_application_config()
            main_module.configure_logging(
                new_settings.log_level, new_settings.log_formatter
            )
            self.settings = new_settings
        except Exception as e:
            self.logger.error("Failed to reload settings.", error=str(e))
            self._reload_requested = False
            return

        self._shutdown_all_sessions()
        for sock in self.listen_sockets.values():
            self._remove_socket(sock)
        self.listen_sockets.clear()

        self.servers_list = new_servers
        self.servers_config_map = {s.listen_port: s for s in self.servers_list}
        for srv_cfg in self.servers_list:
            self._create_listening_socket(srv_cfg)
            if srv_cfg.container_name not in self.server_states:
                self.server_states[srv_cfg.container_name] = {
                    "status": "stopped",
                    "last_activity": 0.0,
                    "pending_tcp_sockets": {},
                    "pending_udp_packets": {},
                }
                self.server_locks[srv_cfg.container_name] = RLock()
        self.logger.info("Configuration reload complete.")
        self._reload_requested = False

    def _establish_tcp_session(self, conn, client_addr, server_config, buffer):
        """Establishes a full TCP session to the backend."""
        self.logger.info("Establishing TCP session", client_addr=client_addr)
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_sock.settimeout(5.0)
            server_sock.connect(
                (server_config.container_name, server_config.internal_port)
            )
            server_sock.setblocking(False)
            if buffer:
                server_sock.sendall(buffer)
        except (socket.error, ConnectionRefusedError) as e:
            self.logger.error("Failed to connect to backend.", error=str(e))
            self._remove_socket(conn)
            return

        self.inputs.append(server_sock)
        with self.session_lock:
            key = (client_addr, server_config.listen_port, "tcp")
            session = {
                "client_socket": conn,
                "server_socket": server_sock,
                "target_container": server_config.container_name,
                "last_packet_time": time.time(),
                "listen_port": server_config.listen_port,
                "protocol": "tcp",
            }
            self.active_sessions[key] = session
            self.socket_to_session_map[conn] = (key, "client")
            self.socket_to_session_map[server_sock] = (key, "server")
            ACTIVE_SESSIONS.labels(server_name=server_config.name).inc()

    def _handle_new_connection(self, sock: socket.socket):
        """Handles a new incoming TCP connection from a client."""
        conn, client_addr = sock.accept()
        server_cfg = self.servers_config_map[sock.getsockname()[1]]
        with self.server_locks[server_cfg.container_name]:
            state = self.server_states[server_cfg.container_name]
            if state["status"] == "running":
                conn.setblocking(False)
                self.inputs.append(conn)
                self._establish_tcp_session(conn, client_addr, server_cfg, b"")
            else:
                self.logger.info("Queuing TCP connection.", client_addr=client_addr)
                conn.setblocking(False)
                self.inputs.append(conn)
                state["pending_tcp_sockets"][conn] = b""
                if state["status"] == "stopped":
                    state["status"] = "starting"
                    self.logger.info(
                        "First connection. Starting server...",
                        container=server_cfg.container_name,
                    )
                    Thread(
                        target=self._start_minecraft_server_task,
                        args=(server_cfg,),
                    ).start()

    def _handle_udp_packet(self, sock: socket.socket):
        """Handles a UDP packet."""
        data, client_addr = sock.recvfrom(4096)
        server_cfg = self.servers_config_map[sock.getsockname()[1]]
        with self.server_locks[server_cfg.container_name]:
            state = self.server_states[server_cfg.container_name]
            if state["status"] == "running":
                self._forward_udp_packet(client_addr, data, server_cfg)
            else:
                if client_addr not in state["pending_udp_packets"]:
                    self.logger.info("Queuing UDP packet.", client_addr=client_addr)
                state["pending_udp_packets"][client_addr] = data
                if state["status"] == "stopped":
                    state["status"] = "starting"
                    self.logger.info(
                        "First UDP packet. Starting server...",
                        container=server_cfg.container_name,
                    )
                    Thread(
                        target=self._start_minecraft_server_task,
                        args=(server_cfg,),
                    ).start()

    def _forward_udp_packet(self, client_addr, data, server_cfg):
        """Forwards a UDP packet to the backend server."""
        session_key = (client_addr, server_cfg.listen_port, "udp")
        with self.session_lock:
            if session_key not in self.active_sessions:
                self._establish_udp_session(client_addr, data, server_cfg, session_key)
            else:
                session = self.active_sessions[session_key]
                session["last_packet_time"] = time.time()
                dest = (server_cfg.container_name, server_cfg.internal_port)
                session["server_socket"].sendto(data, dest)
                BYTES_TRANSFERRED.labels(
                    server_name=server_cfg.name, direction="c2s"
                ).inc(len(data))

    def _establish_udp_session(self, client_addr, data, server_cfg, session_key):
        """Creates a new UDP session."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.setblocking(False)
        self.inputs.append(server_sock)
        session = {
            "server_socket": server_sock,
            "target_container": server_cfg.container_name,
            "last_packet_time": time.time(),
            "listen_port": server_cfg.listen_port,
            "protocol": "udp",
        }
        self.active_sessions[session_key] = session
        self.socket_to_session_map[server_sock] = (session_key, "server")
        ACTIVE_SESSIONS.labels(server_name=server_cfg.name).inc()
        self.logger.info("Establishing UDP session.", client_addr=client_addr)
        dest = (server_cfg.container_name, server_cfg.internal_port)
        server_sock.sendto(data, dest)
        BYTES_TRANSFERRED.labels(server_name=server_cfg.name, direction="c2s").inc(
            len(data)
        )

    def _forward_packet(self, sock: socket.socket):
        """Forwards a packet for an established session."""
        try:
            data = sock.recv(4096)
            if not data:
                raise ConnectionResetError
        except (ConnectionResetError, socket.error, OSError) as e:
            self.logger.info("Connection error.", error=str(e))
            self._cleanup_session_by_socket(sock)
            return

        with self.session_lock:
            session_tuple = self.socket_to_session_map.get(sock)
            if not session_tuple:
                self._remove_socket(sock)
                return
            session_key, role = session_tuple
            session = self.active_sessions.get(session_key)
            if not session:
                self._cleanup_session_by_socket(sock)
                return
            session["last_packet_time"] = time.time()
            server_cfg = self.servers_config_map[session["listen_port"]]

        dest_sock, direction = (
            (session["server_socket"], "c2s")
            if role == "client"
            else (session.get("client_socket"), "s2c")
        )

        if dest_sock:
            try:
                dest_sock.sendall(data)
                BYTES_TRANSFERRED.labels(
                    server_name=server_cfg.name, direction=direction
                ).inc(len(data))
            except (socket.error, OSError):
                self._cleanup_session_by_socket(sock)

    def _cleanup_session_by_socket(self, sock: socket.socket):
        """Finds and cleans up a session based on a socket."""
        with self.session_lock:
            key, _ = self.socket_to_session_map.pop(sock, (None, None))
            if key:
                self._cleanup_session_by_key(key)
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

    def _run_proxy_loop(self, main_module):
        """The main event loop of the proxy."""
        self.logger.info("Starting main proxy loop.")
        while not self._shutdown_requested:
            if self._reload_requested:
                self._reload_configuration(main_module)
            try:
                readable, _, _ = select.select(self.inputs, [], [], 1.0)
            except (select.error, ValueError):
                continue

            if (
                time.time() - self.last_heartbeat_time
                > self.settings.proxy_heartbeat_interval_seconds
            ):
                HEARTBEAT_FILE.write_text(str(int(time.time())))
                self.last_heartbeat_time = time.time()

            for sock in readable:
                if sock in self.listen_sockets.values():
                    if sock.type == socket.SOCK_STREAM:
                        self._handle_new_connection(sock)
                    else:
                        self._handle_udp_packet(sock)
                else:
                    self._forward_packet(sock)
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
