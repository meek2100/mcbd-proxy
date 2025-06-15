# --- Stage 1: Base ---
# This stage installs only the production dependencies.
FROM python:3.10-slim-buster as base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# This stage builds on 'base' and adds the development dependencies for testing.
FROM base as testing
WORKDIR /app
# Copy BOTH requirements files into the build context for this stage
COPY requirements.txt .
COPY tests/requirements-dev.txt .
# Now, pip can find both files and correctly resolve the -r /app/requirements.txt path
RUN pip install --no-cache-dir -r requirements-dev.txt

# --- Stage 3: Final Production Image ---
# This is the minimal final image. It copies only from the 'base' stage.
FROM python:3.10-slim-buster
WORKDIR /app

# Copy only the production packages from the 'base' stage.
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the application code and default configs.
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Expose all necessary ports for the proxy and metrics.
EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Define the command to run the application.
ENTRYPOINT ["python", "nether_bridge.py"]