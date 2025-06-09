#!/bin/sh
set -e

CONFIGURED_FLAG="/tmp/proxy_configured"
HEARTBEAT_FILE="/tmp/proxy_heartbeat"
STALE_THRESHOLD=60 # Seconds

# Stage 1: Check if the application is configured.
# Fails if the /tmp/proxy_configured file does not exist.
if [ ! -f "$CONFIGURED_FLAG" ]; then
  exit 1
fi

# Stage 2: Check if the heartbeat file is present and not empty.
# Fails if the /tmp/proxy_heartbeat file does not exist or is empty.
if [ ! -s "$HEARTBEAT_FILE" ]; then
  exit 1
fi

# Stage 3: Calculate heartbeat age using the portable `expr` command.
# This is more reliable than other arithmetic methods in minimal shells.
LAST_HEARTBEAT=$(cat "$HEARTBEAT_FILE" | tr -cd '0-9')
CURRENT_TIME=$(date +%s)

if [ -z "$LAST_HEARTBEAT" ]; then
  exit 1
fi

# Use 'expr' for maximum portability in shell arithmetic.
AGE=$(expr $CURRENT_TIME - $LAST_HEARTBEAT)

# Exit with success (0) if the age is less than the threshold,
# otherwise exit with failure (1).
if [ "$AGE" -lt "$STALE_THRESHOLD" ]; then
  exit 0
else
  exit 1
fi
