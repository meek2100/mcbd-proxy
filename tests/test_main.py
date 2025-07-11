# tests/test_main.py
from unittest.mock import AsyncMock, patch

import pytest

from main import main


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.load_application_config")
@patch("main.DockerManager")
@patch("main.NetherBridgeProxy")
async def test_main_runs_proxy_and_handles_shutdown(
    mock_proxy_class, mock_docker_manager_class, mock_load_config
):
    """
    Tests that the main function initializes and runs the proxy.
    """
    mock_settings = "mock_settings"
    mock_servers = ["mock_server"]
    mock_load_config.return_value = (mock_settings, mock_servers)
    mock_proxy_instance = AsyncMock()

    async def set_shutdown_event(*args, **kwargs):
        proxy_init_args = mock_proxy_class.call_args.args
        shutdown_event = proxy_init_args[3]
        shutdown_event.set()

    mock_proxy_instance.run.side_effect = set_shutdown_event
    mock_proxy_class.return_value = mock_proxy_instance

    # Patch signal handling to avoid NotImplementedError on Windows
    with patch("asyncio.runners.loop.add_signal_handler", new=lambda *args: None):
        await main()

    mock_load_config.assert_called_once()
    mock_docker_manager_class.return_value.connect.assert_awaited_once()
    mock_proxy_class.assert_called_once()
    mock_proxy_instance.run.assert_awaited_once()
