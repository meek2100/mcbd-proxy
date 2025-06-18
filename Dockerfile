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

# The Docker CLI is NOT needed, as tests use the docker-py library
# via the mounted docker socket. The RUN commands for apt-get have been removed.

# Explicitly copy all source and test files into the image
COPY nether_bridge.py .
COPY pytest.ini .
COPY requirements.txt .
COPY tests/ ./tests/

# Install the development dependencies
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# --- Stage 3: Final Production Image ---
# This is the minimal final image. It only copies from the 'base' stage.
FROM python:3.10-slim-buster
WORKDIR /app

# Copy only the production packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy only the necessary application code and default configs.
COPY nether_bridge.py .
COPY examples/settings.json .
COPY examples/servers.json .

# Expose all necessary ports for the proxy and metrics.
EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Define the command to run the application.
ENTRYPOINT ["python", "nether_bridge.py"]