# tests/test_config.py

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Adjust sys.path to ensure the config module can be found when tests are run.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Imports from the module being tested
from config import (
    DEFAULT_SETTINGS,
    _load_servers_from_env,
    _load_servers_from_json,
    _load_settings_from_json,
    load_application_config,
)


@pytest.mark.unit
def test_load_settings_from_json_decode_error():
    """
    Tests that a JSON decoding error in the settings file is handled gracefully
    and returns an empty dictionary.
    """
    with (
        patch("config.Path.is_file", return_value=True),
        patch("builtins.open"),
        patch(
            "json.load", side_effect=json.JSONDecodeError("JSON error", "", 0)
        ) as mock_json_load,
    ):
        result = _load_settings_from_json(MagicMock())
        assert result == {}
        mock_json_load.assert_called_once()


@pytest.mark.unit
def test_load_servers_from_json_decode_error():
    """
    Tests that a JSON decoding error in the servers file is handled gracefully
    and returns an empty list.
    """
    with (
        patch("config.Path.is_file", return_value=True),
        patch("builtins.open"),
        patch(
            "json.load", side_effect=json.JSONDecodeError("JSON error", "", 0)
        ) as mock_json_load,
    ):
        result = _load_servers_from_json(MagicMock())
        assert result == []
        mock_json_load.assert_called_once()


@pytest.mark.unit
def test_load_servers_from_env_incomplete_definition():
    """
    Tests that an incomplete server definition (e.g., missing container_name)
    in environment variables is skipped.
    """
    with patch.dict(os.environ, {"NB_1_LISTEN_PORT": "12345"}, clear=True):
        result = _load_servers_from_env()
        assert result == []


@pytest.mark.unit
def test_load_servers_from_env_invalid_server_type():
    """
    Tests that a server definition with an invalid server_type
    in environment variables is skipped.
    """
    with patch.dict(
        os.environ,
        {
            "NB_1_LISTEN_PORT": "12345",
            "NB_1_CONTAINER_NAME": "test-container",
            "NB_1_INTERNAL_PORT": "12345",
            "NB_1_SERVER_TYPE": "invalid-type",  # This is the invalid value
        },
        clear=True,
    ):
        result = _load_servers_from_env()
        assert result == []


@pytest.mark.unit
def test_load_config_invalid_env_var_falls_back_to_default():
    """
    Tests that if an environment variable for a setting has an invalid format
    (e.g., text for a number), the application falls back to the default value.
    """
    # Use 'clear=True' to ensure no other environment variables interfere
    with patch.dict(os.environ, {"NB_IDLE_TIMEOUT": "not-a-number"}, clear=True):
        settings, _ = load_application_config()
        # It should ignore the invalid value and use the hardcoded default
        assert settings.idle_timeout_seconds == DEFAULT_SETTINGS["idle_timeout_seconds"]


@pytest.mark.unit
def test_load_servers_from_env_happy_path():
    """
    Tests that a valid and complete server definition from environment
    variables is parsed correctly.
    """
    env_vars = {
        "NB_1_NAME": "My Test Server",
        "NB_1_SERVER_TYPE": "java",
        "NB_1_LISTEN_PORT": "25565",
        "NB_1_CONTAINER_NAME": "mc-java-test",
        "NB_1_INTERNAL_PORT": "25565",
        "NB_1_IDLE_TIMEOUT": "300",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        servers = _load_servers_from_env()
        assert len(servers) == 1
        server_def = servers[0]
        assert server_def["name"] == "My Test Server"
        assert server_def["server_type"] == "java"
        assert server_def["listen_port"] == 25565
        assert server_def["container_name"] == "mc-java-test"
        assert server_def["internal_port"] == 25565
        assert server_def["idle_timeout_seconds"] == 300


@pytest.mark.unit
def test_load_application_config_uses_env_over_json():
    """
    Tests that environment variables have higher precedence than JSON files
    when loading the application configuration.
    """
    settings_json_content = '{"log_level": "INFO", "idle_timeout_seconds": 1000}'

    # Set an environment variable that conflicts with the JSON content
    with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}, clear=True):
        with patch("config.Path.is_file", return_value=True):
            with patch("builtins.open", MagicMock()):
                with patch("json.load", return_value=json.loads(settings_json_content)):
                    settings, _ = load_application_config()

    # The value from the environment variable ("DEBUG") should be used.
    assert settings.log_level == "DEBUG"
    # The value from the JSON file should be used where no env var exists.
    assert settings.idle_timeout_seconds == 1000
