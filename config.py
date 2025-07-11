import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import structlog

# Using a standard logger, which you can configure at the application entry point
logger = structlog.get_logger(__name__)


@dataclass
class ServerConfig:
    """Dataclass for storing individual server configurations."""

    name: str
    server_type: str
    listen_port: int
    container_name: str
    internal_port: int
    idle_timeout_seconds: Optional[int] = None


@dataclass
class ProxySettings:
    """Dataclass for storing proxy-wide settings."""

    idle_timeout_seconds: int = 600
    player_check_interval_seconds: int = 60
    server_ready_max_wait_time_seconds: int = 120
    docker_url: str = "unix://var/run/docker.sock"
    log_level: str = "INFO"
    log_formatter: str = "json"


def _load_settings_from_env() -> dict:
    """Loads proxy settings from environment variables."""
    settings = {}
    if "IDLE_TIMEOUT_SECONDS" in os.environ:
        settings["idle_timeout_seconds"] = int(os.environ["IDLE_TIMEOUT_SECONDS"])
    if "PLAYER_CHECK_INTERVAL_SECONDS" in os.environ:
        settings["player_check_interval_seconds"] = int(
            os.environ["PLAYER_CHECK_INTERVAL_SECONDS"]
        )
    if "SERVER_READY_MAX_WAIT_TIME_SECONDS" in os.environ:
        settings["server_ready_max_wait_time_seconds"] = int(
            os.environ["SERVER_READY_MAX_WAIT_TIME_SECONDS"]
        )
    if "DOCKER_URL" in os.environ:
        settings["docker_url"] = os.environ["DOCKER_URL"]
    if "LOG_LEVEL" in os.environ:
        settings["log_level"] = os.environ["LOG_LEVEL"].upper()

    # Prioritize NB_LOG_FORMATTER, fall back to LOG_FORMAT for compatibility
    if "NB_LOG_FORMATTER" in os.environ:
        settings["log_formatter"] = os.environ["NB_LOG_FORMATTER"].lower()
    elif "LOG_FORMAT" in os.environ:
        settings["log_formatter"] = os.environ["LOG_FORMAT"].lower()
    return settings


def _load_servers_from_env() -> List[ServerConfig]:
    """Loads server definitions from environment variables."""
    servers = []
    i = 1
    while f"SERVER_{i}_NAME" in os.environ:
        try:
            server_data = {
                "name": os.environ[f"SERVER_{i}_NAME"],
                "server_type": os.environ[f"SERVER_{i}_TYPE"],
                "listen_port": int(os.environ[f"SERVER_{i}_LISTEN_PORT"]),
                "container_name": os.environ[f"SERVER_{i}_CONTAINER_NAME"],
                "internal_port": int(os.environ[f"SERVER_{i}_INTERNAL_PORT"]),
            }
            if f"SERVER_{i}_IDLE_TIMEOUT_SECONDS" in os.environ:
                server_data["idle_timeout_seconds"] = int(
                    os.environ[f"SERVER_{i}_IDLE_TIMEOUT_SECONDS"]
                )

            servers.append(ServerConfig(**server_data))
            i += 1
        except KeyError as e:
            logger.error(
                "Missing environment variable for server definition",
                server_index=i,
                missing_key=str(e),
            )
            # Stop processing if a server definition is incomplete
            break
    return servers


def load_application_config() -> Tuple[ProxySettings, List[ServerConfig]]:
    """
    Loads all application configurations from files and environment variables.
    Environment variables will override settings from files.
    """
    settings_file_path = os.getenv("SETTINGS_FILE", "settings.json")
    servers_file_path = os.getenv("SERVERS_FILE", "servers.json")

    settings_data = {}
    if os.path.exists(settings_file_path):
        logger.info("Loading settings from file", path=settings_file_path)
        with open(settings_file_path) as f:
            settings_data = json.load(f)

    servers_list = []
    if os.path.exists(servers_file_path):
        logger.info("Loading servers from file", path=servers_file_path)
        with open(servers_file_path) as f:
            servers_data = json.load(f)
            for server_config in servers_data.get("servers", []):
                servers_list.append(ServerConfig(**server_config))

    # Environment variables override file-based configurations
    env_settings = _load_settings_from_env()
    settings_data.update(env_settings)

    env_servers = _load_servers_from_env()
    if env_servers:
        logger.info("Loading servers from environment variables")
        servers_list = env_servers

    proxy_settings = ProxySettings(**settings_data)

    if not servers_list:
        logger.warning(
            "🚨 No servers were loaded. Check your configuration "
            "(servers.json or environment variables)."
        )

    return proxy_settings, servers_list
