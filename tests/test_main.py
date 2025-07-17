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

    # CRITICAL FIX: Ensure app_config is passed to DockerManager and
    # MetricsManager in the mock setup, mimicking real app flow.
    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    mock_app_config.log_level = "INFO"
    mock_app_config.log_format = "console"
    mock_load_config.return_value = mock_app_config

    # Explicitly mock the instantiation of MetricsManager within AsyncProxy
    # This prevents the real MetricsManager.__init__ from being called,
    # which in turn prevents it from trying to instantiate Prometheus gauges
    # before they are fully patched in other tests if this mock is not global.
    mock_metrics_manager_instance = AsyncMock(spec=MetricsManager)
    # Patch the MetricsManager class where AsyncProxy's __init__ looks for it
    with patch("proxy.MetricsManager", return_value=mock_metrics_manager_instance):
        # Assign the mocked metrics_manager to the mock proxy instance
        mock_proxy_instance.metrics_manager = mock_metrics_manager_instance
        # The AsyncProxy's __init__ will now receive our mock.
        # However, because we already mocked AsyncProxy itself,
        # we need to ensure the _proxy_instance.metrics_manager attribute
        # is correctly set, as AsyncProxy.__init__ won't run here.
        # This is already covered by `mock_proxy_instance.metrics_manager = ...` above.

        # Configure os.environ.get to ensure load_app_config functions correctly
        # within the test environment.
        mock_os_environ_get.side_effect = lambda key, default=None: {
            "LOG_LEVEL": mock_app_config.log_level,
            "NB_LOG_FORMATTER": mock_app_config.log_format,
            # Ensure other NB_X_ variables, if loaded, return sensible defaults
            "NB_1_NAME": "test_server",
            "NB_1_GAME_TYPE": "java",
            "NB_1_CONTAINER_NAME": "test_container",
            "NB_1_PORT": "25565",
            "NB_1_PROXY_PORT": "25565",
            "APP_IMAGE_METADATA": None,  # No metadata for this test
        }.get(key, default)

        mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)

        # Mock create_task to return a cancellable AsyncMock
        mock_heartbeat_task = AsyncMock()
        # Ensure create_task returns distinct mock for heartbeat and other tasks
        # FIX: The `create_task` call count in `amain` is exactly 1 (for heartbeat).
        # The tasks within `mock_proxy_instance.start()` are *its* internal tasks,
        # and we don't need to control them via *this* test's `mock_create_task`.
        # So, simply setting `return_value` is correct, not `side_effect` with a list.
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

        mock_create_task.assert_called_once_with(
            ANY
        )  # Check for heartbeat task creation

        # FIX: Ensure the heartbeat_task is actually the one cancelled
        mock_heartbeat_task.cancel.assert_called_once()
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

    mock_app_config = MagicMock()
    mock_app_config.game_servers = [MagicMock()]
    # FIX: Add mock log_level and log_format for load_app_config context
    mock_app_config.log_level = "INFO"
    mock_app_config.log_format = "console"
    mock_load_config.return_value = mock_app_config

    mock_proxy_instance.start = AsyncMock(side_effect=asyncio.CancelledError)

    # FIX: Mock create_task to return a single mock.
    mock_heartbeat_task = AsyncMock()
    mock_create_task.return_value = mock_heartbeat_task

    # Patch MetricsManager class where AsyncProxy's __init__ looks for it
    mock_metrics_manager_instance = AsyncMock(spec=MetricsManager)
    with patch("proxy.MetricsManager", return_value=mock_metrics_manager_instance):
        mock_proxy_instance.metrics_manager = mock_metrics_manager_instance

        # We need a more precise patch here. Patching
        # os.environ *dict* for load_app_config is more reliable.
        # The simplest way is to manually ensure the
        # required env vars for load_app_config are set for the mock call.

        # Test with valid JSON metadata
        # FIX: Adjust mock_os_environ_get.side_effect for all expected calls.
        mock_os_environ_get.side_effect = lambda key, default=None: {
            "APP_IMAGE_METADATA": '{"version": "1.0.0", "build": "abc"}',
            "LOG_LEVEL": mock_app_config.log_level,
            "NB_LOG_FORMATTER": mock_app_config.log_format,
            # Ensure other NB_X_ variables, if loaded, return sensible defaults
            "NB_1_NAME": "test_server",
            "NB_1_GAME_TYPE": "java",
            "NB_1_CONTAINER_NAME": "test_container",
            "NB_1_PORT": "25565",
            "NB_1_PROXY_PORT": "25565",
        }.get(key, default)

        await amain()
        mock_log.info.assert_any_call(
            "Application build metadata", version="1.0.0", build="abc"
        )
        mock_log.info.reset_mock()
        mock_os_environ_get.reset_mock()  # Reset after first test run

        # Test with malformed JSON metadata
        # FIX: Adjust mock_os_environ_get.side_effect for all expected calls.
        mock_os_environ_get.side_effect = lambda key, default=None: {
            "APP_IMAGE_METADATA": "invalid json",
            "LOG_LEVEL": mock_app_config.log_level,
            "NB_LOG_FORMATTER": mock_app_config.log_format,
            # Ensure other NB_X_ variables, if loaded, return sensible defaults
            "NB_1_NAME": "test_server",
            "NB_1_GAME_TYPE": "java",
            "NB_1_CONTAINER_NAME": "test_container",
            "NB_1_PORT": "25565",
            "NB_1_PROXY_PORT": "25565",
        }.get(key, default)

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
    Tests that the health check succeeds if the heartbeat is within the
    threshold.
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
    # We need to control what AsyncProxy.start() does internally.
    # Instead of side_effect=asyncio.CancelledError,
    # we'll mock its *internal* calls to control flow without cancelling
    # the proxy's `start()` method prematurely in the test.
    mock_proxy_instance = AsyncMock(spec=main_module.AsyncProxy)
    mock_async_proxy_class.return_value = mock_proxy_instance
    mock_proxy_instance.docker_manager = mock_docker_instance

    mock_metrics_manager_class = AsyncMock(spec=MetricsManager)
    mock_metrics_manager_instance = AsyncMock()
    mock_metrics_manager_class.return_value = mock_metrics_manager_instance

    # Patch MetricsManager class where AsyncProxy's __init__ looks for it
    with patch("proxy.MetricsManager", new=mock_metrics_manager_class):
        # Ensure the mock_proxy_instance has the mocked metrics manager
        # that it would receive from its __init__ (which is itself mocked).
        mock_proxy_instance.metrics_manager = mock_metrics_manager_instance

        mock_app_config = MagicMock()
        mock_app_config.game_servers = [MagicMock()]
        mock_app_config.log_level = "INFO"
        mock_app_config.log_format = "console"
        mock_load_config.return_value = mock_app_config

        # Configure os.environ.get.side_effect for this test's specific needs
        mock_os_environ_get.side_effect = lambda key, default=None: {
            "APP_IMAGE_METADATA": None,
            "LOG_LEVEL": mock_app_config.log_level,
            "NB_LOG_FORMATTER": mock_app_config.log_format,
            "NB_1_NAME": "test_server",
            "NB_1_GAME_TYPE": "java",
            "NB_1_CONTAINER_NAME": "test_container",
            "NB_1_PORT": "25565",
            "NB_1_PROXY_PORT": "25565",
        }.get(key, default)

        # Make metrics_manager.start() fail, as per the test's intent
        mock_metrics_manager_instance.start.side_effect = Exception(
            "Metrics server failed to bind"
        )

        # Mock the *internal* calls that AsyncProxy.start() would make,
        # rather than directly mocking AsyncProxy.start() itself with a side_effect
        # that immediately cancels.
        mock_proxy_instance._ensure_all_servers_stopped_on_startup = AsyncMock()
        mock_proxy_instance._start_listener = AsyncMock()
        mock_proxy_instance._monitor_server_activity = AsyncMock()
        # Mock asyncio.gather to ensure the main loop in AsyncProxy.start() exits
        # after attempts to start tasks, simulating a normal completion or exit
        # for testing purposes.
        # This will simulate the main gather finishing after tasks are created.
        with patch("proxy.asyncio.gather", new_callable=AsyncMock) as mock_gather:
            # Configure mock_gather to just resolve immediately to allow test to finish.
            # In a real scenario, this would gather the tasks indefinitely.
            mock_gather.return_value = []  # Resolve immediately

            # FIX: Only one `create_task` is for the heartbeat in `amain`.
            # The other tasks are created inside `proxy_server.start()`.
            mock_heartbeat_task = AsyncMock()
            mock_create_task.return_value = mock_heartbeat_task

            await amain()

            mock_log.error.assert_any_call(
                "Failed to start Prometheus server", exc_info=True
            )
            # FIX: Assert that the heartbeat task was cancelled
            mock_heartbeat_task.cancel.assert_called_once()
            mock_docker_instance.close.assert_awaited_once()
            assert mock_loop.add_signal_handler.call_count >= 2
