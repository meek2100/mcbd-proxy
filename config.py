# config.py

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

# Default values for settings, used if not overridden by env vars or settings.json
DEFAULT_SETTINGS = {
    "idle_timeout_seconds": 600,
    "player_check_interval_seconds": 60,
    "query_timeout_seconds": 5,
    "server_ready_max_wait_time_seconds": 120,
    "initial_boot_ready_max_wait_time_seconds": 180,
    "server_startup_delay_seconds": 5,
    "initial_server_query_delay_seconds": 10,
    "log_level": "INFO",
    "log_formatter": "json",
    "healthcheck_stale_threshold_seconds": 60,
    "proxy_heartbeat_interval_seconds": 15,
    "tcp_listen_backlog": 128,
    "max_concurrent_sessions": -1,  # -1 for unlimited
    "prometheus_enabled": True,
    "prometheus_port": 8000,
    "docker_url": "unix://var/run/docker.sock",  # Default Docker URL
}


@dataclass
class ServerConfig:
    """Dataclass to hold a single server's configuration."""

    name: str
    server_type: str
    listen_port: int
    container_name: str
    internal_port: int
    idle_timeout_seconds: Optional[int] = None


@dataclass
class ProxySettings:
    """Dataclass to hold all proxy-wide settings."""

    idle_timeout_seconds: int
    player_check_interval_seconds: int
    query_timeout_seconds: int
    server_ready_max_wait_time_seconds: int
    initial_boot_ready_max_wait_time_seconds: int
    server_startup_delay_seconds: int
    initial_server_query_delay_seconds: int
    log_level: str
    log_formatter: str
    healthcheck_stale_threshold_seconds: int
    proxy_heartbeat_interval_seconds: int
    tcp_listen_backlog: int
    max_concurrent_sessions: int
    prometheus_enabled: bool
    prometheus_port: int
    docker_url: str  # Add docker_url to proxy settings


def _load_settings_from_json(file_path: Path) -> dict:
    """Loads proxy-wide settings from a JSON file."""
    logger = structlog.get_logger(__name__)
    if not file_path.is_file():
        return {}
    try:
        with open(file_path, "r") as f:
            settings_from_file = json.load(f)
            logger.info("Loaded settings from file.", path=str(file_path))
            return settings_from_file
    except json.JSONDecodeError as e:
        logger.error(
            "Error decoding JSON from file.", path=str(file_path), error=str(e)
        )
        return {}


def _load_servers_from_json(file_path: Path) -> list[dict]:
    """Loads server definitions from a JSON file."""
    logger = structlog.get_logger(__name__)
    if not file_path.is_file():
        return []
    try:
        with open(file_path, "r") as f:
            servers_json_config = json.load(f)
            logger.info("Loaded server definitions from file.", path=str(file_path))
            return servers_json_config.get("servers", [])
    except json.JSONDecodeError as e:
        logger.error(
            "Error decoding JSON from file.", path=str(file_path), error=str(e)
        )
        return []


def _load_servers_from_env() -> list[dict]:
    """Loads server definitions from environment variables (NB_x_...)."""
    logger = structlog.get_logger(__name__)
    env_servers, i = [], 1
    while True:
        listen_port_str = os.environ.get(f"NB_{i}_LISTEN_PORT")
        if not listen_port_str:
            break
        try:
            idle_timeout_str = os.environ.get(f"NB_{i}_IDLE_TIMEOUT")
            server_def = {
                "name": os.environ.get(f"NB_{i}_NAME", f"Server {i}"),
                "server_type": os.environ.get(f"NB_{i}_SERVER_TYPE", "bedrock").lower(),
                "listen_port": int(listen_port_str),
                "container_name": os.environ.get(f"NB_{i}_CONTAINER_NAME"),
                "internal_port": int(os.environ.get(f"NB_{i}_INTERNAL_PORT")),
                "idle_timeout_seconds": int(idle_timeout_str)
                if idle_timeout_str
                else None,
            }
            if not all(
                v is not None
                for v in [
                    server_def["container_name"],
                    server_def["internal_port"],
                ]
            ):
                raise ValueError(f"Incomplete definition for server index {i}.")
            if server_def["server_type"] not in ["bedrock", "java"]:
                raise ValueError(f"Invalid 'server_type' for server index {i}.")
            env_servers.append(server_def)
        except (ValueError, TypeError) as e:
            logger.error(
                "Invalid server definition in environment. Skipping.",
                index=i,
                error=str(e),
            )
        i += 1
    if env_servers:
        logger.info(
            "Loaded server(s) from environment variables.", count=len(env_servers)
        )
    return env_servers


def load_application_config() -> tuple[ProxySettings, List[ServerConfig]]:
    """
    Loads all configuration from files and environment, with env vars taking precedence.
    """
    logger = structlog.get_logger(__name__)
    settings_from_json = _load_settings_from_json(Path("settings.json"))
    final_settings = {}
    for key, default_val in DEFAULT_SETTINGS.items():
        env_map = {
            "idle_timeout_seconds": "NB_IDLE_TIMEOUT",
            "player_check_interval_seconds": "NB_PLAYER_CHECK_INTERVAL",
            "query_timeout_seconds": "NB_QUERY_TIMEOUT",
            "server_ready_max_wait_time_seconds": "NB_SERVER_READY_MAX_WAIT",
            "initial_boot_ready_max_wait_time_seconds": (
                "NB_INITIAL_BOOT_READY_MAX_WAIT"
            ),
            "server_startup_delay_seconds": "NB_SERVER_STARTUP_DELAY",
            "initial_server_query_delay_seconds": "NB_INITIAL_SERVER_QUERY_DELAY",
            "log_level": "LOG_LEVEL",
            "log_formatter": "NB_LOG_FORMATTER",
            "healthcheck_stale_threshold_seconds": "NB_HEALTHCHECK_STALE_THRESHOLD",
            "proxy_heartbeat_interval_seconds": "NB_HEARTBEAT_INTERVAL",
            "tcp_listen_backlog": "NB_TCP_LISTEN_BACKLOG",
            "max_concurrent_sessions": "NB_MAX_SESSIONS",
            "prometheus_enabled": "NB_PROMETHEUS_ENABLED",
            "prometheus_port": "NB_PROMETHEUS_PORT",
            "docker_url": "DOCKER_HOST",  # Map to DOCKER_HOST env var
        }
        env_var_name = env_map.get(key)  # Use .get() to avoid KeyError
        env_val = os.environ.get(env_var_name) if env_var_name else None

        if env_val is not None:
            try:
                if isinstance(default_val, bool):
                    final_settings[key] = env_val.lower() in (
                        "true",
                        "1",
                        "yes",
                    )
                elif isinstance(default_val, int):
                    final_settings[key] = int(env_val)
                else:
                    final_settings[key] = env_val
            except ValueError:
                # Fallback to JSON or default if env var value is invalid type
                final_settings[key] = settings_from_json.get(key, default_val)
        else:
            final_settings[key] = settings_from_json.get(key, default_val)

    proxy_settings = ProxySettings(**final_settings)
    servers_list_raw = _load_servers_from_env() or _load_servers_from_json(
        Path("servers.json")
    )
    final_servers: List[ServerConfig] = []
    for srv_dict in servers_list_raw:
        try:
            final_servers.append(ServerConfig(**srv_dict))
        except TypeError as e:
            logger.error(
                "Failed to load server definition.",
                server_config=srv_dict,
                error=str(e),
            )
    if not final_servers:
        logger.critical("FATAL: No server configurations loaded.")
    return proxy_settings, final_servers
