import json
from unittest.mock import patch

import pytest

from config import (
    DEFAULT_SETTINGS,
    ProxySettings,
    ServerConfig,
    _load_servers_config,
    _load_settings,
    load_application_config,
)

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


@patch("builtins.open")
@patch("json.load")
def test_load_settings_from_json_decode_error(mock_json_load, mock_open):
    """
    Tests that if the settings file is corrupt, the function falls back to
    default settings.
    """
    mock_json_load.side_effect = json.JSONDecodeError("JSON error", "", 0)
    settings = _load_settings()
    assert settings == ProxySettings(**DEFAULT_SETTINGS)


@patch("builtins.open")
@patch("json.load")
def test_load_servers_from_json_decode_error(mock_json_load, mock_open):
    """
    Tests that if the servers file is corrupt, the function returns an empty list.
    """
    mock_json_load.side_effect = json.JSONDecodeError("JSON error", "", 0)
    servers = _load_servers_config(ProxySettings())
    assert servers == []


@patch.dict(
    "os.environ",
    {
        "SERVER_1_NAME": "Test Server",
        "SERVER_1_TYPE": "java",
        # Missing LISTEN_PORT
        "SERVER_1_CONTAINER_NAME": "test-container",
        "SERVER_1_INTERNAL_PORT": "25565",
    },
    clear=True,
)
def test_load_servers_from_env_incomplete_definition():
    """
    Tests that servers with incomplete environment variable definitions are skipped.
    """
    servers = _load_servers_config(ProxySettings())
    assert len(servers) == 0


@patch.dict(
    "os.environ",
    {
        "SERVER_1_NAME": "Test Server",
        "SERVER_1_TYPE": "invalid_type",
        "SERVER_1_LISTEN_PORT": "25565",
        "SERVER_1_CONTAINER_NAME": "test-container",
        "SERVER_1_INTERNAL_PORT": "25565",
    },
    clear=True,
)
def test_load_servers_from_env_invalid_server_type():
    """Tests that servers with invalid 'server_type' are skipped."""
    servers = _load_servers_config(ProxySettings())
    assert len(servers) == 0


@patch.dict("os.environ", {"LOG_LEVEL": "invalid"}, clear=True)
def test_load_config_invalid_env_var_falls_back_to_default():
    """
    Tests that if an environment variable for a setting is invalid,
    it falls back to the default value.
    """
    with patch("builtins.open", side_effect=FileNotFoundError):
        settings = _load_settings()
        assert settings.log_level == "info"


@patch.dict(
    "os.environ",
    {
        "SERVER_1_NAME": "Test Server",
        "SERVER_1_TYPE": "java",
        "SERVER_1_LISTEN_PORT": "25565",
        "SERVER_1_CONTAINER_NAME": "test-container",
        "SERVER_1_INTERNAL_PORT": "25565",
    },
    clear=True,
)
def test_load_servers_from_env_happy_path():
    """
    Tests that a server is correctly loaded from environment variables.
    """
    servers = _load_servers_config(ProxySettings())
    assert len(servers) == 1
    assert servers[0].name == "Test Server"


@patch("config._load_settings")
@patch("config._load_servers_config")
def test_load_application_config_uses_env_over_json(
    mock_load_servers, mock_load_settings
):
    """
    Tests that the application correctly prioritizes environment variables
    over JSON files.
    """
    mock_env_settings = ProxySettings(log_level="debug")
    mock_env_servers = [ServerConfig("env-server", "java", 25565, "c", 25565)]

    mock_load_settings.return_value = mock_env_settings
    mock_load_servers.return_value = mock_env_servers

    settings, servers = load_application_config()

    assert settings == mock_env_settings
    assert servers == mock_env_servers
