# --- Stage 1: Base ---
# This stage installs only the production dependencies.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# This stage builds on 'base' and adds all code, configs, and dev dependencies.
FROM base AS testing
WORKDIR /app

# Create a non-root user 'naeus'
RUN adduser --system --no-create-home naeus

# Change ownership of the work directory
RUN chown naeus:nogroup /app

# Copy source and test files (with --chown to ensure correct ownership)
COPY --chown=naeus:nogroup . .

# Install the development dependencies
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Switch to the non-root user for the test environment
USER naeus

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