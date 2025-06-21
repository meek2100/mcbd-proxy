#!/bin/sh
# entrypoint.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# Check if the Docker socket is mounted.
if [ -S /var/run/docker.sock ]; then
    # Get the Group ID (GID) of the Docker socket.
    DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

    # Check if a group with the same GID already exists.
    # If not, create a new group 'dockersock' with this GID.
    if ! getent group "$DOCKER_SOCKET_GID" >/dev/null; then
        echo "Creating group 'dockersock' with GID ${DOCKER_SOCKET_GID}"
        addgroup --gid "$DOCKER_SOCKET_GID" dockersock
    fi
    
    # Add the 'naeus' user to the group that owns the Docker socket.
    # This grants the necessary permissions.
    echo "Adding user 'naeus' to group GID ${DOCKER_SOCKET_GID}"
    usermod -aG "$DOCKER_SOCKET_GID" naeus
else
    echo "Warning: /var/run/docker.sock is not mounted. Docker functionality will be unavailable."
fi

# Execute the main command passed to the container (e.g., python nether_bridge.py)
# The 'exec' command replaces the shell process with the new process.
# 'su-exec naeus' runs the command as the 'naeus' user.
exec su-exec naeus "$@"