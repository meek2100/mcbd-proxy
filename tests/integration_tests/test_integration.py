# tests/integration_tests/test_integration.py
import asyncio
import socket

import pytest

from tests.helpers import (
    BEDROCK_PROXY_PORT,
    JAVA_PROXY_PORT,
    get_container_status,
    get_proxy_host,
    wait_for_container_status,
    wait_for_log_message,
    wait_for_mc_server_ready,
    wait_for_proxy_to_be_ready,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bedrock_server_starts_on_connection(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that the mc-bedrock server starts when a connection is made.
    """
    proxy_host = get_proxy_host()
    mc_bedrock = "mc-bedrock"

    await wait_for_proxy_to_be_ready(docker_client_fixture)
    initial_status = await get_container_status(docker_client_fixture, mc_bedrock)
    assert initial_status in ["exited", "created"]

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(ping_packet, (proxy_host, BEDROCK_PROXY_PORT))

        assert await wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Server is not running. Initiating startup...",
        ), "Proxy did not log server startup."

        assert await wait_for_container_status(
            docker_client_fixture, mc_bedrock, "running"
        ), "Bedrock container did not start."

        assert await wait_for_mc_server_ready(
            {"host": proxy_host, "port": BEDROCK_PROXY_PORT, "type": "bedrock"}
        ), "Bedrock server did not become query-ready."
    finally:
        client_socket.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_java_server_starts_on_connection(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that the mc-java server starts when a connection is made.
    """
    proxy_host = get_proxy_host()
    mc_java = "mc-java"

    await wait_for_proxy_to_be_ready(docker_client_fixture)
    initial_status = await get_container_status(docker_client_fixture, mc_java)
    assert initial_status in ["exited", "created"]

    try:
        reader, writer = await asyncio.open_connection(proxy_host, JAVA_PROXY_PORT)

        assert await wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Server is not running. Initiating startup...",
        ), "Proxy did not log server startup."

        assert await wait_for_container_status(
            docker_client_fixture, mc_java, "running"
        ), "Java container did not start."

        assert await wait_for_mc_server_ready(
            {"host": proxy_host, "port": JAVA_PROXY_PORT, "type": "java"}
        ), "Java server did not become query-ready."

        writer.close()
        await writer.wait_closed()
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_shuts_down_on_idle(docker_compose_up, docker_client_fixture):
    """
    Tests that a running server is stopped after a period of inactivity.
    """
    proxy_host = get_proxy_host()
    mc_bedrock = "mc-bedrock"

    await wait_for_proxy_to_be_ready(docker_client_fixture)

    # Start the server by making a connection
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.sendto(b"start", (proxy_host, BEDROCK_PROXY_PORT))
    client_socket.close()

    assert await wait_for_container_status(
        docker_client_fixture, mc_bedrock, "running", timeout=180
    ), "Bedrock server did not start."

    # Now, wait for the idle shutdown message
    assert await wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Server is idle. Initiating shutdown.",
        timeout=180,  # Should be greater than idle_timeout + check_interval
    ), "Proxy did not log idle shutdown."

    assert await wait_for_container_status(
        docker_client_fixture, mc_bedrock, "exited", timeout=30
    ), "Container did not stop after idle timeout."
