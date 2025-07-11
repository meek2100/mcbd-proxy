# config.py
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import structlog

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

    idle_timeout_seconds: int
    player_check_interval_seconds: int
    server_ready_max_wait_time_seconds: int
    servers: List[ServerConfig] = field(default_factory=list)


def load_application_config() -> Tuple[ProxySettings, List[ServerConfig]]:
    """Loads all application configurations."""
    settings = ProxySettings(
        idle_timeout_seconds=30,
        player_check_interval_seconds=5,
        server_ready_max_wait_time_seconds=120,
    )
    servers = [
        ServerConfig(
            name="Bedrock Survival",
            server_type="bedrock",
            listen_port=19132,
            container_name="mc-bedrock",
            internal_port=19132,
        ),
        ServerConfig(
            name="Java Creative",
            server_type="java",
            listen_port=25565,
            container_name="mc-java",
            internal_port=25565,
        ),
    ]
    # In a real scenario, you would load servers into settings.servers
    settings.servers = servers
    return settings, servers
