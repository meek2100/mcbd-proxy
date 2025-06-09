# Minecraft Bedrock On-Demand Proxy

This project provides an intelligent proxy for Minecraft Bedrock Edition (MCBE) servers running in Docker. It automatically starts server containers when a player tries to connect and stops them after a period of inactivity, helping to save system resources.

This is ideal for home server environments where multiple Minecraft servers are available but not always in use.

*Developed by [meek2100](https://github.com/meek2100)*

## Features

- **On-Demand Server Startup**: Automatically starts Minecraft server containers when a player connection is detected.
- **Automatic Shutdown**: Monitors server activity and stops containers after a configurable idle period to free up resources.
- **Multi-Server Support**: Manages multiple Minecraft servers simultaneously, each on its own port.
- **Dynamic Readiness Probing**: Uses `mcstatus` to actively query the server status, ensuring it's fully ready before forwarding traffic.
- **Flexible Configuration**: Configure entirely with environment variables or use a `proxy_config.json` file.
- **Robust Health Checks**: A two-stage, Python-native health check correctly reports the container's status, even during startup failures.
- **Docker-Native**: Designed to integrate seamlessly with a Docker-based server setup.

## How It Works
The proxy listens for UDP packets on ports that you map to your Minecraft servers.

1.  When a player tries to connect, the proxy checks if the corresponding Minecraft server container is running.
2.  If the container is stopped, the proxy issues a `docker start` command and begins probing the server's status, buffering the initial connection packets.
3.  Once the server is responsive, the proxy forwards the packets and establishes a two-way communication channel.
4.  A background thread periodically checks the player count of all running servers. If a server is empty for longer than the configured idle timeout, the proxy issues a `docker stop` command.

## Configuration

You can configure the proxy in two ways, with **environment variables always taking precedence**.

#### Method 1: Environment Variables Only (Recommended)

This is the cleanest approach. You can define all settings in your `docker-compose.yml` file and do not need to mount a `proxy_config.json` file.

**General Settings**
| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `PROXY_IDLE_TIMEOUT_SECONDS` | `600` | Seconds a server can be idle before being stopped. |
| `PROXY_PLAYER_CHECK_INTERVAL_SECONDS` | `60` | How often (in seconds) to check for idle servers. |
| `PROXY_SERVER_READY_MAX_WAIT_TIME_SECONDS`| `120` | Max time the proxy will wait for a server to start. |
| ... | ... | *(and so on for all ENV vars)* |

**Server Definitions**
Define each server using an indexed block of variables (`PROXY_SERVER_1_...`, `PROXY_SERVER_2_...`, etc.).

#### Method 2: Using `proxy_config.json`

If no `PROXY_SERVER_...` variables are found in the environment, the proxy will fall back to loading the server list from a `proxy_config.json` file. You can also use this file to set default values for the general settings.

## Usage Example

This example shows a complete setup using environment variables for configuration. It works alongside `strausmann/minecraft-bedrock-connect` which provides the in-game server list.

```yaml
services:
  # Provides the in-game server list to players
  mc-connect:
    container_name: mc-connect
    image: strausmann/minecraft-bedrock-connect:latest
    restart: unless-stopped
    ports:
      - "19132:19132/udp" # Default MCBE port
    volumes:
      # This file points to the proxy's ports (19133, 19134)
      - ./data/connect/serverlist.json:/config/serverlist.json
    networks:
      - mc-proxy-network

  # The on-demand proxy service
  mc-proxy:
    container_name: mc-proxy
    image: ghcr.io/meek2100/mcbd-proxy-builder:latest # Use your own image
    restart: unless-stopped
    environment:
      # General settings
      - LOG_LEVEL=DEBUG
      - PROXY_IDLE_TIMEOUT_SECONDS=300
      # Server definitions
      - PROXY_SERVER_1_NAME=Family Server
      - PROXY_SERVER_1_LISTEN_PORT=19133
      - PROXY_SERVER_1_CONTAINER_NAME=mc-family-server
      - PROXY_SERVER_1_INTERNAL_PORT=19132
      - PROXY_SERVER_2_NAME=Friends Server
      - PROXY_SERVER_2_LISTEN_PORT=19134
      - PROXY_SERVER_2_CONTAINER_NAME=mc-friend-server
      - PROXY_SERVER_2_INTERNAL_PORT=19132
    ports:
      - "19133:19133/udp" # Must match listen ports above
      - "19134:19134/udp"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required
    networks:
      - mc-proxy-network

  # A managed Minecraft server
  mc-family-server:
    container_name: mc-family-server
    image: itzg/minecraft-bedrock-server:latest
    restart: "no" # IMPORTANT: Must be 'no' for proxy management
    environment:
      - EULA=TRUE
    volumes:
      - ./data/family-server:/data
    networks:
      - mc-proxy-network

  # Another managed Minecraft server
  mc-friend-server:
    container_name: mc-friend-server
    image: itzg/minecraft-bedrock-server:latest
    restart: "no"
    environment:
      - EULA=TRUE
    volumes:
      - ./data/friend-server:/data
    networks:
      - mc-proxy-network

networks:
  mc-proxy-network:
    driver: bridge
```

## Building From Source
This repository contains a GitHub Actions workflow (`build-proxy.yml`) that will automatically build and push the Docker image to `ghcr.io` on every push to the `main` branch. You can also build it manually:

```bash
docker build -t your-name/mc-proxy .
```
