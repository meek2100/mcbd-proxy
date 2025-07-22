# tests/test_main.py
import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import amain, health_check, main, shutdown


@pytest.mark.unit
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.health_check")
def test_main_runs_health_check(mock_health_check):
    """
    Tests that main calls health_check when '--healthcheck' is provided.
    """
    main()
    mock_health_check.assert_called_once()


@pytest.mark.unit
@patch("main.asyncio.run")
@patch("main.amain")
def test_main_runs_amain(mock_amain, mock_asyncio_run):
    """
    Tests that the main function calls asyncio.run with the amain coroutine.
    """
    # GIVEN a mock for the amain coroutine function
    mock_amain.return_value = asyncio.Future()  # Return a dummy awaitable

    # WHEN the main function is called
    with patch("main.sys.argv", ["main.py"]):
        main()

    # THEN verify that amain was called and its result was passed to asyncio.run
    mock_amain.assert_called_once()
    mock_asyncio_run.assert_called_once_with(mock_amain.return_value)


@pytest.mark.unit
@patch("main.sys.platform", "linux")
@patch("main.asyncio.gather", new_callable=AsyncMock)
@patch("main.asyncio.get_running_loop")
@patch("main.asyncio.create_task")
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.load_app_config")
async def test_amain_full_lifecycle(
    mock_load_config,
    mock_configure_logging,
    mock_async_proxy_class,
    mock_docker_manager_class,
    mock_create_task,
    mock_get_loop,
    mock_gather,
):
    """
    Verify `amain` orchestrates startup, waits, and then cleans up.
    """
    # GIVEN a mock loop where add_signal_handler is also a mock
    mock_loop = MagicMock()
    mock_loop.add_signal_handler = MagicMock()
    mock_get_loop.return_value = mock_loop

    mock_docker_instance = AsyncMock()
    mock_docker_manager_class.return_value = mock_docker_instance

    mock_proxy_instance = AsyncMock()
    mock_async_proxy_class.return_value = mock_proxy_instance

    mock_app_config = MagicMock(game_servers=[MagicMock()])
    mock_load_config.return_value = mock_app_config

    mock_heartbeat_task = MagicMock(spec=asyncio.Task)
    mock_proxy_task = MagicMock(spec=asyncio.Task)
    main_tasks_list = [mock_heartbeat_task, mock_proxy_task]
    mock_create_task.side_effect = main_tasks_list

    # WHEN amain is run and the shutdown event is immediately triggered
    with patch("main.asyncio.Event.wait", new_callable=AsyncMock):
        await amain()

    # THEN verify the entire application lifecycle
    mock_load_config.assert_called_once()
    mock_async_proxy_class.assert_called_once()
    assert mock_loop.add_signal_handler.call_count == 3  # SIGINT, SIGTERM, SIGHUP
    mock_proxy_instance.start.assert_awaited_once()
    mock_gather.assert_awaited_once_with(*main_tasks_list, return_exceptions=True)
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.unit
async def test_shutdown_coroutine():
    """Verify the shutdown coroutine calls proxy shutdown and sets the event."""
    mock_proxy = AsyncMock()
    mock_event = MagicMock(spec=asyncio.Event)
    mock_event.is_set.return_value = False
    mock_task = MagicMock(spec=asyncio.Task)
    mock_task.done.return_value = False
    test_signal = signal.SIGINT

    await shutdown(test_signal, mock_proxy, mock_event, [mock_task])

    mock_proxy.shutdown.assert_awaited_once()
    mock_event.set.assert_called_once()
    mock_task.cancel.assert_called_once()


@pytest.mark.unit
@patch("main.DockerManager")
@patch("main.sys.exit")
@patch("main.log")
@patch("main.load_app_config")
async def test_amain_exits_if_no_servers_loaded(
    mock_load_config, mock_log, mock_sys_exit, mock_docker_manager_class
):
    """
    Tests that amain exits if the loaded config has no game servers.
    """
    mock_app_config = MagicMock(game_servers=[])
    mock_load_config.return_value = mock_app_config
    mock_sys_exit.side_effect = SystemExit(1)

    with pytest.raises(SystemExit):
        await amain()

    mock_log.critical.assert_called_once_with(
        "FATAL: No server configurations loaded. Exiting."
    )
    mock_sys_exit.assert_called_once_with(1)
    mock_docker_manager_class.assert_not_called()


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat_file):
    """
    Tests that the health check fails if the heartbeat file is missing.
    """
    mock_app_config = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_app_config):
        mock_heartbeat_file.exists.return_value = False
        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 1


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
@patch("time.time")
def test_health_check_fails_if_heartbeat_is_stale(mock_time, mock_heartbeat_file):
    """
    Tests that health check fails if the heartbeat is older than the threshold.
    """
    mock_app_config = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_app_config):
        mock_heartbeat_file.exists.return_value = True
        mock_time.return_value = 1000
        mock_heartbeat_file.read_text.return_value = "900"

        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 1


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
@patch("time.time")
def test_health_check_succeeds_if_heartbeat_is_fresh(mock_time, mock_heartbeat_file):
    """
    Tests that the health check succeeds if the heartbeat is fresh.
    """
    mock_app_config = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_app_config):
        mock_heartbeat_file.exists.return_value = True
        mock_time.return_value = 1000
        mock_heartbeat_file.read_text.return_value = "990"

        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 0
