# config.py
"""
Handles application configuration using Pydantic for validation.
"""

import os
from typing import List, Literal

import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

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
    stop_after_idle: int
    pre_warm: bool


class AppConfig(BaseModel):
    """Main application configuration model."""

    game_servers: List[GameServerConfig]
    log_level: str = "INFO"
    log_format: str = "console"
    server_check_interval: int = 60
    server_startup_timeout: int = 300
    server_stop_timeout: int = 60
    is_prometheus_enabled: bool = True
    prometheus_port: int = 8000


def load_app_config() -> AppConfig:
    """
    Loads application configuration from environment variables and .env files.
    """
    load_dotenv()

    try:
        settings_data = {
            "log_level": os.getenv("NB_LOG_LEVEL", "INFO"),
            "log_format": os.getenv("NB_LOG_FORMATTER", "console"),
            "server_check_interval": int(os.getenv("NB_SERVER_CHECK_INTERVAL", 60)),
            "server_startup_timeout": int(os.getenv("NB_SERVER_STARTUP_TIMEOUT", 300)),
            "server_stop_timeout": int(os.getenv("NB_SERVER_STOP_TIMEOUT", 60)),
            "is_prometheus_enabled": os.getenv("NB_PROMETHEUS_ENABLED", "true").lower()
            == "true",
            "prometheus_port": int(os.getenv("NB_PROMETHEUS_PORT", 8000)),
        }
        game_servers_data = [
            {
                "name": "mc-java",
                "game_type": "java",
                "container_name": "mc-java",
                "host": "127.0.0.1",
                "port": 25565,
                "proxy_port": 25565,
                "query_port": 25565,
                "stop_after_idle": int(os.getenv("NB_JAVA_STOP_AFTER_IDLE", 300)),
                "pre_warm": os.getenv("NB_JAVA_PRE_WARM", "false").lower() == "true",
            },
            {
                "name": "mc-bedrock",
                "game_type": "bedrock",
                "container_name": "mc-bedrock",
                "host": "127.0.0.1",
                "port": 19132,
                "proxy_port": 19132,
                "query_port": 19132,
                "stop_after_idle": int(os.getenv("NB_BEDROCK_STOP_AFTER_IDLE", 300)),
                "pre_warm": os.getenv("NB_BEDROCK_PRE_WARM", "false").lower() == "true",
            },
        ]
        config = AppConfig(game_servers=game_servers_data, **settings_data)
        log.info("Application configuration loaded successfully.")
        return config
    except (ValidationError, ValueError) as e:
        log.error("Configuration validation error", error=e)
        raise ValidationError.from_exception_data(
            title="AppConfig", line_errors=[]
        ) from e
