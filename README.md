# Nether-bridge: On-Demand Minecraft Server Proxy

[![Docker Build Status](https://img.shields.io/github/actions/workflow/status/meek2100/nether-bridge/main-build.yml?branch=main&label=Release%20Build&logo=docker)](https://github.com/meek2100/nether-bridge/actions/workflows/main-build.yml)
[![Docker Image Version](https://img.shields.io/github/v/release/meek2100/nether-bridge?label=Latest%20Release&logo=github)](https://github.com/meek2100/nether-bridge/releases)

Nether-bridge is an intelligent proxy for Minecraft servers running in Docker. It automatically starts server containers when a player tries to connect and stops them after a period of inactivity, helping to save system resources.

This is ideal for home server environments where multiple Minecraft servers (Java or Bedrock) are available but not always in use.

## Features

-   **On-Demand Server Startup**: Automatically starts Minecraft server containers when a player connection is detected.
-   **Multi-Platform Support**: Natively supports both **Minecraft: Java Edition** and **Minecraft: Bedrock Edition** servers.
-   **Automatic Shutdown**: Monitors server activity and stops containers after a configurable idle period to free up resources.
-   **Multi-Server Support**: Manages multiple Minecraft servers simultaneously, each on its own port.
-   **Dynamic Readiness Probing**: Uses `mcstatus` to actively query the server status, ensuring it's fully ready before forwarding player traffic.
-   **Flexible Configuration**: Configure entirely with environment variables or use JSON files for local overrides.
-   **Robust Health Checks**: A two-stage health check correctly reports the container's status for Docker and orchestrators.
-   **Docker-Native**: Designed to integrate seamlessly with a Docker-based server setup using the host's Docker socket.

## How It Works

The proxy listens for initial player connection packets on the ports you define. Its orchestration logic is simple and resource-efficient.

1.  A player attempts to connect to a server address that points to the Nether-bridge proxy.
2.  The proxy receives the first packet and checks if the corresponding Minecraft server container is running.
3.  **If the server is stopped**, the proxy issues a `docker start` command (via the Docker API) and temporarily buffers the player's connection packets.
4.  The proxy then begins probing the server until it responds to a status query, indicating it's fully loaded.
5.  Once the server is ready, the proxy forwards the buffered packets and establishes a direct, two-way UDP stream between the player and the server.
6.  A background thread continuously monitors the player count on all active servers. If a server has zero players for a configurable amount of time, the proxy issues a `docker stop` command (via the Docker API) to shut it down safely.

```mermaid
sequenceDiagram
    participant Player
    participant Nether-bridge Proxy
    participant Docker Daemon
    participant Minecraft Server

    Player->>+Nether-bridge Proxy: Attempts to connect (sends UDP packet)
    Nether-bridge Proxy->>Docker Daemon: Is container 'mc-server' running?
    alt Container is stopped
        Nether-bridge Proxy->>Docker Daemon: Start container 'mc-server' (Docker API call)
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
```

## Installation

### Prerequisites

* Docker and Docker Compose installed on your server.
* Port `19132/udp` (or other desired proxy listen ports) open on your firewall if players connect from outside your local network.

### Setup Steps

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/meek2100/nether-bridge.git](https://github.com/meek2100/nether-bridge.git)
    cd nether-bridge
    ```
2.  **Configure your servers:**
    * **Recommended (Environment Variables):** Edit the `docker-compose.yml` file and set the `NB_X_...` environment variables for each Minecraft server you want to manage. This method is preferred for Docker deployments.
    * **Alternative (JSON Files):** Create `settings.json` and `servers.json` files in the root directory (see **Configuration** section below for examples). Ensure these files are mounted into the `nether-bridge` container in `docker-compose.yml` if you use this method.
3.  **Start the proxy and Minecraft servers:**
    ```bash
    docker compose up -d
    ```
    This will start the `nether-bridge` proxy and your defined Minecraft server containers (though the Minecraft servers will initially be stopped by the proxy until a player connects).

    **Important Security Note:** The `nether-bridge` container requires access to `/var/run/docker.sock` to manage other Docker containers. This gives the `nether-bridge` container significant control over your Docker host. Ensure your Docker host is secured and only trusted applications run with such privileges.

## Configuration

Nether-bridge can be configured using environment variables or JSON files. Environment variables take precedence over settings found in `settings.json` and `servers.json`.

### Main Proxy Settings

These settings control the proxy's behavior, such as idle timeouts and query intervals.

| Environment Variable | `settings.json` Key | Default Value | Description |
| :------------------------------- | :----------------------------------- | :------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOG_LEVEL` | `log_level` | `INFO` | Sets the logging verbosity. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `NB_IDLE_TIMEOUT` | `idle_timeout_seconds` | `600` | Time in seconds a server must have 0 players before it's automatically stopped. |
| `NB_PLAYER_CHECK_INTERVAL` | `player_check_interval_seconds` | `60` | How often (in seconds) the proxy checks for active players on running servers. |
| `NB_QUERY_TIMEOUT` | `query_timeout_seconds` | `5` | Timeout (in seconds) for `mcstatus` queries when checking server readiness or player counts. |
| `NB_SERVER_READY_MAX_WAIT` | `server_ready_max_wait_time_seconds` | `120` | Maximum time (in seconds) the proxy will wait for a newly started server to respond to status queries before giving up and forwarding packets anyway. |
| `NB_INITIAL_BOOT_READY_MAX_WAIT` | `initial_boot_ready_max_wait_time_seconds` | `180` | Maximum time (in seconds) the proxy will wait for servers to become query-ready during initial startup (before issuing safe stops). This allows for longer initial boot times. |
| `NB_SERVER_STARTUP_DELAY` | `server_startup_delay_seconds` | `5` | Delay (in seconds) after issuing a Docker start command before the proxy begins probing the server for readiness. Gives the server a moment to begin initializing. |
| `NB_INITIAL_SERVER_QUERY_DELAY` | `initial_server_query_delay_seconds` | `10` | Delay (in seconds) before the proxy attempts to query a server that was found running on proxy startup. This allows time for the server to stabilize if it was previously mid-startup or in a crashed state. |

**Example `settings.json`:**
```json
{
  "idle_timeout_seconds": 900,
  "player_check_interval_seconds": 30,
  "query_timeout_seconds": 3,
  "log_level": "DEBUG"
}
```

### Server Definitions

These settings define each Minecraft server managed by Nether-bridge. You can define multiple servers.

| Environment Variable Prefix (e.g., `NB_1_`) | `servers.json` Key | Type | Description | Required |
| :------------------------------------------ | :------------------- | :------ | :------------------------------------------------------------------------------------------ | :------- |
| `NB_X_NAME` | `name` | string | A friendly name for the server (e.g., "Bedrock Survival"). | No |
| `NB_X_SERVER_TYPE` | `server_type` | string | Type of Minecraft server: `bedrock` or `java`. | Yes |
| `NB_X_LISTEN_PORT` | `listen_port` | integer | The UDP port Nether-bridge listens on for this server (players connect to this port). | Yes |
| `NB_X_CONTAINER_NAME` | `container_name` | string | The Docker container name of the Minecraft server. | Yes |
| `NB_X_INTERNAL_PORT` | `internal_port` | integer | The internal port the Minecraft server listens on inside its container (usually `19132` for Bedrock, `25565` for Java). | Yes |

*Replace `X` with a unique, sequential number (e.g., `1`, `2`, `3`) for each server when using environment variables.*

**Example `docker-compose.yml` (using environment variables - recommended):**
```yaml
services:
  # ... other services like mc-bedrock, mc-java ...
  nether-bridge:
    container_name: nether-bridge
    image: ghcr.io/meek2100/nether-bridge:latest # Use a specific version in production, e.g., ghcr.io/meek2100/nether-bridge:1.0.0
    restart: unless-stopped
    networks:
      - mc-network
    ports:
      - "19133:19133/udp" # Port for Bedrock server (NB_1_LISTEN_PORT)
      - "25565:25565/udp" # Port for Java server discovery (NB_2_LISTEN_PORT)
      - "25565:25565/tcp" # Port for Java server gameplay (needs to be exposed by Docker)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required for Docker management
      # Optionally mount JSON config files (uncomment to use these instead of environment variables)
      # - ./data/nether-bridge/settings.json:/app/settings.json
      # - ./data/nether-bridge/servers.json:/app/servers.json
    environment:
      # General settings
      - LOG_LEVEL=INFO
      - NB_IDLE_TIMEOUT=300
      - NB_PLAYER_CHECK_INTERVAL=60
      - NB_QUERY_TIMEOUT=5
      - NB_SERVER_READY_MAX_WAIT=120
      - NB_INITIAL_BOOT_READY_MAX_WAIT=180
      - NB_SERVER_STARTUP_DELAY=5
      - NB_INITIAL_SERVER_QUERY_DELAY=10

      # --- Server Definitions ---
      # Server 1: Bedrock Survival
      - NB_1_NAME=Bedrock Survival
      - NB_1_SERVER_TYPE=bedrock
      - NB_1_LISTEN_PORT=19133
      - NB_1_CONTAINER_NAME=mc-bedrock # Matches service name below
      - NB_1_INTERNAL_PORT=19132

      # Server 2: Java Creative
      - NB_2_NAME=Java Creative
      - NB_2_SERVER_TYPE=java
      - NB_2_LISTEN_PORT=25565
      - NB_2_CONTAINER_NAME=mc-java # Matches service name below
      - NB_2_INTERNAL_PORT=25565
```

**Example `servers.json` (alternative to environment variables):**
```json
{
  "servers": [
    {
      "name": "Bedrock Survival",
      "server_type": "bedrock",
      "listen_port": 19133,
      "container_name": "mc-bedrock",  # Must match the actual Docker container name
      "internal_port": 19132
    },
    {
      "name": "Java Creative",
      "server_type": "java",
      "listen_port": 25565,
      "container_name": "mc-java",    # Must match the actual Docker container name
      "internal_port": 25565
    }
  ]
}
```

If using `servers.json` or `settings.json`, you must uncomment and add the volume mounts in your `docker-compose.yml` for the `nether-bridge` service:

```yaml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required
      - ./data/nether-bridge/settings.json:/app/settings.json # Uncomment to use
      - ./data/nether-bridge/servers.json:/app/servers.json   # Uncomment to use
```

## Usage

1.  Ensure your `docker-compose.yml` is configured and running (`docker compose up -d`).
2.  **For Bedrock Edition:** Players connect to the IP address of your server (where Nether-bridge is running) on the configured Bedrock listen port (e.g., `19133`). The proxy will automatically start the associated Bedrock server if it's not running.
3.  **For Java Edition:** Players connect to the IP address of your server on the configured Java listen port (e.g., `25565`). The proxy will start the associated Java server.

## Health Checks

Nether-bridge includes a robust health check that can be used by Docker or Kubernetes to monitor its status. The health check performs two stages:
1.  Verifies that server configurations are loaded successfully (either via environment variables or JSON files).
2.  Checks for a recent "heartbeat" from the main proxy process, ensuring the core loop is active and not frozen.

## Contributing

Contributions are welcome! Please feel free to open issues or pull requests on the GitHub repository.

## License

[MIT License](LICENSE)

## Installation

### Prerequisites

* Docker and Docker Compose installed on your server.
* Port `19132/udp` (or other desired proxy listen ports) open on your firewall if players connect from outside your local network.

### Setup Steps

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/meek2100/nether-bridge.git](https://github.com/meek2100/nether-bridge.git)
    cd nether-bridge
    ```
2.  **Configure your servers:**
    * **Recommended (Environment Variables):** Edit the `docker-compose.yml` file and set the `NB_X_...` environment variables for each Minecraft server you want to manage. This method is preferred for Docker deployments.
    * **Alternative (JSON Files):** Create `settings.json` and `servers.json` files in the root directory (see **Configuration** section below for examples). Ensure these files are mounted into the `nether-bridge` container in `docker-compose.yml` if you use this method.
3.  **Start the proxy and Minecraft servers:**
    ```bash
    docker compose up -d
    ```
    This will start the `nether-bridge` proxy and your defined Minecraft server containers (though the Minecraft servers will initially be stopped by the proxy until a player connects).

    **Important Security Note:** The `nether-bridge` container requires access to `/var/run/docker.sock` to manage other Docker containers. This gives the `nether-bridge` container significant control over your Docker host. Ensure your Docker host is secured and only trusted applications run with such privileges.

## Configuration

Nether-bridge can be configured using environment variables or JSON files. Environment variables take precedence over settings found in `settings.json` and `servers.json`.

### Main Proxy Settings

These settings control the proxy's behavior, such as idle timeouts and query intervals.

| Environment Variable | `settings.json` Key | Default Value | Description |
| :------------------------------- | :----------------------------------- | :------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOG_LEVEL` | `log_level` | `INFO` | Sets the logging verbosity. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `NB_IDLE_TIMEOUT` | `idle_timeout_seconds` | `600` | Time in seconds a server must have 0 players before it's automatically stopped. |
| `NB_PLAYER_CHECK_INTERVAL` | `player_check_interval_seconds` | `60` | How often (in seconds) the proxy checks for active players on running servers. |
| `NB_QUERY_TIMEOUT` | `query_timeout_seconds` | `5` | Timeout (in seconds) for `mcstatus` queries when checking server readiness or player counts. |
| `NB_SERVER_READY_MAX_WAIT` | `server_ready_max_wait_time_seconds` | `120` | Maximum time (in seconds) the proxy will wait for a newly started server to respond to status queries before giving up and forwarding packets anyway. |
| `NB_INITIAL_BOOT_READY_MAX_WAIT` | `initial_boot_ready_max_wait_time_seconds` | `180` | Maximum time (in seconds) the proxy will wait for servers to become query-ready during initial startup (before issuing safe stops). This allows for longer initial boot times. |
| `NB_SERVER_STARTUP_DELAY` | `server_startup_delay_seconds` | `5` | Delay (in seconds) after issuing a Docker start command before the proxy begins probing the server for readiness. Gives the server a moment to begin initializing. |
| `NB_INITIAL_SERVER_QUERY_DELAY` | `initial_server_query_delay_seconds` | `10` | Delay (in seconds) before the proxy attempts to query a server that was found running on proxy startup. This allows time for the server to stabilize if it was previously mid-startup or in a crashed state. |

**Example `settings.json`:**
```json
{
  "idle_timeout_seconds": 900,
  "player_check_interval_seconds": 30,
  "query_timeout_seconds": 3,
  "log_level": "DEBUG"
}

### Server Definitions

These settings define each Minecraft server managed by Nether-bridge. You can define multiple servers.

| Environment Variable Prefix (e.g., `NB_1_`) | `servers.json` Key | Type | Description | Required |
| :------------------------------------------ | :------------------- | :------ | :------------------------------------------------------------------------------------------ | :------- |
| `NB_X_NAME` | `name` | string | A friendly name for the server (e.g., "Bedrock Survival"). | No |
| `NB_X_SERVER_TYPE` | `server_type` | string | Type of Minecraft server: `bedrock` or `java`. | Yes |
| `NB_X_LISTEN_PORT` | `listen_port` | integer | The UDP port Nether-bridge listens on for this server (players connect to this port). | Yes |
| `NB_X_CONTAINER_NAME` | `container_name` | string | The Docker container name of the Minecraft server. | Yes |
| `NB_X_INTERNAL_PORT` | `internal_port` | integer | The internal port the Minecraft server listens on inside its container (usually `19132` for Bedrock, `25565` for Java). | Yes |

*Replace `X` with a unique, sequential number (e.g., `1`, `2`, `3`) for each server when using environment variables.*

**Example `docker-compose.yml` (using environment variables - recommended):**
```yaml
services:
  # ... other services like mc-bedrock, mc-java ...
  nether-bridge:
    container_name: nether-bridge
    image: ghcr.io/meek2100/nether-bridge:latest # Use a specific version in production, e.g., ghcr.io/meek2100/nether-bridge:1.0.0
    restart: unless-stopped
    networks:
      - mc-network
    ports:
      - "19133:19133/udp" # Port for Bedrock server (NB_1_LISTEN_PORT)
      - "25565:25565/udp" # Port for Java server discovery (NB_2_LISTEN_PORT)
      - "25565:25565/tcp" # Port for Java server gameplay (needs to be exposed by Docker)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required for Docker management
      # Optionally mount JSON config files (uncomment to use these instead of environment variables)
      # - ./data/nether-bridge/settings.json:/app/settings.json
      # - ./data/nether-bridge/servers.json:/app/servers.json
    environment:
      # General settings
      - LOG_LEVEL=INFO
      - NB_IDLE_TIMEOUT=300
      - NB_PLAYER_CHECK_INTERVAL=60
      - NB_QUERY_TIMEOUT=5
      - NB_SERVER_READY_MAX_WAIT=120
      - NB_INITIAL_BOOT_READY_MAX_WAIT=180
      - NB_SERVER_STARTUP_DELAY=5
      - NB_INITIAL_SERVER_QUERY_DELAY=10

      # --- Server Definitions ---
      # Server 1: Bedrock Survival
      - NB_1_NAME=Bedrock Survival
      - NB_1_SERVER_TYPE=bedrock
      - NB_1_LISTEN_PORT=19133
      - NB_1_CONTAINER_NAME=mc-bedrock # Matches service name below
      - NB_1_INTERNAL_PORT=19132

      # Server 2: Java Creative
      - NB_2_NAME=Java Creative
      - NB_2_SERVER_TYPE=java
      - NB_2_LISTEN_PORT=25565
      - NB_2_CONTAINER_NAME=mc-java # Matches service name below
      - NB_2_INTERNAL_PORT=25565

**Example `servers.json` (alternative to environment variables):**
```json
{
  "servers": [
    {
      "name": "Bedrock Survival",
      "server_type": "bedrock",
      "listen_port": 19133,
      "container_name": "mc-bedrock",  # Must match the actual Docker container name
      "internal_port": 19132
    },
    {
      "name": "Java Creative",
      "server_type": "java",
      "listen_port": 25565,
      "container_name": "mc-java",    # Must match the actual Docker container name
      "internal_port": 25565
    }
  ]
}

If using `servers.json` or `settings.json`, you must uncomment and add the volume mounts in your `docker-compose.yml` for the `nether-bridge` service:

```yaml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # Required
      - ./data/nether-bridge/settings.json:/app/settings.json # Uncomment to use
      - ./data/nether-bridge/servers.json:/app/servers.json   # Uncomment to use

## Usage

1.  Ensure your `docker-compose.yml` is configured and running (`docker compose up -d`).
2.  **For Bedrock Edition:** Players connect to the IP address of your server (where Nether-bridge is running) on the configured Bedrock listen port (e.g., `19133`). The proxy will automatically start the associated Bedrock server if it's not running.
3.  **For Java Edition:** Players connect to the IP address of your server on the configured Java listen port (e.g., `25565`). The proxy will start the associated Java server.

## Health Checks

Nether-bridge includes a robust health check that can be used by Docker or Kubernetes to monitor its status. The health check performs two stages:
1.  Verifies that server configurations are loaded successfully (either via environment variables or JSON files).
2.  Checks for a recent "heartbeat" from the main proxy process, ensuring the core loop is active and not frozen.

## Contributing

Contributions are welcome! Please feel free to open issues or pull requests on the GitHub repository.

## License

[MIT License](LICENSE)