# tests/test_metrics.py
"""
Unit tests for the Prometheus MetricsManager.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from metrics import MetricsManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_app_config():
    """Fixture for a mock AppConfig."""
    config = MagicMock()
    server = MagicMock()
    server.name = "test_server"
    config.game_servers = [server]
    config.is_prometheus_enabled = True
    config.player_check_interval = 60  # Corrected attribute name
    config.prometheus_port = 8000
    return config


@pytest.fixture
def mock_docker_manager():
    """Fixture for a mock DockerManager."""
    return AsyncMock()


@pytest.fixture
def metrics_manager(mock_app_config, mock_docker_manager):
    """Fixture for a MetricsManager instance."""
    return MetricsManager(mock_app_config, mock_docker_manager)


def test_metrics_manager_init_enabled(metrics_manager, mock_app_config):
    """Tests that gauges are initialized when Prometheus is enabled."""
    assert metrics_manager._is_enabled is True
    # In a real scenario, prometheus_client would set the label.
    # Here we just confirm the manager initialized correctly.


def test_metrics_manager_init_disabled(mock_app_config, mock_docker_manager):
    """Tests that no gauges are initialized when Prometheus is disabled."""
    mock_app_config.is_prometheus_enabled = False
    manager = MetricsManager(mock_app_config, mock_docker_manager)
    assert manager._is_enabled is False


@patch("metrics.active_connections_gauge")
def test_inc_active_connections(mock_gauge, metrics_manager):
    """Tests incrementing the active connections gauge."""
    metrics_manager.inc_active_connections("test_server")
    mock_gauge.labels.assert_called_once_with(server="test_server")
    mock_gauge.labels.return_value.inc.assert_called_once()


@patch("metrics.active_connections_gauge")
def test_dec_active_connections(mock_gauge, metrics_manager):
    """Tests decrementing the active connections gauge."""
    metrics_manager.dec_active_connections("test_server")
    mock_gauge.labels.assert_called_once_with(server="test_server")
    mock_gauge.labels.return_value.dec.assert_called_once()


@patch("metrics.server_startup_duration_histogram")
def test_observe_startup_duration(mock_histogram, metrics_manager):
    """Tests observing server startup duration."""
    duration = 15.5
    metrics_manager.observe_startup_duration("test_server", duration)
    mock_histogram.labels.assert_called_once_with(server="test_server")
    mock_histogram.labels.return_value.observe.assert_called_once_with(duration)


@patch("metrics.bytes_transferred_counter")
def test_inc_bytes_transferred(mock_counter, metrics_manager):
    """Tests incrementing the bytes transferred counter."""
    size = 1024
    direction = "c2s"
    metrics_manager.inc_bytes_transferred("test_server", direction, size)
    mock_counter.labels.assert_called_once_with(
        server="test_server", direction=direction
    )
    mock_counter.labels.return_value.inc.assert_called_once_with(size)


@pytest.mark.asyncio
@patch("metrics.start_http_server")
@patch("metrics.asyncio.create_task")
@patch("metrics.log")
async def test_metrics_manager_start_handles_http_server_failure(
    mock_log, mock_create_task, mock_start_http_server, metrics_manager
):
    """
    Tests that start() handles exceptions during Prometheus HTTP server startup.
    """
    mock_start_http_server.side_effect = Exception("Port already in use")

    # Mock the periodic update task to be cancelled to allow test to finish
    mock_periodic_update_task = AsyncMock()
    mock_create_task.return_value = mock_periodic_update_task

    await metrics_manager.start()

    mock_start_http_server.assert_called_once_with(
        metrics_manager.app_config.prometheus_port
    )
    mock_log.error.assert_called_once_with(
        "Failed to start Prometheus server", exc_info=True
    )
    # Ensure the periodic update task is NOT created if http server fails
    mock_create_task.assert_not_called()


@pytest.mark.asyncio
@patch("metrics.server_status_gauge")
@patch("metrics.total_running_servers_gauge")
@patch("metrics.asyncio.sleep")
async def test_update_server_status_periodically(
    mock_sleep,
    mock_total_running_gauge,
    mock_server_status_gauge,
    metrics_manager,
    mock_app_config,
    mock_docker_manager,
):
    """
    Tests that _update_server_status_periodically correctly updates gauges.
    """
    mock_sleep.side_effect = asyncio.CancelledError  # Allow task to exit

    # Simulate server running then stopped
    mock_docker_manager.is_container_running.side_effect = [
        True,  # First check, server is running
        False,  # Second check, server is stopped (after re-loop)
    ]

    # Run loop for a short time, then cancel
    try:
        await metrics_manager._update_server_status_periodically()
    except asyncio.CancelledError:
        pass

    # Assertions for first iteration (server is running)
    mock_server_status_gauge.labels.assert_any_call(
        server=mock_app_config.game_servers[0].name
    )
    mock_server_status_gauge.labels.return_value.set.assert_any_call(1)
    mock_total_running_gauge.set.assert_any_call(1)

    # Reset mocks for next check (or use assert_has_calls for sequence)
    mock_server_status_gauge.labels.return_value.set.reset_mock()
    mock_total_running_gauge.set.reset_mock()

    # Simulate another iteration (server is stopped)
    try:
        await metrics_manager._update_server_status_periodically()
    except asyncio.CancelledError:
        pass

    mock_server_status_gauge.labels.return_value.set.assert_any_call(0)
    mock_total_running_gauge.set.assert_any_call(0)

    # Assert sleep uses correct interval
    mock_sleep.assert_awaited_with(mock_app_config.player_check_interval)

    # Test cancellation handling
    metrics_manager.log.info.assert_called_with("Metrics manager task cancelled.")
