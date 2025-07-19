# Nether-bridge Agents and Core Components

This document provides a high-level overview of the core components (agents) of the Nether-bridge application for both human developers and AI agents like Jules.

## `AsyncProxy` (in `proxy.py`)

- **Purpose**: This is the main application orchestrator. It is responsible for listening for new player connections (TCP for Java, UDP for Bedrock), managing the on-demand startup and idle shutdown of Minecraft servers, and proxying data between players and the servers.
- **Key Interactions**: It uses the `DockerManager` to control server containers and the `MetricsManager` to report statistics.

## `DockerManager` (in `docker_manager.py`)

- **Purpose**: This component handles all direct interactions with the Docker daemon via the `aiodocker` library.
- **Responsibilities**: Its primary functions are starting, stopping, and checking the running status of Docker containers. It also includes logic to wait for a Minecraft server to become fully queryable after startup.

## `MetricsManager` (in `metrics.py`)

- **Purpose**: Manages the collection and exposition of Prometheus metrics.
- **Key Metrics**: Tracks active connections, server status (running/stopped), server startup times, and bytes transferred.