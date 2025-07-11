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


# --- Integration Test Cases ---
@pytest.mark.integration
def test_bedrock_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-bedrock server starts when a connection attempt is made
    to the nether-bridge proxy on its Bedrock port.
    """
    proxy_host = get_proxy_host()
    bedrock_proxy_port = BEDROCK_PROXY_PORT
    mc_bedrock_container_name = "mc-bedrock"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = get_container_status(
        docker_client_fixture, mc_bedrock_container_name
    )
    assert initial_status in [
        "exited",
        "created",
    ], f"Bedrock server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_bedrock_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        print(
            f"Simulating connection to nether-bridge on port {bedrock_proxy_port} "
            f"on host {proxy_host}..."
        )
        unconnected_ping_packet = (
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
            b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
        print("Bedrock 'Unconnected Ping' packet sent.")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "First packet received for stopped server. Starting...",
            timeout=10,
        ), "Proxy did not log that it was starting the Bedrock server."

        assert wait_for_container_status(
            docker_client_fixture,
            mc_bedrock_container_name,
            ["running"],
            timeout=180,
            interval=2,
        ), f"Bedrock server '{mc_bedrock_container_name}' did not start after 180s."

        assert wait_for_mc_server_ready(
            {
                "host": proxy_host,
                "port": bedrock_proxy_port,
                "type": "bedrock",
            },
            timeout=180,
            interval=5,
        ), "Bedrock server did not become query-ready through proxy."
    finally:
        client_socket.close()


@pytest.mark.integration
def test_java_server_starts_on_connection(docker_compose_up, docker_client_fixture):
    """
    Test that the mc-java server starts when a connection attempt is made
    to the nether-bridge proxy on its Java port.
    """
    proxy_host = get_proxy_host()
    java_proxy_port = JAVA_PROXY_PORT
    mc_java_container_name = "mc-java"

    assert wait_for_proxy_to_be_ready(docker_client_fixture), (
        "Proxy did not become ready."
    )

    initial_status = get_container_status(docker_client_fixture, mc_java_container_name)
    assert initial_status in [
        "exited",
        "created",
    ], f"Java server should be stopped, but is: {initial_status}"
    print(f"\nInitial status of {mc_java_container_name}: {initial_status}")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(
            f"Simulating connection to nether-bridge on port {java_proxy_port} "
            f"on host {proxy_host}..."
        )
        client_socket.connect((proxy_host, java_proxy_port))
        print(f"Successfully connected to {proxy_host}:{java_proxy_port}.")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "First TCP connection for stopped server. Starting...",
            timeout=10,
        ), "Proxy did not log that it was starting the Java server."

        handshake_packet, status_request_packet = (
            get_java_handshake_and_status_request_packets(proxy_host, java_proxy_port)
        )
        client_socket.sendall(handshake_packet)
        client_socket.sendall(status_request_packet)
        print("Java handshake and status request packets sent.")

        assert wait_for_container_status(
            docker_client_fixture,
            mc_java_container_name,
            ["running"],
            timeout=180,
            interval=2,
        ), f"Java server '{mc_java_container_name}' did not start after 180s."

        assert wait_for_mc_server_ready(
            {"host": proxy_host, "port": java_proxy_port, "type": "java"},
            timeout=180,
            interval=5,
        ), "Java server did not become query-ready through proxy."

    finally:
        client_socket.close()


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
        "Server idle with 0 sessions. Initiating shutdown.",
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

    # --- 1. Start the server and confirm it is running ---
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

    # --- 2. Manually stop the container to simulate a crash ---
    print(f"(Crash Test) Manually stopping container '{mc_bedrock_container_name}'...")
    container = docker_client_fixture.containers.get(mc_bedrock_container_name)
    container.stop()
    assert wait_for_container_status(
        docker_client_fixture, mc_bedrock_container_name, ["exited", "dead"], timeout=90
    ), "Container did not stop after manual command."
    print("(Crash Test) Container successfully stopped.")

    time.sleep(2)

    # --- 3. Attempt a new connection to the 'crashed' server ---
    print("\n(Crash Test) Attempting new connection to trigger restart...")
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client_socket.sendto(unconnected_ping_packet, (proxy_host, bedrock_proxy_port))
    finally:
        client_socket.close()

    # --- 4. Verify that the proxy detects this and tries to start it again ---
    assert wait_for_log_message(
        docker_client_fixture,
        "nether-bridge",
        "First packet received for stopped server. Starting...",
        timeout=10,
    ), "Proxy did not log that it was attempting to restart the server."

    print("(Crash Test) Proxy correctly logged its intent to restart. Test passed.")


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

    # --- 1. Verify initial configuration is active ---
    print("\n(SIGHUP Test) Verifying initial server configuration...")
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"initial-ping", (proxy_host, initial_bedrock_port))
        client_socket.recvfrom(1024)
    except socket.timeout:
        print(f"Initial check on port {initial_bedrock_port} is OK.")
    except ConnectionRefusedError:
        pytest.fail(f"Initial port {initial_bedrock_port} should be open.")
    finally:
        client_socket.close()

    # --- 2. Create new config and send SIGHUP ---
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
    # FIX: Use `printf` instead of `echo` to safely write the JSON string
    # without the shell mangling the quotes.
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

    # --- 4. Verify new configuration is active and old one is not ---
    try:
        print(f"(SIGHUP Test) Verifying old port {initial_bedrock_port} is closed...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"old-port-ping", (proxy_host, initial_bedrock_port))
        data, addr = client_socket.recvfrom(1024)
        pytest.fail(f"Old port {initial_bedrock_port} is still open unexpectedly.")
    except (ConnectionRefusedError, OSError):
        print(f"Old port {initial_bedrock_port} is correctly closed.")
    except socket.timeout:
        pytest.fail(f"Old port {initial_bedrock_port} is still open (timed out).")
    finally:
        client_socket.close()

    try:
        print(f"(SIGHUP Test) Verifying new port {reloaded_bedrock_port} is open...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(2)
        client_socket.sendto(b"new-port-ping", (proxy_host, reloaded_bedrock_port))
        client_socket.recvfrom(1024)
    except socket.timeout:
        print(f"New port {reloaded_bedrock_port} is correctly open.")
    except ConnectionRefusedError:
        pytest.fail(f"New port {reloaded_bedrock_port} was refused.")
    finally:
        client_socket.close()

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

    # Step 1: Pre-warm the server to ensure it is running and fully ready.
    print("\n(Chaos Test) Pre-warming server to ensure it is running...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as pre_warm_socket:
            pre_warm_socket.connect((proxy_host, java_proxy_port))
            (
                handshake,
                status_request,
            ) = get_java_handshake_and_status_request_packets(
                proxy_host, java_proxy_port
            )
            pre_warm_socket.sendall(handshake)
            pre_warm_socket.sendall(status_request)
            # FIX: Increase timeout to allow for slow server startup in CI.
            assert wait_for_mc_server_ready(
                {"host": proxy_host, "port": java_proxy_port, "type": "java"},
                timeout=240,
            ), "Server did not become query-ready during pre-warming."
        print("(Chaos Test) Server is confirmed to be running and ready.")
        time.sleep(2)
    except Exception as e:
        pytest.fail(f"Chaos test pre-warming failed: {e}")

    # Step 2: Establish the actual "victim" session to be tested.
    victim_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        victim_socket.connect((proxy_host, java_proxy_port))
        print("(Chaos Test) Victim client connected, session established.")

        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "Establishing new TCP session for running server",
            timeout=30,
        ), "Proxy did not log the establishment of the victim's TCP session."
        print("(Chaos Test) Proxy session is active.")

        # Step 4: Forcibly kill the server container.
        print(f"(Chaos Test) Forcibly killing container: {mc_java_container_name}")
        container = docker_client_fixture.containers.get(mc_java_container_name)
        container.kill()
        assert wait_for_container_status(
            docker_client_fixture, mc_java_container_name, ["exited", "dead"]
        ), "Container did not stop after being killed."
        print("(Chaos Test) Container successfully killed.")

        time.sleep(1)

        # Step 5: Attempt to send data.
        try:
            print("(Chaos Test) Sending data to trigger proxy's error handling...")
            victim_socket.sendall(b"data_after_crash")
        except socket.error as e:
            print(f"(Chaos Test) Client socket error as expected: {e}")

        # Step 6: Assert that the PROXY detected the error and logged the cleanup.
        assert wait_for_log_message(
            docker_client_fixture,
            "nether-bridge",
            "[DEBUG] Session cleanup block triggered by connection error.",
            timeout=10,
        ), "Proxy did not log the session cleanup after the container crash."

        print("(Chaos Test) Test passed: Proxy correctly handled the crashed session.")

    finally:
        victim_socket.close()
