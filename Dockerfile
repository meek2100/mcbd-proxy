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

# Install system dependencies needed for conftest.py to run docker commands
RUN apt-get update && apt-get install -y curl gnupg
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
RUN chmod a+r /etc/apt/keyrings/docker.asc
RUN echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli docker-compose-plugin

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

# Expose all necessary ports for the proxy and metrics.
EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Define the command to run the application.
ENTRYPOINT ["python", "nether_bridge.py"]