# Nether-bridge: On-Demand Minecraft Server Proxy

[![Docker Build Status](https://img.shields.io/github/actions/workflow/status/meek2100/nether-bridge/main-build.yml?branch=main&label=Release%20Build&logo=docker)](https://github.com/meek2100/nether-bridge/actions/workflows/main-build.yml)
[![Docker Image Version](https://img.shields.io/github/v/release/meek2100/nether-bridge?label=Latest%20Release&logo=github)](https://github.com/meek2100/nether-bridge/releases)

Nether-bridge is an intelligent proxy for Minecraft servers running in Docker. It automatically starts server containers when a player tries to connect and stops them after a period of inactivity, helping to save system resources.

This is ideal for home server environments where multiple Minecraft servers (Java or Bedrock) are available but not always in use.

## Features

- **On-Demand Server Startup**: Automatically starts Minecraft server containers when a player connection is detected.
- **Multi-Platform Support**: Natively supports both **Minecraft: Java Edition** and **Minecraft: Bedrock Edition** servers.
- **Automatic Shutdown**: Monitors server activity and stops containers after a configurable idle period to free up resources.
- **Multi-Server Support**: Manages multiple Minecraft servers simultaneously, each on its own port.
- **Dynamic Readiness Probing**: Uses `mcstatus` to actively query the server status, ensuring it's fully ready before forwarding player traffic.
- **Flexible Configuration**: Configure entirely with environment variables or use JSON files for local overrides.
- **Robust Health Checks**: A two-stage health check correctly reports the container's status for Docker and orchestrators.
- **Docker-Native**: Designed to integrate seamlessly with a Docker-based server setup using the host's Docker socket.

## How It Works

The proxy listens for initial player connection packets on the ports you define. Its orchestration logic is simple and resource-efficient.

1.  A player attempts to connect to a server address that points to the Nether-bridge proxy.
2.  The proxy receives the first packet and checks if the corresponding Minecraft server container is running.
3.  **If the server is stopped**, the proxy issues a `docker start` command and temporarily buffers the player's connection packets.
4.  The proxy then begins probing the server until it responds to a status query, indicating it's fully loaded.
5.  Once the server is ready, the proxy forwards the buffered packets and establishes a direct, two-way UDP stream between the player and the server.
6.  A background thread continuously monitors the player count on all active servers. If a server has zero players for a configurable amount of time, the proxy issues a `docker stop` command to shut it down safely.

```mermaid
sequenceDiagram
    participant Player
    participant Nether-bridge Proxy
    participant Docker
    participant Minecraft Server

    Player->>+Nether-bridge Proxy: Attempts to connect (sends UDP packet)
    Nether-bridge Proxy->>Docker: Is container 'mc-server' running?
    alt Container is stopped
        Nether-bridge Proxy->>Docker: Start container 'mc-server'
        Nether-bridge Proxy-->>Player: (Buffers initial packets)
        loop Until Server is Ready
            Nether-bridge Proxy->>+Minecraft Server: Ping for status
            Minecraft Server-->>-Nether-bridge Proxy: Respond (or timeout)
        end
    end
    Note over Nether-bridge Proxy, Minecraft Server: Server is now running
    Nether-bridge Proxy->>+Minecraft Server: Forward buffered and new packets
    Minecraft Server->>+Player: Game Traffic
    Player->>+Minecraft Server: Game Traffic
