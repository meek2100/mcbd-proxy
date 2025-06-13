#!/bin/sh
set -e

# Get the Group ID (GID) of the mounted docker.sock file
DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

# Create a new group 'socketgroup' with the same GID as the docker socket
addgroup --gid ${DOCKER_SOCKET_GID} socketgroup

# Add the 'nonroot' user to this new group
adduser nonroot socketgroup

# Execute the main application as the 'nonroot' user, passing along any CMD
# gosu is a lightweight tool for dropping privileges
exec gosu nonroot "$@"