"""
Shared helper functions for unit, integration, and load tests.
"""

import os


def get_proxy_host():
    """
    Determines the correct IP address or hostname for integration tests
    by checking the environment in a specific order of precedence.

    The order of precedence is:
    1. Inside Docker ('CI_MODE'): Uses Docker's internal DNS.
    2. Explicit 'PROXY_IP': A direct override for specific CI scenarios.
    3. Remote Docker 'DOCKER_HOST_IP': For targeting a remote Docker daemon.
    4. Fallback to '127.0.0.1': For standard local development.
    """
    if os.environ.get("CI_MODE"):
        return "nether-bridge"
    if "PROXY_IP" in os.environ:
        return os.environ["PROXY_IP"]
    if "DOCKER_HOST_IP" in os.environ:
        return os.environ["DOCKER_HOST_IP"]
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
