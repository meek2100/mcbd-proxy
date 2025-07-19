# tests/test_main.py
import asyncio
import os
import sys
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# Add project root to path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import amain, health_check, main


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
@patch("main.sys.argv", ["main.py"])
@patch("main.asyncio.run")
@patch("main.amain")
def test_main_runs_amain(mock_amain, mock_asyncio_run):
    """
    Tests that the main function calls asyncio.run with amain.
    """
    main()
    mock_asyncio_run.assert_called_once_with(mock_amain())


@pytest.mark.unit
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.load_app_config")
@patch("main.asyncio.get_running_loop")
async def test_amain_orchestration_and_shutdown(
    mock_get_running_loop,
    mock_load_config,
    mock_create_task,
    mock_configure_logging,
    mock_async_proxy_class,
    mock_docker_manager_class,
):
    """
    Verify `amain` orchestrates startup and that `finally` block cleans up.
    """
    mock_loop = MagicMock()
    mock_get_running_loop.return_value = mock_loop
    mock_docker_instance = mock_docker_manager_class.return_value = AsyncMock()
    mock_proxy_instance = mock_async_proxy_class.return_value = AsyncMock()

    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_load_config.return_value = mock_app_config

    # Simulate startup cancelling, triggering the finally block
    mock_proxy_instance.start.side_effect = asyncio.CancelledError
    mock_heartbeat_task = AsyncMock()
    mock_create_task.return_value = mock_heartbeat_task

    await amain()

    mock_load_config.assert_called_once()
    mock_configure_logging.assert_called_once_with(
        mock_app_config.log_level, mock_app_config.log_format
    )
    mock_async_proxy_class.assert_called_once_with(
        mock_app_config, mock_docker_instance
    )
    mock_proxy_instance.start.assert_awaited_once()
    mock_create_task.assert_called_once_with(ANY)
    mock_heartbeat_task.cancel.assert_called_once()
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.unit
@patch("main.load_app_config")
@patch("main.sys.exit")
@patch("main.log")
async def test_amain_exits_if_no_servers_loaded(
    mock_log, mock_sys_exit, mock_load_config
):
    """
    Tests that amain exits if the loaded config has no game servers.
    """
    mock_app_config = MagicMock()
    mock_app_config.game_servers = []  # No servers
    mock_load_config.return_value = mock_app_config

    await amain()

    mock_log.critical.assert_called_once_with(
        "FATAL: No server configurations loaded. Exiting."
    )
    mock_sys_exit.assert_called_once_with(1)


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
