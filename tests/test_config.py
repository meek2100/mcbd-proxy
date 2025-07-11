# tests/test_config.py
import os
from unittest.mock import patch

import pytest

from config import load_application_config


@pytest.fixture(autouse=True)
def clear_env_vars():
    """Clears relevant environment variables before each test to ensure isolation."""
    env_vars_to_clear = [
        "SETTINGS_FILE",
        "SERVERS_FILE",
        "LOG_LEVEL",
        "IDLE_TIMEOUT_SECONDS",
        "PROMETHEUS_ENABLED",
        # Add server vars just in case
        "SERVER_1_NAME",
        "SERVER_1_TYPE",
        "SERVER_1_LISTEN_PORT",
        "SERVER_1_CONTAINER_NAME",
        "SERVER_1_INTERNAL_PORT",
    ]
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
def test_load_config_from_env():
    """
    Tests that the application correctly loads its entire configuration
    from environment variables.
    """
    with patch.dict(
        os.environ,
        {
            "IDLE_TIMEOUT_SECONDS": "500",
            "SERVER_1_NAME": "EnvServer",
            "SERVER_1_TYPE": "java",
            "SERVER_1_LISTEN_PORT": "25565",
            "SERVER_1_CONTAINER_NAME": "env-java",
            "SERVER_1_INTERNAL_PORT": "25565",
        },
    ):
        settings, servers = load_application_config()

        # Assert that settings and servers were loaded correctly
        assert settings.idle_timeout_seconds == 500
        assert len(servers) == 1
        assert servers[0].name == "EnvServer"


@pytest.mark.unit
def test_load_config_from_files(tmp_path):
    """
    Tests that the application correctly loads its configuration from
    JSON files when environment variables are not present.
    """
    settings_content = '{"idle_timeout_seconds": 200}'
    servers_content = """
    {
        "servers": [
            {
                "name": "FileServer", "server_type": "bedrock",
                "listen_port": 19132, "container_name": "file-bedrock",
                "internal_port": 19132
            }
        ]
    }
    """
    settings_file = tmp_path / "settings.json"
    servers_file = tmp_path / "servers.json"
    settings_file.write_text(settings_content)
    servers_file.write_text(servers_content)

    with patch.dict(
        os.environ,
        {
            "SETTINGS_FILE": str(settings_file),
            "SERVERS_FILE": str(servers_file),
        },
    ):
        settings, servers = load_application_config()

        assert settings.idle_timeout_seconds == 200
        assert len(servers) == 1
        assert servers[0].name == "FileServer"


@pytest.mark.unit
def test_env_overrides_files(tmp_path):
    """
    Ensures that if both file and environment configurations are present,
    the environment variables take precedence.
    """
    settings_content = '{"idle_timeout_seconds": 200}'
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(settings_content)

    with patch.dict(
        os.environ,
        {
            "SETTINGS_FILE": str(settings_file),
            "IDLE_TIMEOUT_SECONDS": "999",  # This should win
        },
    ):
        settings, _ = load_application_config()
        assert settings.idle_timeout_seconds == 999
