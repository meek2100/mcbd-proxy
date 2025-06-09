#!/bin/sh
set -e

# This script checks the health of the proxy.
# It first checks if the application has been successfully configured.
# If it has, it then checks if the application is "alive" via a heartbeat.

CONFIGURED_FLAG="/tmp/proxy_configured"
HEARTBEAT_FILE="/tmp/proxy_heartbeat"

# Stage 1: Check if the application has successfully started and configured itself.
# The Python script creates this file ONLY after loading the server list.
if [ ! -f "$CONFIGURED_FLAG" ]; then
  echo "Configuration flag not found. Proxy is not ready."
  exit 1
fi

# Stage 2: If configured, check the heartbeat to ensure the main loop is running.
# The Python script touches this file every 15 seconds from its main loop.
# We check if the file has been modified in the last 60 seconds.
if find "$HEARTBEAT_FILE" -mmin -1 | grep -q .; then
  # Heartbeat is recent, app is alive and healthy.
  exit 0
else
  # Configured, but heartbeat is stale. App is frozen or dead.
  echo "Heartbeat file is stale. Proxy may be frozen."
  exit 1
fi
