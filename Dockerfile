# Dockerfile
# Stage 1: Base - Use the modern, faster Python 3.11 on Debian Bookworm.
FROM python:3.11-slim-bookworm AS base
WORKDIR /app
COPY pyproject.toml poetry.lock ./
ENV POETRY_HOME="/opt/poetry" \
  POETRY_VIRTUALENVS_IN_PROJECT=true \
  POETRY_NO_INTERACTION=1 \
  PATH="/opt/poetry/bin:$PATH"

# Install Poetry and core dependencies
RUN curl -sSL https://install.python-poetry.org | python3 - && \
  poetry install --no-root --sync --without dev

# Stage 2: Builder - A complete copy of the source code for use by other stages.
FROM python:3.11-slim-bookworm AS builder
WORKDIR /app
COPY . .

# Stage 3: Testing - A self-contained environment for running tests in CI.
FROM base AS testing
WORKDIR /app
# Install system packages needed by the entrypoint and for testing.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd \
  && rm -rf /var/lib/apt/lists/*
# Copy the entire project context from the builder stage.
COPY --from=builder /app /app
# Install development dependencies using Poetry
ENV POETRY_HOME="/opt/poetry" \
  POETRY_VIRTUALENVS_IN_PROJECT=true \
  POETRY_NO_INTERACTION=1 \
  PATH="/opt/poetry/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python3 - && \
  poetry install --no-root --sync

# Create user and set permissions for the test environment.
RUN adduser --system --no-create-home naeus && \
  chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh
# Set the entrypoint for the test container.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"]

# Stage 4: Final Production Image - Assembled from previous stages for a lean \
# and secure image.
FROM python:3.11-slim-bookworm AS final
WORKDIR /app

# Install 'gosu' for dropping privileges and 'procps' for providing `kill` \
# command.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps \
  && rm -rf /var/lib/apt/lists/*
# Create the non-root user for running the application.
RUN adduser --system --no-create-home naeus

# Copy artifacts from previous stages, not the local context.
COPY --from=base ${POETRY_HOME} ${POETRY_HOME}
COPY --from=base /app/pyproject.toml /app/poetry.lock /app/
COPY --from=base /app/.venv /app/.venv
COPY --from=base /usr/local/lib/python3.11/site-packages \
  /usr/local/lib/python3.11/site-packages
COPY --from=builder /app/entrypoint.sh /usr/local/bin/

# Copy application code
COPY --from=builder --chown=naeus:nogroup /app/main.py .
COPY --from=builder --chown=naeus:nogroup /app/proxy.py .
COPY --from=builder --chown=naeus:nogroup /app/config.py .
COPY --from=builder --chown=naeus:nogroup /app/docker_manager.py .
COPY --from=builder --chown=naeus:nogroup /app/metrics.py .
COPY --from=builder --chown=naeus:nogroup /app/examples/ ./examples/

# Make entrypoint executable and ensure final application directory permissions \
# are correct.
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R naeus:nogroup /app

# Expose the ports the proxy will listen on.
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Restored a more robust health check that validates the heartbeat file.
# CRITICAL FIX: Corrected CMD syntax to JSON array for 'exec' form.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]

# Set the container's entrypoint script.
ENTRYPOINT ["/app/entrypoint.sh"]

# Set the default command to run the main application.
CMD ["python", "main.py"]