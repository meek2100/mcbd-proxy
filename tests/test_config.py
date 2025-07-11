# tests/test_config.py
import os
from unittest.mock import patch

import pytest

from config import (
    ProxySettings,
    _load_servers_config,
    _load_settings,
    load_application_config,
)


@pytest.fixture(autouse=True)
def clear_env_vars():
    """Clears relevant environment variables before each test."""
    env_vars_to_clear = [
        "SETTINGS_FILE",
        "SERVERS_FILE",
        "LOG_LEVEL",
        "PLAYER_CHECK_INTERVAL_SECONDS",
        "IDLE_TIMEOUT_SECONDS",
        "PROMETHEUS_ENABLED",
        "PROMETHEUS_PORT",
        "POLLING_RATE",
    ]
    for i in range(1, 5):  # Clear a few potential server definitions
        env_vars_to_clear.extend(
            [
                f"SERVER_{i}_NAME",
                f"SERVER_{i}_TYPE",
                f"SERVER_{i}_LISTEN_PORT",
                f"SERVER_{i}_CONTAINER_NAME",
                f"SERVER_{i}_INTERNAL_PORT",
                f"SERVER_{i}_IDLE_TIMEOUT_SECONDS",
            ]
        )

    original_values = {var: os.getenv(var) for var in env_vars_to_clear}
    for var in env_vars_to_clear:
        if var in os.environ:
            del os.environ[var]

    yield

    for var, value in original_values.items():
        if value is not None:
            os.environ[var] = value
        elif var in os.environ:
            del os.environ[var]


@pytest.mark.unit
def test_load_settings_from_env():
    """Tests loading settings purely from environment variables."""
    with patch.dict(
        os.environ,
        {
            "LOG_LEVEL": "debug",
            "IDLE_TIMEOUT_SECONDS": "600",
            "PROMETHEUS_ENABLED": "false",
        },
    ):
        settings = _load_settings()
        assert settings.log_level == "debug"
        assert settings.idle_timeout_seconds == 600
        assert not settings.prometheus_enabled


@pytest.mark.unit
def test_load_servers_from_env():
    """Tests loading server configurations from environment variables."""
    with patch.dict(
        os.environ,
        {
            "SERVER_1_NAME": "Test Java",
            "SERVER_1_TYPE": "java",
            "SERVER_1_LISTEN_PORT": "25565",
            "SERVER_1_CONTAINER_NAME": "mc-java",
            "SERVER_1_INTERNAL_PORT": "25565",
            "SERVER_2_NAME": "Test Bedrock",
            "SERVER_2_TYPE": "bedrock",
            "SERVER_2_LISTEN_PORT": "19132",
            "SERVER_2_CONTAINER_NAME": "mc-bedrock",
            "SERVER_2_INTERNAL_PORT": "19132",
        },
    ):
        settings = ProxySettings(idle_timeout_seconds=300)
        servers = _load_servers_config(settings)
        assert len(servers) == 2
        assert servers[0].name == "Test Java"
        assert servers[1].server_type == "bedrock"


@pytest.mark.unit
def test_load_servers_from_file(tmp_path):
    """Tests loading server configurations from a JSON file."""
    servers_content = {
        "servers": [
            {
                "name": "File Server",
                "server_type": "java",
                "listen_port": 25555,
                "container_name": "mc-file",
                "internal_port": 25555,
            }
        ]
    }
    servers_file = tmp_path / "servers.json"
    servers_file.write_text(str(servers_content).replace("'", '"'))

    with patch("config.SERVERS_FILE", str(servers_file)):
        settings = ProxySettings(idle_timeout_seconds=300)
        servers = _load_servers_config(settings)
        assert len(servers) == 1
        assert servers[0].name == "File Server"


@pytest.mark.unit
def test_load_application_config_uses_env_over_json(tmp_path):
    """Ensures environment variables take precedence over files."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"log_level": "file_level"}')

    with patch("config.SETTINGS_FILE", str(settings_file)):
        with patch.dict(os.environ, {"LOG_LEVEL": "env_level"}):
            settings, _ = load_application_config()
            assert settings.log_level == "env_level"
