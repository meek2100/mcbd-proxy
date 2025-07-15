# config.py
"""
Handles application configuration using Pydantic for validation.
"""

import os
from typing import List, Literal

import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

log = structlog.get_logger()


class GameServerConfig(BaseModel):
    """Configuration for a single game server."""

    name: str = Field(..., description="A friendly name for the server.")
    game_type: Literal["java", "bedrock"] = Field(
        ..., description="The type of Minecraft server: 'java' or 'bedrock'."
    )
    container_name: str = Field(
        ..., description="The Docker container name of the Minecraft server."
    )
    host: str = Field("127.0.0.1", description="The internal IP the server runs on.")
    port: int = Field(
        ..., description="The internal port the server listens on inside Docker."
    )
    proxy_port: int = Field(
        ..., description="The public port the proxy listens on for this server."
    )
    proxy_host: str = Field(
        "0.0.0.0", description="The host interface the proxy will bind to."
    )
    query_port: int | None = Field(
        None, description="The query port, if different from the game port."
    )
    pre_warm: bool = Field(
        False, description="If true, start this server when the proxy starts."
    )

    def model_post_init(self, __context):
        """Set query_port to port if it's not explicitly defined."""
        if self.query_port is None:
            self.query_port = self.port


class AppConfig(BaseModel):
    """Main application configuration model."""

    game_servers: List[GameServerConfig]
    log_level: str = "INFO"
    log_format: str = "console"
    idle_timeout: int = 600
    player_check_interval: int = 60
    server_startup_timeout: int = 300
    server_stop_timeout: int = 60
    is_prometheus_enabled: bool = True
    prometheus_port: int = 8000


def load_app_config() -> AppConfig:
    """
    Loads application configuration dynamically from environment variables.
    """
    load_dotenv()
    log.info("Loading application configuration from environment variables...")

    game_servers = []
    i = 1
    while True:
        container_var = f"NB_{i}_CONTAINER_NAME"
        if container_var not in os.environ:
            break

        log.debug(f"Found configuration for server index {i}.")
        try:
            server_data = {
                "name": os.getenv(f"NB_{i}_NAME", f"Server-{i}"),
                "game_type": os.getenv(f"NB_{i}_GAME_TYPE"),
                "container_name": os.getenv(container_var),
                "host": os.getenv(f"NB_{i}_HOST", "127.0.0.1"),
                "port": os.getenv(f"NB_{i}_PORT"),
                "proxy_port": os.getenv(f"NB_{i}_PROXY_PORT"),
                "proxy_host": os.getenv(f"NB_{i}_PROXY_HOST", "0.0.0.0"),
                "query_port": os.getenv(f"NB_{i}_QUERY_PORT"),
                "pre_warm": os.getenv(f"NB_{i}_PRE_WARM", "false").lower() == "true",
            }
            server_data_filtered = {
                k: v for k, v in server_data.items() if v is not None
            }
            server_config = GameServerConfig(**server_data_filtered)
            game_servers.append(server_config)
            log.info(
                "Loaded config for server",
                name=server_config.name,
                type=server_config.game_type,
            )
        except ValidationError as e:
            log.error(f"Configuration error for server index {i}", error=e)
            raise e
        i += 1

    if not game_servers:
        log.warning("No game servers were defined via NB_X_... environment variables.")

    try:
        settings_data = {
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
            "log_format": os.getenv("NB_LOG_FORMATTER", "console"),
            "idle_timeout": int(os.getenv("NB_IDLE_TIMEOUT", 600)),
            "player_check_interval": int(os.getenv("NB_PLAYER_CHECK_INTERVAL", 60)),
            "server_startup_timeout": int(os.getenv("NB_SERVER_READY_MAX_WAIT", 300)),
            "server_stop_timeout": int(os.getenv("NB_SERVER_STOP_TIMEOUT", 60)),
            "is_prometheus_enabled": os.getenv("NB_PROMETHEUS_ENABLED", "true").lower()
            == "true",
            "prometheus_port": int(os.getenv("NB_PROMETHEUS_PORT", 8000)),
        }
        config = AppConfig(game_servers=game_servers, **settings_data)
        log.info(
            "Application configuration loaded successfully.",
            server_count=len(game_servers),
        )
        return config
    except (ValidationError, ValueError) as e:
        log.error("Global configuration validation error", error=e)
        raise ValidationError.from_exception_data(
            title="AppConfig", line_errors=[]
        ) from e
