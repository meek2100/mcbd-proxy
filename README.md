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

You can configure the proxy in two ways, with **environment variables always taking precedence**. This allows you to set base values in the JSON file and override specific ones for testing or production in your `docker-compose.yml`.

### Method 1: Environment Variables (Recommended)

This is the most flexible approach. You can define all settings in your `docker-compose.yml` file.

#### General Settings

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `PROXY_IDLE_TIMEOUT_SECONDS` | `600` | Seconds a server can be idle before being stopped. |
| `PROXY_PLAYER_CHECK_INTERVAL_SECONDS` | `60` | How often (in seconds) to check for idle servers. |
| `PROXY_SERVER_READY_MAX_WAIT_TIME_SECONDS`| `120` | Max time the proxy will wait for a server to start when a player connects. |
| `PROXY_INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS`|`180` | Max readiness wait time for servers found running at proxy boot. |
| `PROXY_SERVER_STARTUP_DELAY_SECONDS` | `5` | A fixed pause (in seconds) after `docker start` before probing begins. |
| `PROXY_INITIAL_SERVER_QUERY_DELAY_SECONDS`| `10` | A fixed pause before probing servers found running at proxy boot. |
| `PROXY_QUERY_TIMEOUT_SECONDS` | `5` | Network timeout for a single server status query. |

#### Server Definitions

If you define servers using environment variables, the `servers` array in `proxy_config.json` will be ignored. Define each server using an indexed block of variables (`PROXY_SERVER_1_...`, `PROXY_SERVER_2_...`, etc.).

```yaml
# In docker-compose.yml environment section:
environment:
  # Server 1
  - PROXY_SERVER_1_NAME=Family Server
  - PROXY_SERVER_1_LISTEN_PORT=19133         # Required
  - PROXY_SERVER_1_CONTAINER_NAME=mc-family-server # Required
  - PROXY_SERVER_1_INTERNAL_PORT=19132      # Required
  
  # Server 2
  - PROXY_SERVER_2_NAME=Friends Server
  - PROXY_SERVER_2_LISTEN_PORT=19134
  - PROXY_SERVER_2_CONTAINER_NAME=mc-friend-server
  - PROXY_SERVER_2_INTERNAL_PORT=19132
```

### Method 2: Using `proxy_config.json` (Fallback)

If no `PROXY_SERVER_...` environment variables are set, the proxy will load the server list from a `proxy_config.json` file mounted at `/app/proxy_config.json`.

```json
{
  "idle_timeout_seconds": 600,
  "player_check_interval_seconds": 60,
  "query_timeout_seconds": 5,
  "server_ready_max_wait_time_seconds": 120,
  "initial_boot_ready_max_wait_time_seconds": 180,
  "minecraft_server_startup_delay_seconds": 5,
  "initial_server_query_delay_seconds": 10,
  "servers": [
    {
      "name": "Family Server",
      "listen_port": 19133,
      "container_name": "mc-family-server",
      "internal_port": 19132
    },
    {
      "name": "Friends Server",
      "listen_port": 19134,
      "container_name": "mc-friend-server",
      "internal_port": 19132
    }
  ]
}
```

## Usage Example

This example shows a complete setup using environment variables for configuration. It works alongside `strausmann/minecraft-bedrock-connect`, which provides the in-game server list.

```yaml
services:
  # Provides the in-game server list to players
  mc-connect:
    container_name: mc-connect
    image: strausmann/minecraft-bedrock-connect:latest
    restart: unless-stopped
    ports:
      - "19132:19132/udp" # Default MCBE port players connect to
    volumes:
      # This file should list the proxy's ports (e.g., 19133, 19134)
      - ./data/connect/serverlist.json:/config/serverlist.json
    networks:
      - mc-proxy-network

  # The on-demand proxy service
  mc-proxy:
    container_name: mc-proxy
    image: ghcr.io/meek2100/mcbd-proxy-builder:latest # Use your image
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

This repository contains a GitHub Actions workflow that will automatically build and push the Docker image to `ghcr.io` on every push to the `main` branch. You can also build it manually:

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/meek2100/mcbd-proxy-builder.git](https://github.com/meek2100/mcbd-proxy-builder.git)
    cd mcbd-proxy-builder
    ```

2.  **Build the image:**
    ```bash
    docker build -t meek2100/mcbd-proxy .
    ```
