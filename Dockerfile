# Stage 1: Base - Installs ONLY production dependencies
FROM python:3.10-slim-buster AS base
WORKDIR /app

COPY pyproject.toml .

# Install the application and its production dependencies
RUN python -m pip install --upgrade pip && \
  pip install --no-cache-dir .

# ---

# Stage 2: Builder - Adds development dependencies for testing in CI
FROM base AS builder

# Install development dependencies
RUN pip install --no-cache-dir ".[dev]"

# Copy source code for testing purposes
COPY . .
RUN chmod +x /app/entrypoint.sh

# ---

# Stage 3: Final Production Image
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install system packages required by the entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*
RUN adduser --system --no-create-home naeus

# Copy the clean, production-only python packages from the 'base' stage
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code
COPY . .

# Set correct permissions
RUN chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh

# The entrypoint will run as root, then use gosu to drop privileges
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

# Expose ports
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Healthcheck
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "main.py", "--healthcheck"]