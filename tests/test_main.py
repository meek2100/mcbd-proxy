import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from config import AppSettings, Server
from main import main

# Minimal logging config for tests
structlog.configure(processors=[structlog.dev.ConsoleRenderer()])


@pytest.fixture
def mock_config():
    """Fixture to mock configuration loading."""
    settings = AppSettings()
    servers = [
        Server(
            server_name="test-server",
            listen_port=25565,
            listen_host="0.0.0.0",
            target_port=25565,
            docker_container_name="mc-test-server",
        )
    ]
    with patch("main.load_application_config") as mock_load:
        mock_load.return_value = (settings, servers)
        yield mock_load


@pytest.fixture
def mock_dependencies():
    """
    Fixture to mock all major dependencies of the main function,
    ensuring async methods are correctly mocked.
    """
    with (
        patch("main.DockerManager") as mock_docker_class,
        patch("main.NetherBridgeProxy") as mock_proxy_class,
        patch("main.start_metrics_server") as mock_metrics,
        patch("main.configure_logging"),
    ):
        # Configure the INSTANCES that will be created from the mocked classes
        mock_docker_instance = mock_docker_class.return_value
        mock_proxy_instance = mock_proxy_class.return_value

        # Ensure that ALL methods we await in the main loop are AsyncMocks
        mock_docker_instance.close = AsyncMock()
        mock_proxy_instance.run = AsyncMock()
        mock_proxy_instance._shutdown_all_sessions = AsyncMock()
        mock_proxy_instance._close_listeners = AsyncMock()
        # FIX: Add the missing mock for the pre-warm startup check
        mock_proxy_instance.ensure_all_servers_stopped_on_startup = AsyncMock()

        yield {
            "docker_manager": mock_docker_instance,
            "proxy": mock_proxy_instance,
            "metrics": mock_metrics,
        }


@pytest.mark.asyncio
async def test_main_startup_and_shutdown(mock_config, mock_dependencies):
    """
    Tests the main application startup and graceful shutdown by controlling
    the shutdown event directly.
    """
    shutdown_event = asyncio.Event()
    mock_dependencies["proxy"].run.side_effect = shutdown_event.wait

    with patch("main.asyncio.Event", return_value=shutdown_event):
        main_task = asyncio.create_task(main())
        await asyncio.sleep(0.01)
        shutdown_event.set()
        await main_task

    # Assert that all startup and shutdown procedures were called
    mock_dependencies[
        "proxy"
    ].ensure_all_servers_stopped_on_startup.assert_awaited_once()
    mock_dependencies["proxy"]._shutdown_all_sessions.assert_awaited_once()
    mock_dependencies["proxy"]._close_listeners.assert_awaited_once()
    mock_dependencies["docker_manager"].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_no_servers_configured_exits(mock_dependencies):
    """
    Tests that the application exits if no servers are found in the config.
    """
    with (
        patch("main.load_application_config") as mock_load,
        patch("sys.exit") as mock_exit,
    ):
        mock_load.return_value = (AppSettings(), [])  # No servers
        await main()
        mock_exit.assert_called_with(1)


@pytest.mark.asyncio
async def test_main_healthcheck_argument(mock_config):
    """
    Tests that the app calls the health check function with --healthcheck.
    """
    with (
        patch.object(sys, "argv", ["main.py", "--healthcheck"]),
        patch("main.perform_health_check") as mock_health_check,
    ):
        await main()
        mock_health_check.assert_called_once()


@patch("sys.exit")
@patch("pathlib.Path.read_text")
@patch("pathlib.Path.is_file")
@patch("time.time")
def test_perform_health_check_ok(
    mock_time, mock_is_file, mock_read_text, mock_exit, mock_config
):
    """Tests the health check logic for a success case."""
    from pathlib import Path

    from main import perform_health_check

    mock_time.return_value = 1000
    mock_is_file.return_value = True
    mock_read_text.return_value = "990"

    perform_health_check(Path("/app/config"))
    mock_exit.assert_called_with(0)


@patch("sys.exit")
@patch("pathlib.Path.is_file")
def test_perform_health_check_no_heartbeat_file(mock_is_file, mock_exit, mock_config):
    """Tests the health check logic when the heartbeat file is missing."""
    from pathlib import Path

    from main import perform_health_check

    mock_is_file.return_value = False
    perform_health_check(Path("/app/config"))
    mock_exit.assert_called_with(1)
