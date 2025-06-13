#!/bin/sh
set -e

# Get the Group ID (GID) of the mounted docker.sock file
DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

# Create a new group named 'socketgroup' with the same GID as the docker socket
# This avoids conflicts if a group with that GID already exists.
addgroup --gid ${DOCKER_SOCKET_GID} socketgroup

# Add the 'nonroot' user to this new group
adduser nonroot socketgroup

# Execute the main application (the Dockerfile's CMD) as the 'nonroot' user
exec gosu nonroot "$@"