import sys
import pytest
import os
import json
import time
import socket
from unittest.mock import MagicMock, patch
import docker

# Adjusting sys.path to allow importing nether_bridge.py from the parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from nether_bridge import NetherBridgeProxy, ProxySettings, ServerConfig

# --- Fixtures for common test setup ---

@pytest.fixture
def mock_docker_client():
    """Mocks the docker.client.DockerClient object."""
    mock_client = MagicMock()
    mock_container_obj = MagicMock(status='running')
    mock_client.containers.get.return_value = mock_container_obj
    yield mock_client

@pytest.fixture
def default_proxy_settings():
    """Provides default ProxySettings for testing."""
    return ProxySettings(
        idle_timeout_seconds=0.2,
        player_check_interval_seconds=0.1,
        query_timeout_seconds=0.1,
        server_ready_max_wait_time_seconds=0.5,
        initial_boot_ready_max_wait_time_seconds=0.5,
        server_startup_delay_seconds=0,
        initial_server_query_delay_seconds=0,
        log_level="DEBUG",
        healthcheck_stale_threshold_seconds=60,
        proxy_heartbeat_interval_seconds=15
    )

@pytest.fixture
def mock_servers_config():
    """Provides a list of mock ServerConfig objects."""
    return [
        ServerConfig(
            name="Bedrock Test", server_type="bedrock", listen_port=19132,
            container_name="test-mc-bedrock", internal_port=19132
        ),
        ServerConfig(
            name="Java Test", server_type="java", listen_port=25565,
            container_name="test-mc-java", internal_port=25565
        )
    ]

@pytest.fixture
def nether_bridge_instance(default_proxy_settings, mock_servers_config, mock_docker_client):
    """Provides a NetherBridgeProxy instance with mocked Docker client."""
    proxy = NetherBridgeProxy(default_proxy_settings, mock_servers_config)
    proxy.docker_client = mock_docker_client
    return proxy

@pytest.fixture
def mock_container():
    """Provides a reusable MagicMock for a Docker container."""
    return MagicMock()

# --- Test Cases ---

def test_proxy_initialization(nether_bridge_instance, default_proxy_settings, mock_servers_config):
    assert nether_bridge_instance.settings == default_proxy_settings
    assert nether_bridge_instance.servers_list == mock_servers_config
    assert "test-mc-bedrock" in nether_bridge_instance.server_states

def test_is_container_running_exists_and_running(nether_bridge_instance, mock_docker_client, mock_container):
    mock_container.status = 'running'
    mock_docker_client.containers.get.return_value = mock_container
    assert nether_bridge_instance._is_container_running("test-mc-bedrock") is True

def test_is_container_running_exists_and_stopped(nether_bridge_instance, mock_docker_client, mock_container):
    mock_container.status = 'exited'
    mock_docker_client.containers.get.return_value = mock_container
    assert nether_bridge_instance._is_container_running("test-mc-bedrock") is False

def test_is_container_running_not_found(nether_bridge_instance, mock_docker_client):
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("No such container")
    assert nether_bridge_instance._is_container_running("non-existent-container") is False

@patch('nether_bridge.NetherBridgeProxy._wait_for_server_query_ready', return_value=True)
def test_start_minecraft_server_success(mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'exited'
    mock_docker_client.containers.get.return_value = mock_container
    nether_bridge_instance.server_states[container_name]["running"] = False
    result = nether_bridge_instance._start_minecraft_server(container_name)
    assert result is True
    mock_container.start.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is True

@patch('nether_bridge.NetherBridgeProxy._wait_for_server_query_ready', return_value=True)
def test_start_minecraft_server_already_running(mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'running'
    mock_docker_client.containers.get.return_value = mock_container
    result = nether_bridge_instance._start_minecraft_server(container_name)
    assert result is True
    mock_container.start.assert_not_called()

@patch('nether_bridge.NetherBridgeProxy._wait_for_server_query_ready', return_value=True)
def test_start_minecraft_server_docker_api_error(mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'exited'
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.start.side_effect = docker.errors.APIError("API error")
    result = nether_bridge_instance._start_minecraft_server(container_name)
    assert result is False

@patch('nether_bridge.NetherBridgeProxy._wait_for_server_query_ready', return_value=False)
def test_start_minecraft_server_readiness_timeout(mock_wait_ready, nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'exited'
    mock_docker_client.containers.get.return_value = mock_container
    result = nether_bridge_instance._start_minecraft_server(container_name)
    assert result is True
    mock_wait_ready.assert_called_once()

def test_stop_minecraft_server_success(nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'running'
    mock_docker_client.containers.get.return_value = mock_container
    result = nether_bridge_instance._stop_minecraft_server(container_name)
    assert result is True
    mock_container.stop.assert_called_once()
    assert nether_bridge_instance.server_states[container_name]["running"] is False

def test_stop_minecraft_server_already_stopped(nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'exited'
    mock_docker_client.containers.get.return_value = mock_container
    result = nether_bridge_instance._stop_minecraft_server(container_name)
    assert result is True
    mock_container.stop.assert_not_called()

def test_stop_minecraft_server_docker_api_error(nether_bridge_instance, mock_docker_client, mock_container):
    container_name = "test-mc-bedrock"
    mock_container.status = 'running'
    mock_docker_client.containers.get.return_value = mock_container
    mock_container.stop.side_effect = docker.errors.APIError("API error")
    result = nether_bridge_instance._stop_minecraft_server(container_name)
    assert result is False

def test_stop_minecraft_server_not_found_on_stop(nether_bridge_instance, mock_docker_client):
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("Not Found")
    result = nether_bridge_instance._stop_minecraft_server("test-mc-bedrock")
    assert result is True

@patch('nether_bridge.BedrockServer.lookup')
@patch('nether_bridge.JavaServer.lookup')
def test_wait_for_server_query_ready_bedrock_success(mock_java_lookup, mock_bedrock_lookup, nether_bridge_instance, mock_servers_config):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == 'bedrock')
    mock_server_instance = MagicMock()
    mock_server_instance.status.return_value = MagicMock(latency=50)
    mock_bedrock_lookup.return_value = mock_server_instance
    result = nether_bridge_instance._wait_for_server_query_ready(bedrock_config, 1, 1)
    assert result is True
    mock_bedrock_lookup.assert_called_once()
    mock_server_instance.status.assert_called_once()

@patch('nether_bridge.BedrockServer.lookup')
@patch('nether_bridge.JavaServer.lookup')
def test_wait_for_server_query_ready_java_success(mock_java_lookup, mock_bedrock_lookup, nether_bridge_instance, mock_servers_config):
    java_config = next(s for s in mock_servers_config if s.server_type == 'java')
    mock_server_instance = MagicMock()
    mock_server_instance.status.return_value = MagicMock(latency=50)
    mock_java_lookup.return_value = mock_server_instance
    result = nether_bridge_instance._wait_for_server_query_ready(java_config, 1, 1)
    assert result is True
    mock_java_lookup.assert_called_once()
    mock_server_instance.status.assert_called_once()

@patch('nether_bridge.time.sleep')
@patch('nether_bridge.BedrockServer.lookup', side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_bedrock_timeout(mock_bedrock_lookup, mock_sleep, nether_bridge_instance, mock_servers_config):
    bedrock_config = next(s for s in mock_servers_config if s.server_type == 'bedrock')
    result = nether_bridge_instance._wait_for_server_query_ready(bedrock_config, 0.2, 0.1)
    assert result is False

@patch('nether_bridge.time.sleep')
@patch('nether_bridge.JavaServer.lookup', side_effect=Exception("Query failed"))
def test_wait_for_server_query_ready_java_timeout(mock_java_lookup, mock_sleep, nether_bridge_instance, mock_servers_config):
    java_config = next(s for s in mock_servers_config if s.server_type == 'java')
    result = nether_bridge_instance._wait_for_server_query_ready(java_config, 0.2, 0.1)
    assert result is False

# MODIFIED TEST: Used side_effect=[None, InterruptedError]
@patch('nether_bridge.time.sleep', side_effect=[None, InterruptedError])
@patch('nether_bridge.NetherBridgeProxy._stop_minecraft_server')
@patch('mcstatus.BedrockServer.lookup')
def test_monitor_servers_activity_stops_idle_server(mock_lookup, mock_stop, mock_sleep, nether_bridge_instance, mock_servers_config):
    container_name = "test-mc-bedrock"
    state = nether_bridge_instance.server_states[container_name]
    state["running"] = True
    state["last_activity"] = time.time() - 20 # Ensure server is idle
    nether_bridge_instance.settings.idle_timeout_seconds = 10
    
    mock_status = MagicMock(players=MagicMock(online=0))
    mock_lookup.return_value.status.return_value = mock_status

    with patch.object(nether_bridge_instance, 'active_sessions', {}):
        with pytest.raises(InterruptedError):
            nether_bridge_instance._monitor_servers_activity()
    
    # Assert that _stop_minecraft_server was called once, as expected for an idle server
    mock_stop.assert_called_once_with(container_name)

# MODIFIED TEST: Used side_effect=[None, InterruptedError]
@patch('nether_bridge.time.sleep', side_effect=[None, InterruptedError])
@patch('nether_bridge.NetherBridgeProxy._stop_minecraft_server')
@patch('mcstatus.BedrockServer.lookup')
def test_monitor_servers_activity_resets_active_server_timer(mock_lookup, mock_stop, mock_sleep, nether_bridge_instance, mock_servers_config):
    container_name = "test-mc-bedrock"
    state = nether_bridge_instance.server_states[container_name]
    state["running"] = True
    original_time = time.time() - 5 # Set some past activity
    state["last_activity"] = original_time

    mock_status = MagicMock(players=MagicMock(online=1)) # Mock active players
    mock_lookup.return_value.status.return_value = mock_status
    
    with patch.object(nether_bridge_instance, 'active_sessions', {}):
        with pytest.raises(InterruptedError):
            nether_bridge_instance._monitor_servers_activity()
    
    mock_stop.assert_not_called() # Should not stop as players are active
    assert state["last_activity"] > original_time # last_activity should have been updated

# MODIFIED TEST: Used side_effect=[None, InterruptedError]
@patch('nether_bridge.time.sleep', side_effect=[None, InterruptedError])
@patch('nether_bridge.NetherBridgeProxy._stop_minecraft_server')
@patch('mcstatus.BedrockServer.lookup', side_effect=Exception("Query failed"))
def test_monitor_servers_activity_handles_query_failure(mock_lookup, mock_stop, mock_sleep, nether_bridge_instance, mock_servers_config):
    container_name = "test-mc-bedrock"
    state = nether_bridge_instance.server_states[container_name]
    state["running"] = True
    state["last_activity"] = time.time() - 20 # Ensure server is idle
    nether_bridge_instance.settings.idle_timeout_seconds = 10

    with patch.object(nether_bridge_instance, 'active_sessions', {}):
        with pytest.raises(InterruptedError):
            nether_bridge_instance._monitor_servers_activity()
            
    # Assert that _stop_minecraft_server was called, as query failure with idle timeout should stop it
    mock_stop.assert_called_once_with(container_name)