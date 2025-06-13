#!/bin/sh
set -e

# Get the Group ID (GID) of the mounted docker.sock file
DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

# Check if the GID is 0 (which is the case for Docker Desktop)
if [ "$DOCKER_SOCKET_GID" -eq 0 ]; then
    echo "Docker socket GID is 0 (likely Docker Desktop). Skipping group modifications."
else
    # If GID is not 0 (standard Linux host), create a group with that GID
    # and add the nonroot user to it for socket permissions.
    echo "Docker socket GID is ${DOCKER_SOCKET_GID}. Configuring user permissions..."
    addgroup --gid ${DOCKER_SOCKET_GID} socketgroup
    adduser nonroot socketgroup
fi

# Execute the main application as the 'nonroot' user
exec gosu nonroot "$@"