#!/bin/sh
set -e

# Get the Group ID (GID) of the mounted docker.sock file
DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

# Create a 'docker' group with the same GID if it doesn't already exist
if ! getent group ${DOCKER_SOCKET_GID} > /dev/null 2>&1; then
    addgroup --gid ${DOCKER_SOCKET_GID} dockergroup
fi

# Add the 'nonroot' user to that group
adduser nonroot $(getent group ${DOCKER_SOCKET_GID} | cut -d: -f1)

# Execute the main application, passing along any arguments
exec gosu nonroot "$@"