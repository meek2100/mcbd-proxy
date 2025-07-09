# Stage 1: Builder - Installs all dependencies and has the full source code.
FROM python:3.10-slim-buster AS builder
WORKDIR /app

# Install system packages needed by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*

# Copy the entire project context.
COPY . .

# Install all dependencies, including development/testing tools.
RUN python -m pip install --upgrade pip && \
  pip install --no-cache-dir ".[dev]"

# Create a non-root user for security.
RUN adduser --system --no-create-home naeus && \
  chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh

# ---

# Stage 2: Final Production Image - Assembled for a lean and secure image.
FROM python:3.10-slim-buster AS final
WORKDIR /app

# Install system packages required by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps && rm -rf /var/lib/apt/lists/*
RUN adduser --system --no-create-home naeus

# Copy installed Python packages from the builder stage.
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the entire application directory, which now includes main.py and other modules.
COPY --from=builder --chown=naeus:nogroup /app /app

# Set the entrypoint.
ENTRYPOINT ["/app/entrypoint.sh"]

# Set the default command to run the application.
CMD ["python", "main.py"]

# Expose the ports the proxy will listen on.
EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

# Healthcheck to ensure the proxy is running correctly.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]