# Stage 1: Base - Installs production dependencies into a clean layer.
# This layer is cached and only rebuilt when requirements.txt changes.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY pyproject.toml .
RUN python -m pip install --upgrade pip && pip install --no-cache-dir

# Stage 2: Builder - A complete copy of the source code for use by other stages.
FROM python:3.10-slim-buster AS builder
WORKDIR /app
COPY . .

# Stage 3: Testing - A self-contained environment for running tests in CI.
# This stage includes development dependencies and the full source code.
FROM base AS testing
WORKDIR /app
# Install system packages needed by the entrypoint and for testing.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*
# Copy the entire project context from the builder stage.
COPY --from=builder /app /app
# Install development dependencies.
RUN pip install --no-cache-dir ".[dev]"
# Create user and set permissions for the test environment.
RUN adduser --system --no-create-home naeus && \
  chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh
# Set the entrypoint for the test container. The CMD is for interactive use.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"]

# Stage 4: Final Production Image - Assembled from previous stages for a lean and secure image.
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install 'gosu' for dropping privileges and 'procps' for providing `kill` command.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*
# Create the non-root user for running the application.
RUN adduser --system --no-create-home naeus

# Copy artifacts from previous stages, not the local context.
# 1. Production python packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# 2. The entrypoint script from the 'builder' stage.
COPY --from=builder /app/entrypoint.sh /usr/local/bin/

# 3. The new, refactored application code modules from the 'builder' stage.
COPY --from=builder --chown=naeus:nogroup /app/main.py .
COPY --from=builder --chown=naeus:nogroup /app/proxy.py .
COPY --from=builder --chown=naeus:nogroup /app/config.py .
COPY --from=builder --chown=naeus:nogroup /app/docker_manager.py .
COPY --from=builder --chown=naeus:nogroup /app/metrics.py .

# 4. The example configuration files from the 'builder' stage.
COPY --from=builder --chown=naeus:nogroup /app/examples/ ./examples/

# Make entrypoint executable and ensure final application directory permissions are correct.
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R naeus:nogroup /app

# Expose the ports the proxy will listen on.
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Update HEALTHCHECK to call the new main.py entrypoint.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]

# Set the container's entrypoint script.
ENTRYPOINT ["entrypoint.sh"]

# Update the default command to run the new main.py application.
CMD ["python", "main.py"]