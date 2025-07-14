# tests/test_main.py
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from config import AppSettings, Server
from main import main, perform_health_check

# Minimal logging config for tests to avoid errors
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
    """Mocks all major dependencies of the main function."""
    with (
        patch("main.DockerManager", autospec=True) as mock_docker_class,
        patch("main.NetherBridgeProxy", autospec=True) as mock_proxy_class,
        patch("main.start_metrics_server") as mock_metrics,
        patch("main.configure_logging"),
    ):
        mock_proxy_instance = mock_proxy_class.return_value
        mock_proxy_instance.run = AsyncMock()
        mock_proxy_instance.ensure_all_servers_stopped_on_startup = AsyncMock()

        yield {
            "docker_manager": mock_docker_class.return_value,
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

    # When the proxy's run method is awaited, it will wait on our event
    mock_dependencies["proxy"].run.side_effect = shutdown_event.wait

    with patch("asyncio.Event", return_value=shutdown_event):
        main_task = asyncio.create_task(main())
        await asyncio.sleep(0.01)  # Allow task to start
        shutdown_event.set()  # Trigger the shutdown
        await main_task

    # Assert that startup and shutdown procedures were called
    mock_dependencies[
        "proxy"
    ].ensure_all_servers_stopped_on_startup.assert_awaited_once()
    mock_dependencies["proxy"].run.assert_awaited_once()
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
        mock_load.return_value = (AppSettings(), [])  # Return no servers
        await main()
        mock_exit.assert_called_with(1)


@pytest.mark.asyncio
async def test_main_healthcheck_argument():
    """
    Tests that the app calls the health check function when launched with
    the --healthcheck argument.
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
@patch("main.load_application_config")
def test_perform_health_check_ok(
    mock_load_config, mock_time, mock_is_file, mock_read_text, mock_exit
):
    """Tests the health check logic for a success case."""
    mock_time.return_value = 1000
    mock_is_file.return_value = True
    mock_read_text.return_value = "990"  # 10 seconds old, within threshold
    mock_load_config.return_value = (AppSettings(), [MagicMock()])

    perform_health_check(Path("/app/config"))
    mock_exit.assert_called_with(0)
