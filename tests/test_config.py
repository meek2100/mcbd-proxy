# tests/test_config.py
"""
Unit tests for the new Pydantic-based configuration loading.
"""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from config import load_app_config


@pytest.mark.unit
def test_load_app_config_defaults():
    """
    Tests that the application loads with default values when no
    environment variables are set.
    """
    with patch.dict(os.environ, {}, clear=True):
        config = load_app_config()
        assert config.log_level == "INFO"
        assert config.log_format == "console"
        assert config.is_prometheus_enabled is True
        assert config.prometheus_port == 8000
        assert len(config.game_servers) == 2
        assert config.game_servers[0].name == "mc-java"
        assert config.game_servers[0].pre_warm is False
        assert config.game_servers[1].name == "mc-bedrock"
        assert config.game_servers[1].stop_after_idle == 300


@pytest.mark.unit
def test_load_app_config_with_env_vars():
    """
    Tests that the application correctly loads configuration from
    environment variables.
    """
    mock_env = {
        "NB_LOG_LEVEL": "DEBUG",
        "NB_PROMETHEUS_ENABLED": "false",
        "NB_JAVA_PRE_WARM": "true",
        "NB_BEDROCK_STOP_AFTER_IDLE": "999",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        config = load_app_config()
        assert config.log_level == "DEBUG"
        assert config.is_prometheus_enabled is False
        assert config.game_servers[0].pre_warm is True
        assert config.game_servers[1].stop_after_idle == 999


@pytest.mark.unit
def test_load_app_config_validation_error():
    """
    Tests that a Pydantic ValidationError is raised if an environment
    variable has an invalid type.
    """
    mock_env = {"NB_PROMETHEUS_PORT": "not-a-number"}
    with patch.dict(os.environ, mock_env, clear=True):
        with pytest.raises(ValidationError):
            load_app_config()
