# Nether-bridge: On-Demand Minecraft Server Proxy

Nether-bridge is an intelligent proxy for Minecraft servers running in Docker. It automatically starts server containers when a player tries to connect and stops them after a period of inactivity, helping to save system resources.

This is ideal for home server environments where multiple Minecraft servers are available but not always in use.

*Developed by [meek2100](https://github.com/meek2100)*

## Features

- **On-Demand Server Startup**: Automatically starts Minecraft server containers when a player connection is detected.
- **Multi-Platform Support**: Natively supports both **Minecraft: Java Edition** and **Minecraft: Bedrock Edition** servers.
- **Automatic Shutdown**: Monitors server activity and stops containers after a configurable idle period to free up resources.
- **Multi-Server Support**: Manages multiple Minecraft servers simultaneously, each on its own port.
- **Dynamic Readiness Probing**: Uses `mcstatus` to actively query the server status, ensuring it's fully ready before forwarding traffic.
- **Flexible Configuration**: Configure entirely with environment variables or use JSON files.
- **Robust Health Checks**: A two-stage, Python-native health check correctly reports the container's status, even during startup failures.
- **Docker-Native**: Designed to integrate seamlessly with a Docker-based server setup.

## How It Works

The proxy listens for UDP (and TCP for Java) packets on ports that you map to your Minecraft servers.

1.  When a player tries to connect, the proxy checks if the corresponding Minecraft server container is running.
2.  If the container is stopped, the proxy issues a `docker start` command and begins probing the server's status, **buffering the initial connection packets**.
3.  Once the server is responsive, the proxy forwards the buffered packets and establishes a two-way communication channel.
4.  A background thread periodically checks the player count of all running servers. If a server is empty for longer than the configured idle timeout, the proxy issues a `docker stop` command.

## Configuration

You can configure the proxy in two ways, with **environment variables always taking precedence**. This allows you to set base values in JSON files and override specific ones for testing or production in your `docker-compose.yml`.

### Method 1: Environment Variables (Recommended)

This is the most flexible approach. You can define all settings in your `docker-compose.yml` file.

#### General Settings

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `NB_IDLE_TIMEOUT` | `600` | Seconds a server can be idle with no players before being stopped. |
| `NB_PLAYER_CHECK_INTERVAL` | `60` | How often (in seconds) to check for idle servers. |
| `NB_SERVER_READY_MAX_WAIT`| `120` | Max time the proxy will wait for a server to respond to a status check after starting. |
| `NB_INITIAL_BOOT_READY_MAX_WAIT`| `180` | Max readiness wait time for servers found running at proxy boot. |
| `NB_SERVER_STARTUP_DELAY` | `5` | A fixed pause (in seconds) after `docker start` before probing begins. |
| `NB_INITIAL_SERVER_QUERY_DELAY`| `10` | A fixed pause before probing servers found running at proxy boot. |
| `NB_QUERY_TIMEOUT` | `5` | Network timeout for a single server status query. |

#### Server Definitions

Define each server using an indexed block of variables (`NB_1_*`, `NB_2_*`, etc.).

```yaml
# In docker-compose.yml environment section:
environment:
  # Server 1: A Bedrock Server
  - NB_1_NAME=Bedrock Survival
  - NB_1_SERVER_TYPE=bedrock      # Required: 'bedrock' or 'java'
  - NB_1_LISTEN_PORT=19133        # Required: External port proxy listens on
  - NB_1_CONTAINER_NAME=mc-bedrock # Required: Name of the Minecraft server container
  - NB_1_INTERNAL_PORT=19132     # Required: Game port inside the server container
