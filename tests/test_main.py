from unittest.mock import AsyncMock, MagicMock, patch

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
    mock_settings = MagicMock()
    mock_settings.docker_url = "dummy_url"
    mock_servers = ["mock_server"]
    mock_load_config.return_value = (mock_settings, mock_servers)

    mock_docker_instance = AsyncMock()
    mock_docker_manager_class.return_value = mock_docker_instance

    mock_proxy_instance = AsyncMock()

    async def set_shutdown_event(*args, **kwargs):
        proxy_init_args = mock_proxy_class.call_args.args
        shutdown_event = proxy_init_args[3]
        shutdown_event.set()

    mock_proxy_instance.run.side_effect = set_shutdown_event
    mock_proxy_class.return_value = mock_proxy_instance

    # This is the correct patch target for the signal handler
    with patch(
        "asyncio.events.AbstractEventLoop.add_signal_handler", new=lambda *args: None
    ):
        await main()

    mock_load_config.assert_called_once()
    mock_docker_manager_class.assert_called_once_with(docker_url="dummy_url")
    mock_proxy_class.assert_called_once()
    mock_proxy_instance.run.assert_awaited_once()
    mock_docker_instance.close.assert_awaited_once()
