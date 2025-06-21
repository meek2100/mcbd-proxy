# --- Stage 1: Base ---
# Installs production dependencies. This stage is lean and well-cached.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# Prepares a non-root environment for running tests inside CI.
# This stage prioritizes correctness over layer caching for reliability.
FROM base AS testing
WORKDIR /app

# Install system packages needed by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*

# Copy the entire project context first to ensure all files are available.
COPY . .

# Install the additional development dependencies.
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Create user and set permissions for the entire app directory.
RUN adduser --system --no-create-home naeus
RUN chown -R naeus:nogroup /app && chmod +x /app/entrypoint.sh

# Set the entrypoint using an absolute path. It runs as root and drops privileges.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"] # Default command for the testing container.

# --- Stage 3: Final Production Image ---
# This is the minimal final image for the actual application.
FROM python:3.10-slim-buster
WORKDIR /app

# Install 'gosu' for privilege dropping.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*

# Create the non-root user 'naeus'.
RUN adduser --system --no-create-home naeus

# Copy the entrypoint script and make it executable.
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy production packages from the 'base' stage.
COPY --from=base /app /app

# Copy application source from the build context and set ownership.
COPY --chown=naeus:nogroup nether_bridge.py .
COPY --chown=naeus:nogroup examples/ ./examples/

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Embed the health check, ensuring it runs as the 'naeus' user via gosu.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "nether_bridge.py", "--healthcheck"]

# Set the entrypoint. It will run as root by default.
ENTRYPOINT ["entrypoint.sh"]

# Set the default command. The entrypoint script will execute this as 'naeus'.
CMD ["python", "nether_bridge.py"]