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

    assert wait_for_container_status(docker_client_fixture, container_name, ["running"])
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": bedrock_proxy_port, "type": "bedrock"}
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

    assert wait_for_container_status(docker_client_fixture, container_name, ["running"])
    assert wait_for_mc_server_ready(
        {"host": proxy_host, "port": java_proxy_port, "type": "java"}
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

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(f"\nTriggering server '{mc_bedrock_container_name}' to start...")
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()
        print("Client socket closed, session terminated.")

    assert wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        ["running"],
        timeout=180,
    ), "Bedrock server did not start after being triggered."
    print(f"Server '{mc_bedrock_container_name}' confirmed to be running.")

    wait_duration = idle_timeout + (2 * check_interval) + 5
    print(f"Server is running. Waiting up to {wait_duration}s for idle shutdown...")

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Server idle. Initiating shutdown.",
        timeout=wait_duration,
    ), "Proxy did not log that it was shutting down an idle server."

    print("Proxy correctly initiated idle shutdown. Test passed.")


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

    print(
        (
            f"\n(Crash Test) Triggering initial server start for "
            f"'{mc_bedrock_container_name}'..."
        )
    )
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["running"], timeout=60
    ), "Container did not start on first connection."
    print("(Crash Test) Initial server start successful.")

    print(f"(Crash Test) Manually stopping container '{mc_bedrock_container_name}'...")
    container = docker_client_fixture.containers.get(mc_bedrock_container_name)
    container.stop()
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["exited", "dead"], timeout=90
    ), "Container did not stop after manual command."
    print("(Crash Test) Container successfully stopped.")

    time.sleep(2)

    print("\n(Crash Test) Attempting new connection to trigger restart...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    assert wait_for_container_status(
        docker_client_fixture,
        mc_bedrock_container_name,
        ["running"],
        timeout=180,
    ), "Proxy did not restart the crashed server upon new connection."

    print("(Crash Test) Proxy correctly restarted the server. Test passed.")


@pytest.mark.integration
def test_configuration_reload_on_sighup(docker_compose_up, docker_client_fixture):
    """
    Tests that the proxy correctly reloads its server configuration upon
    receiving a SIGHUP signal, without requiring a restart.
    """
    proxy_host = get_proxy_host()
    initial_bedrock_port = 19132
    reloaded_bedrock_port = 19134

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    print("\n(SIGHUP Test) Verifying initial server configuration...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(2)
            client_socket.sendto(b"initial-ping", (proxy_host, initial_bedrock_port))
    except (socket.error, ConnectionRefusedError) as e:
        pytest.fail(
            f"Initial port {initial_bedrock_port} should be open but was not: {e}"
        )

    print("(SIGHUP Test) Writing new configuration inside the container...")
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
    exit_code, output = container.exec_run(cmd_write_config, user="naeus", demux=True)
    assert exit_code == 0, (
        "Failed to write new config: "
        f"{output[1].decode() if output[1] else output[0].decode()}"
    )
    print("New servers.json written successfully.")

    print("(SIGHUP Test) Sending SIGHUP signal...")
    container.kill(signal="SIGHUP")

    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "Configuration reload complete.",
        timeout=15,
    ), "Proxy did not log that it completed the configuration reload."

    print("(SIGHUP Test) Proxy logged reload completion. Verifying new behavior...")

    print(f"(SIGHUP Test) Verifying old port {initial_bedrock_port} is closed...")
    with pytest.raises((ConnectionRefusedError, OSError)):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(2)
            client_socket.sendto(b"old-port-ping", (proxy_host, initial_bedrock_port))
            client_socket.recvfrom(1024)

    print(f"(SIGHUP Test) Verifying new port {reloaded_bedrock_port} is open...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(2)
            client_socket.sendto(b"new-port-ping", (proxy_host, reloaded_bedrock_port))
    except (socket.error, ConnectionRefusedError) as e:
        pytest.fail(f"New port {reloaded_bedrock_port} was not open: {e}")

    print("(SIGHUP Test) Test passed: Proxy correctly reloaded its configuration.")


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

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready before chaos test."
    )

    print("\n(Crash Test) Pre-warming server to ensure it is running...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as pre_warm_socket:
            pre_warm_socket.settimeout(10)
            pre_warm_socket.connect((proxy_host, java_proxy_port))
        assert wait_for_container_status(
            docker_client_fixture, mc_java_container_name, ["running"], timeout=240
        ), "Server did not start during pre-warming."
    except Exception as e:
        pytest.fail(f"Chaos test pre-warming failed: {e}")
    print("(Crash Test) Server is confirmed to be running and ready.")
    time.sleep(5)

    victim_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        victim_socket.connect((proxy_host, java_proxy_port))
        print("(Chaos Test) Victim client connected, session established.")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Establishing TCP session",
            timeout=30,
        ), "Proxy did not log the establishment of the victim's TCP session."
        print("(Chaos Test) Proxy session is active.")

        print(f"(Crash Test) Forcibly killing container: {mc_java_container_name}")
        container = docker_client_fixture.containers.get(mc_java_container_name)
        container.kill()
        assert wait_for_container_status(
            docker_client_fixture, mc_java_container_name, ["exited", "dead"]
        ), "Container did not stop after being killed."
        print("(Crash Test) Container successfully killed.")

        time.sleep(1)

        with pytest.raises(socket.error):
            print("(Chaos Test) Sending data to trigger proxy's error handling...")
            victim_socket.sendall(b"data_after_crash")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Connection error, cleaning up.",
            timeout=10,
        ), "Proxy did not log the session cleanup after the container crash."

        print("(Chaos Test) Test passed: Proxy correctly handled the crashed session.")

    finally:
        victim_socket.close()
