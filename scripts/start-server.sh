#!/bin/bash
# start-server.sh - Starts a Docker container by name

if [ -z "$1" ]; then
  echo "Usage: $0 <container_name>"
  exit 1
fi

CONTAINER_NAME=$1

echo "Attempting to start Minecraft Bedrock server: $CONTAINER_NAME"
docker start "$CONTAINER_NAME"

echo "Docker command 'start $CONTAINER_NAME' executed."
exit 0
