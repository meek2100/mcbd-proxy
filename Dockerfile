# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Argument to accept the Docker group ID from the host
ARG DOCKER_GID=999

# Create a 'docker' group with the specified GID, if it doesn't exist.
RUN if ! getent group docker > /dev/null 2>&1; then \
        addgroup --gid ${DOCKER_GID} docker; \
    fi

# Create a non-privileged user and add them to the 'docker' group
RUN adduser --system --ingroup docker --no-create-home nonroot

# Create a writable directory for the application's runtime files
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
EXPOSE 25565/udp
EXPOSE 25565/tcp

# Switch to the non-privileged user
USER nonroot

# Define entrypoint
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command
CMD []