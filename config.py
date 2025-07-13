import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import structlog
from pydantic import BaseModel, Field, ValidationError


class AppSettings(BaseModel):
    """Defines the main application settings, loaded from config.json or ENV."""

    log_level: str = Field("INFO", description="Logging level (e.g., INFO, DEBUG)")
    log_formatter: str = Field(
        "console", description="Log format ('console' or 'json')"
    )
    docker_url: str = Field(
        "unix:///var/run/docker.sock", description="URL for the Docker daemon"
    )
    prometheus_enabled: bool = Field(
        True, description="Enable Prometheus metrics server"
    )
    prometheus_port: int = Field(9000, description="Port for Prometheus server")
    healthcheck_stale_threshold_seconds: int = Field(
        30, description="Threshold for health check heartbeat"
    )


@dataclass(frozen=True)
class Server:
    """Represents a single server configuration, loaded from servers.json or ENV."""

    server_name: str
    listen_port: int
    listen_host: str
    target_port: int
    docker_container_name: str


def load_application_config() -> tuple[AppSettings, list[Server]]:
    """
    Loads application settings and server configurations from files and environment
    variables, with environment variables taking precedence.
    """
    logger = structlog.get_logger(__name__)
    config_path = Path(os.getenv("CONFIG_PATH", "/app/config"))

    # Load AppSettings from file as a base
    settings_path = config_path / "settings.json"
    settings_data = {}
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            logger.error("Failed to decode settings.json, using defaults/ENV.")

    # Selectively override with environment variables that are explicitly set
    for key in AppSettings.model_fields:
        env_var = f"NB_{key.upper()}"
        env_value = os.getenv(env_var)
        if env_value is not None:
            settings_data[key] = env_value

    try:
        settings = AppSettings(**settings_data)
    except ValidationError as e:
        logger.warning(
            "Config validation error. Falling back to defaults.", errors=str(e)
        )
        settings = AppSettings()

    # Load Servers
    servers_list = _load_servers_from_env()
    if not servers_list:
        servers_path = config_path / "servers.json"
        if servers_path.exists():
            try:
                servers_data = json.loads(servers_path.read_text())
                servers_list = [Server(**s) for s in servers_data]
            except (json.JSONDecodeError, TypeError) as e:
                logger.error("Failed to load/parse servers.json.", error=str(e))
                servers_list = []

    logger.info(
        "Configuration loaded.",
        settings=settings.model_dump_json(),
        server_count=len(servers_list),
    )
    return settings, servers_list


def _load_servers_from_env() -> List[Server]:
    """Loads server configurations from environment variables."""
    servers = []
    i = 1
    while True:
        server_name = os.getenv(f"SERVER_{i}_NAME")
        if not server_name:
            break

        try:
            servers.append(
                Server(
                    server_name=server_name,
                    listen_port=int(os.environ[f"SERVER_{i}_LISTEN_PORT"]),
                    listen_host=os.getenv(f"SERVER_{i}_LISTEN_HOST", "0.0.0.0"),
                    target_port=int(os.environ[f"SERVER_{i}_TARGET_PORT"]),
                    docker_container_name=os.environ[f"SERVER_{i}_DOCKER_CONTAINER"],
                )
            )
        except (KeyError, ValueError) as e:
            structlog.get_logger(__name__).error(
                "Incomplete/invalid ENV config for server.",
                server_index=i,
                error=str(e),
            )
            break
        i += 1
    return servers
