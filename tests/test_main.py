# tests/test_main.py
"""
Unit tests for the main application entrypoint and lifecycle.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import amain

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_load_config():
    """Fixture to mock config loading."""
    with patch("main.load_app_config") as mock:
        # Provide a mock config object that can be used by the code
        mock.return_value = MagicMock()
        yield mock


@pytest.fixture
def mock_docker_manager():
    """Fixture to mock DockerManager."""
    with patch("main.DockerManager") as mock:
        instance = AsyncMock()
        mock.return_value = instance
        yield instance


@pytest.fixture
def mock_proxy():
    """Fixture to mock AsyncProxy."""
    with patch("main.AsyncProxy") as mock:
        instance = AsyncMock()
        # Make the proxy's start() method raise an exception to break the loop
        instance.start.side_effect = RuntimeError("Test-induced loop break")
        mock.return_value = instance
        yield instance


@pytest.mark.asyncio
async def test_amain_initializes_and_starts_services_in_loop(
    mock_load_config, mock_docker_manager, mock_proxy
):
    """
    Tests that the main amain() loop correctly initializes all services
    and starts the proxy on its first iteration.
    """
    # The amain function has a 'while True' loop. We expect it to run once,
    # call proxy.start(), which we've mocked to raise an error to break the
    # loop, allowing us to assert that the setup process worked correctly.
    with pytest.raises(RuntimeError, match="Test-induced loop break"):
        await amain()

    # Verify that the core components were initialized on the first loop
    mock_load_config.assert_called_once()
    mock_docker_manager.assert_called_once_with(mock_load_config.return_value)
    mock_proxy.assert_called_once_with(
        mock_load_config.return_value, mock_docker_manager.return_value
    )

    # Verify the proxy's main start method was awaited
    mock_proxy.return_value.start.assert_awaited_once()
