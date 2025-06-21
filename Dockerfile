# Stage 1: Base - Installs production dependencies.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Stage 2: Builder - A complete copy of the source code.
FROM python:3.10-slim-buster AS builder
WORKDIR /app
COPY . .

# Stage 3: Testing - A self-contained environment for running tests in CI.
FROM base AS testing
WORKDIR /app
# Install system packages needed by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd && rm -rf /var/lib/apt/lists/*
# Copy the entire project context from the builder stage.
COPY --from=builder /app /app
# Install development dependencies.
RUN pip install --no-cache-dir -r tests/requirements-dev.txt
# Create user and set permissions.
RUN adduser --system --no-create-home naeus && \
    chown -R naeus:nogroup /app && \
    chmod +x /app/entrypoint.sh
# Set the entrypoint using an absolute path.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"]

# Stage 4: Final Production Image - Built from previous stages for reliability.
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install only 'gosu' for dropping privileges.
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
# Create the non-root user.
RUN adduser --system --no-create-home naeus

# Copy artifacts from previous stages, not the local context.
# 1. Production python packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# 2. The entrypoint script from the 'builder' stage.
COPY --from=builder /app/entrypoint.sh /usr/local/bin/
# 3. The application code and examples from the 'builder' stage.
COPY --from=builder --chown=naeus:nogroup /app/nether_bridge.py .
COPY --from=builder --chown=naeus:nogroup /app/examples/ ./examples/

# Make entrypoint executable and set final permissions.
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R naeus:nogroup /app

EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "nether_bridge.py", "--healthcheck"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "nether_bridge.py"]