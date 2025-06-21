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

# Install tools needed for the entrypoint and for testing.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Create a non-root user 'naeus'
RUN adduser --system --no-create-home naeus

# Change ownership of the work directory.
RUN chown -R naeus:nogroup /app

# Copy the entrypoint script which will handle runtime permissions.
COPY --chown=naeus:nogroup entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy the rest of the source code with correct ownership.
COPY --chown=naeus:nogroup . .

# Set the entrypoint. It will run as root and drop privileges to naeus.
ENTRYPOINT ["entrypoint.sh"]
# The default command for this stage, can be overridden by docker-compose.
CMD ["/bin/bash"]


# --- Stage 3: Final Production Image ---
# This is the minimal final image.
FROM python:3.10-slim-buster
WORKDIR /app

# Install 'gosu' for privilege dropping and 'passwd' for the 'adduser' command.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*

# Create the non-root user 'naeus'
RUN adduser --system --no-create-home naeus

# Change ownership of the work directory and its contents
RUN chown -R naeus:nogroup /app

# Copy only the production packages from the 'base' stage with correct ownership
COPY --from=base --chown=naeus:nogroup /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code and example configs with correct ownership
COPY --chown=naeus:nogroup nether_bridge.py .
COPY --chown=naeus:nogroup examples/settings.json .
COPY --chown=naeus:nogroup examples/servers.json .

# Copy and set up the entrypoint script, which will run as root
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Embed the health check, ensuring it runs as the 'naeus' user via gosu.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "nether_bridge.py", "--healthcheck"]

# Set the entrypoint. It will run as root by default.
ENTRYPOINT ["entrypoint.sh"]

# Set the default command for the entrypoint. The entrypoint script will execute this as the 'naeus' user.
CMD ["python", "nether_bridge.py"]