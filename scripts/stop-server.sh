#!/bin/bash
# stop-server.sh - Stops a Docker container by name and reports status.

set -e # Exit immediately if a command exits with a non-zero status.

if [ -z "$1" ]; then
  echo "Usage: $0 <container_name>" >&2
  exit 1
fi

CONTAINER_NAME=$1

echo "Attempting to stop container: $CONTAINER_NAME" >&2
# The 'docker stop' command might fail if the container is already stopped.
# We check the exit code to provide clear feedback.
if docker stop "$CONTAINER_NAME"; then
  echo "Docker command 'stop $CONTAINER_NAME' executed successfully." >&2
  exit 0
else
  STATUS=$?
  # Exit code 1 can mean the container was already stopped, which is not a critical error.
  # Other non-zero codes might indicate a real problem.
  echo "Warning: Docker command 'stop $CONTAINER_NAME' failed with exit code $STATUS." >&2
  exit $STATUS
fi
