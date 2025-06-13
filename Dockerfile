# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Argument to accept the Docker group ID from the host. Defaults to 999 for most systems.
ARG DOCKER_GID=999

# Create a group with the host's Docker GID and a dedicated user for the app.
# Add the new user to this group so it can access the Docker socket.
RUN addgroup --gid ${DOCKER_GID} dockersocket && \
    adduser --system --no-create-home --ingroup dockersocket nonroot

# Create a writable directory for runtime files
RUN mkdir -p /run/app && chown nonroot:dockersocket /run/app

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

# Change ownership of the app directory and its contents to the non-root user
RUN chown -R nonroot:dockersocket /app

# Expose ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Switch to the non-privileged user before running the application
USER nonroot

# Define entrypoint
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command
CMD []