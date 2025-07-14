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

    name: str
    game_type: Literal["java", "bedrock"]
    container_name: str
    host: str
    port: int
    proxy_port: int
    proxy_host: str = "0.0.0.0"
    query_port: int
    stop_after_idle: int = 300
    pre_warm: bool = False


class AppConfig(BaseModel):
    """Main application configuration model."""

    game_servers: List[GameServerConfig]
    log_level: str = Field("INFO", env="NB_LOG_LEVEL")
    log_format: str = Field("console", env="NB_LOG_FORMATTER")
    server_check_interval: int = Field(60, env="NB_SERVER_CHECK_INTERVAL")
    server_startup_timeout: int = Field(300, env="NB_SERVER_STARTUP_TIMEOUT")
    server_stop_timeout: int = Field(60, env="NB_SERVER_STOP_TIMEOUT")
    is_prometheus_enabled: bool = Field(True, env="NB_PROMETHEUS_ENABLED")
    prometheus_port: int = Field(8000, env="NB_PROMETHEUS_PORT")


def load_app_config() -> AppConfig:
    """
    Loads application configuration from environment variables and .env files.
    """
    load_dotenv()
    try:
        config = AppConfig(
            game_servers=[
                GameServerConfig(
                    name="mc-java",
                    game_type="java",
                    container_name="mc-java",
                    host="127.0.0.1",
                    port=25565,
                    proxy_port=25565,
                    query_port=25565,
                    stop_after_idle=int(os.getenv("NB_JAVA_STOP_AFTER_IDLE", 300)),
                    pre_warm=os.getenv("NB_JAVA_PRE_WARM", "false").lower() == "true",
                ),
                GameServerConfig(
                    name="mc-bedrock",
                    game_type="bedrock",
                    container_name="mc-bedrock",
                    host="127.0.0.1",
                    port=19132,
                    proxy_port=19132,
                    query_port=19132,
                    stop_after_idle=int(os.getenv("NB_BEDROCK_STOP_AFTER_IDLE", 300)),
                    pre_warm=os.getenv("NB_BEDROCK_PRE_WARM", "false").lower()
                    == "true",
                ),
            ]
        )
        log.info("Application configuration loaded successfully.")
        return config
    except ValidationError as e:
        log.error("Configuration validation error", error=e)
        raise
