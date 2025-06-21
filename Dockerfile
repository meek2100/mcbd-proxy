# --- Stage 1: Base ---
# This stage installs only the production dependencies.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# This stage prepares a non-root environment for running tests inside the CI.
FROM base AS testing
WORKDIR /app

# Install system packages needed by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*

# Copy requirements files first to leverage Docker cache. This also creates the /app/tests/ directory.
COPY requirements.txt .
COPY tests/ ./tests/

# Install dev dependencies. This layer is cached as long as requirements don't change.
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Now copy the rest of the application source code.
COPY . .

# Create a non-root user 'naeus'.
RUN adduser --system --no-create-home naeus

# Set ownership for the entire app directory now that it's populated
# and ensure the entrypoint is executable.
RUN chown -R naeus:nogroup /app && chmod +x /app/entrypoint.sh

# Set the entrypoint using an absolute path, as WORKDIR is not in $PATH.
ENTRYPOINT ["/app/entrypoint.sh"]
# The default command for this stage.
CMD ["/bin/bash"]


# --- Stage 3: Final Production Image ---
# This is the minimal final image.
FROM python:3.10-slim-buster
WORKDIR /app

# Install 'gosu' for privilege dropping and 'passwd' for the 'adduser' command.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*

# Create the non-root user 'naeus'.
RUN adduser --system --no-create-home naeus

# Copy the entrypoint script to a standard bin location and make it executable.
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy application files and set ownership. Note the heartbeat file is NOT copied.
COPY --chown=naeus:nogroup nether_bridge.py .
COPY --chown=naeus:nogroup examples/ ./examples/
COPY --chown=naeus:nogroup requirements.txt .

# Copy production packages from the 'base' stage.
COPY --from=base --chown=naeus:nogroup /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Set final ownership for the entire app directory.
RUN chown -R naeus:nogroup /app

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