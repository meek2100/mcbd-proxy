# tests/test_main.py
import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import the class and functions to be tested
from main import Application, main


@pytest.mark.unit
@patch("main.health_check")
def test_main_runs_health_check(mock_health_check):
    """Tests that main() calls health_check() when --healthcheck is passed."""
    with patch("main.sys.argv", ["main.py", "--healthcheck"]):
        main()
    mock_health_check.assert_called_once()


@pytest.mark.unit
@patch("main.Application.run", new_callable=AsyncMock)
def test_main_runs_application(mock_app_run):
    """Tests that main() creates and runs the Application."""
    with patch("main.sys.argv", ["main.py"]):
        main()
    mock_app_run.assert_awaited_once()


@pytest.mark.asyncio
@patch("main.sys.platform", "linux")
@patch("main.asyncio.get_running_loop")
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.load_app_config")
async def test_application_lifecycle(
    mock_load_config,
    mock_configure_logging,
    mock_async_proxy_class,
    mock_docker_manager_class,
    mock_get_loop,
):
    """Verify Application.run orchestrates startup, task management, and cleanup."""
    # GIVEN
    mock_docker_instance = AsyncMock()
    mock_docker_manager_class.return_value = mock_docker_instance
    mock_proxy_instance = AsyncMock()
    mock_async_proxy_class.return_value = mock_proxy_instance
    mock_app_config = MagicMock(game_servers=[MagicMock()])
    mock_load_config.return_value = mock_app_config

    mock_loop = MagicMock()
    mock_loop.add_signal_handler = MagicMock()
    mock_get_loop.return_value = mock_loop

    app = Application()

    # WHEN we run the application but immediately cancel the main gatherer
    with patch("main.asyncio.gather", new_callable=AsyncMock) as mock_gather:
        mock_gather.side_effect = asyncio.CancelledError  # Simulate shutdown
        await app.run()

    # THEN
    mock_async_proxy_class.assert_called_once_with(
        mock_app_config, mock_docker_instance
    )
    assert mock_loop.add_signal_handler.call_count == 3
    mock_proxy_instance.start.assert_called_once()
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_shutdown():
    """Verify the Application.shutdown method cancels tasks."""
    app = Application()
    app.proxy_server = AsyncMock()
    # Create real, simple tasks that can be cancelled
    task1 = asyncio.create_task(asyncio.sleep(0.1))
    task2 = asyncio.create_task(asyncio.sleep(0.1))
    app.tasks = [task1, task2]

    await app.shutdown(signal.SIGINT)

    assert app.shutdown_event.is_set()
    assert task1.cancelled()
    assert task2.cancelled()
    app.proxy_server.shutdown.assert_awaited_once()
