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
# Permissions for Docker socket will be handled at runtime by the entrypoint.
RUN adduser --system --no-create-home naeus

# FIX: Change ownership of the work directory itself
# This allows the non-root user to create new files/dirs like .ruff_cache
RUN chown naeus:nogroup /app

# Copy source and test files with correct ownership
COPY . . 

# Install the development dependencies
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Switch to the non-root user for the test environment
USER naeus

# --- Stage 3: Final Production Image ---
# This is the minimal final image.
FROM python:3.10-slim-buster
WORKDIR /app

# Install necessary tools for the entrypoint script (su-exec, shadow for usermod)
RUN apt-get update && apt-get install -y --no-install-recommends su-exec shadow && rm -rf /var/lib/apt/lists/*

# Create the same non-root user as the testing stage
RUN adduser --system --no-create-home naeus

# FIX: Change ownership of the work directory itself.
RUN chown naeus:nogroup /app

# Copy only the production packages from the 'base' stage with correct ownership
COPY --from=base --chown=naeus:nogroup /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code and example configs with correct ownership
COPY --chown=naeus:nogroup nether_bridge.py .
COPY --chown=naeus:nogroup examples/settings.json . 
COPY --chown=naeus:nogroup examples/servers.json . 

# Copy and set up the entrypoint script
COPY --chown=naeus:nogroup entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to the non-root user for the final image
USER naeus

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Embed the health check directly into the image.
# It uses the Python script's built-in health check capability.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "nether_bridge.py", "--healthcheck"]

# Set the entrypoint to our new script.
ENTRYPOINT ["entrypoint.sh"]
# Set the default command for the entrypoint.
CMD ["python", "nether_bridge.py"]