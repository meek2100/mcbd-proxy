# tests/test_main.py
import asyncio
import os
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

import main as main_module  # Import main as module to patch its internal log

# Imports from the module being tested
from main import amain, health_check, main
from metrics import MetricsManager  # Import MetricsManager to use as spec


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
@patch("main.DockerManager", spec=main_module.DockerManager)
@patch("main.AsyncProxy", spec=main_module.AsyncProxy)
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.load_app_config")
@patch("main.asyncio.get_running_loop")
@patch.object(os.environ, "get")
async def test_amain_orchestration_and_shutdown(
    mock_os_environ_get,
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
    mock_loop.add_signal_handler = MagicMock()

    mock_docker_instance = mock_docker_manager_class.return_value = AsyncMock()
    mock_proxy_instance = mock_async_proxy_class.return_value = AsyncMock()

    mock_proxy_instance.docker_manager = mock_docker_instance
    mock_proxy_instance.metrics_manager = AsyncMock(
        spec=MetricsManager  # Use imported MetricsManager for spec
    )

    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_app_config.log_level = "INFO"
    mock_app_config.log_format = "console"
    mock_load_config.return_value = mock_app_config

    original_os_environ_get = os.environ.get
    mock_os_environ_get.side_effect = lambda key, default=None: (
        None if key == "APP_IMAGE_METADATA" else original_os_environ_get(key, default)
    )

    mock_create_task.return_value = AsyncMock()

    mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)

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

    mock_create_task.return_value.cancel.assert_called_once()
    mock_docker_instance.close.assert_awaited_once()

    assert mock_loop.add_signal_handler.call_count >= 2


@pytest.mark.unit
@patch("main.DockerManager", spec=main_module.DockerManager)
@patch("main.AsyncProxy", spec=main_module.AsyncProxy)
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.load_app_config")
@patch("main.log")
@patch("main.asyncio.get_running_loop")
@patch.object(os.environ, "get")
async def test_amain_logs_app_image_metadata(
    mock_os_environ_get,
    mock_get_running_loop,
    mock_log,
    mock_load_config,
    mock_create_task,
    mock_configure_logging,
    mock_async_proxy_class,
    mock_docker_manager_class,
):
    """
    Tests that amain correctly logs APP_IMAGE_METADATA if present.
    """
    mock_loop = MagicMock()
    mock_get_running_loop.return_value = mock_loop
    mock_loop.add_signal_handler = MagicMock()

    mock_docker_instance = mock_docker_manager_class.return_value = AsyncMock()
    mock_proxy_instance = mock_async_proxy_class.return_value = AsyncMock()
    mock_proxy_instance.docker_manager = mock_docker_instance
    mock_proxy_instance.metrics_manager = AsyncMock(
        spec=MetricsManager  # Use imported MetricsManager for spec
    )

    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_load_config.return_value = mock_app_config
    mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)
    mock_create_task.return_value = AsyncMock()

    # Test with valid JSON metadata
    mock_os_environ_get.side_effect = [
        '{"version": "1.0.0", "build": "abc"}',  # APP_IMAGE_METADATA
        # Subsequent calls from load_app_config, needs to return None for other env vars
        # as load_app_config calls os.environ.get for each AppConfig field
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # For AppConfig fields
        None,  # For second call to amain() below
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # For AppConfig fields on second call
    ]
    await amain()
    mock_log.info.assert_any_call(
        "Application build metadata", version="1.0.0", build="abc"
    )
    mock_log.info.reset_mock()

    # Test with malformed JSON metadata
    mock_os_environ_get.side_effect = [
        "invalid json",  # APP_IMAGE_METADATA
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # For AppConfig fields
    ]
    await amain()
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
@patch("main.DockerManager", spec=main_module.DockerManager)
@patch("main.AsyncProxy", spec=main_module.AsyncProxy)
@patch("main.configure_logging")
@patch("main.asyncio.create_task")
@patch("main.log")
@patch("main.asyncio.get_running_loop")
@patch.object(os.environ, "get")
async def test_amain_handles_metrics_manager_start_failure(
    mock_os_environ_get,
    mock_get_running_loop,
    mock_log,
    mock_create_task,
    mock_configure_logging,
    mock_async_proxy_class,
    mock_docker_manager_class,
    mock_load_config,
):
    """
    Tests that amain gracefully handles a failure when MetricsManager.start()
    raises an exception, logging it and allowing shutdown to proceed.
    """
    mock_loop = MagicMock()
    mock_get_running_loop.return_value = mock_loop
    mock_loop.add_signal_handler = MagicMock()

    mock_docker_instance = mock_docker_manager_class.return_value = AsyncMock()
    mock_proxy_instance = mock_async_proxy_class.return_value = AsyncMock()
    mock_proxy_instance.docker_manager = mock_docker_instance

    mock_metrics_manager_class = AsyncMock(spec=MetricsManager)
    mock_metrics_manager_instance = AsyncMock()
    mock_metrics_manager_class.return_value = mock_metrics_manager_instance

    # Patch MetricsManager class where AsyncProxy's __init__ looks for it
    with patch("proxy.MetricsManager", new=mock_metrics_manager_class):
        mock_proxy_instance.metrics_manager = mock_metrics_manager_instance

        mock_app_config = MagicMock()
        mock_app_config.game_servers = [MagicMock()]
        mock_app_config.log_level = "INFO"
        mock_app_config.log_format = "console"
        mock_load_config.return_value = mock_app_config

        # Configure os.environ.get.side_effect for this test's specific needs
        # Needs to return None for APP_IMAGE_METADATA, and then default for others
        original_os_environ_get = os.environ.get
        mock_os_environ_get.side_effect = lambda key, default=None: (
            None
            if key == "APP_IMAGE_METADATA"
            else original_os_environ_get(key, default)
        )

        mock_metrics_manager_instance.start.side_effect = Exception(
            "Metrics server failed to bind"
        )
        mock_proxy_instance.start.side_effect = asyncio.CancelledError

        mock_create_task.return_value = AsyncMock()

        await amain()

        mock_log.error.assert_any_call(
            "Failed to start Prometheus server", exc_info=True
        )
        mock_docker_instance.close.assert_awaited_once()
        mock_create_task.return_value.cancel.assert_called_once()
        assert mock_loop.add_signal_handler.call_count >= 2
