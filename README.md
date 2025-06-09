# Minecraft Bedrock On-Demand Proxy

This project provides an intelligent proxy for Minecraft Bedrock Edition (MCBE) servers running in Docker. It automatically starts server containers when a player tries to connect and stops them after a period of inactivity, helping to save system resources.

This is ideal for home server environments where multiple Minecraft servers are available but not always in use.

## Features

* **On-Demand Server Startup**: Automatically starts Minecraft server containers when a player connection is detected.
* **Automatic Shutdown**: Monitors server activity and stops containers after a configurable idle period to free up resources.
* **Multi-Server Support**: Manages multiple Minecraft servers simultaneously, each on its own port.
* **Dynamic Readiness Probing**: Uses `mcstatus` to actively query the server status, ensuring it's fully ready before forwarding traffic.
* **Highly Configurable**: Behavior can be fine-tuned using environment variables or a `proxy_config.json` file.
* **Docker-Native**: Designed to integrate seamlessly with a Docker-based server setup.

## How It Works

The proxy listens for UDP packets on ports that you map to your Minecraft servers.

1.  When a player's game client sends a connection packet to a specific port, the proxy checks if the corresponding Minecraft server container is running.
2.  If the container is stopped, the proxy issues a `docker start` command and begins probing the server's status.
3.  While the server starts, the proxy buffers the initial packets from the client.
4.  Once the server is responsive, the proxy forwards the buffered packets and establishes a two-way communication channel between the player and the server.
5.  A background thread periodically checks the player count of all running servers. If a server has zero players for longer than the configured idle timeout, the proxy issues a `docker stop` command.

## Configuration

You can configure the proxy in two ways, with environment variables taking precedence.

### 1. Environment Variables (in `docker-compose.yml`)

This is the recommended method for fine-tuning.

| Variable                                         | Default | Description                                                                                              |
| ------------------------------------------------ | ------- | -------------------------------------------------------------------------------------------------------- |
| `LOG_LEVEL`                                      | `INFO`  | [cite_start]Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`).                                                  |
| `PROXY_IDLE_TIMEOUT_SECONDS`                     | `600`   | [cite_start]Seconds a server can be idle before being stopped.                                                  |
| `PROXY_PLAYER_CHECK_INTERVAL_SECONDS`            | `60`    | [cite_start]How often (in seconds) to check for idle servers.                                                   |
| `PROXY_SERVER_READY_MAX_WAIT_TIME_SECONDS`       | `120`   | [cite_start]Max time the proxy will wait for a server to start when a player connects.                          |
| `PROXY_SERVER_STARTUP_DELAY_SECONDS`             | `5`     | [cite_start]A fixed pause after `docker start` before readiness probing begins.                                 |
| `PROXY_QUERY_TIMEOUT_SECONDS`                    | `5`     | [cite_start]Network timeout for a single server status query.                                                   |
| `PROXY_INITIAL_BOOT_READY_MAX_WAIT_TIME_SECONDS` | `180`   | [cite_start]Max readiness wait time for servers found running at proxy boot.                                    |
| `PROXY_INITIAL_SERVER_QUERY_DELAY_SECONDS`       | `10`    | [cite_start]A fixed pause before probing servers found running at proxy boot.                                   |

### 2. `proxy_config.json` File

For base configuration, you can mount a `proxy_config.json` file.

```json
{
  "idle_timeout_seconds": 600,
  "player_check_interval_seconds": 60,
  "minecraft_server_startup_delay_seconds": 5,
  "query_timeout_seconds": 5,
  "server_ready_max_wait_time_seconds": 120,
  "initial_boot_ready_max_wait_time_seconds": 180,
  "initial_server_query_delay_seconds": 10,
  "servers": [
    {
      "name": "Family Server",
      "listen_port": 19133,
      "container_name": "mcbd-family-server",
      "internal_port": 19132
    },
    {
      "name": "Friends Server",
      "listen_port": 19134,
      "container_name": "mcbd-friend-server",
      "internal_port": 19132
    }
  ]
}
```

[cite_start]The `servers` array is required and defines the mapping between the proxy's listening ports and your Minecraft server containers. 

## Usage (`docker-compose.yml`)

This proxy is designed to work alongside your Minecraft server containers and a tool like `itzg/mc-router` or `strausmann/minecraft-bedrock-connect` to present a server list to players.

Here is an example `docker-compose.yml` structure:

```yaml
services:
  # A server list provider for game clients
  mcbd-connect:
    container_name: mcbd-connect
    image: strausmann/minecraft-bedrock-connect:latest
    restart: unless-stopped
    ports:
      - "19132:19132/udp" # The default port clients connect to
    volumes:
      # serverlist.json should point to the proxy's ports (e.g., 19133, 19134)
      - ./mcbd-connect/serverlist.json:/config/serverlist.json
    networks:
      - mcbd-network

  # The on-demand proxy
  mcbd-proxy:
    container_name: mcbd-proxy
    image: ghcr.io/your-username/mcbd-proxy-builder:latest # Use your image
    restart: unless-stopped
    environment:
      # Fine-tune with environment variables here
      - LOG_LEVEL=DEBUG
      - PROXY_IDLE_TIMEOUT_SECONDS=300
    ports:
      - "19133:19133/udp" # Expose a port for each server
      - "19134:19134/udp"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required to manage containers
      - ./mcbd-proxy/proxy_config.json:/app/proxy_config.json
    networks:
      - mcbd-network

  # A Minecraft server container (managed by the proxy)
  mcbd-family-server:
    container_name: mcbd-family-server
    image: itzg/minecraft-bedrock-server:latest
    restart: "no" # IMPORTANT: Must be 'no' for the proxy to manage it
    environment:
      - EULA=TRUE
    volumes:
      - ./mcbd-family-server/data:/data
    networks:
      - mcbd-network

  # Another Minecraft server container
  mcbd-friend-server:
    container_name: mcbd-friend-server
    image: itzg/minecraft-bedrock-server:latest
    restart: "no"
    environment:
      - EULA=TRUE
    volumes:
      - ./mcbd-friend-server/data:/data
    networks:
      - mcbd-network

networks:
  mcbd-network:
    driver: bridge
```

## Building From Source

This repository contains the necessary files to build the Docker image.

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/your-username/mcbd-proxy-builder.git](https://github.com/your-username/mcbd-proxy-builder.git)
    cd mcbd-proxy-builder
    ```

2.  **Build the image:**
    ```bash
    docker build -t your-username/mcbd-proxy .
    ```

For automated builds, a GitHub Actions workflow is included that will build and push the image to the GitHub Container Registry (`ghcr.io`) on every push to the `main` branch.
