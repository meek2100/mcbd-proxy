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
# Install Docker CLI tools required by conftest.py
RUN apt-get update && apt-get install -y curl gnupg
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
RUN chmod a+r /etc/apt/keyrings/docker.asc
RUN echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli docker-compose-plugin
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