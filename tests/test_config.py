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
        "LOG_LEVEL": "DEBUG",
        # Server 1: Java
        "NB_1_NAME": "Test Java",
        "NB_1_SERVER_TYPE": "java",
        "NB_1_CONTAINER_NAME": "mc-java-test",
        "NB_1_INTERNAL_PORT": "25565",
        "NB_1_LISTEN_PORT": "25565",
        "NB_1_IDLE_TIMEOUT": "300",
        # Server 2: Bedrock
        "NB_2_NAME": "Test Bedrock",
        "NB_2_SERVER_TYPE": "bedrock",
        "NB_2_CONTAINER_NAME": "mc-bedrock-test",
        "NB_2_INTERNAL_PORT": "19132",
        "NB_2_LISTEN_PORT": "19132",
        "NB_2_PRE_WARM": "true",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        config = load_app_config()
        assert config.log_level == "DEBUG"
        assert len(config.game_servers) == 2

        java_server = config.game_servers[0]
        assert java_server.name == "Test Java"
        assert java_server.game_type == "java"
        assert java_server.container_name == "mc-java-test"
        assert java_server.port == 25565
        assert java_server.query_port == 25565
        assert java_server.pre_warm is False
        assert java_server.idle_timeout == 300

        bedrock_server = config.game_servers[1]
        assert bedrock_server.name == "Test Bedrock"
        assert bedrock_server.game_type == "bedrock"
        assert bedrock_server.port == 19132
        assert bedrock_server.pre_warm is True
        assert bedrock_server.idle_timeout is None


def test_load_config_with_query_port_override():
    """Tests that query_port can be set independently from the game port."""
    mock_env = {
        "NB_1_CONTAINER_NAME": "mc-test",
        "NB_1_SERVER_TYPE": "java",
        "NB_1_INTERNAL_PORT": "25565",
        "NB_1_LISTEN_PORT": "25565",
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
        "NB_1_CONTAINER_NAME": "mc-incomplete",
        "NB_1_INTERNAL_PORT": "12345",
        "NB_1_LISTEN_PORT": "12345",
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
        "NB_1_SERVER_TYPE": "java",
        "NB_1_INTERNAL_PORT": "not-a-number",
        "NB_1_LISTEN_PORT": "25565",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        with pytest.raises(ValidationError):
            load_app_config()
