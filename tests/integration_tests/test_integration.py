import socket
import time

import pytest

from tests.helpers import (
    BEDROCK_PROXY_PORT,
    JAVA_PROXY_PORT,
    get_container_status,
    get_java_handshake_and_status_request_packets,
    get_proxy_host,
    wait_for_container_status,
    wait_for_log_message,
    wait_for_mc_server_ready,
    wait_for_proxy_to_be_ready,
)


@pytest.mark.integration
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-bedrock server starts when a connection attempt is made.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    container_name = "mc-bedrock"

    assert wait_for_proxy_to_be_ready(docker_client_fixture)
    assert get_container_status(docker_client_fixture, container_name) in [
        "exited",
        "created",
    ]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        client_socket.sendto(b"ping", (proxy_host, bedrock_proxy_port))

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First UDP packet. Starting server...",
        timeout=20,
    )
    assert wait_for_container_status(
        docker_client_fixture, container_name, ["running"], timeout=240
    )
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": bedrock_proxy_port, "type": "bedrock"},
        timeout=240,
    )


@pytest.mark.integration
def test_java_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-java server starts when a connection attempt is made.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    container_name = "mc-java"

    assert wait_for_proxy_to_be_ready(docker_client_fixture)
    assert get_container_status(docker_client_fixture, container_name) in [
        "exited",
        "created",
    ]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
        client_socket.connect((proxy_host, java_proxy_port))
        handshake, status_req = get_java_handshake_and_status_request_packets(
            proxy_host, java_proxy_port
        )
        client_socket.sendall(handshake)
        client_socket.sendall(status_req)

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First connection. Starting server...",
        timeout=20,
    )
    assert wait_for_container_status(
        docker_client_fixture, container_name, ["running"], timeout=240
    )
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": java_proxy_port, "type": "java"}, timeout=240
    )


@pytest.mark.integration
def test_server_shuts_down_on_idle(docker_compose_up, docker_client_fixture):
    """
    Tests that a running server is automatically stopped by the proxy after a
    period of inactivity.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
    idle_timeout = 30
    check_interval = 5

    assert wait_for_proxy_to_be_ready(docker_client_fixture)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=240
    )
    time.sleep(10)  # Allow session to be fully established and activity logged
    wait_duration = idle_timeout + (2 * check_interval) + 10
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Server idle, shutting down.",
        timeout=wait_duration,
    )


@pytest.mark.integration
def test_proxy_restarts_crashed_server_on_new_connection(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that the proxy will re-start a server that has been stopped
    or has crashed.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"
    unconnected_ping_packet = (
        b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
        b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
    )

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=240
    )
    container = docker_client_fixture.containers.get(mc_bedrock_container_name)
    container.stop()
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["exited"], timeout=90
    )
    time.sleep(2)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First UDP packet. Starting server...",
        timeout=20,
    )
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=240
    )


@pytest.mark.integration
def test_configuration_reload_on_sighup(docker_compose_up, docker_client_fixture):
    """
    Tests that the proxy correctly reloads its server configuration upon
    receiving a SIGHUP signal.
    """
    proxy_host = get_proxy_host()
    initial_bedrock_port = 19132
    reloaded_bedrock_port = 19134

    assert wait_for_proxy_to_be_ready(docker_client_fixture)

    container = docker_client_fixture.containers.get("nether-bridge")
    new_config_json = """
{
  "servers": [
    {
      "name": "Bedrock RELOADED",
      "server_type": "bedrock",
      "listen_port": 19134,
      "container_name": "mc-bedrock",
      "internal_port": 19132
    }
  ]
}
"""
    cmd_write_config = f"printf '%s' '{new_config_json}' > /app/servers.json"
    exit_code, _ = container.exec_run(cmd_write_config, user="naeus")
    assert exit_code == 0

    container.kill(signal="SIGHUP")
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Configuration reload complete.",
        timeout=15,
    )

    with pytest.raises((ConnectionRefusedError, OSError, socket.timeout)):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(2)
            client_socket.sendto(b"old-port-ping", (proxy_host, initial_bedrock_port))
            client_socket.recvfrom(1024)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(2)
            client_socket.sendto(b"new-port-ping", (proxy_host, reloaded_bedrock_port))
    except (socket.error, ConnectionRefusedError) as e:
        pytest.fail(f"New port {reloaded_bedrock_port} was not open: {e}")


@pytest.mark.integration
def test_proxy_cleans_up_session_on_container_crash(
    docker_compose_up, docker_client_fixture
):
    """
    Tests that if a server container crashes during an active session,
    the proxy detects the resulting connection error and cleans up the session.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"

    assert wait_for_proxy_to_be_ready(docker_client_fixture)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as pre_warm_socket:
        pre_warm_socket.settimeout(10)
        pre_warm_socket.connect((proxy_host, java_proxy_port))
    assert wait_for_container_status(
        docker_client_fixture, mc_java_container_name, ["running"], timeout=240
    )
    time.sleep(5)  # Allow server to stabilize

    victim_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        victim_socket.connect((proxy_host, java_proxy_port))
        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Establishing TCP session",
            timeout=30,
        )

        container = docker_client_fixture.containers.get(mc_java_container_name)
        container.kill()
        assert wait_for_container_status(
            docker_client_fixture, mc_java_container_name, ["exited", "dead"]
        )
        time.sleep(1)

        with pytest.raises(socket.error):
            victim_socket.sendall(b"data_after_crash")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Connection error, cleaning up.",
            timeout=10,
        )
    finally:
        victim_socket.close()
