# --- Stage 1: Base ---
# Installs production dependencies. This stage is lean and well-cached.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# Prepares a non-root environment for running tests inside CI.
# This stage now copies all files at once for maximum reliability.
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

# Set the entrypoint using an absolute path, as WORKDIR is not in $PATH.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"] # Default command for the testing container.

# --- Stage 3: Final Production Image ---
# This is the minimal final image, built from the stages above.
FROM python:3.10-slim-buster
WORKDIR /app

# Install 'gosu' for privilege dropping.
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

# Create the non-root user 'naeus'.
RUN adduser --system --no-create-home naeus

# Copy production python packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the entire prepared application directory from the 'testing' stage.
# This is a robust way to include all source code and the entrypoint script
# with the correct permissions already set.
COPY --from=testing --chown=naeus:nogroup /app /app

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Embed the health check, ensuring it runs as the 'naeus' user via gosu.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "nether_bridge.py", "--healthcheck"]

# Set the entrypoint using an absolute path. It will run as root.
ENTRYPOINT ["/app/entrypoint.sh"]

# Set the default command. The entrypoint script will execute this as 'naeus'.
CMD ["python", "nether_bridge.py"]