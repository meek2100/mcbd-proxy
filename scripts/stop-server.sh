#!/bin/bash
# stop-server.sh - Stops a Docker container by name

if [ -z "$1" ]; then
  echo "Usage: $0 <container_name>"
  exit 1
fi

CONTAINER_NAME=$1

echo "Attempting to stop Minecraft Bedrock server: $CONTAINER_NAME"

docker stop "$CONTAINER_NAME"

echo "Docker command 'stop $CONTAINER_NAME' executed."
exit 0
