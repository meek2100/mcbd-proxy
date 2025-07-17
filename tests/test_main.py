# tests/test_main.py
import asyncio
import os
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
@patch("main.asyncio.create_task")
@patch("main.load_app_config")
@patch("main.os.environ")  # Patch os.environ to control APP_IMAGE_METADATA
async def test_amain_orchestration_and_shutdown(
    mock_os_environ,  # New mock
    mock_load_config,
    mock_create_task,
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

    # Ensure APP_IMAGE_METADATA is not set for this test
    mock_os_environ.get.side_effect = lambda key, default=None: (
        None if key == "APP_IMAGE_METADATA" else os.environ.get(key, default)
    )

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

    mock_create_task.assert_called_once_with(ANY)

    mock_create_task.return_value.cancel.assert_called_once()
    mock_docker_instance.close.assert_awaited_once()


@pytest.mark.unit
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.load_app_config")
@patch("main.log")  # Patch the structlog logger
@patch("main.os.environ")  # Patch os.environ for APP_IMAGE_METADATA
async def test_amain_logs_app_image_metadata(
    mock_os_environ,
    mock_log,  # New mock
    mock_load_config,
    mock_create_task,
    mock_configure_logging,
    mock_async_proxy,
    mock_docker_manager,
):
    """
    Tests that amain correctly logs APP_IMAGE_METADATA if present.
    """
    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]  # Prevent early exit
    mock_load_config.return_value = mock_app_config
    mock_async_proxy.return_value.start = AsyncMock(
        side_effect=asyncio.CancelledError  # Exit gracefully
    )
    mock_create_task.return_value = AsyncMock()

    # Set mock APP_IMAGE_METADATA
    mock_os_environ.get.side_effect = lambda key, default=None: (
        '{"version": "1.0.0", "build": "abc"}'
        if key == "APP_IMAGE_METADATA"
        else os.environ.get(key, default)
    )

    await amain()

    mock_log.info.assert_any_call(
        "Application build metadata", version="1.0.0", build="abc"
    )

    # Verify warning if metadata is malformed JSON
    mock_os_environ.get.side_effect = lambda key, default=None: (
        "invalid json" if key == "APP_IMAGE_METADATA" else os.environ.get(key, default)
    )
    await amain()  # Call again with malformed data
    mock_log.warning.assert_any_call(
        "Could not parse APP_IMAGE_METADATA", metadata="invalid json"
    )


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
@patch("main.configure_logging")
def test_main_cleans_up_heartbeat_on_exit(
    mock_configure_logging, mock_heartbeat_file, mock_asyncio_run
):
    """
    Tests that the main function unlinks the heartbeat file on normal exit.
    """
    mock_asyncio_run.return_value = None
    mock_heartbeat_file.exists.return_value = True

    main()

    mock_heartbeat_file.unlink.assert_called_once_with(missing_ok=True)


@pytest.mark.unit
@patch("main.sys.argv", ["main.py"])
@patch("main.asyncio.run")
@patch("main.HEARTBEAT_FILE")
@patch("main.configure_logging")
def test_main_cleans_up_heartbeat_on_keyboard_interrupt(
    mock_configure_logging, mock_heartbeat_file, mock_asyncio_run
):
    """
    Tests that the main function unlinks the heartbeat file on KeyboardInterrupt.
    """
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
    Tests that the health check succeeds if the heartbeat is within the threshold.
    """
    mock_settings = MagicMock(healthcheck_stale_threshold=60)
    with patch("main.load_app_config", return_value=mock_settings):
        mock_heartbeat_file.exists.return_value = True
        mock_time.return_value = 1000
        mock_heartbeat_file.read_text.return_value = "990"

        with pytest.raises(SystemExit) as e:
            health_check()
        assert e.value.code == 0


@pytest.mark.unit
@patch("main.load_app_config")
@patch("main.DockerManager")
@patch("main.AsyncProxy")
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.log")  # Patch the structlog logger
@patch("main.os.environ")
async def test_amain_handles_metrics_manager_start_failure(
    mock_os_environ,
    mock_log,
    mock_create_task,
    mock_configure_logging,
    mock_async_proxy,
    mock_docker_manager,
    mock_load_config,
):
    """
    Tests that amain gracefully handles a failure when MetricsManager.start()
    raises an exception, logging it and allowing shutdown to proceed.
    """
    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_app_config.log_level = "INFO"
    mock_app_config.log_format = "console"
    mock_load_config.return_value = mock_app_config

    mock_docker_instance = mock_docker_manager.return_value = AsyncMock()
    mock_proxy_instance = mock_async_proxy.return_value

    # Mock the MetricsManager instance created within AsyncProxy
    mock_metrics_manager = AsyncMock()
    type(mock_proxy_instance).metrics_manager = MagicMock(
        return_value=mock_metrics_manager
    )

    # Configure AsyncProxy.start to cause MetricsManager.start to fail
    # We need to simulate the nested call for this. The actual call
    # is proxy_server.metrics_manager.start() within proxy_server.start()
    # So we mock the start() method of the mocked metrics_manager
    # We also need to ensure other tasks are cancelled to allow amain to proceed
    mock_proxy_instance.metrics_manager.start.side_effect = Exception(
        "Metrics server failed to bind"
    )
    mock_proxy_instance.start.side_effect = (
        asyncio.CancelledError
    )  # Allow graceful exit

    mock_create_task.return_value = AsyncMock()  # For heartbeat task

    await amain()

    # Assert that the error was logged
    mock_log.error.assert_any_call("Failed to start Prometheus server", exc_info=True)
    # Assert that subsequent cleanup still happens
    mock_docker_instance.close.assert_awaited_once()
    mock_create_task.return_value.cancel.assert_called_once()
