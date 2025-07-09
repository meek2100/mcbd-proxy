#!/bin/bash
# Ensures the script will exit immediately if any command fails.
set -e

# --- Test Configuration Generation ---
# This block correctly checks for a special flag passed only during CI tests.
if [[ "$1" == "--generate-test-config" ]]; then
    echo "Found --generate-test-config flag. Generating test servers.json..."
    
    # This is the critical fix. Using a quoted 'EOF' ensures the JSON block
    # is written literally, without the shell trying to interpret its contents.
    # This prevents the malformed file issue.
    cat << 'EOF' > /app/servers.json
{
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
}
EOF
    # This command solves the permissions error by ensuring the dynamically
    # created config file is owned by the `naeus` user.
    chown naeus:nogroup /app/servers.json
    # This removes the flag so it doesn't get passed to the Python app.
    shift
fi


# --- Docker Socket Permissions ---
# This block correctly and safely handles permissions for the Docker socket.
if [ -S /var/run/docker.sock ]; then
    DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)
    DOCKER_GROUP_NAME=$(getent group "$DOCKER_SOCKET_GID" | cut -d: -f1)

    # Creates a new group with the correct GID if one doesn't already exist.
    if [ -z "$DOCKER_GROUP_NAME" ]; then
        echo "Creating group 'dockersock' with GID ${DOCKER_SOCKET_GID}"
        addgroup --system --gid "$DOCKER_SOCKET_GID" dockersock
        DOCKER_GROUP_NAME=dockersock
    fi

    # Adds the application user to the group, granting socket access.
    echo "Adding user 'naeus' to group '${DOCKER_GROUP_NAME}'"
    usermod -aG "$DOCKER_GROUP_NAME" naeus
else
    echo "Warning: /var/run/docker.sock is not mounted. Docker functionality will be unavailable."
fi


# --- Privilege Dropping ---
# This is the final and most important step. `exec` replaces the shell process
# with the application, and `gosu` drops privileges from root to the `naeus` user
# before starting the Python script.
exec gosu naeus "$@"