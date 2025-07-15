# tests/test_config.py
"""
Unit tests for the new Pydantic-based dynamic configuration loading.
"""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from config import load_app_config

pytestmark = pytest.mark.unit


def test_load_config_with_no_servers():
    """Tests that the app loads with defaults when no servers are defined."""
    with patch.dict(os.environ, {}, clear=True):
        config = load_app_config()
        assert config.log_level == "INFO"
        assert config.log_format == "console"
        assert not config.game_servers


def test_load_config_with_dynamic_servers():
    """Tests that servers are loaded dynamically from NB_X_... env vars."""
    mock_env = {
        "NB_LOG_LEVEL": "DEBUG",
        # Server 1: Java
        "NB_1_NAME": "Test Java",
        "NB_1_GAME_TYPE": "java",
        "NB_1_CONTAINER_NAME": "mc-java-test",
        "NB_1_PORT": "25565",
        "NB_1_PROXY_PORT": "25565",
        "NB_1_STOP_AFTER_IDLE": "900",
        # Server 2: Bedrock
        "NB_2_NAME": "Test Bedrock",
        "NB_2_GAME_TYPE": "bedrock",
        "NB_2_CONTAINER_NAME": "mc-bedrock-test",
        "NB_2_PORT": "19132",
        "NB_2_PROXY_PORT": "19132",
        "NB_2_PRE_WARM": "true",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        config = load_app_config()
        assert config.log_level == "DEBUG"
        assert len(config.game_servers) == 2

        # Verify Java server config
        java_server = config.game_servers[0]
        assert java_server.name == "Test Java"
        assert java_server.game_type == "java"
        assert java_server.container_name == "mc-java-test"
        assert java_server.port == 25565
        assert java_server.query_port == 25565  # Should default to port
        assert java_server.stop_after_idle == 900
        assert java_server.pre_warm is False  # Default value

        # Verify Bedrock server config
        bedrock_server = config.game_servers[1]
        assert bedrock_server.name == "Test Bedrock"
        assert bedrock_server.game_type == "bedrock"
        assert bedrock_server.port == 19132
        assert bedrock_server.stop_after_idle == 300  # Default value
        assert bedrock_server.pre_warm is True


def test_load_config_with_query_port_override():
    """Tests that query_port can be set independently from the game port."""
    mock_env = {
        "NB_1_CONTAINER_NAME": "mc-test",
        "NB_1_GAME_TYPE": "java",
        "NB_1_PORT": "25565",
        "NB_1_PROXY_PORT": "25565",
        "NB_1_QUERY_PORT": "25575",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        config = load_app_config()
        assert config.game_servers[0].port == 25565
        assert config.game_servers[0].query_port == 25575


def test_load_config_missing_required_field_raises_error():
    """Tests that a ValidationError is raised if a required field is missing."""
    mock_env = {
        "NB_1_NAME": "Incomplete Server",
        # Missing NB_1_CONTAINER_NAME, NB_1_GAME_TYPE, etc.
    }
    with patch.dict(os.environ, mock_env, clear=True):
        with pytest.raises(ValidationError):
            load_app_config()


def test_load_config_invalid_value_type_raises_error():
    """
    Tests that a ValidationError is raised if an env var has an invalid type.
    """
    mock_env = {
        "NB_1_CONTAINER_NAME": "mc-test",
        "NB_1_GAME_TYPE": "java",
        "NB_1_PORT": "not-a-number",
        "NB_1_PROXY_PORT": "25565",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        with pytest.raises(ValidationError):
            load_app_config()
