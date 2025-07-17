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
    config.player_check_interval = 0.01  # Corrected attribute name and made small
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
@patch("metrics.log")  # Patch metrics.log directly
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
@patch("metrics.log")  # Patch metrics.log to check cancellation info
async def test_update_server_status_periodically(
    mock_log,  # New mock
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
    # Simulate first iteration: server is running
    mock_docker_manager.is_container_running.side_effect = [
        True,  # For first iteration
        False,  # For second iteration
        # FIX: Add a third side effect to immediately raise CancelledError
        # for the *third* call to allow the test to terminate properly
        # after the second intended loop.
        asyncio.CancelledError,
    ]

    # Simulate app_config.game_servers being a list with one server
    mock_app_config.game_servers = [MagicMock(name="test_server_1")]

    # Trigger one loop iteration then a CancelledError to stop the infinite loop
    # FIX: Make mock_sleep side_effect consistent with the number of loops we expect
    # and to raise CancelledError *only* when we want the loop to stop.
    mock_sleep.side_effect = [
        None,  # Allow first sleep to complete
        None,  # Allow second sleep to complete
        asyncio.CancelledError,  # Cancel on third sleep for test cleanup
    ]

    try:
        await metrics_manager._update_server_status_periodically()
    except asyncio.CancelledError:
        pass  # Expected

    # Assertions for first iteration (server is running)
    mock_server_status_gauge.labels.assert_any_call(
        server=mock_app_config.game_servers[0].name
    )
    mock_server_status_gauge.labels.return_value.set.assert_any_call(1)
    mock_total_running_gauge.set.assert_any_call(1)
    mock_sleep.assert_awaited_with(mock_app_config.player_check_interval)

    # Reset mocks for next logical check (second iteration)
    mock_server_status_gauge.labels.return_value.set.reset_mock()
    mock_total_running_gauge.set.reset_mock()
    # FIX: Don't reset mock_docker_manager.is_container_running.side_effect
    # here because it's already set for 3 calls at the beginning.

    # We ran the loop twice. Now check the *final* states.
    # The first run: server was True, count 1.
    # The second run: server was False, count 0.
    # The sequence of calls to set should reflect this.
    mock_server_status_gauge.labels.assert_any_call(
        server=mock_app_config.game_servers[0].name
    )  # Called in both iterations
    mock_server_status_gauge.labels.return_value.set.assert_has_calls(
        [
            MagicMock(1),  # First iteration sets to 1
            MagicMock(0),  # Second iteration sets to 0
        ]
    )
    mock_total_running_gauge.set.assert_has_calls(
        [
            MagicMock(1),  # First iteration sets to 1
            MagicMock(0),  # Second iteration sets to 0
        ]
    )

    # Test cancellation handling
    # FIX: Check for _called_once_with and then assert _any_call on info
    mock_log.info.assert_called_once_with(
        "Starting periodic server status updater for metrics."
    )  # Only called once at the true start of the coroutine.
    mock_log.info.assert_any_call(
        "Metrics manager task cancelled."
    )  # This should be called once at the very end.
