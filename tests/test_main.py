import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ProxySettings
from main import main, perform_health_check


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.load_application_config")
@patch("main.DockerManager")
@patch("main.NetherBridgeProxy")
async def test_main_runs_proxy_and_handles_shutdown(
    mock_proxy_class, mock_docker_manager_class, mock_load_config
):
    mock_settings = MagicMock(spec=ProxySettings)
    mock_settings.docker_url = "dummy_url"
    mock_load_config.return_value = (mock_settings, ["mock_server"])

    mock_docker_instance = AsyncMock()
    mock_docker_manager_class.return_value = mock_docker_instance
    mock_proxy_instance = AsyncMock()

    async def set_shutdown_event(*args, **kwargs):
        mock_proxy_class.call_args.args[3].set()

    mock_proxy_instance.run.side_effect = set_shutdown_event
    mock_proxy_class.return_value = mock_proxy_instance

    with patch(
        "asyncio.events.AbstractEventLoop.add_signal_handler", new=lambda *a: None
    ):
        await main()

    mock_load_config.assert_called_once()
    mock_docker_manager_class.assert_called_once_with(docker_url="dummy_url")
    mock_proxy_class.assert_called_once()
    mock_proxy_instance.run.assert_awaited_once()
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.unit
# This test is synchronous, so the asyncio mark is removed.
@patch("main.perform_health_check")
@patch("sys.argv", ["main.py", "--healthcheck"])
def test_main_calls_health_check(mock_perform_health):
    """
    Tests that main() calls perform_health_check when --healthcheck is passed.
    """
    with patch("main.load_application_config", return_value=(MagicMock(), [])):
        # Because main() is async, we need to run it in a test loop
        asyncio.run(main())
        # The mock is now a regular MagicMock, so we use assert_called_once
        mock_perform_health.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
@patch("main.HEARTBEAT_FILE")
async def test_health_check_fails_if_file_missing(mock_heartbeat_file):
    mock_heartbeat_file.is_file.return_value = False
    with pytest.raises(SystemExit) as e:
        await perform_health_check(MagicMock(spec=ProxySettings))
    assert e.value.code == 1


@pytest.mark.unit
@pytest.mark.asyncio
@patch("time.time", return_value=1000)
@patch("main.HEARTBEAT_FILE")
async def test_health_check_fails_if_heartbeat_is_stale(mock_heartbeat_file, mock_time):
    mock_heartbeat_file.is_file.return_value = True
    mock_heartbeat_file.read_text.return_value = "900"
    settings = MagicMock(spec=ProxySettings)
    settings.healthcheck_stale_threshold_seconds = 60
    with pytest.raises(SystemExit) as e:
        await perform_health_check(settings)
    assert e.value.code == 1


@pytest.mark.unit
@pytest.mark.asyncio
@patch("time.time", return_value=1000)
@patch("main.HEARTBEAT_FILE")
async def test_health_check_succeeds_if_heartbeat_is_fresh(
    mock_heartbeat_file, mock_time
):
    mock_heartbeat_file.is_file.return_value = True
    mock_heartbeat_file.read_text.return_value = "980"
    settings = MagicMock(spec=ProxySettings)
    settings.healthcheck_stale_threshold_seconds = 60
    with pytest.raises(SystemExit) as e:
        await perform_health_check(settings)
    assert e.value.code == 0
