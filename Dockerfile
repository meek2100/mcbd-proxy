# --- Stage 1: Base ---
# This stage installs only the production dependencies.
FROM python:3.10-slim-buster AS base
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Testing ---
# This stage builds on 'base' and adds all code and dev dependencies.
FROM base AS testing
WORKDIR /app

# Copy the entire project context into the testing stage.
# This ensures nether_bridge.py, pytest.ini, and the tests/ dir are available.
COPY . .

# Install the development dependencies from the requirements file.
RUN pip install --no-cache-dir -r tests/requirements-dev.txt

# --- Stage 3: Final Production Image ---
FROM python:3.10-slim-buster
WORKDIR /app
COPY --from=base /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp
ENTRYPOINT ["python", "nether_bridge.py"]