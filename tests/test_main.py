# tests/test_main.py
"""
Unit tests for the main application entrypoint.
"""

from unittest.mock import AsyncMock, patch

import pytest

from main import amain

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_load_config():
    """Fixture to mock config loading."""
    with patch("main.load_app_config") as mock:
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
        mock.return_value = instance
        yield instance


@pytest.mark.asyncio
async def test_amain_starts_proxy_and_closes_docker_manager(
    mock_load_config, mock_docker_manager, mock_proxy
):
    """
    Test that amain starts the proxy and ensures docker_manager.close is called.
    """
    await amain()

    mock_proxy.start.assert_awaited_once()
    mock_docker_manager.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_amain_closes_docker_manager_on_exception(
    mock_load_config, mock_docker_manager, mock_proxy
):
    """
    Test that amain ensures docker_manager.close is called even if proxy
    start fails.
    """
    mock_proxy.start.side_effect = Exception("Proxy failed to start")

    with pytest.raises(Exception, match="Proxy failed to start"):
        await amain()

    mock_docker_manager.close.assert_awaited_once()
