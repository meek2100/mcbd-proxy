#!/bin/sh
set -e

CONFIGURED_FLAG="/tmp/proxy_configured"
HEARTBEAT_FILE="/tmp/proxy_heartbeat"
STALE_THRESHOLD=60 # Seconds

# Stage 1: Check if the application has been successfully configured.
if [ ! -f "$CONFIGURED_FLAG" ]; then
  echo "Configuration flag not found. Proxy is not ready."
  exit 1
fi

# Stage 2: Check if the heartbeat file exists and is not empty.
if [ ! -s "$HEARTBEAT_FILE" ]; then
    echo "Heartbeat file is missing or empty."
    exit 1
fi

# Stage 3: Calculate the age of the heartbeat and check if it's stale.
LAST_HEARTBEAT=$(cat "$HEARTBEAT_FILE")
CURRENT_TIME=$(date +%s)
AGE=$(($CURRENT_TIME - $LAST_HEARTBEAT))

if [ "$AGE" -lt "$STALE_THRESHOLD" ]; then
  # Heartbeat is recent, app is alive and healthy.
  exit 0
else
  # Configured, but heartbeat is stale. App is frozen or dead.
  echo "Heartbeat is stale. Age: $AGE seconds."
  exit 1
fi
