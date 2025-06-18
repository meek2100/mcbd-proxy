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

# Add a build argument to accept the Docker group ID from the host
ARG DOCKER_GID

# Create a non-root user 'appuser' and add it to a 'docker' group with the correct GID
# This allows the user to access the mounted docker socket. Defaults to 999 if not provided.
RUN addgroup --gid ${DOCKER_GID:-999} docker && \
    adduser --system --ingroup docker --no-create-home appuser

# Copy source and test files with the correct ownership
COPY --chown=appuser:docker . .

# Install the development dependencies
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# Switch to the non-root user for the test environment as well
USER appuser

# --- Stage 3: Final Production Image ---
# This is the minimal final image.
FROM python:3.10-slim-buster
WORKDIR /app

# Add the same build argument for the Docker GID
ARG DOCKER_GID

# Create the same non-root user and group as the testing stage
RUN addgroup --gid ${DOCKER_GID:-999} docker && \
    adduser --system --ingroup docker --no-create-home appuser

# Copy only the production packages from the 'base' stage with correct ownership
COPY --from=base --chown=appuser:docker /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code and example configs with correct ownership
COPY --chown=appuser:docker nether_bridge.py .
COPY --chown=appuser:docker examples/settings.json .
COPY --chown=appuser:docker examples/servers.json .

# Switch to the non-root user for the final image
USER appuser

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

ENTRYPOINT ["python", "nether_bridge.py"]