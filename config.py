# config.py
"""
Handles application configuration using Pydantic for validation, while preserving
the original project's configuration loading hierarchy.
"""

import json
import os
from pathlib import Path
from typing import List, Literal, Optional

import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger()


class GameServerConfig(BaseModel):
    """Configuration for a single game server."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="A friendly name for the server.")
    game_type: Literal["java", "bedrock"] = Field(
        ...,
        alias="server_type",
        description="The type of Minecraft server: 'java' or 'bedrock'.",
    )
    container_name: str = Field(
        ..., description="The Docker container name of the Minecraft server."
    )
    host: Optional[str] = Field(
        None, description="The internal IP or hostname the server runs on."
    )
    port: int = Field(
        ...,
        alias="internal_port",
        description="The internal port the server listens on inside Docker.",
    )
    proxy_port: int = Field(
        ...,
        alias="listen_port",
        description="The public port the proxy listens on for this server.",
    )
    proxy_host: str = Field(
        "0.0.0.0", description="The host interface the proxy will bind to."
    )
    query_port: Optional[int] = Field(
        None, description="The query port, if different from the game port."
    )
    pre_warm: bool = Field(
        False, description="If true, start this server when the proxy starts."
    )

    def model_post_init(self, __context):
        """Set query_port and host to sane defaults if not defined."""
        if self.query_port is None:
            self.query_port = self.port
        if self.host is None:
            self.host = self.container_name


class AppConfig(BaseModel):
    """Main application configuration model."""

    model_config = ConfigDict(case_sensitive=False, populate_by_name=True)

    game_servers: List[GameServerConfig] = []
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_format: str = Field("console", alias="NB_LOG_FORMATTER")
    idle_timeout: int = Field(600, alias="NB_IDLE_TIMEOUT")
    player_check_interval: int = Field(60, alias="NB_PLAYER_CHECK_INTERVAL")
    server_startup_timeout: int = Field(300, alias="NB_SERVER_READY_MAX_WAIT")
    server_stop_timeout: int = Field(60, alias="NB_SERVER_STOP_TIMEOUT")
    query_timeout: int = Field(5, alias="NB_QUERY_TIMEOUT")
    is_prometheus_enabled: bool = Field(True, alias="NB_PROMETHEUS_ENABLED")
    prometheus_port: int = Field(8000, alias="NB_PROMETHEUS_PORT")
    healthcheck_stale_threshold: int = Field(60, alias="NB_HEALTHCHECK_STALE_THRESHOLD")
    # Restored from original implementation
    initial_boot_ready_max_wait: int = Field(
        180, alias="NB_INITIAL_BOOT_READY_MAX_WAIT"
    )
    server_startup_delay: int = Field(5, alias="NB_SERVER_STARTUP_DELAY")
    initial_server_query_delay: int = Field(10, alias="NB_INITIAL_SERVER_QUERY_DELAY")
    tcp_listen_backlog: int = Field(128, alias="NB_TCP_LISTEN_BACKLOG")
    max_concurrent_sessions: int = Field(-1, alias="NB_MAX_SESSIONS")


def load_app_config() -> AppConfig:
    """
    Loads configuration from JSON files and environment variables,
    preserving the original loading hierarchy (Env > JSON > Defaults).
    """
    load_dotenv()
    log.info("Loading application configuration...")

    # 1. Load Server Definitions (Prioritizing Environment)
    game_servers = []
    i = 1
    while f"NB_{i}_CONTAINER_NAME" in os.environ:
        try:
            server_data = {
                "name": os.getenv(f"NB_{i}_NAME", f"Server-{i}"),
                "game_type": os.getenv(f"NB_{i}_GAME_TYPE"),
                "container_name": os.getenv(f"NB_{i}_CONTAINER_NAME"),
                "host": os.getenv(f"NB_{i}_HOST"),
                "port": os.getenv(f"NB_{i}_PORT"),
                "proxy_port": os.getenv(f"NB_{i}_PROXY_PORT"),
                "proxy_host": os.getenv(f"NB_{i}_PROXY_HOST", "0.0.0.0"),
                "query_port": os.getenv(f"NB_{i}_QUERY_PORT"),
                "pre_warm": os.getenv(f"NB_{i}_PRE_WARM", "false"),
            }
            game_servers.append(
                GameServerConfig.model_validate(
                    {k: v for k, v in server_data.items() if v is not None}
                )
            )
        except ValidationError as e:
            log.error(f"Config error for server index {i}", error=e)
            raise
        i += 1

    if not game_servers:
        servers_file = Path("servers.json")
        if servers_file.is_file():
            log.info("No env servers found, loading from servers.json.")
            try:
                file_content = json.loads(servers_file.read_text())
                game_servers = [
                    GameServerConfig.model_validate(s)
                    for s in file_content.get("servers", [])
                ]
            except (ValidationError, json.JSONDecodeError) as e:
                log.error("Failed to load or parse servers.json", error=e)

    # 2. Load Global Settings (Env > JSON > Defaults)
    final_settings = {}
    settings_file = Path("settings.json")
    if settings_file.is_file():
        try:
            final_settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            log.error("Could not parse settings.json", path=settings_file)

    for field_name, field_info in AppConfig.model_fields.items():
        if field_info.alias and field_info.alias in os.environ:
            final_settings[field_name] = os.environ[field_info.alias]

    try:
        app_config = AppConfig(game_servers=game_servers, **final_settings)
        log.info(
            "Application configuration loaded successfully.",
            server_count=len(game_servers),
        )
        return app_config
    except ValidationError as e:
        log.error("Global configuration validation error", error=e)
        raise
