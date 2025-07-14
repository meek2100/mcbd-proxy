import json
from pathlib import Path
from unittest.mock import patch

import pytest

from config import AppSettings, Server, load_application_config


@pytest.fixture
def mock_env():
    """Provides a basic mock for environment variables."""
    # We use patch.dict to ensure we don't interfere with other tests
    # or the real environment.
    with patch.dict("os.environ", {}, clear=True) as mock_environ:
        yield mock_environ


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Creates a temporary directory for config files."""
    d = tmp_path / "config"
    d.mkdir()
    return d


def test_load_settings_from_json(config_dir: Path, mock_env):
    """Tests loading AppSettings from a valid settings.json file."""
    settings_file = config_dir / "settings.json"
    settings_data = {"log_level": "DEBUG", "prometheus_port": 9001}
    settings_file.write_text(json.dumps(settings_data))

    mock_env["CONFIG_PATH"] = str(config_dir)

    settings, _ = load_application_config()

    assert settings.log_level == "DEBUG"
    assert settings.prometheus_port == 9001
    assert settings.log_formatter == "console"  # From default


def test_load_settings_override_with_env(config_dir: Path, mock_env):
    """Ensures environment variables take precedence over settings.json."""
    settings_file = config_dir / "settings.json"
    settings_data = {"log_level": "INFO", "prometheus_port": 9001}
    settings_file.write_text(json.dumps(settings_data))

    mock_env["CONFIG_PATH"] = str(config_dir)
    mock_env["NB_LOG_LEVEL"] = "WARNING"  # This should override 'INFO'
    mock_env["NB_DOCKER_URL"] = "tcp://test:1234"

    settings, _ = load_application_config()

    assert settings.log_level == "WARNING"  # Overridden by env
    assert settings.prometheus_port == 9001  # From file, not in env
    assert settings.docker_url == "tcp://test:1234"  # Overridden by env


def test_load_servers_from_json(config_dir: Path, mock_env):
    """Tests loading a list of Servers from a valid servers.json file."""
    servers_file = config_dir / "servers.json"
    servers_data = [
        {
            "server_name": "mc-creative",
            "listen_port": 25565,
            "listen_host": "0.0.0.0",
            "target_port": 25565,
            "docker_container_name": "mc-creative-container",
        }
    ]
    servers_file.write_text(json.dumps(servers_data))
    mock_env["CONFIG_PATH"] = str(config_dir)

    _, servers = load_application_config()

    assert len(servers) == 1
    assert servers[0] == Server(**servers_data[0])


def test_load_servers_from_env_takes_precedence(config_dir: Path, mock_env):
    """Ensures server config from ENV is used even if servers.json exists."""
    (config_dir / "servers.json").write_text('[{"server_name": "ignore"}]')

    mock_env["CONFIG_PATH"] = str(config_dir)
    mock_env["SERVER_1_NAME"] = "env-server"
    mock_env["SERVER_1_LISTEN_PORT"] = "25570"
    mock_env["SERVER_1_TARGET_PORT"] = "25570"
    mock_env["SERVER_1_DOCKER_CONTAINER"] = "env-container"

    _, servers = load_application_config()

    assert len(servers) == 1
    assert servers[0].server_name == "env-server"
    assert servers[0].listen_port == 25570


def test_load_config_with_no_files_uses_defaults(tmp_path: Path, mock_env):
    """Tests that default settings are used when no config files are present."""
    empty_dir = tmp_path / "empty_config"
    empty_dir.mkdir()
    mock_env["CONFIG_PATH"] = str(empty_dir)

    settings, servers = load_application_config()

    assert settings == AppSettings()  # Should be all defaults
    assert servers == []
