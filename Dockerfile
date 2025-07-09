# Stage 1: Base - A clean stage with only production dependencies.
FROM python:3.10-slim-buster AS base
WORKDIR /app

# Copy only the files needed to install dependencies.
COPY pyproject.toml .

# Install ONLY production dependencies.
RUN python -m pip install --upgrade pip && \
  pip install --no-cache-dir .

# ---

# Stage 2: Builder - Based on the production image, but with development tools added.
# This stage is used for running tests in CI.
FROM base AS builder
WORKDIR /app

# Install development dependencies.
# Note: It will use the cached production dependencies from the 'base' stage.
RUN pip install --no-cache-dir ".[dev]"

# Copy the rest of the source code for testing.
COPY . .
RUN chmod +x /app/entrypoint.sh

# ---

# Stage 3: Final Production Image - Assembled for a lean and secure image.
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install system packages required by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*
RUN adduser --system --no-create-home naeus

# Copy the clean, production-only python packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code.
COPY . /app

# Set permissions and the user.
RUN chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh

# The entrypoint will run as root to set up permissions,
# then use gosu to drop to the 'naeus' user for the application.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

# Expose ports.
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Healthcheck.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]