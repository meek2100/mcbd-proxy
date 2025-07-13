import os

# --- Constants ---
BEDROCK_PROXY_PORT = 19132
JAVA_PROXY_PORT = 25565
BEDROCK_UNCONNECTED_PING = (
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\xfe\xfe\xfe\xfe"
    b"\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00"
)


def get_proxy_host():
    """
    Determines the correct IP address or hostname for the proxy, preserving
    the original logic for different test environments.
    """
    # Inside the test container network, services are reached by their name.
    if os.environ.get("NB_TEST_MODE") == "container":
        return "nether-bridge"

    # In CI, services are also on the same Docker network.
    if os.environ.get("CI_MODE"):
        return "nether-bridge"

    # 3. Check for an explicit IP for remote testing via pytest.
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]  #
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]

    # Default for local development outside a container.
    return "127.0.0.1"


def encode_varint(value):
    """Helper to encode VarInt for Java protocol."""
    buf = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80
        buf += bytes([byte])
        if value == 0:
            break
    return buf


def get_java_handshake_and_status_request_packets(host, port):
    """Constructs the two packets needed to request a status from a Java server."""
    server_address_bytes = host.encode("utf-8")
    handshake_payload = (
        encode_varint(754)
        + encode_varint(len(server_address_bytes))
        + server_address_bytes
        + port.to_bytes(2, byteorder="big")
        + encode_varint(1)
    )
    handshake_packet = (
        encode_varint(len(handshake_payload) + 1) + b"\x00" + handshake_payload
    )

    status_request_payload = b""
    status_request_packet = (
        encode_varint(len(status_request_payload) + 1)
        + b"\x00"
        + status_request_payload
    )

    return handshake_packet, status_request_packet
