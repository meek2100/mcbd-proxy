# config.py
import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")
SERVERS_FILE = os.getenv("SERVERS_FILE", "servers.json")

DEFAULT_SETTINGS = {
    "log_level": "info",
    "log_formatter": "console",
    "player_check_interval_seconds": 10,
    "idle_timeout_seconds": 300,
    "prometheus_enabled": True,
    "prometheus_port": 8000,
    "polling_rate": 10,
}


@dataclass
class ProxySettings:
    """Dataclass for storing proxy-wide settings."""

    log_level: str = "info"
    log_formatter: str = "console"
    player_check_interval_seconds: int = 10
    idle_timeout_seconds: int = 300
    prometheus_enabled: bool = True
    prometheus_port: int = 8000
    polling_rate: int = 10


@dataclass
class ServerConfig:
    """Dataclass for storing individual server configurations."""

    name: str
    server_type: str
    listen_port: int
    container_name: str
    internal_port: int
    idle_timeout_seconds: Optional[int] = None


def load_proxy_config() -> Tuple[ProxySettings, List[ServerConfig]]:
    """Loads proxy settings and server configurations."""
    settings = _load_settings()
    servers = _load_servers_config(settings)
    return settings, servers


def _load_settings() -> ProxySettings:
    """Loads proxy-wide settings from a JSON file or environment variables."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            logger.info("Loaded settings from file.", path=SETTINGS_FILE)
            return ProxySettings(**data)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(
            "Could not load settings from file, falling back to environment variables.",
            error=str(e),
        )
        log_level = os.getenv("LOG_LEVEL", DEFAULT_SETTINGS["log_level"])
        if log_level not in ["debug", "info", "warning", "error", "critical"]:
            log_level = DEFAULT_SETTINGS["log_level"]
        return ProxySettings(
            log_level=log_level,
            log_formatter=os.getenv("LOG_FORMATTER", DEFAULT_SETTINGS["log_formatter"]),
            player_check_interval_seconds=int(
                os.getenv(
                    "PLAYER_CHECK_INTERVAL_SECONDS",
                    DEFAULT_SETTINGS["player_check_interval_seconds"],
                )
            ),
            idle_timeout_seconds=int(
                os.getenv(
                    "IDLE_TIMEOUT_SECONDS", DEFAULT_SETTINGS["idle_timeout_seconds"]
                )
            ),
            prometheus_enabled=os.getenv(
                "PROMETHEUS_ENABLED", str(DEFAULT_SETTINGS["prometheus_enabled"])
            ).lower()
            == "true",
            prometheus_port=int(
                os.getenv("PROMETHEUS_PORT", DEFAULT_SETTINGS["prometheus_port"])
            ),
            polling_rate=int(
                os.getenv("POLLING_RATE", DEFAULT_SETTINGS["polling_rate"])
            ),
        )


def _load_servers_config(settings: ProxySettings) -> List[ServerConfig]:
    """Loads server configurations from a JSON file or environment variables."""
    servers = []
    # Try loading from environment first
    i = 1
    while True:
        server_name = os.getenv(f"SERVER_{i}_NAME")
        if not server_name:
            break
        try:
            server_type = os.getenv(f"SERVER_{i}_TYPE")
            if server_type not in ["java", "bedrock"]:
                raise ValueError(f"Invalid 'server_type' for server index {i}.")
            server = ServerConfig(
                name=server_name,
                server_type=server_type,
                listen_port=int(os.getenv(f"SERVER_{i}_LISTEN_PORT")),
                container_name=os.getenv(f"SERVER_{i}_CONTAINER_NAME"),
                internal_port=int(os.getenv(f"SERVER_{i}_INTERNAL_PORT")),
                idle_timeout_seconds=int(os.getenv(f"SERVER_{i}_IDLE_TIMEOUT_SECONDS"))
                if os.getenv(f"SERVER_{i}_IDLE_TIMEOUT_SECONDS")
                else settings.idle_timeout_seconds,
            )
            servers.append(server)
        except (TypeError, ValueError) as e:
            logger.error(
                "Invalid server definition in environment. Skipping.",
                index=i,
                error=str(e),
            )
        i += 1

    if servers:
        logger.info("Loaded server(s) from environment variables.", count=len(servers))
        return servers

    # Fallback to JSON file if no environment variables are set
    try:
        with open(SERVERS_FILE, "r") as f:
            data = json.load(f).get("servers", [])
            for server_data in data:
                servers.append(ServerConfig(**server_data))
            logger.info("Loaded server definitions from file.", path=SERVERS_FILE)
            return servers
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("Error decoding JSON from file.", path=SERVERS_FILE, error=str(e))
        return []


def load_application_config():
    """A convenience function to load all configurations."""
    return load_proxy_config()
