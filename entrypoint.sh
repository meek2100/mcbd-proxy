#!/bin/sh
# entrypoint.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- ADD THIS BLOCK TO HANDLE TEST CONFIG GENERATION ---
if [ "$1" = "--generate-test-config" ]; then
    echo "Found --generate-test-config flag. Generating test servers.json..."
    # Create the servers.json file
    echo '{
      "servers": [
        {
          "name": "Bedrock Survival",
          "server_type": "bedrock",
          "listen_port": 19132,
          "container_name": "mc-bedrock",
          "internal_port": 19132
        },
        {
          "name": "Java Creative",
          "server_type": "java",
          "listen_port": 25565,
          "container_name": "mc-java",
          "internal_port": 25565
        }
      ]
    }' > /app/servers.json
    
    # Remove the flag from the argument list so it's not passed to the Python app
    shift
fi
# --- END OF NEW BLOCK ---

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