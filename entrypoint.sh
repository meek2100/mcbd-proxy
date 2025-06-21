#!/bin/sh
# entrypoint.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# Check if the Docker socket is mounted.
if [ -S /var/run/docker.sock ]; then
    DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

    # Check if a group with the socket's GID already exists.
    # If not, create a new group named 'dockersock'.
    if ! getent group "$DOCKER_SOCKET_GID" > /dev/null 2>&1; then
        echo "Creating group 'dockersock' with GID ${DOCKER_SOCKET_GID}"
        addgroup --system --gid "$DOCKER_SOCKET_GID" dockersock
    fi
    
    # Get the name of the group with the socket's GID.
    DOCKER_GROUP_NAME=$(getent group "$DOCKER_SOCKET_GID" | cut -d: -f1)

    # Add the 'naeus' user to that group by its NAME.
    # Using 'adduser' is a safe and standard way to do this on Debian.
    echo "Adding user 'naeus' to group '${DOCKER_GROUP_NAME}'"
    adduser naeus "$DOCKER_GROUP_NAME"
else
    echo "Warning: /var/run/docker.sock is not mounted. Docker functionality will be unavailable."
fi

# Execute the main command passed to the container (e.g., python nether_bridge.py)
# 'gosu' runs the command as the 'naeus' user, with the correct group permissions.
exec gosu naeus "$@"