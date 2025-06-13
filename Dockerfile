# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Argument to accept the Docker group ID from the host, default to 999 for Linux systems
ARG DOCKER_GID=999

# Create a 'docker' group with the specified GID.
# This is for interacting with the Docker socket.
RUN addgroup --gid ${DOCKER_GID} docker

# Create a 'nonroot' group and user, then add the user to the 'docker' group.
RUN addgroup --system nonroot && \
    adduser --system --ingroup nonroot --no-create-home nonroot && \
    adduser nonroot docker

# Create a writable directory for the application's runtime files
# and give ownership to the new nonroot user and group.
RUN mkdir -p /run/app && chown nonroot:nonroot /run/app

# Arguments for build metadata
ARG BUILD_DATE
ARG APP_VERSION
ARG VCS_REF

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Expose ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Switch to the non-privileged user
USER nonroot

# Define entrypoint
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command
CMD []