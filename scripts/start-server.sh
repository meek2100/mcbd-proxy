#!/bin/bash
# start-server.sh - Starts a Docker container by name and reports status.

set -e # Exit immediately if a command exits with a non-zero status.

if [ -z "$1" ]; then
  echo "Usage: $0 <container_name>" >&2
  exit 1
fi

CONTAINER_NAME=$1

echo "Attempting to start container: $CONTAINER_NAME" >&2
if docker start "$CONTAINER_NAME"; then
  echo "Docker command 'start $CONTAINER_NAME' executed successfully." >&2
  exit 0
else
  STATUS=$?
  echo "Error: Docker command 'start $CONTAINER_NAME' failed with exit code $STATUS." >&2
  exit $STATUS
fi
