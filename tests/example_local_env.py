# =============================================================================
#  TEMPLATE FOR REMOTE DOCKER HOST TESTING
# =============================================================================
#
# This file allows the test suite to target a Docker daemon running on a remote
# machine (e.g., a Linux VM) instead of the local Docker Desktop instance.
#
# TO USE:
# 1. Uncomment the lines below.
# 2. Fill in the correct values for your remote Docker host.
# 3. Ensure the remote Docker daemon is configured to accept connections on a TCP port.
#
# This file is intentionally included in .gitignore to prevent personal
# connection settings from being committed to source control.
#
# =============================================================================


# The IP address of the remote machine where the Docker daemon is running.
# This IP is used by the test script to connect to the running containers.
# VM_HOST_IP = "192.168.1.100"

# The full URL of the remote Docker daemon's TCP socket.
# DOCKER_HOST = "tcp://192.168.1.100:2375"

# The Group ID (GID) of the 'docker' group on the remote Linux host.
# This is required for the non-root user inside the proxy container to have
# permission to access the mounted Docker socket.
#
# You can find this value by running the following command on the remote host:
# getent group docker | cut -d: -f3
#
# DOCKER_GID = 999
