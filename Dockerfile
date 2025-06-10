# --- Stage 1: Builder ---
# Use an official Python runtime as a parent image
FROM python:3.9-slim AS builder

WORKDIR /app

# Install Python requirements into a specific layer that can be copied
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# --- Stage 2: Final Production Image ---
FROM python:3.9-slim

# --- Image Metadata ---
# Define build-time arguments passed in during the 'docker build' command.
ARG BUILD_DATE
ARG APP_VERSION
ARG VCS_REF

# Set standard OCI labels for image metadata and traceability.
LABEL org.opencontainers.image.created=$BUILD_DATE
LABEL org.opencontainers.image.source="https://github.com/meek2100/nether-bridge"
LABEL org.opencontainers.image.version=$APP_VERSION
LABEL org.opencontainers.image.revision=$VCS_REF

# Set environment variables for runtime access to metadata.
ENV APP_BUILD_DATE=$BUILD_DATE
ENV APP_BUILD_VERSION=$APP_VERSION
ENV APP_VCS_REF=$VCS_REF
ENV APP_IMAGE_METADATA="{\"version\":\"${APP_VERSION}\", \"build_date\":\"${BUILD_DATE}\", \"commit\":\"${VCS_REF}\"}"


# --- Dependency Installation ---
# Install Docker CLI so the container can execute 'docker start/stop' commands.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \
    "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    \"$(. /etc/os-release && echo \"$VERSION_CODENAME\")\" stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# --- Application Setup ---
# Copy the INSTALLED Python packages from the builder stage
COPY --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages

# Copy the application source code
COPY nether_bridge.py .
COPY scripts/ ./scripts/

# Make the helper shell scripts executable.
RUN chmod +x /app/scripts/start-server.sh /app/scripts/stop-server.sh

# --- HEALTHCHECK ---
# Executes the health check logic within the Python application itself.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "nether_bridge.py", "--healthcheck" ]

# Command to run the application
CMD ["python", "nether_bridge.py"]
