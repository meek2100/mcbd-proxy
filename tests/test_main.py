# tests/test_main.py
import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# Imports from the module being tested
from main import amain, health_check, main


@pytest.mark.unit
@patch("main.sys.argv", ["main.py", "--healthcheck"])
@patch("main.health_check")
def test_main_runs_health_check(mock_health_check):
    """
    Tests that the main function correctly calls perform_health_check
    when the '--healthcheck' argument is provided.
    """
    main()
    mock_health_check.assert_called_once()


@pytest.mark.unit
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.asyncio.create_task")  # Patch create_task directly
@patch("main.load_app_config")
async def test_amain_orchestration_and_shutdown(
    mock_load_config,
    mock_create_task,  # This mock now represents asyncio.create_task
    mock_configure_logging,
    mock_async_proxy,
    mock_docker_manager,
):
    """
    Verify `amain` orchestrates startup and that `finally` block cleans up.
    """
    mock_proxy_instance = mock_async_proxy.return_value
    mock_docker_instance = mock_docker_manager.return_value = AsyncMock()

    # Mock load_app_config to return a valid config, including game_servers
    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_app_config.log_level = "INFO"
    mock_app_config.log_format = "console"
    mock_load_config.return_value = mock_app_config

    # Mock create_task to return an AsyncMock that behaves like a task.
    mock_create_task.return_value = AsyncMock()

    # Simulate a cancellation to test the finally block
    mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)

    await amain()

    mock_load_config.assert_called_once()
    mock_configure_logging.assert_called_once_with(
        mock_app_config.log_level, mock_app_config.log_format
    )
    mock_async_proxy.assert_called_once_with(mock_app_config, mock_docker_instance)
    mock_proxy_instance.start.assert_awaited_once()

    # Assert that asyncio.create_task was called with the _update_heartbeat coroutine.
    # FIX: Ensure assertion expects no kwargs, which is default for create_task in main.
    mock_create_task.assert_called_once_with(ANY)

    # Assert that the created task (mock_create_task.return_value) was cancelled
    mock_create_task.return_value.cancel.assert_called_once()
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.unit
@patch("main.load_app_config")
@patch("main.sys.exit")
def test_main_exits_on_config_load_failure(mock_sys_exit, mock_load_config):
    """
    Tests that main exits if load_app_config raises an exception.
    """
    mock_load_config.side_effect = Exception("Config load error")

    main()

    mock_load_config.assert_called_once()
    mock_sys_exit.assert_called_once_with(1)


@pytest.mark.unit
@patch("main.sys.argv", ["main.py"])
@patch("main.asyncio.run")
@patch("main.HEARTBEAT_FILE")
@patch("main.configure_logging")  # Patch logging setup
def test_main_cleans_up_heartbeat_on_exit(
    mock_configure_logging, mock_heartbeat_file, mock_asyncio_run
):
    """
    Tests that the main function unlinks the heartbeat file on normal exit.
    """
    # Simulate a successful run
    mock_asyncio_run.return_value = None
    mock_heartbeat_file.exists.return_value = True

    main()

    mock_heartbeat_file.unlink.assert_called_once_with(missing_ok=True)


@pytest.mark.unit
@patch("main.sys.argv", ["main.py"])
@patch("main.asyncio.run")
@patch("main.HEARTBEAT_FILE")
@patch("main.configure_logging")  # Patch logging setup
def test_main_cleans_up_heartbeat_on_keyboard_interrupt(
    mock_configure_logging, mock_heartbeat_file, mock_asyncio_run
):
    """
    Tests that the main function unlinks the heartbeat file on KeyboardInterrupt.
    """
    # Simulate KeyboardInterrupt during asyncio.run
    mock_asyncio_run.side_effect = KeyboardInterrupt
    mock_heartbeat_file.exists.return_value = True

    main()

    mock_heartbeat_file.unlink.assert_called_once_with(missing_ok=True)


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
def test_health_check_fails_if_file_missing(mock_heartbeat_file):
    """
    Tests that the health check fails with exit code 1 if the
    heartbeat file does not exist.
    """
    mock_app_config = MagicMock()
    mock_app_config.healthcheck_stale_threshold = 60
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
    Tests that the health check fails with exit code 1 if the heartbeat
    file is older than the configured threshold.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_settings):
        mock_heartbeat_file.exists.return_value = True
        mock_time.return_value = 1000  # Current time
        mock_heartbeat_file.read_text.return_value = "900"  # Stale timestamp

        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 1


@pytest.mark.unit
@patch("main.HEARTBEAT_FILE")
@patch("time.time")
def test_health_check_succeeds_if_heartbeat_is_fresh(mock_time, mock_heartbeat_file):
    """
    Tests that the health check succeeds if the heartbeat is within the threshold.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_settings):
        mock_heartbeat_file.exists.return_value = True
        mock_time.return_value = 1000  # Current time
        mock_heartbeat_file.read_text.return_value = "990"  # Fresh timestamp

        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 0
