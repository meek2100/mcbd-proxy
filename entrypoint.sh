#!/bin/sh
# entrypoint.sh
# This script is the entrypoint for the Docker container. It prepares the
# environment and then executes the main application command.

# Exit immediately if any command fails.
set -e

# This block checks for a special flag used only during CI/CD integration tests.
# If found, it generates a servers.json file and ensures the application user can access it.
if [ "$1" = "--generate-test-config" ]; then
    echo "Found --generate-test-config flag. Generating test servers.json..."
    # Create the servers.json file for the test environment.
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

    # Change ownership of the generated file to the application user ('naeus').
    # This is critical for tests that need to modify this file at runtime.
    chown naeus:nogroup /app/servers.json
    
    # Remove the flag from the argument list so it isn't passed to the Python app.
    shift
fi

# This block grants the non-root user 'naeus' permission to use the Docker socket.
# It is a critical security step.
if [ -S /var/run/docker.sock ]; then
    # Get the Group ID (GID) of the Docker socket file.
    DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)

    # Harden the script: Check if stat returned a valid number.
    if ! [ "$DOCKER_SOCKET_GID" -ge 0 ] 2>/dev/null; then
        echo "Error: Could not determine GID for /var/run/docker.sock." >&2
        exit 1
    fi

    # Check if a group with the socket's GID already exists.
    # If not, create a new group named 'dockersock' with that GID.
    if ! getent group "$DOCKER_SOCKET_GID" > /dev/null 2>&1; then
        echo "Creating group 'dockersock' with GID ${DOCKER_SOCKET_GID}"
        addgroup --system --gid "$DOCKER_SOCKET_GID" dockersock
    fi
    
    # Get the name of the group that owns the Docker socket.
    DOCKER_GROUP_NAME=$(getent group "$DOCKER_SOCKET_GID" | cut -d: -f1)

    # Add the 'naeus' user to that group.
    echo "Adding user 'naeus' to group '${DOCKER_GROUP_NAME}'"
    adduser naeus "$DOCKER_GROUP_NAME"
else
    echo "Warning: /var/run/docker.sock is not mounted. Docker functionality will be unavailable."
fi

# This is the final step. 'exec' replaces the shell process with the application.
# 'gosu' drops from root to the specified user ('naeus').
# '"$@"' passes along the command from the Dockerfile CMD (e.g., "python", "main.py").
exec gosu naeus "$@"