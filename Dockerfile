# Stage 1: Builder - Installs all dependencies and has the full source code.
# This stage is used for building and for running tests in CI.
FROM python:3.10-slim-buster AS builder
WORKDIR /app

# Install system packages needed by the entrypoint and for testing.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd procps && rm -rf /var/lib/apt/lists/*

# Copy the entire project context.
COPY . .

# Install all dependencies, including development/testing tools.
# This single command replaces the separate requirements.txt installs.
RUN python -m pip install --upgrade pip && \
  pip install --no-cache-dir ".[dev]"

# Create user and set permissions.
RUN adduser --system --no-create-home naeus && \
  chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh

# ---

# Stage 2: Final Production Image - Assembled for a lean and secure image.
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install 'gosu' for dropping privileges and 'procps' for providing `kill` command.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*
# Create the non-root user for running the application.
RUN adduser --system --no-create-home naeus

# Copy artifacts from the builder stage.
# 1. The entire installed python environment, which now includes our app's code and its dependencies.
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# 2. The entrypoint script.
COPY --from=builder /app/entrypoint.sh /usr/local/bin/
# 3. The example configuration files.
COPY --from=builder --chown=naeus:nogroup /app/examples/ ./examples/

# Make entrypoint executable and ensure final application directory permissions are correct.
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R naeus:nogroup /app

# Expose the ports the proxy will listen on.
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Update HEALTHCHECK to call the main.py entrypoint.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]

# Set the container's entrypoint script.
ENTRYPOINT ["entrypoint.sh"]

# Update the default command to run the main.py application.
CMD ["python", "main.py"]